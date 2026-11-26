"""Planner agent: natural language -> structured plan, and plan revision.

* ``plan(request)`` is LLM-first (Ollama). If no model is reachable, or the
  model's answer is unusable, a rule-based template planner builds the plan.
* ``revise(plan, report)`` is the self-correction step. With a model it sends
  the failed plan plus the validator's error list back to the LLM; without one
  (or if the LLM's revision is still broken) a deterministic repairer fixes
  each error code mechanically.
"""
from __future__ import annotations

import copy
import difflib
import json
import re

from . import catalog, llm
from .plan import REF_RE, find_cycle, node_map, normalize, refs_in, substitute, topo_order, trigger_of

# =========================================================== LLM planning
SYSTEM_PROMPT = """You are FlowForge's planner. Convert an automation request into a JSON plan.
Use ONLY these node types (name [kind]: description | params, * = required | outputs):
{catalog}

Plan format:
{{"name": "<short title>",
  "nodes": [{{"id": "<snake_case id>", "type": "<node type>", "label": "<short label>", "params": {{...}}}}],
  "edges": [{{"from": "<id>", "to": "<id>", "branch": "true" | "false" (ONLY on edges leaving a condition)}}]}}

Rules:
- Exactly one trigger, and it must be the first node with id "trigger".
- Refer to earlier outputs with {{{{node_id.field}}}}, e.g. {{{{trigger.subject}}}}. Only reference nodes that run earlier.
- Secrets and account-specific values are environment references: {{{{env.SLACK_WEBHOOK_URL}}}}, {{{{env.SHEET_ID}}}}, {{{{env.MY_EMAIL}}}}.
  Never write a real URL token, password or API key.
- A condition node needs edges with branch "true" and/or "false".
- Return only the JSON object.

Example request: "when I get an email with an invoice attached, save it to Drive and ping me on Slack"
Example plan:
{example}
"""


def _example() -> str:
    return json.dumps(template_plan("when I get an email with an invoice attached, save it to Drive and ping me on Slack"))


def llm_plan(request: str) -> dict:
    system = SYSTEM_PROMPT.format(catalog=catalog.catalog_for_prompt(), example=_example())
    out = llm.chat_json(system, f"Request: {request}")
    if "plan" in out and isinstance(out["plan"], dict):
        out = out["plan"]
    out["request"] = request
    return out


def llm_revise(plan: dict, report, request: str) -> dict:
    system = SYSTEM_PROMPT.format(catalog=catalog.catalog_for_prompt(), example=_example())
    errors = "\n".join(f"- {i.code} ({i.node}): {i.message}" for i in report.errors)
    user = (f"Request: {request}\n\nThis plan failed validation:\n{json.dumps(plan)}\n\n"
            f"Errors:\n{errors}\n\nReturn the corrected plan as JSON.")
    out = llm.chat_json(system, user)
    if "plan" in out and isinstance(out["plan"], dict):
        out = out["plan"]
    out["request"] = request
    return out


# =========================================================== template planning
TRIGGER_FIELDS = {  # what a message about the event should say
    "email_received": "New email from {{trigger.from}}: {{trigger.subject}}",
    "webhook": "Webhook received: {{trigger.body}}",
    "schedule": "Scheduled run at {{trigger.timestamp}}",
    "manual": "{{trigger.input}}",
}

ACTION_PATTERNS: list[tuple[str, str]] = [
    ("slack_message", r"\bslack\b"),
    ("discord_message", r"\bdiscord\b"),
    ("teams_message", r"\b(?:microsoft )?teams\b"),
    ("sms_message", r"\b(?:sms|text me|text message|send (?:me )?a text)\b"),
    ("drive_list", r"\b(?:list|find|search)\b[^.,;]{0,30}\bfiles?\b"),
    ("drive_save", r"\b(?:google )?drive\b|\bupload\b"),
    ("sheets_read", r"\b(?:read|get|fetch|pull)\b[^.,;]{0,30}\b(?:rows?|sheet|spreadsheet)\b"),
    ("sheets_append", r"\b(?:spreadsheet|google sheets?|sheet|add a row|new row)\b"),
    ("send_email", r"\b(?:send (?:an |a |me an |me a )?(?:email|e-mail|mail)|email (?:me|the|him|her|them|it|us|a|back|[\w.+-]+@[\w.-]+)|e-?mail them|reply|thank-you email|thank you email|forward (?:it|them|the emails?)(?![^.,;]{0,12}https?://)|mail me)\b"
                   r"|\bsend\b[^,;]{0,50}?(?<!receive )(?<!get )\b(?:by )?e-?mail\b(?! (?:arrives|comes))"),
    ("llm_summarize", r"\b(?:summari[sz]e|summary|tl;?dr|classify|translate)\b"),
    ("create_task", r"\b(?:jira|ticket|create (?:a )?task|add (?:a )?task|open an issue)\b"),
    ("calendar_create", r"\b(?:calendar|book a meeting|create (?:a |an )?(?:meeting|event)|schedule a (?:call|meeting))\b"),
    ("delay", r"\b(?:wait|delay|pause)\b(?:\s+for)?\s+\d+\s*(?:s|sec|seconds?|m|min|minutes?|h|hours?|d|days?)\b"),
    ("transform", r"\b(?:convert|reformat|transform|format|uppercase|lowercase|capitali[sz]e)\b(?![^.,;]{0,20}\bfrom https?://)"),
    ("http_request", r"\b(?:call|hit|fetch|download|pull|post|forward|send|get)\b[^.,;]{0,40}?\b(?:api|endpoint|url|https?://\S+)|\bfrom https?://\S+"),
    ("log_message", r"\blog\b(?![^.,;]{0,30}\b(?:sheet|spreadsheet)\b)"),
    ("notify", r"\b(?:notify me|let me know|tell me|reply with|respond with|confirm|remind me|reminder)\b"),
    ("ai_agent", r"\b(?:agent|assistant|chat ?bot|tutor|advisor|answer (?:my |student |customer |user )?questions|"
                 r"research|explain|help (?:me|students|users|people)|solve|plan my|recommend)\b"),
]

