# Flowise import check (all evaluation requests)

Run 2026-11-24 22:20 against Flowise 3.1.4 at http://localhost:3000.

**52/52** flows were accepted by `POST /api/v1/chatflows` and read back with identical node ids, node types and edge count (each flow was deleted afterwards).

This proves Flowise accepts and stores the generated JSON. It does not prove each flow runs: flows using Gmail/Drive/Sheets tools or a chat model need credentials/a model attached in Flowise first. Runtime execution is shown in `flowise-runtime-demo.md`.

| ID | Nodes | Edges | Result (Flowise node types) |
|---|---|---|---|
| R01 | 6 | 5 | PASS (agent, customFunction, http, start, tool) |
| R02 | 2 | 1 | PASS (start, tool) |
| R03 | 3 | 2 | PASS (start, tool) |
| R04 | 4 | 3 | PASS (llm, start, tool) |
| R05 | 5 | 4 | PASS (condition, customFunction, http, start, tool) |
| R06 | 4 | 3 | PASS (customFunction, http, start) |
| R07 | 4 | 3 | PASS (agent, condition, start, tool) |
| R08 | 3 | 2 | PASS (agent, start, tool) |
| R09 | 5 | 4 | PASS (agent, customFunction, http, llm, start) |
| R10 | 3 | 2 | PASS (start, tool) |
| R11 | 5 | 3 | PASS (customFunction, http, start, stickyNote, tool) |
| R12 | 3 | 2 | PASS (start, tool) |
| R13 | 3 | 1 | PASS (http, start, stickyNote) |
| R14 | 4 | 3 | PASS (customFunction, http, start) |
| R15 | 4 | 3 | PASS (condition, start, tool) |
| R16 | 5 | 4 | PASS (agent, customFunction, start, tool) |
| R17 | 4 | 3 | PASS (http, llm, start, tool) |
| R18 | 3 | 2 | PASS (directReply, llm, start) |
| R19 | 2 | 1 | PASS (agent, start) |
| R20 | 2 | 1 | PASS (agent, start) |
| R21 | 2 | 1 | PASS (agent, start) |
| R22 | 4 | 3 | PASS (agent, llm, start, tool) |
| H01 | 3 | 2 | PASS (customFunction, start, tool) |
| H02 | 3 | 2 | PASS (http, start, tool) |
| H03 | 3 | 2 | PASS (customFunction, http, start) |
| H04 | 4 | 3 | PASS (agent, customFunction, start, tool) |
| H05 | 5 | 4 | PASS (condition, customFunction, http, start, tool) |
| H06 | 4 | 3 | PASS (customFunction, http, start, tool) |
| H07 | 3 | 1 | PASS (start, stickyNote, tool) |
| H08 | 5 | 4 | PASS (agent, customFunction, http, llm, start) |
| H09 | 2 | 1 | PASS (start, tool) |
| H10 | 3 | 2 | PASS (http, start, tool) |
| H11 | 3 | 2 | PASS (condition, http, start) |
| H12 | 5 | 4 | PASS (agent, customFunction, http, start, tool) |
| H13 | 3 | 2 | PASS (start, tool) |
| H14 | 2 | 1 | PASS (directReply, start) |
| H15 | 4 | 3 | PASS (customFunction, http, start) |
| G01 | 4 | 3 | PASS (agent, customFunction, http, start) |
| G02 | 3 | 2 | PASS (http, start, tool) |
| G03 | 3 | 2 | PASS (customFunction, http, start) |
| G04 | 3 | 2 | PASS (start, tool) |
| G05 | 4 | 3 | PASS (customFunction, http, start, tool) |
| G06 | 2 | 1 | PASS (start, tool) |
| G07 | 3 | 2 | PASS (start, tool) |
| G08 | 4 | 3 | PASS (customFunction, http, llm, start) |
| G09 | 2 | 1 | PASS (start, tool) |
| G10 | 5 | 4 | PASS (agent, customFunction, start, tool) |
| G11 | 4 | 2 | PASS (customFunction, http, start, stickyNote) |
| G12 | 3 | 2 | PASS (customFunction, start, tool) |
| G13 | 4 | 3 | PASS (http, llm, start, tool) |
| G14 | 2 | 1 | PASS (start, tool) |
| G15 | 4 | 3 | PASS (agent, condition, start, tool) |
