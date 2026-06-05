# OrcaVault Relay — Morning Deploy Checklist

When you're ready, do these steps in order.

---

## RELAY WALLET (already generated — save this!)

Address:    0xC19dc1E0d9d63E6E204207c9eFb53ec1F302015f
Private key: stored only in Railway env var (see Step 3)

**Send 10 LCAI to that address for gas.** This covers thousands of uploads.

---

## Step 1 — Deploy V3 Contract in Remix

1. Open https://remix.ethereum.org in Chrome (VPN ON, MetaMask connected to Lightchain)
2. Create new file: OrcaVaultV3.sol
3. Paste the full code from ~/Desktop/orcavault-app/OrcaVaultV3.sol
4. Compile: Solidity Compiler → 0.8.20 → Compile
5. Deploy: Deploy tab → Environment: Injected Provider → Contract: OrcaVaultV3
6. Constructor arg (_relayWallet): 0xC19dc1E0d9d63E6E204207c9eFb53ec1F302015f
7. Click Deploy → confirm in MetaMask
8. Copy the deployed contract address → paste into index.html as V3_CONTRACT_ADDRESS

---

## Step 2 — Deploy Relay Server to Railway

1. Go to railway.app → New Project → Deploy from GitHub repo
   OR: New Project → Empty Project → upload the relay-server/ folder
2. Set environment variables:
   - RELAY_PRIVATE_KEY = 0x68cc9cea48b5d85e78c13aee0208ea792bb7271c3d2ea1686ef17cc5d8efb9e1
   - V3_CONTRACT_ADDRESS = (address from Step 1)
   - OWNER_WALLET = 0xA3a653 (your full Trust Wallet address — gets free relay forever)
   - RELAY_FEE_LCAI = 2.0
3. Add a Railway Volume mounted at /data (for storing paid wallets list persistently)
4. Railway will auto-detect Python and deploy
5. Copy the Railway URL (e.g. web-production-XXXXX.up.railway.app)

---

## Step 3 — Update index.html

Claude will update these constants once you have the addresses:
- V3_CONTRACT_ADDRESS = deployed address from Step 1
- RELAY_URL = Railway URL from Step 2

Then Claude adds the "Auto-Upload" button that signs once and sends the rest automatically.

---

## What you get after this is done

**You (owner wallet):**
- Tap "Auto Upload" → sign once → done. Free forever.

**Users who pay 2 LCAI:**
- Send 2 LCAI to relay wallet → click "Unlock One-Click" → verified on-chain → one-click forever

**Everyone else:**
- Normal upload with a few wallet taps (no relay, no cost to you)

Relay wallet auto-refills itself from user payments over time.
