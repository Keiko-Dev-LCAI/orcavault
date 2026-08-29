# GROK — OrcaVault MCP Deploy Runbook

Hand-off for Grok. Claude wrote + verified the code (`py_compile` OK). Grok does all
git / deploy / on-chain steps. This runbook covers everything from a fresh house wallet
to a listed, discoverable MCP server.

## Hard constraints (do not violate)

- **Commit identity ONLY:** `KeikoDev <keikodev@users.noreply.github.com>`. No real name/email.
- **Keiko's personal Ledger `0x69DEd8…D7156` stays OUT.** It is NOT the house wallet, NOT
  the funder, NOT the owner. Never touch it for this.
- **No private keys in git or the agent card.** House wallet key lives in env / secret
  manager only. `.env` is git-ignored; only `.env.example` is committed.
- **The 2 LCAI paywall stays.** No free tier. The house wallet pays the same one-time fee.
- After every push, run an identity/email/key scan on the diff before it goes public.

## Files in this build

```
mcp-server/
  orcavault_mcp.py            # the MCP server (5 tools, custodial signing, rate limits)
  requirements.txt            # mcp, requests, eth-account
  .env.example                # config template — NO secrets
  README.md                   # agent-facing storefront
  GROK-MCP-DEPLOY-RUNBOOK.md  # this file
```

## Step 1 — Create the house wallet

Generate ONE fresh, anonymous wallet. This is the custodial signer the MCP server holds.

```python
from eth_account import Account
acct = Account.create()
print(acct.address)          # public — safe to share
print(acct.key.hex())        # SECRET — into secret manager only, never git
```

Record only the **public address** in the App Manager dashboard. Put the private key
straight into the host's secret manager as `ORCAVAULT_AGENT_WALLET_KEY`.

## Step 2 — Register the house wallet (pay the one-time 2 LCAI)

Same flow a human uses. Keiko funds it (small LCAI for the one payment); the house wallet
does NOT need an ongoing balance afterward — the relay pays gas.

1. Send **2 LCAI** (native, chain 9200) from the house wallet to the relay payment address
   `0xbb0ab4c9E15a20661CA0C4d2b6f5D32A7EdF7646`.
2. Call `POST /api/register-payment` with the house wallet address + that tx hash.
3. Confirm with `GET /api/check-access?wallet=<house_address>` → `access: true`.

(The signed-transfer + register-payment steps that require the key are Keiko's to run,
since Claude cannot move funds. Grok can prep the exact calls.)

## Step 3 — Smoke test locally

```bash
cd mcp-server
pip install -r requirements.txt
cp .env.example .env         # fill ORCAVAULT_AGENT_WALLET_KEY
python orcavault_mcp.py
```

Then, from an MCP client, run the loop: `relay_status` → `check_access` (expect
`access: true`) → `store_memory` a tiny test doc → `get_memory_status` until ready →
`get_memory` and confirm the bytes round-trip.

## Step 4 — Deploy as its own small service

Keep it **separate from the relay** (zero risk to the live relay). Railway, next to the
relay, is fine.

- New service / repo (anonymous identity as usual).
- Set env from `.env.example` in the host's secret store — `ORCAVAULT_AGENT_WALLET_KEY`
  as a secret, never plaintext in the repo.
- For remote agents, front the stdio server with an MCP-aware HTTP/SSE gateway.
- Re-run the loop from Step 3 against the deployed URL.

## Step 5 — List it so agents can discover it

1. Publish the server repo (with `README.md` as the storefront).
2. Confirm the agent card `agents/orcavault-mcp-card.json` points at the live MCP endpoint.
3. List in: the community MCP directory (modelcontextprotocol.io), Claude's connector /
   Cowork connector registry, other assistants' marketplaces, and Lightchain's own app
   directory (first-party capability).

## Note on the USDC rail (separate, in progress)

The USDC one-time-unlock path is a different work item (Hyperlane warp route on Base +
relay `_verify_usdc_payment`). When that lands: set `ORCAVAULT_USDC_TOKEN_ADDRESS` on the
relay, flip the card's USDC `paymentOptions` entry to `active: true` with the real
`tokenAddress`, run a $1 test, then identity scan. Not required for the MCP door to work —
LCAI unlock is enough for v1.

## Private v1 first

Per Keiko: v1 is a **private test** — Claude/Keiko upload through it to prove the full loop
before any public listing. Do Steps 1–4, verify the round-trip, THEN decide on Step 5.