AGENT_TOOL_CUES = [
    ("calculator", r"\b(?:math|calculat\w*|percent\w*|%|convert\w*|budget|cost|price|interest|grade|marks|gpa|loan|emi|sum|average)\b"),
    ("datetime", r"\b(?:date|time|today|tomorrow|deadline|schedule|day|week|month|year|age|remind)\b"),
    ("web", r"\b(?:web|website|page|url|link|article|news|wikipedia|online|internet|look up|search|latest|current)\b"),
    ("arxiv", r"\b(?:paper|papers|research|arxiv|academic|literature|journal)\b"),
]


def agent_tools_for(text: str) -> str:
    tools = [name for name, rx in AGENT_TOOL_CUES if re.search(rx, text, re.I)]
    for default in ("calculator", "datetime"):  # cheap, broadly useful
        if default not in tools:
            tools.append(default)
    if re.search(r"https?://", text):
        tools.append("http")
    order = ["calculator", "datetime", "web", "arxiv", "http"]
    return ",".join(sorted(set(tools), key=order.index))

COND_RE = re.compile(
    r"\b(?:only )?if\s+(?:the\s+|its\s+|their\s+)?(?P<field>[\w ]+?)\s+"
    r"(?P<op>contains|includes|mentions|has|is over|is more than|is greater than|exceeds|is above|is larger than|"
    r"is under|is less than|is below|is smaller than|equals|is)\s+"
    r"(?P<value>\"[^\"]+\"|'[^']+'|\$?[\d,.]+|[\w@.-]+)", re.I)

OP_MAP = {"contains": "contains", "includes": "contains", "mentions": "contains", "has": "contains",
          "is over": "greater_than", "is more than": "greater_than", "is greater than": "greater_than",
          "exceeds": "greater_than", "is above": "greater_than", "is larger than": "greater_than",
          "is under": "less_than", "is less than": "less_than", "is below": "less_than",
          "is smaller than": "less_than", "equals": "equals", "is": "equals"}


def detect_trigger(text: str) -> tuple[str, dict]:
    t = text.lower()
    if re.search(r"\bwebhook\b|\bhttp request\b|\bpost request\b|\bwhen a request\b|\breceives? a (?:post|request)\b|\bapi call comes", t):
        m = re.search(r"(?:path|endpoint|at)\s+/([\w-]+)", text, re.I)
        return "webhook", {"path": m.group(1) if m else "flowforge-hook", "method": "POST"}
    if re.search(r"\bform\b", t) and re.search(r"submit|fills?|submission|signs? up|complet|\bbooks?\b|using the|through the|via the", t):
        fields = _form_fields(t)
        title = "Contact form"
        m = re.search(r"(?:the |a |my |our )?([\w-]+(?: [\w-]+)?) form", t)
        if m and m.group(1) not in ("a", "the", "my", "our"):
            title = m.group(1).strip().title() + " form"
        return "form_submitted", {"title": title, "fields": ",".join(fields)}
    if (re.search(r"\b(?:when|whenever|each time|every time|if|once)\b[^.]{0,40}\b(?:e-?mails?|mail|inbox|gmail)\b", t)
            and re.search(r"\b(?:get|gets|receive|receives|arrives?|comes? in|new|lands?)\b", t)) \
            or re.search(r"\b(?:every|each|all|any)?\s*(?:new|incoming|unread)\s+e-?mails?\b", t) \
            or re.search(r"\b(?:receive|get)\b[^.,]{0,15}\bby e-?mail\b|\be-?mail attachments?\b", t):
        q = []
        if re.search(r"attach", t):
            q.append("has:attachment")
        m = re.search(r"(?:subject|titled|about|with)\s+(?:an?\s+)?[\"']?(invoice|receipt|order|urgent|report|resume|application)s?\b", t)
        if m:
            q.append(f"subject:{m.group(1)}")
        m = re.search(r"from\s+([\w.+-]+@[\w.-]+)", t)
        if m:
            q.append(f"from:{m.group(1)}")
        return "email_received", {"query": " ".join(q) or "is:unread"}
    cron = parse_schedule(t)
    if cron:
        return "schedule", {"cron": cron, "timezone": "UTC"}
    return "manual", {"input": "Run the automation"}


def _form_fields(t: str) -> list[str]:
    known = ["name", "email", "phone", "company", "message", "date", "amount", "feedback", "rating", "address"]
    found = [f for f in known if re.search(rf"\b{f}s?\b", t)]
    m = re.search(r"\bfields?\s*[:(]?\s*([\w ,]+?)(?:\)|$|\.|then)", t)
    if m:
        for f in re.split(r",|\band\b", m.group(1)):
            f = re.sub(r"\W+", "_", f.strip())
            if f and f not in found:
                found.append(f)
    for f in ("name", "email"):
        if f not in found:
            found.insert(0 if f == "name" else 1, f)
    return found


