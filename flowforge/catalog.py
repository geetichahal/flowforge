"""The node catalog: FlowForge's shared vocabulary.

Every node a plan may use is defined here exactly once. The planner reads the
descriptions, the validator checks parameters against the schemas, the three
generators look up how each node maps onto Flowise / n8n / Python, and the
simulator uses ``sample_output`` to produce realistic mock results.

Mapping status (per target):
  direct       confirmed node + confirmed parameters
  approximate  node exists, but wiring/params need checking in the target UI
  none         no equivalent; the generator reports the step as unsupported
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Param:
    name: str
    required: bool = False
    default: Any = None
    description: str = ""
    kind: str = "text"  # text | secret | number | enum | mapping
    options: tuple = ()


@dataclass(frozen=True)
class NodeType:
    type: str
    kind: str  # "trigger" | "action" | "logic"
    label: str
    description: str
    params: tuple = ()
    outputs: dict = field(default_factory=dict)
    sample_output: dict = field(default_factory=dict)
    side_effect: bool = False  # real execution must pass the approval gate
    service: str = ""  # mock connector used by the simulator
    flowise: str = "direct"
    n8n: str = "direct"
    keywords: tuple = ()

    def param(self, name: str) -> Param | None:
        return next((p for p in self.params if p.name == name), None)

    @property
    def required_params(self) -> list[str]:
        return [p.name for p in self.params if p.required]


CONDITION_OPERATORS = (
    "contains", "not_contains", "equals", "not_equals", "starts_with",
    "ends_with", "greater_than", "less_than", "is_empty", "is_not_empty",
)

_SAMPLE_EMAIL = {
    "message_id": "msg-001",
    "from": "billing@acme-supplies.com",
    "subject": "Invoice INV-2041 for October",
    "body": "Hi, please find attached invoice INV-2041 for $1,240.00 due 30 Nov.",
    "has_attachments": True,
    "attachments": [
        {"name": "INV-2041.pdf", "mime_type": "application/pdf", "size": 48213,
         "content": "%PDF-1.4 (mock invoice bytes)"},
    ],
}

NODES: list[NodeType] = [
    # ------------------------------------------------------------------ triggers
    NodeType(
        "manual", "trigger", "Manual Trigger",
        "Run the automation on demand (a person clicks run or sends a chat message).",
        params=(Param("input", default="Run the automation", description="Default input text"),),
        outputs={"input": "Text typed by the person who ran the flow"},
        sample_output={"input": "Run the automation"},
        keywords=("manually", "on demand", "when i click", "when i run"),
    ),
    NodeType(
        "webhook", "trigger", "Webhook",
        "Start when an HTTP request is sent to the automation's webhook URL.",
        params=(
            Param("path", True, "flowforge-hook", "URL path segment"),
            Param("method", False, "POST", "HTTP method", "enum", ("GET", "POST", "PUT", "PATCH", "DELETE")),
        ),
        outputs={"body": "Parsed JSON body", "headers": "Request headers", "query": "Query parameters"},
        sample_output={
            "body": {"name": "Ada Lovelace", "email": "ada@example.com", "amount": 250,
                     "message": "Interested in the premium plan", "event": "signup"},
            "headers": {"content-type": "application/json"},
            "query": {},
        },
        keywords=("webhook", "http request comes in", "api call", "receives a post", "when a request"),
    ),
    NodeType(
        "schedule", "trigger", "Schedule",
        "Start on a time schedule (cron expression).",
        params=(
            Param("cron", True, "0 9 * * *", "Cron expression (min hour dom mon dow)"),
            Param("timezone", False, "UTC", "IANA timezone"),
        ),
        outputs={"timestamp": "ISO time the run started"},
        sample_output={"timestamp": "2026-11-24T09:00:00Z"},
        n8n="direct",
        keywords=("every", "daily", "each day", "weekly", "hourly", "every morning", "schedule"),
    ),
    NodeType(
        "email_received", "trigger", "Email Received",
        "Start when a new email arrives in Gmail matching a search query.",
        params=(
            Param("query", False, "is:unread", "Gmail search query, e.g. 'has:attachment subject:invoice'"),
        ),
        outputs={
            "message_id": "Gmail message id", "from": "Sender address", "subject": "Subject line",
            "body": "Plain-text body", "has_attachments": "True if the email has attachments",
            "attachments": "List of {name, mime_type, size, content}",
        },
        sample_output=_SAMPLE_EMAIL,
        service="email",
        flowise="approximate",  # Flowise has no email-arrival event: Agent polls Gmail on a schedule
        keywords=("email", "inbox", "gmail", "mail arrives", "get an email", "receive an email"),
    ),
    NodeType(
        "form_submitted", "trigger", "Form Submitted",
        "Start when someone submits a form with the given fields.",
        params=(
            Param("title", True, "Contact form", "Form title"),
            Param("fields", True, "name,email,message", "Comma-separated field names"),
        ),
        outputs={"submitted_at": "Submission time"},  # plus one output per declared field
        sample_output={"name": "Grace Hopper", "email": "grace@example.com",
                       "message": "Please call me back", "submitted_at": "2026-11-24T10:15:00Z"},
        keywords=("form", "submits", "submission", "sign up form", "survey"),
    ),
    # ------------------------------------------------------------------ messaging
    NodeType(
        "send_email", "action", "Send Email",
        "Send an email through Gmail.",
        params=(
            Param("to", True, None, "Recipient address(es)"),
            Param("subject", True, "Automated message", "Subject"),
            Param("body", True, None, "Body text; may reference earlier steps"),
        ),
        outputs={"message_id": "Sent message id", "status": "sent"},
        sample_output={"message_id": "sent-778", "status": "sent"},
        side_effect=True, service="email",
        keywords=("send an email", "email me", "email the", "reply", "send email", "mail me"),
    ),
    NodeType(
        "slack_message", "action", "Slack Message",
        "Post a message to Slack through an incoming-webhook URL.",
        params=(
            Param("webhook_url", True, "{{env.SLACK_WEBHOOK_URL}}", "Slack incoming-webhook URL", "secret"),
            Param("text", True, None, "Message text"),
            Param("channel", False, "", "Channel name (informational; the webhook decides)"),
        ),
        outputs={"ok": "True when Slack accepted the message"},
        sample_output={"ok": True},
        side_effect=True, service="messaging",
        keywords=("slack", "ping me", "post to slack", "notify slack"),
    ),
    NodeType(
        "discord_message", "action", "Discord Message",
        "Post a message to a Discord channel through a webhook URL.",
        params=(
            Param("webhook_url", True, "{{env.DISCORD_WEBHOOK_URL}}", "Discord webhook URL", "secret"),
            Param("content", True, None, "Message text"),
        ),
        outputs={"ok": "True when Discord accepted the message"},
        sample_output={"ok": True},
        side_effect=True, service="messaging",
        keywords=("discord",),
    ),
    NodeType(
        "teams_message", "action", "Teams Message",
        "Post a message to Microsoft Teams through an incoming-webhook URL.",
        params=(
            Param("webhook_url", True, "{{env.TEAMS_WEBHOOK_URL}}", "Teams incoming-webhook URL", "secret"),
            Param("text", True, None, "Message text"),
        ),
        outputs={"ok": "True when Teams accepted the message"},
        sample_output={"ok": True},
        side_effect=True, service="messaging",
        keywords=("teams", "microsoft teams"),
    ),
    NodeType(
        "sms_message", "action", "SMS Message",
        "Send an SMS text message (Twilio in n8n/Python; AWS SNS tool in Flowise).",
        params=(
            Param("to", True, None, "Phone number in E.164 format"),
            Param("body", True, None, "Message text"),
        ),
        outputs={"sid": "Provider message id"},
        sample_output={"sid": "SM-mock-123"},
        side_effect=True, service="messaging", flowise="approximate",
        keywords=("sms", "text me", "text message", "send a text"),
    ),
    NodeType(
        "notify", "action", "Notify",
        "Return a final message to whoever ran the automation (chat reply / webhook response / console).",
        params=(Param("message", True, None, "Message text"),),
        outputs={"message": "The message returned"},
        sample_output={"message": "Done"},
        service="messaging",
        keywords=("notify me", "let me know", "tell me", "reply with", "respond with"),
    ),
    # ------------------------------------------------------------------ data & storage
    NodeType(
        "sheets_append", "action", "Append to Google Sheet",
        "Append one row to a Google Sheet.",
        params=(
            Param("spreadsheet_id", True, "{{env.SHEET_ID}}", "Spreadsheet id"),
            Param("range", False, "Sheet1!A1", "Target sheet/range in A1 notation"),
            Param("values", True, None, "Comma-separated cell values; may reference earlier steps"),
        ),
        outputs={"updated_range": "Range written", "row": "Values written"},
        sample_output={"updated_range": "Sheet1!A12:C12", "row": ["a", "b", "c"]},
        side_effect=True, service="spreadsheet",
        keywords=("spreadsheet", "google sheet", "sheet", "add a row", "log it in", "append"),
    ),
    NodeType(
        "sheets_read", "action", "Read Google Sheet",
        "Read rows from a Google Sheet.",
        params=(
            Param("spreadsheet_id", True, "{{env.SHEET_ID}}", "Spreadsheet id"),
            Param("range", False, "Sheet1!A1:Z100", "Range in A1 notation"),
        ),
        outputs={"rows": "List of rows", "row_count": "Number of rows"},
        sample_output={"rows": [["Task", "Owner"], ["Ship v2", "Ada"], ["Fix bug", "Alan"]], "row_count": 3},
        service="spreadsheet",
        keywords=("read the sheet", "read rows", "from the spreadsheet", "get rows"),
    ),
    NodeType(
        "drive_save", "action", "Save to Google Drive",
        "Save a file to Google Drive.",
        params=(
            Param("file_name", True, None, "File name"),
            Param("content", True, None, "File content (text) or a reference to an attachment"),
            Param("folder_id", False, "", "Parent folder id (empty = My Drive root)"),
            Param("mime_type", False, "text/plain", "MIME type"),
        ),
        outputs={"file_id": "Drive file id", "link": "Web link"},
        sample_output={"file_id": "drv-555", "link": "https://drive.google.com/file/d/drv-555/view"},
        side_effect=True, service="storage",
        keywords=("drive", "google drive", "save it", "upload", "store the file"),
    ),
    NodeType(
        "drive_list", "action", "List Google Drive Files",
        "List files in Google Drive matching a query.",
        params=(Param("query", False, "", "Drive search query, e.g. \"name contains 'report'\""),),
        outputs={"files": "List of {id, name}", "count": "Number of files"},
        sample_output={"files": [{"id": "drv-1", "name": "Q3 report.pdf"}], "count": 1},
        service="storage",
        keywords=("list files", "files in drive", "find files"),
    ),
    NodeType(
        "extract_attachments", "action", "Extract Attachments",
        "Pull attachments (optionally only certain file types) out of an email.",
        params=(
            Param("source", True, "{{trigger.attachments}}", "Reference to the email's attachments"),
            Param("file_types", False, "pdf", "Comma-separated extensions to keep (empty = all)"),
        ),
        outputs={"files": "Matching attachments", "count": "How many matched",
                 "first_name": "Name of the first match", "first_content": "Content of the first match"},
        sample_output={"files": _SAMPLE_EMAIL["attachments"], "count": 1,
                       "first_name": "INV-2041.pdf", "first_content": "%PDF-1.4 (mock invoice bytes)"},
        flowise="approximate",
        keywords=("attachment", "attached"),
    ),
    NodeType(
        "http_request", "action", "HTTP Request",
        "Call any HTTP API.",
        params=(
            Param("method", False, "GET", "HTTP method", "enum", ("GET", "POST", "PUT", "PATCH", "DELETE")),
            Param("url", True, None, "Request URL"),
            Param("body", False, "", "JSON body (for POST/PUT/PATCH)"),
        ),
        outputs={"status": "HTTP status code", "body": "Response body"},
        sample_output={"status": 200, "body": {"ok": True, "data": [1, 2, 3]}},
        side_effect=True, service="http",
        keywords=("http", "api", "call the", "fetch", "endpoint", "get request", "post request"),
    ),
    # ------------------------------------------------------------------ logic
    NodeType(
        "condition", "logic", "Condition (If/Else)",
        "Branch: follow the 'true' edge when the check passes, otherwise the 'false' edge.",
        params=(
            Param("field", True, None, "Value to test, usually a reference like {{trigger.subject}}"),
            Param("operator", True, "contains", "Comparison", "enum", CONDITION_OPERATORS),
            Param("value", False, "", "Value to compare against"),
        ),
        outputs={"result": "True or False"},
        sample_output={"result": True},
        keywords=("if ", "only if", "when the", "otherwise", "unless"),
    ),
    NodeType(
        "transform", "logic", "Transform Data",
        "Build new named fields from templates over earlier outputs.",
        params=(Param("fields", True, None, "Mapping of new field name -> template", "mapping"),),
        outputs={},  # one output per key of `fields`
        sample_output={},
        flowise="approximate",
        keywords=("format", "transform", "combine", "reformat", "extract the"),
    ),
    NodeType(
        "delay", "logic", "Delay",
        "Wait for a number of seconds before continuing.",
        params=(Param("seconds", True, 60, "Seconds to wait", "number"),),
        outputs={"waited": "Seconds waited"},
        sample_output={"waited": 60},
        flowise="none",
        keywords=("wait", "delay", "after", "pause"),
    ),
    NodeType(
        "llm_summarize", "action", "Summarize with LLM",
        "Summarize or rewrite text with a local LLM (Ollama).",
        params=(
            Param("text", True, None, "Text to summarize"),
            Param("instruction", False, "Summarize this in two sentences.", "What to do with the text"),
            Param("model", False, "qwen3:4b", "Ollama model name"),
        ),
        outputs={"summary": "Model output"},
        sample_output={"summary": "(mock summary)"},
        service="llm",
        keywords=("summarize", "summary", "tl;dr", "rewrite", "classify"),
    ),
    # ------------------------------------------------------------------ productivity
    NodeType(
        "create_task", "action", "Create Task (Jira)",
        "Create an issue/task in Jira.",
        params=(
            Param("project", True, "OPS", "Jira project key"),
            Param("title", True, None, "Task title"),
            Param("description", False, "", "Task description"),
        ),
        outputs={"task_id": "Issue key", "url": "Issue URL"},
        sample_output={"task_id": "OPS-42", "url": "https://example.atlassian.net/browse/OPS-42"},
        side_effect=True, service="tasks", flowise="approximate",
        keywords=("jira", "task", "ticket", "issue", "to-do", "todo"),
    ),
    NodeType(
        "calendar_create", "action", "Create Calendar Event",
        "Create an event in Google Calendar.",
        params=(
            Param("title", True, None, "Event title"),
            Param("start", True, None, "Start time (ISO 8601)"),
            Param("duration_minutes", False, 30, "Duration", "number"),
            Param("attendees", False, "", "Comma-separated attendee emails"),
        ),
        outputs={"event_id": "Event id", "link": "Event link"},
        sample_output={"event_id": "evt-9", "link": "https://calendar.google.com/event?eid=evt-9"},
        side_effect=True, service="calendar", flowise="approximate",
        keywords=("calendar", "meeting", "event", "schedule a call", "book"),
    ),
    NodeType(
        "log_message", "action", "Log Message",
        "Write a line to the automation's execution log.",
        params=(Param("message", True, None, "Log line"),),
        outputs={"logged": "True"},
        sample_output={"logged": True},
        flowise="none",
        keywords=("log", "record that"),
    ),
]

CATALOG: dict[str, NodeType] = {n.type: n for n in NODES}
TRIGGERS = [n.type for n in NODES if n.kind == "trigger"]

assert len(CATALOG) == len(NODES) == 24, "catalog must have 24 unique nodes"


def get(node_type: str) -> NodeType | None:
    return CATALOG.get(node_type)


def outputs_for(node: dict) -> set[str]:
    """Output fields a concrete plan node produces (some are parameter-dependent)."""
    nt = CATALOG.get(node.get("type", ""))
    if nt is None:
        return set()
    out = set(nt.outputs)
    params = node.get("params") or {}
    if nt.type == "form_submitted":
        out |= {f.strip() for f in str(params.get("fields", "")).split(",") if f.strip()}
    if nt.type == "transform" and isinstance(params.get("fields"), dict):
        out |= set(params["fields"])
    return out


def catalog_for_prompt() -> str:
    """Compact catalog description given to the LLM planner."""
    lines = []
    for n in NODES:
        ps = ", ".join(f"{p.name}{'*' if p.required else ''}" + (f"={p.default!r}" if p.default not in (None, "") else "")
                       for p in n.params)
        outs = ", ".join(n.outputs) or "(fields named in params)"
        lines.append(f"- {n.type} [{n.kind}]: {n.description} | params: {ps or '-'} | outputs: {outs}")
    return "\n".join(lines)
