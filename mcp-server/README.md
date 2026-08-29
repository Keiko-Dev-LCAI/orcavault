# OrcaVault MCP — permanent on-chain memory for AI agents

OrcaVault lets an AI agent store any file **permanently on-chain** (Lightchain) and
retrieve it later by id. This MCP server is the door: connect it and your agent gets
five tools to check, store, confirm, and retrieve — no wallet of its own required.

## What it does

You hand OrcaVault a file (as base64). It writes the bytes on-chain and hands back a
memory id plus a public URL. The record is permanent and verifiable: anyone can see
exactly what was stored. Retrieval is open — give the id, get the file.

## Tools

| Tool | What it does |
|---|---|
| `relay_status` | Is the OrcaVault relay online and healthy? |
| `check_access` | Is the house wallet allowed to upload, and what's the fee? |
| `store_memory` | Store a file permanently on-chain. Returns a memory id + retrieval URL. |
| `get_memory_status` | Has memory `#id` finished writing and is it ready? |
| `get_memory` | Retrieve memory `#id` — always a URL; small files also returned inline. |

### `store_memory` inputs

- `title` (required) — human label for the memory.
- `content_base64` (required) — the file bytes, base64-encoded.
- `mime_type` — e.g. `image/png`, `application/pdf`. Default `application/octet-stream`.
- `caption` — optional description.
- `mem_type` — one of `photo`, `video`, `audio`, `document`. Default `document`.
- `template` — optional display template tag.

Returns `{ success, memoryId, retrieveUrl, statusUrl, ... }`. Large files write as many
on-chain chunks, so uploads can take a while — poll `get_memory_status` until ready.

## Pricing

**One-time unlock, not per-upload.** The house wallet pays the relay fee **once**
(2 LCAI native on Lightchain, chain 9200). After that the relay covers all gas and the
wallet can keep uploading with no ongoing balance. Ethereum-side agents holding USDC can
pay the equivalent one-time unlock in USDC once the bridged-USDC route is live (see the
agent card `paymentOptions`).

## Guardrails

Because the relay spends the operator's gas on every upload, the server enforces limits
**before** it ever calls the relay:

- Max file size (default 5 MB).
- Rate limit (default 3 uploads / rolling minute).
- Daily quota (default 50 uploads / rolling 24h).

All are configurable via env — see `.env.example`.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in ORCAVAULT_AGENT_WALLET_KEY
python orcavault_mcp.py   # stdio transport (local)
```

For remote hosting, run behind an MCP-aware HTTP/SSE gateway. Deploy notes and the
house-wallet setup live in `GROK-MCP-DEPLOY-RUNBOOK.md`.

## Trust

OrcaVault is registered under ERC-8004 (Trustless Agents) with on-chain identity on both
Lightchain and Base. Every stored memory has a verifiable on-chain record of what was
stored and by which wallet.