def parse_schedule(t: str) -> str | None:
    hour, minute = 9, 0
    m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if m:
        hour, minute = int(m.group(1)) % 24, int(m.group(2) or 0)
        if m.group(3) == "pm" and hour < 12:
            hour += 12
        if m.group(3) == "am" and hour == 12:
            hour = 0
    elif "evening" in t:
        hour = 18
    elif "noon" in t:
        hour = 12
    elif "midnight" in t:
        hour = 0
    m = re.search(r"every\s+(\d+)\s*(minutes?|mins?|hours?)", t)
    if m:
        n = int(m.group(1))
        return f"*/{n} * * * *" if m.group(2).startswith("m") else f"0 */{n} * * *"
    if re.search(r"\b(?:every hour|hourly)\b", t):
        return "0 * * * *"
    days = {"monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5, "saturday": 6, "sunday": 0}
    for d, n in days.items():
        if re.search(rf"\bevery {d}\b|\bon {d}s\b|\b{d}s\b", t):
            return f"{minute} {hour} * * {n}"
    if re.search(r"\b(?:every weekday|each weekday|weekdays|monday to friday)\b", t):
        return f"{minute} {hour} * * 1-5"
    if re.search(r"\b(?:every week|weekly)\b", t):
        return f"{minute} {hour} * * 1"
    if re.search(r"\b(?:every month|monthly)\b", t):
        return f"{minute} {hour} 1 * *"
    if re.search(r"\b(?:every day|daily|each day|every morning|every evening|every night|each morning)\b", t):
        return f"{minute} {hour} * * *"
    return None


def _find_actions(segment: str) -> list[tuple[int, str, re.Match]]:
    hits, taken = [], set()
    for typ, pat in ACTION_PATTERNS:
        for m in re.finditer(pat, segment, re.I):
            if typ in taken:
                break
            if typ == "sheets_append" and "sheets_read" in taken:
                break
            if typ == "drive_save" and "drive_list" in taken:
                break
            hits.append((m.start(), typ, m))
            taken.add(typ)
            break
    if "notify" in taken and "ai_agent" in taken and re.search(r"\bremind(?:er| me)\b", segment, re.I):
        hits = [h for h in hits if h[1] != "ai_agent"]
    # messaging beats generic notify
    if "notify" in taken and taken & {"slack_message", "discord_message", "teams_message", "sms_message", "send_email"}:
        hits = [h for h in hits if h[1] != "notify"]
    hits.sort(key=lambda h: h[0])

    # Data sources run before the steps that use their data ("post the weather from <url> to slack").
    def stage(h):
        typ, m = h[1], h[2]
        if typ in ("sheets_read", "drive_list", "ai_agent"):  # these produce the content later steps use
            return 0
        if typ == "http_request" and not re.search(r"\bpost\b|\bsend\b|\bforward\b", m.group(0), re.I):
            return 0
        return 1 if typ in ("llm_summarize", "transform") else 2
    return sorted(hits, key=lambda h: (stage(h), h[0]))


def _split_condition(text: str) -> tuple[str, re.Match | None, str, str]:
    """-> (before, condition match, then-part, else-part)"""
    m = COND_RE.search(text)
    if not m:
        return text, None, "", ""
    before, rest = text[:m.start()], text[m.end():]
    parts = re.split(r"\b(?:otherwise|else|if not)\b", rest, maxsplit=1, flags=re.I)
    then_part, else_part = parts[0], parts[1] if len(parts) > 1 else ""
    # "save to drive, but only if the subject contains X" -> actions before the if belong to the true branch
    if re.search(r"\bonly if\b", m.group(0), re.I) or re.search(r"\bbut\s*$", before.strip(" ,"), re.I):
        lead_actions = _find_actions(before)
        if lead_actions and not _find_actions(then_part):
            return "", m, before, else_part
    return before, m, then_part, else_part


_NEXT_ACTION = r"(?=\s*$|[.;]\s*$|,?\s+and\s+(?:then\s+)?(?:post|add|save|send|notify|ping|create|text|append|upload|log)\b)"
_BODY_LEAD = r"\b(?:saying|that says|which says|with (?:the |a )?(?:message|text|note)|body)"


def custom_email_text(request: str) -> tuple[str | None, str | None]:
    """The user's own e-mail wording, if the request gives it.

    "send them an email saying Welcome to our demo", "... with the message 'See you soon'",
    "... with subject Hello and body Thanks for watching", or a quoted sentence after "email".
    Returns (subject, body); either may be None.
    """
    subject = body = None
    # only an explicit outgoing subject: "with subject X" / "with the subject line 'X'" / "subject: X"
    lead = r"(?:\bwith (?:the |a )?subject(?: line)?\s*[:\-–]?|\bsubject(?: line)?\s*[:\-–])\s*"
    m = (re.search(lead + r"[\"“]([^\"”]+)[\"”]", request, re.I)
         or re.search(lead + r"(.+?)(?=\s+(?:and|with|saying)\b|[,;]|\.?\s*$)", request, re.I))
    if m:
        subject = m.group(1).strip()
    m = (re.search(_BODY_LEAD + r"\s*[:\-–]?\s*[\"“]([^\"”]+)[\"”]", request, re.I)
         or re.search(_BODY_LEAD + r"\s*[:\-–]?\s*(.+?)" + _NEXT_ACTION, request, re.I)
         or re.search(r"\be-?mail\b[^\"“]*[\"“]([^\"”]{3,})[\"”]", request, re.I))
    if m:
        body = m.group(1).strip()
    return subject or None, body or None


class _Builder:
    def __init__(self, request: str):
        self.request = request
        self.t = request.lower()
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self.ids: set[str] = set()
        self.trigger_type = "manual"
        self.last_content: str | None = None  # most recent reference worth sending onwards
        self.saved_link: str | None = None
        self.pending: list[str] = []
        self.transformed: dict[str, str] | None = None  # field -> {{transform.field}} after a transform step

    def nid(self, base: str) -> str:
        i, k = base, 2
        while i in self.ids:
            i, k = f"{base}_{k}", k + 1
        self.ids.add(i)
        return i

    def add(self, typ: str, params: dict, label: str | None = None, base: str | None = None) -> str:
        i = self.nid(base or typ)
        node = {"id": i, "type": typ, "params": params}
        if label:
            node["label"] = label
        self.nodes.append(node)
        return i

    def link(self, a: str | None, b: str, branch: str | None = None) -> None:
        if a is None:
            return
        e = {"from": a, "to": b}
        if branch:
            e["branch"] = branch
        self.edges.append(e)

    # ----------------------------------------------------------- field refs
    def field_ref(self, words: str) -> str:
        w = words.lower().strip()
        tt = self.trigger_type
        if tt == "email_received":
            if re.search(r"subject|title", w):
                return "{{trigger.subject}}"
            if re.search(r"sender|from", w):
                return "{{trigger.from}}"
            if re.search(r"attach", w):
                return "{{trigger.has_attachments}}"
            return "{{trigger.body}}"
        if tt == "form_submitted":
            fields = self.nodes[0]["params"]["fields"].split(",")
            for f in fields:
                if f in w.replace(" ", "_"):
                    return f"{{{{trigger.{f}}}}}"
            key = re.sub(r"\W+", "_", w.split()[-1]) if w else "message"
            if key not in fields:
                self.nodes[0]["params"]["fields"] += f",{key}"
            return f"{{{{trigger.{key}}}}}"
        if tt == "webhook":
            key = re.sub(r"\W+", "_", w.split()[-1]) if w else "message"
            return f"{{{{trigger.body.{key}}}}}"
        if self.last_content:
            return self.last_content
        return "{{trigger.input}}" if tt == "manual" else "{{trigger.timestamp}}"

    def event_text(self) -> str:
        tt = self.trigger_type
        if tt == "form_submitted":
            fields = self.nodes[0]["params"]["fields"].split(",")
            return "New submission: " + ", ".join(f"{f}={{{{trigger.{f}}}}}" for f in fields[:4])
        return TRIGGER_FIELDS.get(tt, "Automation ran")

    def message_text(self) -> str:
        base = self.event_text()
        if self.last_content and self.last_content not in base:
            base = self.last_content if self.trigger_type in ("schedule", "manual") else f"{base}\n{self.last_content}"
        if self.saved_link:
            base += f" (saved: {self.saved_link})"
        return base

    def recipient(self, seg: str) -> str:
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", seg) or re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", self.request)
        if m:
            return m.group(0).rstrip(".")
        if re.search(r"\b(?:them|the sender|reply|back|the customer|the person|thank|the candidate|the applicant|the user)", seg, re.I):
            if self.trigger_type == "email_received":
                return "{{trigger.from}}"
            if self.trigger_type == "form_submitted":
                return "{{trigger.email}}"
            if self.trigger_type == "webhook":
                return "{{trigger.body.email}}"
        return "{{env.MY_EMAIL}}"

    # ----------------------------------------------------------- actions
    def action(self, typ: str, seg: str) -> str | None:
        tt = self.trigger_type
        if typ == "drive_save":
            if tt == "email_received" and re.search(r"attach", self.t):
                ext = re.search(r"\b(pdf|csv|xlsx|docx|png|jpg)s?\b", self.t)
                ex = self.add("extract_attachments", {"source": "{{trigger.attachments}}",
                                                      "file_types": ext.group(1) if ext else ""},
                              base="extract_attachments")
                self.pending.append(ex)
                params = {"file_name": f"{{{{{ex}.first_name}}}}", "content": f"{{{{{ex}.first_content}}}}",
                          "mime_type": "application/pdf" if (ext and ext.group(1) == "pdf") or "invoice" in self.t else "application/octet-stream"}
            else:
                content = self.last_content or self.message_text()
                stamp = "{{trigger.timestamp}}" if tt == "schedule" else "{{trigger.submitted_at}}" if tt == "form_submitted" else "latest"
                params = {"file_name": f"flowforge-{stamp}.txt", "content": content, "mime_type": "text/plain"}
            folder = re.search(r"\bfolder (?:called|named) [\"']?([\w-]+)", seg + " " + self.t, re.I) or \
                re.search(r"(?:in|to|into)\s+(?:the|my|our)?\s*[\"']?([\w -]{2,30}?)[\"']?\s+folder", seg + " " + self.t, re.I)
            label = None
            if folder:
                params["folder_id"] = "{{env.DRIVE_FOLDER_ID}}"
                label = f"Save to Drive ({folder.group(1).strip().title()})"
            i = self.add("drive_save", params, label, base="save_to_drive")
            self.saved_link = f"{{{{{i}.link}}}}"
            return i
        if typ == "drive_list":
            m = re.search(r"(?:named|called|containing|with)\s+[\"']?([\w -]+?)[\"']?(?:\s|$|,|\.)", seg, re.I)
            i = self.add("drive_list", {"query": f"name contains '{m.group(1)}'" if m else ""}, base="list_files")
            self.last_content = f"{{{{{i}.files}}}}"
            return i
        if typ == "sheets_append":
            if tt == "email_received":
                values = "{{trigger.from}}, {{trigger.subject}}, {{trigger.message_id}}"
            elif self.transformed:
                values = ", ".join(self.transformed.values())
            elif tt == "form_submitted":
                values = ", ".join(f"{{{{trigger.{f}}}}}" for f in self.nodes[0]["params"]["fields"].split(","))
            elif tt == "webhook":
                keys = [k for k in ("name", "email", "amount", "event", "message") if k in self.t] or ["name", "email"]
                values = ", ".join(f"{{{{trigger.body.{k}}}}}" for k in keys)
            else:
                values = f"{{{{trigger.{'timestamp' if tt == 'schedule' else 'input'}}}}}" + (f", {self.last_content}" if self.last_content else "")
            return self.add("sheets_append", {"spreadsheet_id": "{{env.SHEET_ID}}", "range": "Sheet1!A1", "values": values},
                            base="append_row")
        if typ == "sheets_read":
            i = self.add("sheets_read", {"spreadsheet_id": "{{env.SHEET_ID}}", "range": "Sheet1!A1:Z100"}, base="read_sheet")
            self.last_content = f"{{{{{i}.rows}}}}"
            return i
        if typ == "slack_message":
            ch = re.search(r"#[\w-]+", seg) or re.search(r"#[\w-]+", self.request)
            return self.add("slack_message", {"webhook_url": "{{env.SLACK_WEBHOOK_URL}}", "text": self.message_text(),
                                              "channel": ch.group(0) if ch else ""}, base="notify_slack")
        if typ == "discord_message":
            return self.add("discord_message", {"webhook_url": "{{env.DISCORD_WEBHOOK_URL}}", "content": self.message_text()},
                            base="notify_discord")
        if typ == "teams_message":
            return self.add("teams_message", {"webhook_url": "{{env.TEAMS_WEBHOOK_URL}}", "text": self.message_text()},
                            base="notify_teams")
        if typ == "sms_message":
            m = re.search(r"\+\d[\d -]{7,}\d", self.request)
            return self.add("sms_message", {"to": m.group(0).replace(" ", "").replace("-", "") if m else "{{env.MY_PHONE}}",
                                            "body": self.message_text()[:300]}, base="send_sms")
        if typ == "send_email":
            to = self.recipient(seg)
            if tt == "email_received" and to == "{{trigger.from}}":
                subject = "Re: {{trigger.subject}}"
            elif re.search(r"thank", seg + self.t):
                subject = "Thank you"
            elif tt == "schedule":
                subject = "Scheduled report {{trigger.timestamp}}"
            else:
                subject = "FlowForge notification"
            name = "{{trigger.name}}" if tt == "form_submitted" else "{{trigger.body.name}}" if tt == "webhook" else "there"
            own_subject, own_body = custom_email_text(self.request)
            if own_body:
                body = own_body if re.match(r"(hi|hello|dear|hey)\b", own_body, re.I) else f"Hi {name},\n\n{own_body}"
            elif re.search(r"thank", seg, re.I):
                body = f"Hi {name},\n\nThanks for getting in touch. We received your message and will reply soon."
            else:
                body = self.message_text()
            if own_subject:
                subject = own_subject
            return self.add("send_email", {"to": to, "subject": subject, "body": body}, base="send_email")
        if typ == "llm_summarize":
            text = self.last_content or ("{{trigger.body}}" if tt in ("email_received", "webhook")
                                         else "{{trigger.message}}" if tt == "form_submitted" else self.field_ref(""))
            if tt == "form_submitted" and "message" not in self.nodes[0]["params"]["fields"].split(","):
                self.nodes[0]["params"]["fields"] += ",message"
            instruction = "Summarize this in two sentences."
            tr = re.search(r"translate[^.,;]*?\bto (\w+)", seg, re.I)
            if tr:
                instruction = f"Translate this into {tr.group(1).title()}. Reply with the translation only."
            if re.search(r"classify", seg, re.I):
                instruction = "Classify this as one of: urgent, normal, spam. Answer with one word."
            i = self.add("llm_summarize", {"text": text, "instruction": instruction, "model": llm.agent_model()}, base="summarize")
            self.last_content = f"{{{{{i}.summary}}}}"
            return i
        if typ == "create_task":
            title = "{{trigger.subject}}" if tt == "email_received" else (
                "Follow up: {{trigger.name}}" if tt == "form_submitted" else "Follow up: " + self.event_text()[:60])
            proj = re.search(r"\b([A-Z]{2,10})\b project|project ([A-Z]{2,10})\b", self.request)
            desc = self.last_content or ("{{trigger.body}}" if tt == "email_received" else self.event_text())
            return self.add("create_task", {"project": (proj.group(1) or proj.group(2)) if proj else "OPS",
                                            "title": title, "description": desc}, base="create_task")
        if typ == "calendar_create":
            start = {"webhook": "{{trigger.body.start}}", "schedule": "{{trigger.timestamp}}",
                     "form_submitted": "{{trigger.date}}"}.get(tt, "{{env.DEFAULT_EVENT_START}}")
            if tt == "form_submitted" and "date" not in self.nodes[0]["params"]["fields"].split(","):
                self.nodes[0]["params"]["fields"] += ",date"
            title = "Call with {{trigger.name}}" if tt == "form_submitted" else (
                "{{trigger.subject}}" if tt == "email_received" else "FlowForge event")
            attendees = "{{trigger.email}}" if tt == "form_submitted" else ""
            return self.add("calendar_create", {"title": title, "start": start, "duration_minutes": 30,
                                                "attendees": attendees}, base="create_event")
        if typ == "delay":
            m = re.search(r"(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hours?|d|days?)\b", seg, re.I)
            n = int(m.group(1)) if m else 60
            unit = (m.group(2).lower() if m else "s")[0]
            return self.add("delay", {"seconds": n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]}, base="wait")
        if typ == "transform":
            known = ["name", "email", "phone", "company", "message", "amount", "address", "subject", "status", "rating"]
            words = [f for f in known if re.search(rf"\b{f}s?\b", seg, re.I)] or ["name", "email"]
            fields = {f: self.field_ref(f) for f in words}
            params = {"fields": fields}
            case = "upper" if re.search(r"upper ?case|capitals|capitali[sz]e", seg, re.I) else \
                "lower" if re.search(r"lower ?case", seg, re.I) else ""
            if case:
                params["case"] = case
            i = self.add("transform", params, base="transform")
            self.transformed = {f: f"{{{{{i}.{f}}}}}" for f in fields}
            self.last_content = json.dumps(self.transformed)
            return i
        if typ == "http_request":
            url = re.search(r"https?://[^\s,;]+", seg) or re.search(r"https?://[^\s,;]+", self.request)
            # the verb right before the URL decides: "post/send/forward ... to <url>" vs "pull/fetch ... from <url>"
            pre = seg[max(0, url.start() - 45):url.start()] if url and url.re.pattern and url.string is seg else seg
            method = "POST" if re.search(r"\b(?:post|send|forward|submit|push)\b", pre, re.I) \
                and not re.search(r"\bfrom\s*$", pre, re.I) else "GET"
            params = {"method": method, "url": url.group(0).rstrip(".") if url else "{{env.API_URL}}"}
            if method == "POST":
                # forward the whole payload when there is one; otherwise a small JSON event
                params["body"] = (self.last_content if self.transformed else "{{trigger.body}}" if tt == "webhook" else
                                  self.last_content if self.last_content else
                                  json.dumps({"event": self.event_text().replace('"', "'")}))
            i = self.add("http_request", params, base="call_api")
            self.last_content = f"{{{{{i}.body}}}}"
            return i
        if typ == "ai_agent":
            i = self.add("ai_agent", self.agent_params(seg), base="agent")
            self.last_content = f"{{{{{i}.answer}}}}"
            return i
        if typ == "log_message":
            return self.add("log_message", {"message": self.message_text()}, base="log")
        if typ == "notify":
            m = re.search(r"\bremind me\b[^.]*?\bto (.+?)(?:\.|$)", self.request, re.I)
            if m:
                return self.add("notify", {"message": "Reminder: " + m.group(1).strip()}, label="Reminder", base="remind")
            return self.add("notify", {"message": "Done. " + self.message_text()}, base="notify")
        return None

    def agent_params(self, seg: str) -> dict:
        task = re.sub(r"\s+", " ", self.request.strip().rstrip("."))
        chat = self.trigger_type == "manual"
        params = {
            "instruction": (f"You are an AI agent built for this purpose: {task}. "
                            "Work step by step, use your tools for facts, numbers, dates and web pages, "
                            "and say so when you are not sure."),
            "input": "{{trigger.input}}" if chat else (self.last_content or f"{task}\n\nContext: {self.event_text()}"),
            "tools": agent_tools_for(self.request),
            "model": llm.agent_model(),
        }
        url = re.search(r"https?://[^\s,;]+", self.request)
        if url:
            params["url"] = url.group(0).rstrip(".")
        return params

    def chain(self, segment: str, start: str | None, branch: str | None) -> str | None:
        """Add the actions found in `segment` as a chain after `start`. Returns the last id."""
        prev, first_branch = start, branch
        for _, typ, _m in _find_actions(segment):
            self.pending: list[str] = []
            i = self.action(typ, segment)
            if i is None:
                continue
            for p in self.pending + [i]:
                self.link(prev, p, first_branch)
                prev, first_branch = p, None
        return prev if prev != start else None


