# 🎞️ Audio & Visual Archives — Deployment Guide
## For Keiko — Step-by-Step (No Technical Experience Needed!)

---

## PART 1: Deploy the Smart Contract (Do This First!)
**You'll use: deploy.lightchain.ai + MetaMask or Trust Wallet**

### Step 1 — Open the Remix-like deployer
1. Go to **https://deploy.lightchain.ai** in your browser
2. Connect your **MetaMask** wallet (the one ending in `...0A9bA`)
3. Make sure it's on **Lightchain AI Mainnet** (Chain ID: 9200)

### Step 2 — Paste the contract
1. In the code editor, **delete everything** that's already there
2. Open the file `index.html` in a text editor (Notepad or TextEdit)
3. At the very top of the file, after the `<!--` comment, you'll see the full Solidity contract code
4. Copy everything from `// SPDX-License-Identifier: MIT` down to the closing `}` of the contract
5. Paste it into the Remix/deploy editor

### Step 3 — Compile & Deploy
1. Click **"Compile"** — wait for the green checkmark ✅
2. Click **"Deploy"**
3. Make sure:
   - **Network:** Lightchain AI Mainnet
   - **Contract:** AudioVisualArchives
   - **Constructor arguments:** none (leave blank)
4. Click **Confirm** in MetaMask
5. Wait ~10 seconds for the transaction to confirm

### Step 4 — Copy the Contract Address
1. After deployment, you'll see a **contract address** — it looks like: `0x1234...abcd`
2. **COPY this address and save it somewhere safe** (like Notepad)
3. This is the permanent address of your archive contract on Lightchain — it never changes!

---

## PART 2: Update the App with Your Contract Address

### Step 5 — Update index.html
1. Open `index.html` in any text editor (Notepad on Windows, TextEdit on Mac)
2. Press **Ctrl+F** (or Cmd+F on Mac) to open Find
3. Search for: `REPLACE_WITH_CONTRACT_ADDRESS`
4. Replace it with your actual contract address from Step 4
   - Example: change `REPLACE_WITH_CONTRACT_ADDRESS` to `0x1234567890abcdef1234567890abcdef12345678`
5. **Save the file**

---

## PART 3: Push to GitHub Pages
**You'll need your GitHub Personal Access Token (PAT)**

### Step 6 — Create the GitHub Repository
1. Go to **https://github.com** and sign in as **Keiko-Dev-LCAI**
2. Click the **+** button in the top right → **"New repository"**
3. Fill in:
   - **Repository name:** `orcavault`
   - **Description:** Audio & Visual Archives — Personal memory archive on Lightchain
   - **Visibility:** ✅ Public
   - **Initialize with README:** ❌ Leave UNCHECKED
4. Click **"Create repository"**

### Step 7 — Get Your Personal Access Token (PAT)
If you already have one, skip to Step 8. If not:
1. Click your profile photo → **Settings**
2. Scroll to the bottom → **Developer settings**
3. Click **Personal access tokens** → **Tokens (classic)**
4. Click **Generate new token (classic)**
5. Give it a name like "orcavault deploy"
6. Check the box: **repo** (full control)
7. Click **Generate token**
8. **COPY the token immediately** — it starts with `ghp_` — you can't see it again!

### Step 8 — Open Terminal and Push
On your computer, open **Terminal** (Mac) or **Command Prompt** (Windows):

```bash
cd ~/Desktop/orcavault-app
git push https://YOUR_TOKEN@github.com/Keiko-Dev-LCAI/orcavault.git main
```

Replace `YOUR_TOKEN` with your actual PAT token (the one starting with `ghp_`).

**Example:**
```bash
git push https://ghp_abc123xyz@github.com/Keiko-Dev-LCAI/orcavault.git main
```

You should see: `Branch 'main' set up to track remote branch 'main'` — that means it worked! ✅

### Step 9 — Enable GitHub Pages
1. Go to your new repo at **https://github.com/Keiko-Dev-LCAI/orcavault**
2. Click **Settings** (top tab)
3. Scroll down to **Pages** (in the left sidebar)
4. Under **Source**, select:
   - Branch: **main**
   - Folder: **/ (root)**
5. Click **Save**
6. Wait about 60 seconds, then go to:
   👉 **https://keiko-dev-lcai.github.io/orcavault/**

Your app is live! 🎉

---

## PART 4: Every Time You Update the App

If you ever need to update the app (e.g., Claude makes improvements):

```bash
cd ~/Desktop/orcavault-app
git add index.html
git commit -m "Update app"
git push https://YOUR_TOKEN@github.com/Keiko-Dev-LCAI/orcavault.git main
```

GitHub Pages updates automatically within about 60 seconds.

---

## Quick Reference

| Item | Value |
|------|-------|
| App URL | https://keiko-dev-lcai.github.io/orcavault/ |
| GitHub Repo | https://github.com/Keiko-Dev-LCAI/orcavault |
| Lightchain RPC | https://rpc.mainnet.lightchain.ai |
| Chain ID | 9200 |
| Network Name | Lightchain AI Mainnet |
| Contract Deployer to use | MetaMask (0x6518...0A9bA) |
| Contract Address | (fill in after deployment) |

---

## ❓ Troubleshooting

**"Transaction failed" when creating archive or adding memory:**
- Make sure you have LCAI in your wallet for gas
- Gas costs are tiny (< $0.001) but you need SOME LCAI

**"Could not load archive" error:**
- You probably haven't updated `REPLACE_WITH_CONTRACT_ADDRESS` yet
- Follow Part 2, Step 5 above

**"Please install Trust Wallet or MetaMask":**
- Install Trust Wallet browser extension, or use MetaMask
- Make sure you're on the right network (Lightchain, Chain ID 9200)

**Page shows blank / not loading:**
- Wait 1-2 minutes after pushing — GitHub Pages takes time
- Try a hard refresh: Ctrl+Shift+R (Windows) or Cmd+Shift+R (Mac)

---
*Built with ❤️ for Keiko · Audio & Visual Archives · Lightchain AI Mainnet*
