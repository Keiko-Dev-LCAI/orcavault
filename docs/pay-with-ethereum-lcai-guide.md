# Pay with Ethereum for native LCAI on Lightchain


A builder-facing guide to the **official** Lightchain payment rail: a user holds **ETH on Ethereum**, swaps it to **LCAI ERC-20**, then bridges to **native LCAI on Lightchain (chain 9200)** via the protocol Hyperlane warp. OrcaVault uses this so someone can unlock with **Ξ Pay with Ethereum** instead of already holding native LCAI.

This is **not** the self-hosted Base→Lightchain USDC warp. That is a separate guide: [Hyperlane USDC warp](hyperlane-usdc-warp-guide.md).

## What this is and is not

- **Is:** User-signed, non-custodial. Swap on Uniswap **V3**, bridge on the **official** Lightchain Hyperlane route (`bridge.lightchain.ai` uses the same warp).
- **Is not:** A hosted custodian, a testnet faucet, or Circle. Native LCAI is **not instant** — the official Hyperlane relayer delivers after the Ethereum txs confirm.
- **LCAI has no Uniswap V2 pool.** Quotes/swaps **must** use V3 (0.3% LCAI/WETH). A V2 `getAmountsIn` reverts with empty data.

## Flow (three Ethereum txs, then wait)

1. **Quote** — Uniswap V3 Quoter `quoteExactOutputSingle` (how much ETH for N LCAI) plus a buffer for slippage and bridge gas.
2. **Swap** — `exactInputSingle` on the V3 SwapRouter: **ETH → WETH → LCAI ERC-20** (fee tier **3000** = 0.3%).
3. **Approve** the warp router to spend that LCAI (skip if allowance is already enough).
4. **Bridge** — `transferRemote(destinationDomain=9200, recipient, amount)` on the Ethereum **EvmHypCollateral** warp, paying `quoteGasPayment` in ETH.
5. **Credit** — App backend verifies the **Ethereum-side** `transferRemote` (correct warp, dest 9200, recipient, amount). Native LCAI is minted on Lightchain by the official **EvmHypNative** router. Do not wait on L1 in the button click.

Recipient on Lightchain is typically **your app’s relay/payment wallet** (bytes32 left-padded address), not the user’s L1 address — so they do not send a second native payment.

## Public contracts (Ethereum mainnet unless noted)

| Role | Address | Notes |
|------|---------|--------|
| LCAI ERC-20 | `0x9cA8530CA349c966Fe9ef903Df17a75B8A778927` | Official $LCAI on Ethereum |
| WETH9 | `0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2` | Canonical |
| Uniswap V3 SwapRouter | `0xE592427A0AEce92De3Edee1F18E0157C05861564` | Periphery |
| Uniswap V3 Quoter | `0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6` | Use `.staticCall` in ethers v6 |
| LCAI/WETH V3 pool | `0x0d047a370611437a1b8e6c2a95ea36f69fdda3be` | **0.3%** fee tier |
| Warp (Ethereum collateral) | `0x01f80bb8e78e79881E8Ec7832fB6C2c59f64e353` | `transferRemote` target |
| Warp (Lightchain native) | `0xEc7096A3116EE769457C939617375Ec1785AA6f1` | Mints native LCAI — no direct call from ETH |
| Lightchain domain | `9200` | Hyperlane destination = chain id |

OrcaVault example receiver (already published on the ERC-8004 page): payment / relay wallet on Lightchain — native LCAI from this warp is aimed there so one Ethereum session unlocks the app.

## Implementer notes

- Wallet must be on **Ethereum (chainId 1)** for swap + bridge. Switch back to Lightchain **9200** for app use.
- Bridge **the LCAI balance actually received** after the swap (before/after `balanceOf`), not the quote.
- Slippage example: **300 bps** (3%). Deadline example: **1200 s**.
- Verify `tx.to` is the warp, selector `transferRemote(uint32,bytes32,uint256)`, destination **9200**, recipient matches your relay, amount ≥ fee, plus a **Transfer** of that LCAI ERC-20 from the user **into the warp**.
- Replay-protect Ethereum tx hashes. Kill-switch the rail if the official relayer or Uniswap pool is unhealthy.
- Never commit keys. Users sign in their own wallet.

## Security checklist

1. Official warp only — do not point `transferRemote` at a random Hyperlane router.
2. V3 0.3% pool only — V2 will revert.
3. Credit only after the **bridge** tx is confirmed on Ethereum, not the swap alone.
4. Native LCAI delivery is the **protocol relayer**, not a machine you operate for this rail (unlike the self-hosted USDC warp).
5. Do not market this as instant or as a liquid CEX on-ramp. It is swap + official bridge.

Reference: [bridge.lightchain.ai](https://bridge.lightchain.ai), Uniswap V3 docs, Hyperlane TokenRouter `transferRemote`. Related: [Get LCAI (native LCAI to your wallet)](get-lcai-guide.md) · [USDC self-hosted warp](hyperlane-usdc-warp-guide.md).