def template_plan(request: str) -> dict:
    b = _Builder(request)
    # the user's own e-mail wording must not be read as extra steps ("saying we saved it to drive")
    clean = request
    for own in custom_email_text(request):
        if own:
            clean = clean.replace(own, " ")
    ttype, tparams = detect_trigger(clean)
    b.trigger_type = ttype
    b.add(ttype, tparams, base="trigger")

    text = clean
    # drop the trigger clause so "when I get an email" does not read as "send an email"
    trig_clause = re.match(r"\s*(?:when(?:ever)?|each time|every time|once|if)\b[^,]*?(?:,|\bthen\b)", text, re.I)
    if ttype != "manual" and trig_clause and not COND_RE.match(text[trig_clause.start():].strip()):
        text = text[trig_clause.end():]
    elif ttype == "schedule":
        text = re.sub(r"\b(?:(?:every|each|on)\s+(?:\d+\s+)?(?:day|morning|evening|night|week|weekday|month|hour|minute|monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?|daily|weekly|hourly|monthly)"
                      r"(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?\s*,?", "", text, count=1, flags=re.I)

    before, cond, then_part, else_part = _split_condition(text)
    last = b.chain(before, "trigger", None) or "trigger"
    if cond:
        value = cond.group("value").strip("\"'")
        op = OP_MAP[cond.group("op").lower()]
        if op in ("greater_than", "less_than"):
            value = value.replace("$", "").replace(",", "")
        cid = b.add("condition", {"field": b.field_ref(cond.group("field")), "operator": op, "value": value},
                    label=f"If {' '.join(cond.group('field').split()[-2:])} {cond.group('op').lower()} {value}", base="check")
        b.link(last, cid)
        saved = (b.last_content, b.saved_link)
        b.chain(then_part, cid, "true")
        b.last_content, b.saved_link = saved
        if else_part.strip():
            b.chain(else_part, cid, "false")
        if not any(e["from"] == cid for e in b.edges):
            nid = b.add("notify", {"message": "Condition matched: " + b.event_text()}, base="notify")
            b.link(cid, nid, "true")
    if len(b.nodes) == 1:
        # Nothing automation-shaped was found: the request is a task for an AI agent
        # (e.g. "help students plan their study week"), so build one that can do it.
        nid = b.add("ai_agent", b.agent_params(request), base="agent")
        b.link("trigger", nid)

    name = re.sub(r"\s+", " ", request.strip().rstrip("."))
    name = name[0].upper() + name[1:] if name else "Automation"
    return {"name": name[:80], "request": request, "nodes": b.nodes, "edges": b.edges}


