/* FlowForge website.
 * Two engines, same results:
 *  - server:  the FastAPI backend (python -m flowforge serve) — LLM planner + push to Flowise
 *  - browser: the same Python engine running in Pyodide — for static hosting, no server needed
 * Add ?engine=browser to force the in-browser engine.
 * The server address comes from <meta name="flowforge-api"> (set at build time), or ?api=<url>;
 * empty means "same origin" (the page is served by the server itself).
 */
"use strict";

const PYODIDE_VERSION = "0.26.4";
const PYODIDE_URL = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
const ENGINE_FILES = [
  "__init__.py", "catalog.py", "plan.py", "planner.py", "llm.py", "validator.py", "simulator.py",
  "pipeline.py", "generators/__init__.py", "generators/flowise.py", "generators/n8n.py",
  "generators/python_gen.py", "data/flowise_node_specs.json",
];
const EXAMPLES = [
  "a study assistant that helps engineering students with calculations, deadlines and reading web articles",
  "what is the EMI on a 5 lakh loan at 9% for 3 years",
  "when I get an email with an invoice attached, save it to Drive and ping me on Slack",
  "when someone submits the contact form, email them a thank-you email and add a row to the spreadsheet",
  "every monday at 9am read the sales sheet, summarize it and email me the summary",
  "when a webhook receives an order, if the amount is over 100 post to slack otherwise add a row to the spreadsheet",
  "every day at 8am call https://api.example.com/status and post it to discord",
  "when the feedback form is submitted, if the rating is less than 3 create a jira task otherwise add a row to the spreadsheet",
];
const TRIGGERS = new Set(["manual", "webhook", "schedule", "email_received", "form_submitted"]);
const LOGIC = new Set(["condition", "transform", "delay"]);

const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return n;
};

const API_BASE = (() => {
  const q = new URLSearchParams(location.search).get("api");
  const m = document.querySelector('meta[name="flowforge-api"]');
  const v = (q ?? (m ? m.content : "")).trim();
  return v && !v.endsWith("/") ? v + "/" : v;
})();
const api = (path) => API_BASE + path;

let engine = null;
let last = null;

// ------------------------------------------------------------------ engines
async function detectEngine() {
  const forceBrowser = new URLSearchParams(location.search).get("engine") === "browser";
  if (!forceBrowser) {
    if (API_BASE) setEngine("busy", "Connecting to the FlowForge server…");
    try {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), API_BASE ? 8000 : 3000);
      const r = await fetch(api("health"), { cache: "no-store", signal: ctl.signal });
      clearTimeout(timer);
      if (r.ok) {
        const h = await r.json();
        if (h && h.catalog_nodes) return { kind: "server", health: h, remote: Boolean(API_BASE) };
      }
    } catch (_) { /* no backend, or it is asleep: fall through */ }
  }
  const eng = await loadBrowserEngine();
  eng.serverDown = Boolean(API_BASE) && !forceBrowser;
  return eng;
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src; s.onload = resolve; s.onerror = () => reject(new Error("Could not load " + src));
    document.head.append(s);
  });
}

async function loadBrowserEngine() {
  setEngine("busy", "Loading the Python engine in your browser…");
  await loadScript(PYODIDE_URL + "pyodide.js");
  const py = await window.loadPyodide({ indexURL: PYODIDE_URL });
  py.FS.mkdirTree("/app/flowforge/generators");
  py.FS.mkdirTree("/app/flowforge/data");
  await Promise.all(ENGINE_FILES.map(async (f) => {
    const r = await fetch("engine/flowforge/" + f);
    if (!r.ok) throw new Error("Missing engine file " + f);
    py.FS.writeFile("/app/flowforge/" + f, await r.text());
  }));
  py.runPython("import sys, json\nsys.path.insert(0, '/app')\nfrom flowforge import pipeline");
  return { kind: "browser", py };
}

