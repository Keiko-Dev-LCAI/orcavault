# OrcaVault V2 — Deployment Guide

Deploying the V2 contract adds **unlimited file size** support to your vault via chunked blockchain uploads. V1 memories keep working throughout — this is purely additive.

---

## Before you start

- You'll need LCAI in your wallet to pay gas
- Have your wallet (e.g. Trust Wallet or MetaMask) connected to **Lightchain AI Mainnet** (Chain ID 9200)
- The contract file is already written: `~/Desktop/orcavault-app/OrcaVaultV2.sol`

---

## Step 1 — Open the Lightchain deployer

Go to **https://deploy.lightchain.ai** in your browser and connect your wallet.

---

## Step 2 — Paste the contract

1. Click **New Contract** (or the paste/upload area)
2. Open `OrcaVaultV2.sol` and copy its entire contents
3. Paste it into the editor

---

## Step 3 — Compile

1. Select compiler version **0.8.20** (or higher — it's `^0.8.20`)
2. Click **Compile**
3. You should see a green success message and the contract name `OrcaVaultV2` appear

---

## Step 4 — Deploy

1. Make sure your wallet is on **Lightchain AI Mainnet** (Chain ID 9200)
2. No constructor arguments needed — just click **Deploy**
3. Approve the transaction in your wallet
4. Wait for confirmation (usually 5–15 seconds)
5. **Copy the new contract address** that appears — it looks like `0x...` (42 characters)

---

## Step 5 — Activate V2 in the frontend

1. Open `~/Desktop/orcavault-app/index.html` in a text editor
2. Find this line (near the top of the `<script>` section):

   ```javascript
   const V2_CONTRACT_ADDRESS = null; // replace null with '0xYourV2Address' after deploy
   ```

3. Replace `null` with your new address in quotes:

   ```javascript
   const V2_CONTRACT_ADDRESS = '0xYourNewAddressHere';
   ```

4. Save the file

---

## Step 6 — Test locally

Open `index.html` in Chrome (or via a local server). Try:

- Viewing an existing V1 vault — memories should still load ✅
- Uploading a file larger than 90KB — you should see the chunk confirm modal ✅
- Uploading a small file — still uses single transaction (fast path) ✅

---

## Step 7 — Push to GitHub

```bash
cd ~/Desktop/orcavault-app
git add index.html OrcaVaultV2.sol DEPLOY-V2.md
git commit -m "feat: OrcaVault V2 — chunked uploads, V2_CONTRACT_ADDRESS set"
git push
```

GitHub Pages will rebuild in ~30 seconds. The live site is then updated.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Vault not found" on V2 addMemory | V2 vaults are separate — create a new vault on V2, or use the chunked path (initMemory) instead |
| Chunk upload stalls mid-way | Approve each wallet prompt — one per chunk. If a chunk fails, the memory will be partially stored. Re-upload is needed. |
| V2 memories not showing | Check that V2_CONTRACT_ADDRESS is set correctly (no typos, includes `0x`) |
| "Memory not found" on chunked playback | All chunks must be confirmed on-chain. If upload was interrupted, chunks are missing. |

---

## How chunked uploads work

1. `initMemory(title, caption, mediaType, totalChunks, template)` — registers the memory, returns `memoryId`
2. `addChunk(memoryId, 0, chunk0Data)` through `addChunk(memoryId, N-1, chunkNData)` — each chunk is a 90KB slice of the base64 data URI
3. On playback: the app queries all `ChunkStored(memoryId)` events, sorts by `chunkIndex`, and concatenates `chunkData` to reconstruct the original file

Each chunk = one Lightchain transaction ≈ $0.000015 gas. A 5MB video = ~55 chunks ≈ $0.001 total.

---

## Contract addresses

| Contract | Address | Notes |
|----------|---------|-------|
| OrcaVault V1 | `0x2e7507aB9aF8bd706B1B28B5a7316ce5F17d3D4e` | Live, read-only for display |
| OrcaVault V2 | *(deploy to get address)* | Needed for chunked uploads |

---

*Built on Lightchain AI Mainnet — Chain ID 9200 — RPC: https://rpc.mainnet.lightchain.ai*
