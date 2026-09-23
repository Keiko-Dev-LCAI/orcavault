/*
 * eth-pay.js — "Pay with Ethereum" helper for OrcaVault (and reusable across Orca/Light apps).
 *
 * Flow (all signed by the USER in their own wallet — non-custodial, no secrets, same
 * spirit as keiko-pay.js):
 *
 *   1. Swap ETH -> LCAI-ERC20 on Uniswap (Ethereum).
 *   2. Bridge LCAI-ERC20 -> native LCAI via the official Lightchain Hyperlane warp route.
 *   3. Backend watches for native LCAI landing on Lightchain L1, then credits/unlocks
 *      the OrcaVault item. (The bridge is NOT instant — a relayer delivers on the L1
 *      side after a short delay, so step 3 is asynchronous. Design the UI to show a
 *      "bridging…" state and confirm when it lands.)
 *
 * Requires ethers v6 (BrowserProvider). Add via CDN in the page:
 *   <script src="https://cdnjs.cloudflare.com/ajax/libs/ethers/6.13.2/ethers.umd.min.js"></script>
 *
 * -----------------------------------------------------------------------------
 * ADDRESS VERIFICATION STATUS  (do NOT ship to mainnet until all are VERIFIED)
 * -----------------------------------------------------------------------------
 *   LCAI_ERC20        VERIFIED  — from official lightchain.ai ("Official $LCAI contract")
 *   WETH              VERIFIED  — canonical Ethereum mainnet WETH9
 *   UNISWAP_V2_ROUTER VERIFIED  — canonical Uniswap V2 Router02 (confirm LCAI has a V2
 *                                 pool; if liquidity is V3, switch to the V3 SwapRouter)
 *   LCAI_WARP_ROUTE   VERIFIED  — Ethereum-side Hyperlane collateral router (EvmHypCollateral)
 *                                 from lightchain-protocol/bridge-ui src/consts/warpRoutes.ts.
 *                                 Wraps the VERIFIED LCAI ERC-20 as collateral. This is the
 *                                 contract transferRemote() is called on.
 *   LIGHTCHAIN_DOMAIN VERIFIED  — Hyperlane destination domain id for Lightchain = 9200,
 *                                 confirmed in bridge-ui src/consts/chains.ts (domainId === chainId).
 *   (Native LCAI is minted on Lightchain by the EvmHypNative router
 *    0xEc7096A3116EE769457C939617375Ec1785AA6f1 — no direct call needed from this side.)
 * -----------------------------------------------------------------------------
 */
