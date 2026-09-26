# Build a Hyperlane USDC Warp Route (Base → your chain)

Pretty print: **[PDF](hyperlane-usdc-warp-guide.pdf)** (same scrubbed content).

A builder-facing guide to the payment rail ERC-8004 leaves out: a **self-hosted** Hyperlane warp route that locks real USDC on **Base** and mints synthetic USDC 1:1 on **your chain**. The worked example uses USDC → Lightchain, but any ERC-20 (or a native coin) and any Hyperlane-registered chain works.

> **Read this first.** This is a route *you operate* — your own validator signs, your own relayer delivers. It is **not** Circle's official bridge and **not** a public one-click UI. The default security is **threshold-1** (a single validator whose key can authorize mints), so it is a single point of trust. Keep the agents online, protect the key, and add validators before calling it trustless. Liquidity is only whatever you bridge — this is working plumbing, not a market on-ramp.

## 0. Prerequisites

- **Funds:** ops wallet with **ETH on Base** (deploy + gas) and **native gas on your destination chain** (deploy + relay).
- **Host:** an always-on machine with **Docker** (validator + relayer run 24/7).
- **Tooling:** Node.js + the Hyperlane CLI (`@hyperlane-xyz/cli`, pin a known version) and the agent image `ghcr.io/hyperlane-xyz/hyperlane-agent`.
- **Key:** a deployer/signer keyfile, permissions `0600`, **never committed to git**. Load it into the CLI via the `HYP_KEY` env var — never echo or paste the value.

## 1. Register your destination chain with Hyperlane

Add your chain to the Hyperlane registry: a name, its **chainId / domain**, an **RPC**, and a deployed **Mailbox** (+ ISM factory). If your chain has no Hyperlane core yet, run `hyperlane core deploy` first to get a Mailbox. Agent chain metadata lives in `configs/agent-config.json`.

*Example (Lightchain):* name `lightchainai` · domain `9200` · RPC `https://rpc.mainnet.lightchain.ai` · Mailbox `0x142a9CEf00ACcAddB76283c49A1Bf37f20c1F00e`.

## 2. Write the warp recipe (`warp-route-deployment.yaml`)

Base side = **collateral** wrapping Circle USDC. Your side = **synthetic** USDC that mints when a Base lock is delivered. Set `owner` to your own ops wallet (not a hardware/treasury wallet).

```yaml
# Base collateral → <yourchain> synthetic. Owner = your anon deployer.
base:
  type: collateral
  token: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # Circle USDC on Base
  owner: "0xYOUR_OPS_WALLET"
  mailbox: "0xeA87ae93Fa0019a82A727bfd3eBd1cFCa8f64f1D"  # Base mailbox
  decimals: 6
  name: "USD Coin"
  symbol: "USDC"
yourchain:                                              # e.g. lightchainai
  type: synthetic
  owner: "0xYOUR_OPS_WALLET"
  mailbox: "0xYOUR_CHAIN_MAILBOX"
  decimals: 6
  name: "USD Coin"
  symbol: "USDC"
```

**Using a different token?** The route is token-agnostic. On the **origin** side set `token` to that ERC-20's contract and match its real `decimals`/`symbol` (18 for most ERC-20s, 6 for USDC/USDT); the **synthetic** side mirrors them. To bridge a chain's **native coin** instead of an ERC-20, set the origin `type: native` (no `token` field). One warp route = one token, so deploy a separate route per coin.

## 3. Deploy the route

```bash
export PATH="$HOME/.npm-global/bin:$PATH"
# Load HYP_KEY from your local keyfile — do NOT print it
cd ~/.config/<your-deploy-dir>/warp-workdir
hyperlane warp deploy -y -w USDC/base-yourchain --verbosity info
unset HYP_KEY
```

**Output (yours will differ):** a `HypERC20Collateral` on Base (locks USDC) and a synthetic `HypERC20` on your chain (mints USDC). The CLI enrolls the two routers to each other and writes artifacts to `~/.hyperlane/`.

> If the Base RPC flakes during deploy, edit `~/.hyperlane/chains/base/metadata.yaml` to list several RPCs (`mainnet.base.org`, `base.llamarpc.com`, `1rpc.io/base`, …) and re-run.

## 4. Choose your ISM (security module)

Point the synthetic token at an ISM that decides which messages are valid. The simplest bootstrap is a **TrustedRelayerIsm** (you relay). For self-verifying delivery, move to a **MessageIdMultisigIsm** with your validator announced on Base:

```bash
hyperlane ism deploy --chain yourchain -i ism-config.yaml -y --verbosity info
```

A live example runs **MessageIdMultisigIsm, threshold 1**, validator set = a single Base-announced validator. **Threshold 1 is a single trust point** — add validators to harden it.