async function runPipeline(request, mode, injectFault) {
  if (engine.kind === "server") {
    const r = await fetch(api("run"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request, mode, inject_fault: injectFault }),
    });
    if (r.status === 524 || r.status === 504) throw new Error("The server took too long to answer. Try the rule-based planner, or try again.");
    if (!r.ok) throw new Error(`Server returned ${r.status}: ${(await r.text()).slice(0, 200)}`);
    return r.json();
  }
  const py = engine.py;
  py.globals.set("REQ", request);
  py.globals.set("FAULT", injectFault);
  const out = py.runPython("json.dumps(pipeline.run(REQ, mode='template', inject_fault=FAULT), default=str)");
  return JSON.parse(out);
}

function setEngine(state, text) {
  const e = $("engine");
  e.querySelector(".dot").className = "dot " + state;
  $("engine-text").textContent = text;
}

// ------------------------------------------------------------------ helpers
function download(name, text, type = "application/json") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = el("a", { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function copy(text, btn) {
  try { await navigator.clipboard.writeText(text); flash(btn, "Copied"); }
  catch (_) { flash(btn, "Select the text to copy"); }
}

function flash(btn, msg) {
  const old = btn.textContent; btn.textContent = msg;
  setTimeout(() => { btn.textContent = old; }, 1400);
}

function codeBlock(text) { return el("pre", { class: "code", tabindex: "0" }, el("code", { text })); }

function toolbar(hint, name, text, type) {
  const c = el("button", { class: "secondary", type: "button" }, "Copy");
  c.addEventListener("click", () => copy(text, c));
  const d = el("button", { class: "secondary", type: "button" }, "Download " + name);
  d.addEventListener("click", () => download(name, text, type));
  return el("div", { class: "toolbar" }, el("span", { class: "hint", text: hint }), c, d);
}

const slugify = (s) => (s || "automation").toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 50).replace(/^-+|-+$/g, "");

// ------------------------------------------------------------------ diagram
function renderDiagram(plan) {
  const nodes = plan.nodes || [], edges = plan.edges || [];
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const depth = {}, row = {};
  const order = topo(nodes, edges);
  const taken = new Set();
  for (const id of order) {
    const ins = edges.filter((e) => e.to === id && depth[e.from] !== undefined);
    depth[id] = ins.length ? Math.max(...ins.map((e) => depth[e.from] + 1)) : 0;
    let r = ins.length ? Math.min(...ins.map((e) => row[e.from] + (e.branch === "false" ? 1 : 0))) : 0;
    while (taken.has(depth[id] + ":" + r)) r++;
    taken.add(depth[id] + ":" + r);
    row[id] = r;
  }
  // top-to-bottom: depth runs down, branches spread sideways
  const W = 196, H = 52, GX = 28, GY = 44, PAD = 14;
  const lanes = Math.max(0, ...Object.values(row)) + 1, levels = Math.max(0, ...Object.values(depth)) + 1;
  const width = PAD * 2 + lanes * W + (lanes - 1) * GX, height = PAD * 2 + levels * H + (levels - 1) * GY;
  const x = (id) => PAD + row[id] * (W + GX), y = (id) => PAD + depth[id] * (H + GY);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", width); svg.setAttribute("height", height);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Flow: " + order.map((id) => byId[id].label || byId[id].type).join(", then "));
  const mk = (tag, attrs, text) => {
    const n = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text != null) n.textContent = text;
    svg.append(n); return n;
  };
  const defs = mk("defs", {});
  const marker = document.createElementNS(ns, "marker");
  for (const [k, v] of Object.entries({ id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto" })) marker.setAttribute(k, v);
  const tip = document.createElementNS(ns, "path");
  tip.setAttribute("d", "M0,0 L10,5 L0,10 z"); tip.setAttribute("fill", "var(--muted)");
  marker.append(tip); defs.append(marker);

  for (const e of edges) {
    if (depth[e.from] === undefined || depth[e.to] === undefined) continue;
    const x1 = x(e.from) + W / 2, y1 = y(e.from) + H, x2 = x(e.to) + W / 2, y2 = y(e.to) - 2, my = (y1 + y2) / 2;
    mk("path", { d: `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`, fill: "none", stroke: "var(--muted)", "stroke-width": "1.5", "marker-end": "url(#arrow)" });
    if (e.branch) {
      const label = e.branch === "true" ? "yes" : "no";
      const lx = (x1 + x2) / 2, ly = my;
      mk("rect", { x: lx - 15, y: ly - 9, width: 30, height: 18, rx: 9, fill: "var(--surface)", stroke: e.branch === "true" ? "var(--ok)" : "var(--err)" });
      mk("text", { x: lx, y: ly + 4, "text-anchor": "middle", "font-size": "11", "font-weight": "600", fill: e.branch === "true" ? "var(--ok)" : "var(--err)" }, label);
    }
  }
  for (const id of order) {
    const n = byId[id];
    const color = TRIGGERS.has(n.type) ? "var(--trigger)" : LOGIC.has(n.type) ? "var(--logic)" : "var(--action)";
    const label = n.label || n.type.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()).replace(/^Ai /, "AI ");
    mk("rect", { x: x(id), y: y(id), width: W, height: H, rx: 8, fill: "var(--surface)", stroke: color, "stroke-width": "1.5" });
    mk("rect", { x: x(id), y: y(id), width: 6, height: H, rx: 3, fill: color });
    const t = mk("text", { x: x(id) + 16, y: y(id) + 24, "font-size": "13", "font-weight": "600", fill: "var(--ink)" }, label.length > 25 ? label.slice(0, 24) + "…" : label);
    if (label.length > 25) { const tt = document.createElementNS(ns, "title"); tt.textContent = label; t.append(tt); }
    mk("text", { x: x(id) + 16, y: y(id) + 42, "font-size": "11", fill: "var(--muted)", "font-family": "var(--font-mono)" }, n.id);
  }
  $("diagram").replaceChildren(svg);
}

function topo(nodes, edges) {
  const indeg = Object.fromEntries(nodes.map((n) => [n.id, 0]));
  for (const e of edges) if (e.to in indeg && e.from in indeg) indeg[e.to]++;
  const q = nodes.filter((n) => indeg[n.id] === 0).map((n) => n.id), out = [];
  while (q.length) {
    const id = q.shift(); out.push(id);
    for (const e of edges) if (e.from === id && e.to in indeg && --indeg[e.to] === 0) q.push(e.to);
  }
  return out;
}

// ------------------------------------------------------------------ panels
function renderAttempts(r) {
  const list = $("attempts");
  list.replaceChildren();
  if (r.planner_note) list.append(el("li", { class: "fault" }, el("div", { class: "head" }, "Planner note"), el("div", { class: "muted", text: r.planner_note })));
  for (const a of r.attempts) {
    if (a.source === "fault-injection") {
      list.append(el("li", { class: "fault" }, el("div", { class: "head" }, "Fault injected ", el("small", {}, "(demo)")),
        el("ul", {}, a.actions.map((x) => el("li", { text: x })))));
      continue;
    }
    const items = [...a.errors.map((x) => el("li", { class: "error", text: x })), ...(a.revision || []).map((x) => el("li", { class: "fix", text: "Fixed: " + x }))];
    list.append(el("li", { class: a.ok ? "ok" : "err" },
      el("div", { class: "head" }, `Attempt ${a.attempt} · ${a.ok ? "valid" : a.errors.length + " error(s)"} `, el("small", {}, "planner: " + a.source)),
      items.length ? el("ul", {}, items) : null));
  }
  if (r.removed_steps && r.removed_steps.length) {
    list.append(el("li", { class: "err" }, el("div", { class: "head" }, "Warning: valid, but not what was asked"),
      el("div", { class: "muted", text: "Self-correction removed requested steps: " + r.removed_steps.join(", ") })));
  }
}

function warningsFor(r, target) {
  return (r.report.issues || []).filter((i) => i.target === target && i.level !== "error");
}

function renderFlowise(r) {
  const p = $("panel-flowise");
  if (!r.ok) return p.replaceChildren(el("p", { class: "muted", text: "No Flowise flow: the plan did not pass validation." }));
  const text = JSON.stringify(r.flowise.export, null, 2);
  const warns = warningsFor(r, "flowise");
  const note = r.flowise.post_import.length
    ? el("div", { class: "note" },
        el("strong", {}, "Needs a little setup in Flowise. "),
        "Deploy below, or import the file (Agentflows → Import), then:",
        el("ul", {}, r.flowise.post_import.map((s) => el("li", { text: s }))))
    : el("div", { class: "note" }, el("strong", {}, "No extra setup needed. "),
        "It runs on your local model. Deploy below, or import the file (Agentflows → Import).");
  const kids = [toolbar("AgentFlow v2 export for Flowise 3.1", slugify(r.plan.name) + "-flowise.json", text), note];
  if (warns.length) kids.push(el("ul", { class: "issues" }, warns.map(issueItem)));
  kids.push(codeBlock(text));
  if (engine.kind === "server") kids.push(pushForm(r));
  p.replaceChildren(...kids);
}

function store(key, value) {
  try { if (value === undefined) return localStorage.getItem(key) || ""; localStorage.setItem(key, value); } catch (_) { return ""; }
}

function pushForm(r) {
  const flowData = r.flowise.export.AgentFlowV2[0].flowData;
  const varNames = [...new Set([...flowData.matchAll(/\{\{\s*\$vars\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g)].map((m) => m[1]))];
  const url = el("input", { id: "fw-url", value: store("flowforge-flowise-url") || "http://localhost:3000", "aria-label": "Flowise URL" });
  const key = el("input", { id: "fw-key", type: "password", placeholder: engine.remote ? "Flowise API key" : "Flowise API key (optional if set on the server)", "aria-label": "Flowise API key" });
  const vars = varNames.map((n) => el("input", { id: "var-" + n, placeholder: n + " value (e.g. your Slack webhook URL)", "aria-label": n }));
  const isChat = r.plan.nodes[0] && r.plan.nodes[0].type === "manual";
  const testQ = isChat ? el("input", { id: "fw-test", placeholder: "Test message (optional), e.g. a question your professor might ask", "aria-label": "Test message" }) : null;
  const out = el("div", { class: "deploy-out" });
  const btn = el("button", { class: "primary", type: "button" }, "Deploy & run in Flowise");
  btn.addEventListener("click", async () => {
    store("flowforge-flowise-url", url.value);
    btn.disabled = true; btn.textContent = "Deploying…";
    out.replaceChildren(el("p", { class: "muted", text: "Creating the flow, connecting it, and running a test. An agent's first answer can take up to a minute while the model loads." }));
    const variables = {};
    varNames.forEach((n, i) => { if (vars[i].value.trim()) variables[n] = vars[i].value.trim(); });
    try {
      const res = await fetch(api("flowise/deploy"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan: r.plan, url: url.value, api_key: key.value, variables, confirm: true,
                               test_input: testQ && testQ.value.trim() ? testQ.value.trim() : null }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail));
      out.replaceChildren(...deployResult(d, url.value, key.value));
    } catch (e) {
      out.replaceChildren(el("p", { class: "error-text", text: "Could not deploy: " + e.message }));
    }
    btn.disabled = false; btn.textContent = "Deploy & run in Flowise";
  });
  return el("div", { class: "push" },
    el("strong", {}, "Deploy to your Flowise and run it"),
    el("p", { class: "muted", text: "Creates the flow in your Flowise, connects the local model, variables and any credentials you saved in Flowise, then runs it once and shows the result." }),
    el("div", { class: "row" }, url, key),
    vars.length ? el("div", { class: "row" }, vars) : null,
    testQ ? el("div", { class: "row" }, testQ) : null,
    el("div", { class: "row" }, btn),
    engine.health && engine.health.public
      ? el("p", { class: "muted", text: "Through the public link, FlowForge can only reach a Flowise with a public address. On your own Mac, open http://127.0.0.1:8000 to deploy into a local Flowise." })
      : null,
    out);
}

function deployResult(d, flowiseUrl, apiKey) {
  const kids = [
    el("ul", { class: "steps" }, d.steps.map((s) => el("li", { class: s.ok ? "ok" : "err" },
      el("span", { class: "mark" }, s.ok ? "✓" : "!"), el("span", {}, el("b", { text: s.step + " " }), s.detail)))),
  ];
  if (d.todo.length) kids.push(el("div", { class: "note" }, el("strong", {}, "Still to do"), el("ul", {}, d.todo.map((t) => el("li", { text: t })))));
  const run = d.run;
  if (run && !run.skipped) {
    kids.push(el("div", { class: "run " + (run.ok ? "ok" : "err") },
      el("div", {}, el("span", { class: "pill " + (run.ok ? "ok" : "err") }, run.ok ? "Ran in Flowise" : "Run failed"), " ", el("span", { class: "muted", text: run.seconds + "s" })),
      run.sent ? el("div", { class: "muted", text: "Sent: " + run.sent }) : null,
      run.answer ? el("div", { class: "answer", text: run.answer }) : null,
      run.error ? el("div", { class: "error-text", text: run.error }) : null,
      run.nodes && run.nodes.length ? el("div", { class: "muted", text: run.nodes.map((n) => `${n.node}: ${n.status}`).join(" → ") }) : null));
  } else if (run && run.skipped) {
    kids.push(el("p", { class: "muted", text: run.detail }));
  }
  if (d.platform === "n8n") {
    kids.push(el("p", {}, el("a", { href: d.url, target: "_blank", rel: "noopener" }, "Open it in the n8n editor"),
      d.form_url ? " · " : null,
      d.form_url ? el("a", { href: d.form_url, target: "_blank", rel: "noopener" }, "Open its live form") : null));
  } else {
    kids.push(el("p", {}, el("a", { href: d.url, target: "_blank", rel: "noopener" }, "Open it on the Flowise canvas")));
  }
  if (d.chat && run && run.ok) kids.push(chatBox(d.id, run.chat_id, flowiseUrl, apiKey));
  return kids;
}

function chatBox(flowId, chatId, flowiseUrl, apiKey) {
  const log = el("div", { class: "chatlog", "aria-live": "polite" });
  const input = el("input", { id: "chat-input", placeholder: "Ask your agent anything…", "aria-label": "Message to the agent" });
  const send = el("button", { class: "secondary", type: "submit" }, "Send");
  const form = el("form", { class: "row" }, input, send);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = input.value.trim();
    if (!msg) return;
    log.append(el("div", { class: "msg you", text: msg }));
    input.value = ""; send.disabled = true;
    const waiting = el("div", { class: "msg bot muted", text: "Thinking…" });
    log.append(waiting);
    try {
      const res = await fetch(api("flowise/chat"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ flow_id: flowId, message: msg, chat_id: chatId, url: flowiseUrl, api_key: apiKey }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(typeof d.detail === "string" ? d.detail : res.statusText);
      chatId = d.chat_id || chatId;
      waiting.replaceWith(el("div", { class: "msg bot", text: d.answer + "  (" + d.seconds + "s)" }));
    } catch (err) {
      waiting.replaceWith(el("div", { class: "msg bot error-text", text: "Error: " + err.message }));
    }
    send.disabled = false; input.focus();
  });
  return el("div", { class: "chat" }, el("strong", {}, "Talk to your agent (running in Flowise)"), log, form);
}

function renderN8n(r) {
  const p = $("panel-n8n");
  if (!r.ok) return p.replaceChildren(el("p", { class: "muted", text: "No n8n workflow: the plan did not pass validation." }));
  const text = JSON.stringify(r.n8n.workflow, null, 2);
  const warns = warningsFor(r, "n8n");
  p.replaceChildren(
    toolbar("n8n → Workflows → Import from File. Pick credentials on the nodes that ask.", slugify(r.plan.name) + "-n8n.json", text),
    warns.length ? el("ul", { class: "issues" }, warns.map(issueItem)) : null,
    codeBlock(text));
}

function renderPython(r) {
  const p = $("panel-python");
  if (!r.ok) return p.replaceChildren(el("p", { class: "muted", text: "No script: the plan did not pass validation." }));
  const code = r.python.code;
  const env = r.python.env_vars || [];
  p.replaceChildren(
    toolbar("Standard library + requests. Dry run by default.", "automation.py", code, "text/x-python"),
    el("div", { class: "note" },
      el("div", {}, el("code", {}, "python automation.py"), " prints every action and changes nothing. ",
        el("code", {}, "FLOWFORGE_DRY_RUN=0 python automation.py"), " runs it for real and asks before each external action."),
      env.length ? el("div", {}, "Environment variables: ", env.map((v, i) => [i ? ", " : "", el("code", { text: v })])) : null),
    codeBlock(code));
}

function issueItem(i) {
  return el("li", {}, el("span", { class: "lvl " + i.level, text: i.level }),
    el("span", {}, el("span", { class: "code", text: `${i.target}/${i.code}${i.node ? " · " + i.node : ""}  ` }), i.message));
}

function renderChecks(r) {
  const issues = r.report.issues || [];
  const summary = r.ok
    ? `Plan and all three outputs passed. ${r.report.warnings} warning(s) to read before deploying.`
    : `${r.report.errors} error(s) left after ${r.attempts.filter((a) => a.source !== "fault-injection").length} attempts.`;
  const checks = ["Exactly one trigger, every step reachable from it", "Every connection points at a real step",
    "Every required parameter is filled", "No cycles", "Data references point at earlier steps and real fields",
    "Secrets are environment references, never literal values", "Flowise: real node names, versions, handles and edges",
    "n8n: node shape, unique names, valid connections, no embedded credentials", "Python: parses, one function per step, stdlib + requests only"];
  $("panel-checks").replaceChildren(
    el("p", { text: summary }),
    issues.length ? el("ul", { class: "issues" }, issues.map(issueItem)) : el("p", { class: "muted", text: "No warnings or errors." }),
    el("details", {}, el("summary", {}, "What is checked"), el("ul", {}, checks.map((c) => el("li", { text: c })))));
  const count = issues.length;
  $("tab-checks").replaceChildren("Checks", count ? el("span", { class: "count", text: count }) : "");
}

function renderSim(r) {
  const p = $("panel-sim");
  if (!r.simulation) return p.replaceChildren(el("p", { class: "muted", text: "Not run: the plan did not pass validation." }));
  const sim = r.simulation;
  const trace = el("ol", { class: "trace" }, sim.trace.map((s) => el("li", { class: s.status },
    el("span", { class: "mark", "aria-label": s.status }, s.status === "ok" ? "✓" : s.status === "error" ? "!" : "–"),
    el("div", {},
      el("div", { class: "title" }, s.note + " ", el("small", {}, s.node)),
      s.status === "ok" ? el("details", {}, el("summary", {}, "inputs and outputs"),
        el("pre", { text: JSON.stringify({ inputs: s.inputs, outputs: s.outputs }, null, 2) })) : null))));
  const w = sim.world;
  const show = [["Emails sent", w.email_outbox], ["Messages posted", w.messages_posted], ["Files in mock Drive", w.storage_files],
    ["Sheet rows", w.spreadsheet && w.spreadsheet.Sheet1 ? w.spreadsheet.Sheet1.slice(3) : []], ["HTTP calls", w.http_calls],
    ["Tasks", w.tasks], ["Calendar events", w.calendar_events], ["Log", w.log]]
    .filter(([name, v]) => v && (Array.isArray(v) ? v.length : Object.keys(v).length > (name.startsWith("Files") ? 1 : 0)));
  p.replaceChildren(
    el("p", {}, el("span", { class: "pill " + (sim.ok ? "ok" : "err") }, sim.ok ? "Completed" : "Failed"),
      " Dry run against mock email, Drive, Sheets, messaging and HTTP services. No real account was touched."),
    trace,
    show.length ? el("h3", { text: "What the mock services recorded" }) : null,
    show.length ? el("div", { class: "world" }, show.map(([k, v]) => el("div", {}, el("b", { text: k }), el("pre", { text: JSON.stringify(v, null, 1) })))) : null);
}

function renderJson(r) {
  const text = JSON.stringify(r.plan, null, 2);
  $("panel-json").replaceChildren(toolbar("The shared plan all three outputs are generated from.", "plan.json", text), codeBlock(text));
}

function renderStatus(r) {
  const tries = r.attempts.filter((a) => a.source !== "fault-injection").length;
  $("status").replaceChildren(
    el("span", { class: "pill " + (r.ok ? "ok" : "err") }, r.ok ? "Verified" : "Not valid"),
    el("span", { class: "meta", text: `${r.plan.nodes.length} steps · ${tries} attempt${tries > 1 ? "s" : ""} · ${r.seconds}s · planner: ${r.attempts.filter((a) => a.source !== "fault-injection")[0].source}` }));
  $("plan-name").textContent = r.plan.name;
}

function render(r) {
  last = r;
  renderStatus(r); renderDiagram(r.plan); renderAttempts(r);
  renderFlowise(r); renderN8n(r); renderPython(r); renderChecks(r); renderSim(r); renderJson(r);
}

// ------------------------------------------------------------------ wiring
function selectTab(id) {
  for (const t of document.querySelectorAll(".tab")) {
    const on = t.id === id;
    t.setAttribute("aria-selected", on);
    $(t.getAttribute("aria-controls")).hidden = !on;
  }
  try { localStorage.setItem("flowforge-tab", id); } catch (_) {}
}

async function generate(modeOverride) {
  const req = $("request").value.trim();
  if (!req) { $("status").replaceChildren(el("span", { class: "pill err" }, "Type a request first")); return; }
  $("go").disabled = true; $("go").textContent = "Generating…";
  const mode = typeof modeOverride === "string" ? modeOverride : $("mode").value;
  const usesLlm = engine.kind === "server" && engine.health.llm.available && mode !== "template";
  const started = Date.now();
  const meta = el("span", { class: "meta" });
  $("status").replaceChildren(el("span", { class: "pill neutral" }, usesLlm ? "The LLM is planning…" : "Planning, generating and checking…"), meta);
  const tick = setInterval(() => {
    const sec = Math.round((Date.now() - started) / 1000);
    meta.textContent = usesLlm ? `${sec}s · a small model on free CPU takes 30–120s` : `${sec}s`;
  }, 1000);
  $("live-out").hidden = true;
  try {
    await new Promise((r) => setTimeout(r, 20)); // let the status paint before Pyodide blocks the thread
    const r = await runPipeline(req, mode, $("fault").checked);
    render(r);
    // only when the person clicked Generate (the automatic first result on page load passes a mode string)
    if (r.ok && typeof modeOverride !== "string" && engine.kind === "server" && $("live").checked) {
      clearInterval(tick);
      await runLive(r);
    }
  } catch (e) {
    $("status").replaceChildren(el("span", { class: "pill err" }, "Error"), el("span", { class: "meta", text: e.message }));
  }
  clearInterval(tick);
  $("go").disabled = false; $("go").textContent = "Generate";
}

// One click: generate, then create it in Flowise or n8n, run it, and show the real result at the top.
async function runLive(r) {
  const box = $("live-out");
  const target = $("live-target").value === "n8n" ? "n8n" : "flowise";
  const where = target === "n8n" ? "n8n" : "Flowise";
  box.hidden = false;
  box.replaceChildren(el("h3", { text: `Running it for real in ${where}…` }),
    el("p", { class: "muted", text: target === "n8n"
      ? "Creating the workflow in your n8n, publishing it and submitting it once."
      : "Creating the flow in your Flowise and running it. An assistant's first answer can take up to a minute." }));
  $("go").textContent = `Running in ${where}…`;
  const url = store("flowforge-flowise-url") || "http://localhost:3000";
  const sendText = $("live-input").value.trim();
  try {
    const res = await fetch(api(target === "n8n" ? "n8n/deploy" : "flowise/deploy"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(target === "n8n"
        ? { plan: r.plan, confirm: true, test_input: sendText || null }
        : { plan: r.plan, url, api_key: "", variables: {}, confirm: true, test_input: sendText || null }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail));
    const ran = d.run && !d.run.skipped && d.run.ok;
    box.replaceChildren(el("h3", { text: ran ? `✓ It ran for real in ${where}` : `It was created in ${where}, but did not run` }),
      ...deployResult(d, url, ""));
  } catch (e) {
    box.replaceChildren(el("h3", { text: `Could not run it in ${where}` }), el("p", { class: "error-text", text: e.message }),
      el("p", { class: "muted", text: target === "n8n"
        ? "Is n8n running, with an API key saved? Start it with ~/n8n-local/start.sh"
        : "Is Flowise running? Start it with tools/flowise_start.sh" }));
  }
}

function init() {
  const syncLive = () => { $("live-input-row").hidden = !$("live").checked; try { localStorage.setItem("flowforge-live", $("live").checked ? "1" : ""); } catch (_) {} };
  $("live").addEventListener("change", syncLive);
  try { const t = localStorage.getItem("flowforge-live-target"); if (t) $("live-target").value = t; } catch (_) {}
  $("live-target").addEventListener("change", () => { try { localStorage.setItem("flowforge-live-target", $("live-target").value); } catch (_) {} });
  $("examples").replaceChildren(...EXAMPLES.map((x) => {
    const b = el("button", { class: "chip", type: "button", text: x });
    b.addEventListener("click", () => { $("request").value = x; generate(); });
    return b;
  }));
  for (const t of document.querySelectorAll(".tab")) t.addEventListener("click", () => selectTab(t.id));
  $("tabs").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const tabs = [...document.querySelectorAll(".tab")];
    const i = tabs.findIndex((t) => t.getAttribute("aria-selected") === "true");
    const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    selectTab(next.id); next.focus();
  });
  try { const t = localStorage.getItem("flowforge-tab"); if (t && $(t)) selectTab(t); } catch (_) {}
  // shareable links: ?q=<request>&fault=1&tab=tab-sim
  const params = new URLSearchParams(location.search);
  if (params.get("q")) $("request").value = params.get("q");
  if (params.get("fault") === "1") $("fault").checked = true;
  if (params.get("tab") && $(params.get("tab"))) selectTab(params.get("tab"));
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); generate(); });
  $("request").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) generate(); });

  detectEngine().then((eng) => {
    engine = eng;
    if (eng.kind === "server") {
      const llm = eng.health.llm;
      const where = eng.remote ? "Online server" : "Server";
      setEngine(llm.available ? "ok" : "warn", llm.available ? `${where} · LLM planner: ${llm.detail}` : `${where} · rule-based planner (Ollama not running)`);
      if (!eng.health.public) {
        $("live-field").hidden = false;
        try { $("live").checked = localStorage.getItem("flowforge-live") === "1"; } catch (_) {}
        $("live-input-row").hidden = !$("live").checked;
      }
    } else {
      setEngine(eng.serverDown ? "warn" : "ok", eng.serverDown
        ? "Server asleep or unreachable · running in your browser (reload in a minute to use it)"
        : "Running in your browser · rule-based planner");
      $("mode").value = "template";
      for (const o of $("mode").options) if (o.value !== "template") o.disabled = true;
    }
    $("go").disabled = false;
    generate("template"); // instant first result; your own requests use the planner chosen above
  }).catch((e) => {
    setEngine("warn", "Engine failed to load");
    $("status").replaceChildren(el("span", { class: "pill err" }, "Could not start"), el("span", { class: "meta", text: e.message }));
  });
}

init();