(function (global) {
  "use strict";

  var CFG = {
    // --- Ethereum side (chainId 1) ---
    ETH_CHAIN_ID_HEX: "0x1",
    LCAI_ERC20: "0x9cA8530CA349c966Fe9ef903Df17a75B8A778927", // VERIFIED
    WETH:        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", // VERIFIED
    UNISWAP_V2_ROUTER: "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D", // VERIFIED (V2 Router02)

    // --- The bridge (Hyperlane warp route, Ethereum -> Lightchain) ---
    LCAI_WARP_ROUTE: "0x01f80bb8e78e79881E8Ec7832fB6C2c59f64e353", // VERIFIED (EvmHypCollateral on Ethereum, wraps LCAI_ERC20)
    LIGHTCHAIN_DOMAIN: 9200, // VERIFIED (Hyperlane domainId for lcai, == chainId)

    // Slippage tolerance for the ETH->LCAI swap, in basis points (300 = 3%).
    SLIPPAGE_BPS: 300,
    // How long the swap tx stays valid, seconds.
    DEADLINE_SECS: 1200,
  };

  // Minimal ABIs (only the functions we call).
  var ABI_ROUTER = [
    "function getAmountsOut(uint amountIn, address[] path) view returns (uint[] amounts)",
    "function getAmountsIn(uint amountOut, address[] path) view returns (uint[] amounts)",
    "function swapExactETHForTokens(uint amountOutMin, address[] path, address to, uint deadline) payable returns (uint[] amounts)"
  ];
  var ABI_ERC20 = [
    "function balanceOf(address) view returns (uint256)",
    "function allowance(address owner, address spender) view returns (uint256)",
    "function approve(address spender, uint256 amount) returns (bool)"
  ];
  // Hyperlane warp route (TokenRouter). transferRemote is the canonical bridge call.
  var ABI_WARP = [
    "function quoteGasPayment(uint32 destinationDomain) view returns (uint256)",
    "function transferRemote(uint32 destination, bytes32 recipient, uint256 amount) payable returns (bytes32 messageId)"
  ];

  function addrToBytes32(addr) {
    // Hyperlane recipients are bytes32 (left-padded address).
    return "0x" + addr.toLowerCase().replace(/^0x/, "").padStart(64, "0");
  }

  async function getSigner() {
    var provider = global.ethereum;
    if (!provider) throw new Error("No wallet found. Install/enable MetaMask.");
    // Make sure we're on Ethereum mainnet for the swap + bridge legs.
    try {
      var cur = await provider.request({ method: "eth_chainId" });
      if (!cur || cur.toLowerCase() !== CFG.ETH_CHAIN_ID_HEX) {
        await provider.request({
          method: "wallet_switchEthereumChain",
          params: [{ chainId: CFG.ETH_CHAIN_ID_HEX }],
        });
      }
    } catch (e) { /* surface later if still wrong */ }
    var bp = new global.ethers.BrowserProvider(provider);
    await bp.send("eth_requestAccounts", []);
    return bp.getSigner();
  }

  // Quote: how much LCAI you'd get for `ethAmount` (whole ETH string, e.g. "0.01").
  async function quoteLcaiForEth(ethAmount) {
    var signer = await getSigner();
    var router = new global.ethers.Contract(CFG.UNISWAP_V2_ROUTER, ABI_ROUTER, signer);
    var amountIn = global.ethers.parseEther(String(ethAmount));
    var path = [CFG.WETH, CFG.LCAI_ERC20];
    var amounts = await router.getAmountsOut(amountIn, path);
    return amounts[amounts.length - 1]; // LCAI out (BigInt, 18 decimals)
  }

  // Reverse quote: how much ETH (whole-ETH string) to buy `lcaiAmount` LCAI (whole-token
  // string), padded by `bufferPct` (e.g. 0.15 = +15%) to absorb slippage + bridge gas.
  async function ethForLcai(lcaiAmount, bufferPct) {
    var signer = await getSigner();
    var router = new global.ethers.Contract(CFG.UNISWAP_V2_ROUTER, ABI_ROUTER, signer);
    var lcaiOut = global.ethers.parseEther(String(lcaiAmount)); // 18 decimals
    var path = [CFG.WETH, CFG.LCAI_ERC20];
    var amounts = await router.getAmountsIn(lcaiOut, path);
    var ethIn = amounts[0]; // WETH in (BigInt)
    var pad = BigInt(Math.round((Number(bufferPct) || 0) * 10000));
    var padded = ethIn + (ethIn * pad) / 10000n;
    return global.ethers.formatEther(padded); // whole-ETH string
  }

  // Step 1: swap ETH -> LCAI, delivered to the user's own address.
  async function swapEthForLcai(ethAmount, onStatus) {
    var signer = await getSigner();
    var me = await signer.getAddress();
    var router = new global.ethers.Contract(CFG.UNISWAP_V2_ROUTER, ABI_ROUTER, signer);
    var amountIn = global.ethers.parseEther(String(ethAmount));
    var path = [CFG.WETH, CFG.LCAI_ERC20];

    var quoted = await router.getAmountsOut(amountIn, path);
    var expected = quoted[quoted.length - 1];
    var minOut = expected - (expected * BigInt(CFG.SLIPPAGE_BPS)) / 10000n;
    var deadline = Math.floor(Date.now() / 1000) + CFG.DEADLINE_SECS;

    if (onStatus) onStatus("swap", "Swapping ETH → LCAI on Uniswap…");
    var tx = await router.swapExactETHForTokens(minOut, path, me, deadline, { value: amountIn });
    await tx.wait();
    return { txHash: tx.hash, expectedLcai: expected, minLcai: minOut };
  }

  // Step 2: bridge LCAI-ERC20 -> native LCAI on Lightchain, to `recipientL1`
  // (defaults to the same wallet address on the L1 side).
  async function bridgeLcaiToNative(lcaiAmount /* BigInt */, recipientL1, onStatus) {
    if (CFG.LCAI_WARP_ROUTE === "0x0000000000000000000000000000000000000000") {
      throw new Error("LCAI_WARP_ROUTE not set — pull the real warp-route address from the bridge first.");
    }
    var signer = await getSigner();
    var me = await signer.getAddress();
    var to = recipientL1 || me;

    var lcai = new global.ethers.Contract(CFG.LCAI_ERC20, ABI_ERC20, signer);
    var warp = new global.ethers.Contract(CFG.LCAI_WARP_ROUTE, ABI_WARP, signer);

    // Approve the warp route to pull the LCAI (only if needed).
    var allowance = await lcai.allowance(me, CFG.LCAI_WARP_ROUTE);
    if (allowance < lcaiAmount) {
      if (onStatus) onStatus("approve", "Approving LCAI for the bridge…");
      var ap = await lcai.approve(CFG.LCAI_WARP_ROUTE, lcaiAmount);
      await ap.wait();
    }

    // Hyperlane charges an interchain gas payment (paid in ETH via msg.value).
    var gasPay = 0n;
    try { gasPay = await warp.quoteGasPayment(CFG.LIGHTCHAIN_DOMAIN); } catch (e) { /* some routes are 0 */ }

    if (onStatus) onStatus("bridge", "Bridging LCAI → native LCAI…");
    var tx = await warp.transferRemote(
      CFG.LIGHTCHAIN_DOMAIN,
      addrToBytes32(to),
      lcaiAmount,
      { value: gasPay }
    );
    var rc = await tx.wait();
    return { txHash: tx.hash, receipt: rc };
  }

  /*
   * Orchestrator. Runs swap -> bridge, then hands the details to the app's backend so
   * it can watch the L1 for arrival and unlock the item.
   *
   *   payWithEth({
   *     ethAmount:  "0.01",                 // what the user deposits
   *     recipientL1:"0xUserL1WalletOrVault",// where native LCAI should land
   *     verifyUrl:  "/api/eth/verify",      // backend: record intent, watch for arrival
   *     meta:       { item: "upload-123" }
   *   }, onStatus)
   *
   * NOTE: the bridge is async. This resolves once the swap + bridge txs are submitted
   * and confirmed on Ethereum; the native LCAI lands on L1 a short while later. The
   * backend (verifyUrl) is responsible for confirming arrival and flipping the unlock.
   */
  async function payWithEth(opts, onStatus) {
    opts = opts || {};
    if (!opts.ethAmount) return { ok: false, error: "Missing ethAmount." };
    try {
      var swap = await swapEthForLcai(opts.ethAmount, onStatus);
      var bridge = await bridgeLcaiToNative(swap.minLcai, opts.recipientL1, onStatus);

      if (onStatus) onStatus("pending", "Bridging… native LCAI will land shortly.");

      if (opts.verifyUrl) {
        var signer = await getSigner();
        var from = await signer.getAddress();
        var body = {
          walletAddress: from,
          recipientL1: opts.recipientL1 || from,
          swapTxHash: swap.txHash,
          bridgeTxHash: bridge.txHash,
          lcaiAmount: swap.minLcai.toString(),
          meta: opts.meta || null,
        };
        var resp = await fetch(opts.verifyUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        var out = await resp.json().catch(function () { return {}; });
        if (!resp.ok || out.error) {
          return { ok: false, error: out.error || "Backend could not record the bridge.", swapTxHash: swap.txHash, bridgeTxHash: bridge.txHash };
        }
        return { ok: true, pending: true, swapTxHash: swap.txHash, bridgeTxHash: bridge.txHash, result: out };
      }
      return { ok: true, pending: true, swapTxHash: swap.txHash, bridgeTxHash: bridge.txHash };
    } catch (e) {
      return { ok: false, error: (e && e.message) || "Payment cancelled or failed." };
    }
  }

  global.EthPay = {
    pay: payWithEth,
    quote: quoteLcaiForEth,
    ethForLcai: ethForLcai,
    swapEthForLcai: swapEthForLcai,
    bridgeLcaiToNative: bridgeLcaiToNative,
    config: CFG,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = global.EthPay;
})(typeof window !== "undefined" ? window : this);