# =========================================================== repair
ALIASES = {
    "slack": "slack_message", "post_to_slack": "slack_message", "slack_notify": "slack_message",
    "discord": "discord_message", "teams": "teams_message", "sms": "sms_message", "twilio": "sms_message",
    "email": "send_email", "gmail": "send_email", "gmail_send": "send_email", "send_mail": "send_email",
    "email_trigger": "email_received", "gmail_trigger": "email_received", "new_email": "email_received",
    "cron": "schedule", "timer": "schedule", "start": "manual", "manual_trigger": "manual",
    "form": "form_submitted", "form_trigger": "form_submitted", "webhook_trigger": "webhook",
    "google_sheets": "sheets_append", "sheet_append": "sheets_append", "append_row": "sheets_append",
    "spreadsheet": "sheets_append", "google_drive": "drive_save", "drive_upload": "drive_save", "upload": "drive_save",
    "if": "condition", "branch": "condition", "filter": "condition", "switch": "condition",
    "wait": "delay", "sleep": "delay", "set": "transform", "map": "transform", "code": "transform",
    "http": "http_request", "api": "http_request", "request": "http_request",
    "summarize": "llm_summarize", "llm": "llm_summarize", "ai": "llm_summarize",
    "jira": "create_task", "task": "create_task", "calendar": "calendar_create", "log": "log_message",
    "reply": "notify", "respond": "notify",
}

