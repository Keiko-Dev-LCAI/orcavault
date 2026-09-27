# Get LCAI — swap Ethereum for native LCAI

Pretty print: **[PDF](get-lcai-guide.pdf)** (same scrubbed content).

Live app: **[orcavault.win/get-lcai/](https://orcavault.win/get-lcai/)**

A public, **non-custodial** page: you connect a wallet that holds **ETH on Ethereum**, pick an amount, get a live Uniswap **V3** quote, then swap **ETH → LCAI ERC-20** and bridge to **native LCAI on Lightchain (domain 9200)** via the **official** Hyperlane warp. Native LCAI is delivered to **your own Lightchain wallet** (same address). No backend, no relay unlock, no API keys.

This is **not** OrcaVault “Pay with Ethereum” (that sends native LCAI to the **app relay** to unlock uploads). This page is **buy-and-hold** for you.

This is **not** the self-hosted Base USDC warp. See [Hyperlane USDC warp](hyperlane-usdc-warp-guide.md).

## What you do in the app

1. Open https://orcavault.win/get-lcai/
2. Connect MetaMask / Trust (must be able to use **Ethereum mainnet**).
3. Enter ETH (or tap a preset). The quote is live from Uniswap V3.
4. Approve swap, approve LCAI spend if asked, approve the bridge.
5. Wait: native LCAI is **not instant**. The official Lightchain Hyperlane relayer delivers after the Ethereum txs confirm. Switch the wallet to **Lightchain AI (9200)** to see the balance.

You sign every step. The page never holds funds.

## Seeing your balance

Native LCAI lands on **Lightchain**, not Ethereum, so it won't appear in your wallet's default (Ethereum) view. To see it:

1. Add the Lightchain network to your wallet (Add Custom Network):
   - **Network name:** Lightchain AI
   - **RPC URL:** `https://rpc.mainnet.lightchain.ai`
   - **Chain ID:** `9200`
   - **Currency symbol:** `LCAI`
   - **Block explorer:** `https://lightscan.app`
2. Switch the wallet to **Lightchain AI**. LCAI is the native coin, so it shows as your main balance — in some wallets you may also need to toggle LCAI on under **Manage crypto**.
3. Delivery isn't instant: the official Hyperlane relayer mints native LCAI a few minutes after your bridge tx confirms.

**Source of truth:** some wallet indexers (e.g. Trust Wallet) lag or don't fully cover smaller networks, so the balance may read `0` even after it arrives. Search your address at **[lightscan.app](https://lightscan.app)** — if it shows there, the LCAI is in your wallet regardless of what the wallet UI displays.

## Also on getlcai: ETH · Base → synthetic ETH (2026-09-27)

The **"ETH · Base"** option is a different rail from the ETH → LCAI swap above. It does **not** swap into LCAI — it moves your **native ETH on Base** across to **synthetic ETH on Lightchain**, delivered to the **same address**. Synthetic ETH is a payment token the Orca apps accept alongside LCAI/USDC; it carries real ETH value bridged from Base. There are **no pools and no fees** — you pay only the bridged amount plus interchain gas.

**What you do:**

1. In getlcai, pick **"ETH · Base"** as what you Have. Connect a wallet holding **ETH on Base** (chain 8453). If two wallet extensions are installed, disable the one you're not using — a second extension can hijack the connect request.
2. Enter an amount and send. One transaction locks your ETH on Base.
3. Switch your wallet to **Lightchain AI (9200)**. Synthetic ETH mints to your address a few minutes after the Base tx confirms (once the relayer delivers). Confirm at [lightscan.app](https://lightscan.app) if your wallet UI lags.

**This is a self-hosted warp** — delivery depends on the operator's validator + relayer being online, so mints are not instant. It is separate from Circle, from the official LCAI warp, and from the USDC warp — **rails never mix**. Builders who want to reproduce this route: see the [Hyperlane ETH warp guide](hyperlane-eth-warp-guide.md).

| Role | Address |
|------|---------|
| Base native collateral (HypNative) | `0x330a28CD6e7b71a040eeA0cEe6fa81540D1D32e9` |
| Lightchain synthetic ETH (HypERC20) | `0x620d3e60461651042812BDbCbB3733dE13967BD3` |
| Base domain | `8453` |
| Lightchain domain | `9200` |

## Why V3

LCAI’s Ethereum liquidity is a Uniswap **V3** LCAI/WETH pool (**0.3%**). There is **no V2 pool**. V2 quotes revert.

## Public contracts (Ethereum unless noted)

Same official stack as the Pay-with-Ethereum rail; only the **recipient** differs (you vs an app relay).

| Role | Address |
|------|---------|
| LCAI ERC-20 | `0x9cA8530CA349c966Fe9ef903Df17a75B8A778927` |
| WETH9 | `0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2` |
| Uniswap V3 SwapRouter | `0xE592427A0AEce92De3Edee1F18E0157C05861564` |
| Uniswap V3 Quoter | `0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6` |
| LCAI/WETH V3 pool (0.3%) | `0x0d047a370611437a1b8e6c2a95ea36f69fdda3be` |
| Warp (Ethereum collateral) | `0x01f80bb8e78e79881E8Ec7832fB6C2c59f64e353` |
| Warp (Lightchain native mint) | `0xEc7096A3116EE769457C939617375Ec1785AA6f1` |
| Lightchain domain | `9200` |

## Safety

- Official warp only. Do not send LCAI ERC-20 to a random router.
- Keep enough ETH for swap + Uniswap fee + `quoteGasPayment` on the bridge.
- Not a CEX, not instant, not Circle. Slippage applies.
- Source: `get-lcai/` in the OrcaVault repo (relative `./eth-pay.js`, no DNS change).

Related: [Pay with Ethereum (app unlock)](pay-with-ethereum-lcai-guide.md) · [bridge.lightchain.ai](https://bridge.lightchain.ai)
