# GROK — Final Handoff (everything left to finish OrcaVault-for-AI)

This is the complete remaining plate. Nothing outside this list. Work top to bottom.

## Standing rules (apply to every step)
- Commit ONLY as `KeikoDev <keikodev@users.noreply.github.com>`. No real name/email.
- Run an identity/email/key scan on every push before it's public.
- No private keys in git or the agent card — secret manager / env only.
- Keiko's personal Ledger `0x69DEd8…D7156` stays out entirely (not funder, owner, or signer).
- Keep the 2 LCAI paywall and the now-live USDC card untouched.

## 1. Security hygiene (do first)
- Rotate any Railway secrets/env values that were printed in plain text during the deploy
  session, and update Railway with the new values. You know which were exposed.

## 2. MCP server — deploy it
Code is written + verified in this folder. Full detail in `GROK-MCP-DEPLOY-RUNBOOK.md`.
1. Create one fresh anonymous house wallet. Public address → dashboard only; private key →
   secret manager as `ORCAVAULT_AGENT_WALLET_KEY`.
2. Register it: pay the one-time 2 LCAI to the relay, call `register-payment`, confirm
   `check-access` → access true.
3. Deploy the server as its own small service (separate from the relay), env from
   `.env.example`. Front with an MCP-aware HTTP/SSE gateway for remote agents.
4. Private v1 test: run the loop `relay_status` → `check_access` → `store_memory` (tiny
   file) → `get_memory_status` → `get_memory`, confirm the bytes round-trip.
5. Only after that passes, list it: community MCP directory (modelcontextprotocol.io),
   Claude/Cowork connector registry, other assistant marketplaces, Lightchain app directory.

## 3. USDC bridge — make it self-deliver
The $1 test used a manual TrustedRelayerIsm. Finish the automatic path.
1. Stand up a Hyperlane relayer for the warp route (Base collateral `0x9F1ff33…99F1` ↔
   Lightchain synthetic `0x1113E233…b003`).
2. Run/point a validator that actually announces storage, then switch the synthetic's ISM
   off TrustedRelayerIsm to the normal multisig/validator ISM.
3. Re-test: send 1 USDC on Base, confirm it auto-arrives on Lightchain and `register-payment`
   unlocks with `paid_with: usdc` — no manual `Mailbox.process`.

## Done =
Secrets rotated, MCP server live + privately tested + listed, USDC bridge self-delivering
and re-tested. At that point OrcaVault-for-AI is fully finished on both rails.