SMART_DEFAULTS = {
    "to": "{{env.MY_EMAIL}}", "subject": "FlowForge notification", "title": "FlowForge task",
    "start": "{{env.DEFAULT_EVENT_START}}", "url": "{{env.API_URL}}", "spreadsheet_id": "{{env.SHEET_ID}}",
    "file_name": "flowforge-output.txt", "project": "OPS", "path": "flowforge-hook", "cron": "0 9 * * *",
    "title_form": "Contact form", "fields": "name,email,message",
}


def _first_output_ref(plan: dict, before: str) -> str:
    trig = trigger_of(plan)
    if not trig:
        return "FlowForge"
    outs = sorted(catalog.outputs_for(trig))
    prefer = [o for o in ("subject", "body", "message", "input", "timestamp") if o in outs]
    return f"{{{{{trig['id']}.{(prefer or outs or ['input'])[0]}}}}}"


def _type_from_params(node: dict) -> str | None:
    """Guess an unknown node's type from the parameters it carries (e.g. spreadsheet_id+values -> sheets_append)."""
    have = set(node.get("params") or {})
    if not have:
        return None
    best, score = None, 0.0
    for nt in catalog.NODES:
        names = {p.name for p in nt.params}
        if not names:
            continue
        overlap = len(have & names)
        s = overlap / len(have | names)
        if overlap >= 2 and s > score:
            best, score = nt.type, s
    return best if score >= 0.5 else None


