# OrcaVault Relay — Deploy Checklist

**Security:** Never commit `RELAY_PRIVATE_KEY` to git, briefings, or GitHub. Store only in Railway env vars (or local `.env` excluded by `.gitignore`).

---

## Relay wallet

| Item | Value |
|------|-------|
| **Current relay (2026-07-01)** | `0xbb0ab4c9E15a20661CA0C4d2b6f5D32A7EdF7646` |
| **Old relay (compromised — abandoned)** | `0xC19dc1E0d9d63E6E204207c9eFb53ec1F302015f` |
| **Private key** | Railway env var `RELAY_PRIVATE_KEY` only — generate offline, never paste into repos |

Fund the relay with **500+ LCAI** for gas before heavy upload use.

After rotating the relay wallet, call `setRelayWallet()` on all on-chain contracts — see `SET-RELAY-WALLET.md`.

---

## Step 1 — Deploy V3 Contract in Remix

1. Open https://remix.ethereum.org (MetaMask on Lightchain mainnet, chain ID 9200)
2. Paste `OrcaVaultV3.sol` from `~/Desktop/orcavault-app/`
3. Compile Solidity 0.8.20
4. Deploy → Injected Provider → constructor `_relayWallet`: current relay address above
5. Copy deployed address → `V3_CONTRACT_ADDRESS` in `index.html` and Railway

---

## Step 2 — Deploy Relay Server to Railway

Project: **vivacious-empathy** → orcavault service

**Environment variables (no secrets in git):**

| Variable | Example / note |
|----------|----------------|
| `RELAY_PRIVATE_KEY` | `0x…` — paste in Railway UI only |
| `V3_CONTRACT_ADDRESS` | Deployed OrcaVault V3 address |
| `LIGHTTUBE_V2_ADDRESS` | `0xf8cdC4f6241D655bB4E30fF5f1433Fb9F358aDB5` |
| `LIGHTTUBE_V3_ADDRESS` | `0x2077AbeBe64461f0265937328C9e710C35E18Fd3` |
| `LIGHTTUNES_V1_ADDRESS` | `0x3587067a1E37A1c05095B3cc053564Db49a27F7D` |
| `LIGHTTUNES_FEE_WALLET` | MetaMask fee collector — not relay wallet |
| `OWNER_WALLET` | Owner wallet (free relay access) |
| `RELAY_FEE_LCAI` | `2.0` |

Mount Railway Volume at `/data` for persistent paid-wallet registry.

---

## Step 3 — Register relay on contracts

**Required after any relay wallet rotation.** Owner wallet (MetaMask) must call `setRelayWallet(newRelay)` on:

- LightTunes V1
- LightTube V2 + V3
- OrcaVault V3

Full steps: `SET-RELAY-WALLET.md`

---

## Incident note (2026-06-27)

Old relay key was exposed in a prior commit of this file on public GitHub (`Keiko-Dev-LCAI/orcavault`). Root cause: **plaintext key in git**, not Railway-only. Key scrubbed from history 2026-07-01. Old wallet abandoned; use new relay only.