# Flowise runtime demonstration

Run: 2026-11-24 22:19:53
Flowise: http://localhost:3000 (version 3.1.4)

## Request

> when a webhook receives an order, if the amount is over 100 post it to http://127.0.0.1:53116/big-orders otherwise post it to http://127.0.0.1:53116/small-orders

## Generated flow

FlowForge Flowise validator: pass (0 errors, 0 warnings)

| Node id | Flowise node | Label |
|---|---|---|
| startAgentflow_0 | startAgentflow v1.4 | Start |
| conditionAgentflow_0 | conditionAgentflow v1 | If amount is over 100 |
| httpAgentflow_0 | httpAgentflow v1.1 | HTTP Request |
| httpAgentflow_1 | httpAgentflow v1.1 | HTTP Request |

## Import

`POST /api/v1/chatflows` -> id `86e33469-c81d-40ad-84c1-81a536cbee6e`, type `AGENTFLOW`; read back with 4 nodes.
Canvas: http://localhost:3000/v2/agentcanvas/86e33469-c81d-40ad-84c1-81a536cbee6e

## Execution in Flowise (via `POST /api/v1/webhook/<id>`)

### amount = 250 -> expected `/big-orders`: PASS

Node statuses reported by Flowise: Start = FINISHED, If amount is over 100 = FINISHED, HTTP Request = FINISHED

Receiver got: `[["/big-orders", {"order_id": "A-250", "amount": 250}]]`

### amount = 40 -> expected `/small-orders`: PASS

Node statuses reported by Flowise: Start = FINISHED, If amount is over 100 = FINISHED, HTTP Request = FINISHED

Receiver got: `[["/small-orders", {"order_id": "A-40", "amount": 40}]]`

## Result: PASS