def repair(plan: dict, report, request: str = "") -> tuple[dict, list[str]]:
    """Deterministically fix every error the validator reported. Returns (plan, actions taken).

    Missing parameters are re-derived from the original request first (so a lost
    'greater_than' comes back as 'greater_than'), and only then from catalog defaults."""
    plan = copy.deepcopy(plan)
    try:
        reference = node_map(normalize(template_plan(request))) if request else {}
    except Exception:
        reference = {}
    actions: list[str] = []
    nodes = node_map(plan)
    codes = {i.code for i in report.errors}

    def drop_node(nid: str):
        ins = [e for e in plan["edges"] if e["to"] == nid]
        outs = [e for e in plan["edges"] if e["from"] == nid]
        plan["nodes"] = [n for n in plan["nodes"] if n["id"] != nid]
        plan["edges"] = [e for e in plan["edges"] if nid not in (e["from"], e["to"])]
        for a in ins:
            for b in outs:
                e = {"from": a["from"], "to": b["to"], **({"branch": a["branch"]} if a.get("branch") else {})}
                if e not in plan["edges"]:
                    plan["edges"].append(e)

    for issue in report.errors:
        n = node_map(plan).get(issue.node or "")
        c = issue.code
        if c == "E_UNKNOWN_TYPE" and n:
            raw = re.sub(r"[^a-z0-9]+", "_", str(n.get("type", "")).lower()).strip("_")
            new = ALIASES.get(raw) or _type_from_params(n) or ALIASES.get(re.sub(r"^(?:post|send|add|save|create|do)_(?:to_)?", "", raw)) \
                or next(iter(difflib.get_close_matches(raw, list(catalog.CATALOG), 1, 0.6)), None)
            if new:
                actions.append(f"{n['id']}: unknown type '{n['type']}' -> '{new}'")
                n["type"] = new
            else:
                actions.append(f"{n['id']}: removed node of unknown type '{n['type']}'")
                drop_node(n["id"])
        elif c == "E_DANGLING_EDGE":
            ids = set(node_map(plan))
            before = len(plan["edges"])
            plan["edges"] = [e for e in plan["edges"] if e.get("from") in ids and e.get("to") in ids]
            if len(plan["edges"]) != before:
                actions.append("removed edges pointing at missing nodes")
        elif c in ("E_SELF_LOOP", "E_TRIGGER_INCOMING"):
            trig = trigger_of(plan)
            plan["edges"] = [e for e in plan["edges"] if e["from"] != e["to"] and not (trig and e["to"] == trig["id"])]
            actions.append("removed self-loops / edges into the trigger")
        elif c == "E_BRANCH_LABEL" and n:
            outs = [e for e in plan["edges"] if e["from"] == n["id"]]
            used = {e.get("branch") for e in outs}
            for e in outs:
                if e.get("branch") not in ("true", "false"):
                    e["branch"] = "true" if "true" not in used else "false"
                    used.add(e["branch"])
            actions.append(f"{n['id']}: labelled condition branches")
        elif c == "E_MISSING_PARAM" and n:
            nt = catalog.get(n["type"])
            m = re.search(r"parameter '(\w+)'", issue.message)
            if nt and m:
                pname = m.group(1)
                p = nt.param(pname)
                ref_node = reference.get(n["id"])
                if ref_node and ref_node["type"] == n["type"] and ref_node["params"].get(pname) not in (None, ""):
                    val = copy.deepcopy(ref_node["params"][pname])
                elif p and p.default not in (None, ""):
                    val = copy.deepcopy(p.default)
                elif p and p.kind == "mapping":
                    val = {"value": _first_output_ref(plan, n["id"])}
                elif p and p.kind == "secret":
                    val = f"{{{{env.{pname.upper()}}}}}"
                elif pname in SMART_DEFAULTS:
                    val = SMART_DEFAULTS[pname]
                elif pname in ("body", "text", "message", "content", "description") and trigger_of(plan):
                    trig = trigger_of(plan)
                    outs = [o for o in sorted(catalog.outputs_for(trig)) if o not in ("attachments", "headers", "query")]
                    val = "FlowForge: " + ", ".join(f"{o}={{{{{trig['id']}.{o}}}}}" for o in outs[:4])
                else:
                    val = _first_output_ref(plan, n["id"])
                n.setdefault("params", {})[pname] = val
                actions.append(f"{n['id']}: filled missing '{pname}' with {val!r}")
        elif c in ("E_LITERAL_SECRET", "E_EMBEDDED_CREDENTIAL") and n:
            nt = catalog.get(n["type"])
            for k, v in list(n.get("params", {}).items()):
                p = nt.param(k) if nt else None
                from .validator import SECRET_PATTERNS
                if p and p.kind == "secret":
                    if not re.fullmatch(r"\s*\{\{\s*env\.[A-Z][A-Z0-9_]*\s*\}\}\s*", str(v)):
                        env = {"slack_message": "SLACK_WEBHOOK_URL", "discord_message": "DISCORD_WEBHOOK_URL",
                               "teams_message": "TEAMS_WEBHOOK_URL"}.get(n["type"], k.upper())
                        n["params"][k] = f"{{{{env.{env}}}}}"
                        actions.append(f"{n['id']}: moved literal secret in '{k}' to env var {env}")
                elif isinstance(v, str):
                    new = v
                    for _name, rx in SECRET_PATTERNS:
                        new = rx.sub("{{env.REDACTED_SECRET}}", new)
                    if new != v:
                        n["params"][k] = new
                        actions.append(f"{n['id']}: redacted an embedded credential in '{k}'")
        elif c in ("E_BAD_REF", "E_REF_NOT_UPSTREAM", "E_BAD_REF_FIELD", "E_BAD_ENV_REF") and n:
            from .plan import ancestors
            anc = ancestors(plan, n["id"])
            nm = node_map(plan)

            def fix(root, path, _anc=anc, _nm=nm):
                if root == "env":
                    name = re.sub(r"[^A-Z0-9_]", "_", (path[0] if path else "VALUE").upper())
                    return "{{env." + (name if name[0].isalpha() else "V_" + name) + "}}"
                if root in _anc and root in _nm:
                    outs = sorted(catalog.outputs_for(_nm[root]))
                    if not path or path[0] in outs:
                        return "{{" + ".".join([root, *path]) + "}}"
                    best = difflib.get_close_matches(path[0], outs, 1, 0.0)
                    return "{{" + ".".join([root, best[0] if best else outs[0], *path[1:]]) + "}}"
                return _first_output_ref(plan, n["id"])
            n["params"] = substitute(n["params"], fix)
            actions.append(f"{n['id']}: rewired invalid data references")
        elif c == "E_BAD_ENUM" and n:
            nt = catalog.get(n["type"])
            m = re.search(r"\.(\w+)=", issue.message)
            if nt and m and (p := nt.param(m.group(1))):
                cur = str(n["params"].get(p.name, "")).lower().replace(" ", "_")
                best = difflib.get_close_matches(cur, [o.lower() for o in p.options], 1, 0.3)
                val = next((o for o in p.options if best and o.lower() == best[0]), p.default or p.options[0])
                n["params"][p.name] = val
                actions.append(f"{n['id']}: '{p.name}' -> {val!r}")
        elif c == "E_BAD_NUMBER" and n:
            nt = catalog.get(n["type"])
            m = re.search(r"\.(\w+)=", issue.message)
            if nt and m and (p := nt.param(m.group(1))):
                digits = re.findall(r"[\d.]+", str(n["params"].get(p.name, "")))
                n["params"][p.name] = float(digits[0]) if digits else p.default
                actions.append(f"{n['id']}: '{p.name}' coerced to a number")
        elif c == "E_BAD_MAPPING" and n:
            v = n["params"].get("fields")
            n["params"]["fields"] = {"value": v} if isinstance(v, str) else {"value": _first_output_ref(plan, n["id"])}
            actions.append(f"{n['id']}: 'fields' wrapped into a mapping")

    # structural fixes that need the whole graph
    if "E_NO_TRIGGER" in codes:
        roots = [x["id"] for x in plan["nodes"] if not any(e["to"] == x["id"] for e in plan["edges"])]
        plan["nodes"].insert(0, {"id": "trigger", "type": "manual", "params": {"input": "Run the automation"}})
        for r in roots[:1] or [x["id"] for x in plan["nodes"][1:2]]:
            plan["edges"].append({"from": "trigger", "to": r})
        actions.append("added a manual trigger")
    if "E_MULTIPLE_TRIGGERS" in codes:
        trigs = [x for x in plan["nodes"] if (nt := catalog.get(x["type"])) and nt.kind == "trigger"]
        for extra in trigs[1:]:
            drop_node(extra["id"])
            actions.append(f"removed extra trigger '{extra['id']}'")
    for _ in range(10):
        cyc = find_cycle(plan)
        if not cyc:
            break
        a, b = cyc[-2], cyc[-1]
        plan["edges"] = [e for e in plan["edges"] if not (e["from"] == a and e["to"] == b)]
        actions.append(f"broke cycle by removing edge {a} -> {b}")
    if "E_UNREACHABLE" in codes or "E_NO_TRIGGER" in codes:
        trig = trigger_of(plan)
        for _ in range(len(plan["nodes"])):
            reach, stack = set(), [trig["id"]] if trig else []
            while stack:
                u = stack.pop()
                if u not in reach:
                    reach.add(u)
                    stack.extend(e["to"] for e in plan["edges"] if e["from"] == u)
            orphan = next((x for x in plan["nodes"] if x["id"] not in reach), None)
            if orphan is None:
                break
            order = [x["id"] for x in plan["nodes"]]
            prev = next((order[i] for i in range(order.index(orphan["id"]) - 1, -1, -1) if order[i] in reach), trig["id"])
            if node_map(plan)[prev]["type"] == "condition":
                plan["edges"].append({"from": prev, "to": orphan["id"], "branch": "true"})
            else:
                plan["edges"].append({"from": prev, "to": orphan["id"]})
            actions.append(f"connected unreachable '{orphan['id']}' after '{prev}'")
    if "E_NO_ACTIONS" in codes:
        trig = trigger_of(plan)
        plan["nodes"].append({"id": "notify", "type": "notify", "params": {"message": "Automation ran"}})
        plan["edges"].append({"from": trig["id"], "to": "notify"})
        actions.append("added a notify step (plan had no actions)")
    return plan, actions