## 5. Run validator + relayer (Docker, always-on)

```bash
cd ~/.config/<your-deploy-dir>
CONFIG_FILES="$(pwd)/configs/agent-config.json"
SIGDIR="$(pwd)/tmp/validator-signatures"
# KEY = load your signer into a shell var from the keyfile; never echo it

docker run -d --name hyp-validator-base --restart unless-stopped \
  --mount type=bind,source="$CONFIG_FILES",target=/config/agent-config.json,readonly \
  --mount type=bind,source="$(pwd)/db_validator",target=/hyperlane_db \
  --mount type=bind,source="$SIGDIR",target=/tmp/validator-signatures \
  -e CONFIG_FILES=/config/agent-config.json \
  ghcr.io/hyperlane-xyz/hyperlane-agent ./validator \
  --db /hyperlane_db --originChainName base \
  --checkpointSyncer.type localStorage \
  --checkpointSyncer.path /tmp/validator-signatures \
  --validator.key "$KEY"

docker run -d --name hyp-relayer --restart unless-stopped \
  --mount type=bind,source="$CONFIG_FILES",target=/config/agent-config.json,readonly \
  --mount type=bind,source="$(pwd)/db_relayer",target=/hyperlane_db \
  --mount type=bind,source="$SIGDIR",target=/tmp/validator-signatures,readonly \
  -e CONFIG_FILES=/config/agent-config.json \
  ghcr.io/hyperlane-xyz/hyperlane-agent ./relayer \
  --db /hyperlane_db --relayChains base,yourchain \
  --allowLocalCheckpointSyncers true --defaultSigner.key "$KEY"
```

> **Key hygiene:** passing `--validator.key` on the command line leaves the hex visible via `docker inspect`. Prefer env/file injection with tight permissions. Keep the relayer wallet funded on your chain for gas.

## 6. Test send — $1 end-to-end before you trust it

```bash
export PATH="$HOME/.npm-global/bin:$PATH"
# HYP_KEY loaded from keyfile — do not print
cd ~/.config/<your-deploy-dir>/warp-workdir
hyperlane warp send -y -w USDC/base-yourchain \
  --origin base --destination yourchain \
  --amount 1000000 --relay --verbosity info   # 1000000 = 1.0 USDC (6 decimals)
unset HYP_KEY
```

A healthy 1 USDC send should show as processed on [explorer.hyperlane.xyz](https://explorer.hyperlane.xyz) once your relayer (or `--relay`) delivers it — often a couple of minutes. Anyone bridging afterward needs Base USDC + Base gas **and** your delivery online.

## 7. Consume the bridged USDC in your app (optional)

A minimal payment flow: the user holds synthetic USDC on your chain → sends the required amount to your receiver wallet → your backend verifies by watching the token's ERC-20 `Transfer` event into that receiver (**not** a native-coin balance check). This is the pattern OrcaVault uses to accept "2 native **or** 1 bridged USDC" for ERC-8004 agent registration.

## Reference addresses

| Role | Address | Reuse? |
|------|---------|--------|
| Circle USDC (Base) | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | Universal — use as-is |
| Mailbox (Base) | `0xeA87ae93Fa0019a82A727bfd3eBd1cFCa8f64f1D` | Universal — use as-is |
| ValidatorAnnounce (Base) | `0x182E8d7c5F1B06201b102123FC7dF0EaeB445a7B` | Universal — use as-is |
| Mailbox (Lightchain example) | `0x142a9CEf00ACcAddB76283c49A1Bf37f20c1F00e` | Swap for your chain's mailbox |
| Collateral (Base) | *generated by your deploy* | Yours will differ |
| Synthetic USDC | *generated by your deploy* | Yours will differ |
| ISM / validator | *your own values* | Yours will differ |
| Owner / signer | *your ops wallet* | Yours — never a hardware/treasury key |

## Security checklist

1. Threshold-1 ISM means the validator key can authorize mints — protect it and add validators before calling it trustless.
2. Inject keys via env/files with tight permissions; avoid leaving hex in the Docker `Cmd` (visible via `docker inspect`).
3. Never commit the deployer keyfile, agent keys, or API keys. Keep the deploy directory out of git.
4. Hyperlane CLI flags shift between versions — treat command *shapes* as guidance and confirm against current CLI docs before running.
5. Don't market liquidity: synthetic USDC on a new chain is working plumbing, not a liquid on-ramp.

---

*Reference: Hyperlane docs (warp routes, ISMs, agents) at [docs.hyperlane.xyz](https://docs.hyperlane.xyz). Circle USDC and the Hyperlane mailbox addresses are public infrastructure; every wallet, ISM, collateral, and synthetic address in your own route is generated at deploy time and will differ from any example here.*