# =========================================================== fault injection (demo/eval)
def inject_fault(plan: dict) -> tuple[dict, list[str]]:
    """Corrupt a valid plan the way LLMs typically do, to exercise self-correction."""
    plan = copy.deepcopy(plan)
    done = []
    acts = [n for n in plan["nodes"] if catalog.get(n["type"]) and catalog.get(n["type"]).kind != "trigger"]
    if acts:
        n = acts[0]
        req = catalog.get(n["type"]).required_params
        if req:
            n["params"].pop(req[-1], None)
            done.append(f"removed required param '{req[-1]}' from '{n['id']}'")
    plan["edges"].append({"from": plan["nodes"][-1]["id"], "to": "ghost_step"})
    done.append("added an edge to a non-existent step 'ghost_step'")
    for n in plan["nodes"]:
        if n["type"] == "slack_message":
            # A FAKE webhook, assembled at runtime so the source holds no webhook-shaped literal
            # (GitHub push protection would rightly flag one). The validator must still catch it.
            n["params"]["webhook_url"] = "/".join(["https://hooks.slack.com/services", "T0" + "FAKE1234",
                                                   "B0" + "FAKE5678", "x" * 24])
            done.append("pasted a literal Slack webhook URL into the plan")
            break
    if acts and len(acts) > 1:
        acts[-1]["type"] = "post_to_" + acts[-1]["type"].split("_")[0]
        done.append(f"renamed '{acts[-1]['id']}' to an unknown node type '{acts[-1]['type']}'")
    return plan, done


# =========================================================== public API
def plan(request: str, mode: str = "auto") -> tuple[dict, str, str]:
    """-> (plan, source, note). mode: auto | llm | template."""
    if mode in ("auto", "llm"):
        ok, info = llm.available()
        if ok:
            try:
                return llm_plan(request), f"llm:{info}", ""
            except Exception as exc:  # fall back, but say so
                note = f"LLM planning failed ({exc.__class__.__name__}: {str(exc)[:120]}); used template planner"
                return template_plan(request), "template", note
        if mode == "llm":
            raise RuntimeError(info)
        return template_plan(request), "template", info
    return template_plan(request), "template", ""


def revise(plan_: dict, report, request: str, source: str) -> tuple[dict, str, list[str]]:
    """One self-correction step. -> (new plan, source, actions)."""
    if source.startswith("llm"):
        try:
            new = normalize(llm_revise(plan_, report, request))
            from .validator import validate_plan
            if len(validate_plan(new).errors) < len(report.errors):
                return new, source, ["LLM revised the plan using the validation report"]
        except Exception as exc:
            pass
    # Deterministic repair. Fixing one error can reveal another (e.g. a node's params are only
    # checked once its type is known), so repeat passes while they keep reducing the error count.
    from .validator import validate_plan
    new, actions = plan_, []
    current = report
    for _ in range(4):
        new, acts = repair(new, current, request)
        new = normalize(new)
        actions += acts
        nxt = validate_plan(new)
        if nxt.ok or not acts or len(nxt.errors) >= len(current.errors) and not acts:
            break
        current = nxt
    return new, "repair", actions or ["no automatic repair available"]
