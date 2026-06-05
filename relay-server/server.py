"""
OrcaVault Relay Server — v3
One-click uploads for authorized wallets. Self-funding, self-sustaining.

Access tiers:
  - Owner wallet (OWNER_WALLET env var) : always free, no payment needed, forever
  - Paid wallets                         : pay once → unlimited relay forever
  - Everyone else                        : must pay before relay access is granted

Payment flow:
  1. User sends LCAI to the relay wallet address (amount = current_fee)
  2. User calls POST /api/register-payment with their address + tx hash
  3. Server verifies tx on-chain (to=relay wallet, value>=fee, from=user)
  4. Address added to paid list → relay access unlocked forever

Self-sustaining fee model:
  - Base fee: RELAY_FEE_LCAI (default 2.0 LCAI)
  - When relay balance drops below LOW_BALANCE_THRESHOLD, fee scales up automatically
  - Existing paid users are NEVER affected — only new signups pay the higher rate
  - Fee tiers (based on relay balance vs threshold):
      balance >= threshold      → base fee (2 LCAI)
      balance < threshold       → base fee × 2 (4 LCAI)
      balance < threshold / 2   → base fee × 5 (10 LCAI)
      balance < 1 LCAI          → new registrations paused (existing users still work)

Environment variables required:
  RELAY_PRIVATE_KEY        = private key of the relay wallet (funded with LCAI)
  V3_CONTRACT_ADDRESS      = deployed OrcaVaultV3 contract address
  OWNER_WALLET             = owner's wallet address (always free, no payment check)

Optional:
  RELAY_FEE_LCAI           = base fee in LCAI (default: 2.0)
  LOW_BALANCE_THRESHOLD    = LCAI balance that triggers fee scaling (default: 10.0)
  PAID_WALLETS_FILE        = path to persistent JSON file (default: /data/paid_wallets.json)
"""

import os, json, time
from flask import Flask, request, jsonify
from flask_cors import CORS
from web3 import Web3
from eth_account import Account

app = Flask(__name__)
CORS(app)

# ─── Config ───────────────────────────────────────────────────────────────────

RPC_URL                  = "https://rpc.mainnet.lightchain.ai"
RELAY_PRIVATE_KEY        = os.environ.get("RELAY_PRIVATE_KEY", "")
V3_CONTRACT_ADDRESS      = os.environ.get("V3_CONTRACT_ADDRESS", "")
OWNER_WALLETS            = {w.strip().lower() for w in os.environ.get("OWNER_WALLET", "").split(",") if w.strip()}
BASE_FEE_LCAI            = float(os.environ.get("RELAY_FEE_LCAI", "2.0"))
LOW_BALANCE_THRESHOLD    = float(os.environ.get("LOW_BALANCE_THRESHOLD", "10.0"))
PAID_WALLETS_FILE        = os.environ.get("PAID_WALLETS_FILE", "/data/paid_wallets.json")
CHUNK_SIZE               = 2 * 1024 * 1024   # 2MB chunks (matches client)
CHAIN_ID                 = 9200

def current_fee_lcai(relay_balance: float) -> float:
    """
    Dynamic fee based on relay wallet balance.
    As balance drops, fee for NEW registrations climbs automatically.
    Existing paid users are never affected.
    """
    if relay_balance < 1.0:
        return None  # Paused — balance critically low, no new signups
    elif relay_balance < LOW_BALANCE_THRESHOLD / 2:
        return BASE_FEE_LCAI * 5   # e.g. 10 LCAI
    elif relay_balance < LOW_BALANCE_THRESHOLD:
        return BASE_FEE_LCAI * 2   # e.g. 4 LCAI
    else:
        return BASE_FEE_LCAI        # e.g. 2 LCAI (normal)

V3_ABI = [
    {
        "name": "initMemoryRelay",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "owner",       "type": "address"},
            {"name": "title",       "type": "string"},
            {"name": "description", "type": "string"},
            {"name": "mediaType",   "type": "string"},
            {"name": "totalChunks", "type": "uint256"},
            {"name": "template",    "type": "string"}
        ],
        "outputs": [{"name": "memoryId", "type": "uint256"}]
    },
    {
        "name": "addChunkRelay",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "memoryId",   "type": "uint256"},
            {"name": "chunkIndex", "type": "uint256"},
            {"name": "chunkData",  "type": "string"}
        ],
        "outputs": []
    }
]

# ─── Web3 setup ───────────────────────────────────────────────────────────────

w3 = Web3(Web3.HTTPProvider(RPC_URL))

def get_relay_account():
    return Account.from_key(RELAY_PRIVATE_KEY)

def get_contract():
    return w3.eth.contract(
        address=Web3.to_checksum_address(V3_CONTRACT_ADDRESS),
        abi=V3_ABI
    )

def send_tx(fn, relay_acct):
    """Sign and send a contract transaction, return receipt."""
    nonce = w3.eth.get_transaction_count(relay_acct.address)
    gas_price = w3.eth.gas_price
    tx = fn.build_transaction({
        'from':     relay_acct.address,
        'nonce':    nonce,
        'gasPrice': gas_price,
        'chainId':  CHAIN_ID,
    })
    tx['gas'] = int(w3.eth.estimate_gas(tx) * 1.2)
    signed = relay_acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    return receipt, tx_hash.hex()

# ─── Paid wallet registry ─────────────────────────────────────────────────────

def load_paid_wallets():
    """Load paid wallet set from disk. Returns a set of lowercase addresses."""
    try:
        os.makedirs(os.path.dirname(PAID_WALLETS_FILE), exist_ok=True)
        with open(PAID_WALLETS_FILE, 'r') as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_paid_wallets(wallets):
    """Persist paid wallet set to disk."""
    try:
        os.makedirs(os.path.dirname(PAID_WALLETS_FILE), exist_ok=True)
        with open(PAID_WALLETS_FILE, 'w') as f:
            json.dump(list(wallets), f)
    except Exception as e:
        print(f"Warning: could not save paid_wallets: {e}")

def has_relay_access(wallet_address: str) -> bool:
    """True if wallet is owner (free) or has paid."""
    addr = wallet_address.lower()
    if addr in OWNER_WALLETS:
        return True
    paid = load_paid_wallets()
    return addr in paid

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    relay        = get_relay_account()
    balance      = w3.eth.get_balance(relay.address)
    balance_lcai = float(w3.from_wei(balance, 'ether'))
    return jsonify({
        'status':         'ok',
        'relay_address':  relay.address,
        'relay_balance':  str(balance_lcai) + ' LCAI',
        'relay_fee_lcai': current_fee_lcai(balance_lcai),
        'v3_contract':    V3_CONTRACT_ADDRESS,
        'chain_id':       CHAIN_ID,
    })

@app.route('/api/check-access', methods=['GET'])
def check_access():
    """
    Check if a wallet has relay access.
    Query param: ?wallet=0x...
    Returns: { access: true/false, tier: "owner"|"paid"|"none", fee_lcai: N, relay_wallet: "0x...", paused: bool }
    """
    wallet = request.args.get('wallet', '').strip()
    if not wallet:
        return jsonify({'error': 'wallet param required'}), 400

    try:
        wallet = Web3.to_checksum_address(wallet)
    except Exception:
        return jsonify({'error': 'Invalid wallet address'}), 400

    relay        = get_relay_account()
    balance_lcai = float(w3.from_wei(w3.eth.get_balance(relay.address), 'ether'))
    fee          = current_fee_lcai(balance_lcai)
    addr         = wallet.lower()

    if addr in OWNER_WALLETS:
        tier   = 'owner'
        access = True
    elif addr in load_paid_wallets():
        tier   = 'paid'
        access = True
    else:
        tier   = 'none'
        access = False

    return jsonify({
        'access':         access,
        'tier':           tier,
        'fee_lcai':       fee,          # None = new signups paused
        'paused':         fee is None,
        'relay_wallet':   relay.address,
        'relay_balance':  balance_lcai,
    })

@app.route('/api/register-payment', methods=['POST'])
def register_payment():
    """
    Register a payment transaction to unlock relay access.
    Body: { walletAddress: "0x...", txHash: "0x..." }
    Verifies on-chain: tx.from == walletAddress, tx.to == relay wallet, tx.value >= fee
    """
    body = request.get_json()
    if not body:
        return jsonify({'error': 'No JSON body'}), 400

    wallet_address = body.get('walletAddress', '').strip()
    tx_hash        = body.get('txHash', '').strip()

    if not wallet_address or not tx_hash:
        return jsonify({'error': 'walletAddress and txHash required'}), 400

    try:
        wallet_address = Web3.to_checksum_address(wallet_address)
    except Exception:
        return jsonify({'error': 'Invalid wallet address'}), 400

    relay = get_relay_account()

    # Already has access?
    if has_relay_access(wallet_address):
        return jsonify({'success': True, 'message': 'Already has relay access', 'tier': 'owner' if wallet_address.lower() in OWNER_WALLETS else 'paid'})

    # Look up the transaction on-chain
    try:
        tx = w3.eth.get_transaction(tx_hash)
    except Exception:
        return jsonify({'error': 'Transaction not found on chain — wait a moment and try again'}), 404

    # Verify: sent from the right wallet
    if tx['from'].lower() != wallet_address.lower():
        return jsonify({'error': 'Transaction was not sent from your wallet address'}), 400

    # Verify: sent to the relay wallet
    if tx.get('to', '').lower() != relay.address.lower():
        return jsonify({'error': f"Transaction was not sent to the relay wallet ({relay.address})"}), 400

    # Verify: amount >= fee (dynamic — based on relay balance right now)
    relay_balance = float(w3.from_wei(w3.eth.get_balance(relay.address), 'ether'))
    fee = current_fee_lcai(relay_balance)
    if fee is None:
        return jsonify({'error': 'New registrations temporarily paused — relay wallet is being refilled. Try again soon.'}), 503
    fee_wei = Web3.to_wei(fee, 'ether')
    if tx['value'] < fee_wei:
        paid_lcai = float(w3.from_wei(tx['value'], 'ether'))
        return jsonify({'error': f"Payment too small: sent {paid_lcai:.4f} LCAI, required {fee} LCAI"}), 400

    # All good — register the wallet
    paid = load_paid_wallets()
    paid.add(wallet_address.lower())
    save_paid_wallets(paid)

    return jsonify({
        'success': True,
        'message': f"Relay access unlocked for {wallet_address}",
        'tier':    'paid',
    })

@app.route('/api/relay-upload', methods=['POST'])
def relay_upload():
    """
    One-click upload: server submits all chunk transactions on behalf of the user.
    Requires relay access (owner wallet or paid 2 LCAI).

    Body (JSON):
      ownerAddress  : string   — user's wallet address
      title         : string
      caption       : string
      memType       : string   — "photo" | "video" | "audio" | "document"
      template      : string
      dataURI       : string   — full base64 data URI of the file

    Returns:
      { success: true, memoryId: N, totalChunks: N, txHashes: [...] }
    """
    if not RELAY_PRIVATE_KEY or not V3_CONTRACT_ADDRESS:
        return jsonify({'error': 'Relay not configured'}), 500

    body = request.get_json()
    if not body:
        return jsonify({'error': 'No JSON body'}), 400

    owner_address = body.get('ownerAddress', '').strip()
    title         = body.get('title', 'Untitled').strip()
    caption       = body.get('caption', '').strip()
    mem_type      = body.get('memType', 'video').strip()
    template      = body.get('template', '').strip()
    data_uri      = body.get('dataURI', '')

    if not owner_address or not data_uri:
        return jsonify({'error': 'ownerAddress and dataURI are required'}), 400

    try:
        owner_address = Web3.to_checksum_address(owner_address)
    except Exception:
        return jsonify({'error': 'Invalid owner address'}), 400

    # ── Access check ──────────────────────────────────────────────────────────
    if not has_relay_access(owner_address):
        relay        = get_relay_account()
        balance_lcai = float(w3.from_wei(w3.eth.get_balance(relay.address), 'ether'))
        fee          = current_fee_lcai(balance_lcai)
        return jsonify({
            'error':        'No relay access',
            'message':      f"Send {fee} LCAI to {relay.address} to unlock one-click uploads, then call /api/register-payment",
            'relay_wallet': relay.address,
            'fee_lcai':     fee,
            'paused':       fee is None,
        }), 403

    # ── Upload ────────────────────────────────────────────────────────────────
    chunks       = [data_uri[i:i+CHUNK_SIZE] for i in range(0, len(data_uri), CHUNK_SIZE)]
    total_chunks = len(chunks)
    relay_acct   = get_relay_account()
    contract     = get_contract()
    tx_hashes    = []

    try:
        # 1. initMemoryRelay — get memoryId from receipt logs
        receipt, tx_hash = send_tx(
            contract.functions.initMemoryRelay(
                owner_address, title, caption, mem_type, total_chunks, template
            ),
            relay_acct
        )
        tx_hashes.append(tx_hash)

        # Parse memoryId from MemoryCreated event
        memory_id = None
        MEMORY_CREATED_TOPIC = w3.keccak(
            text="MemoryCreated(uint256,address,string,string,string,uint256,string,uint256)"
        ).hex()
        for log in receipt.logs:
            if len(log.topics) > 0 and log.topics[0].hex() == MEMORY_CREATED_TOPIC:
                memory_id = int(log.topics[1].hex(), 16)
                break

        if memory_id is None:
            return jsonify({'error': 'Failed to get memoryId from initMemoryRelay'}), 500

        # 2. addChunkRelay for each chunk
        for i, chunk_data in enumerate(chunks):
            receipt, tx_hash = send_tx(
                contract.functions.addChunkRelay(memory_id, i, chunk_data),
                relay_acct
            )
            tx_hashes.append(tx_hash)

        return jsonify({
            'success':     True,
            'memoryId':    memory_id,
            'totalChunks': total_chunks,
            'txHashes':    tx_hashes,
            'owner':       owner_address,
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/relay-status', methods=['GET'])
def relay_status():
    """Check relay wallet balance and paid wallet count."""
    relay = get_relay_account()
    balance_wei  = w3.eth.get_balance(relay.address)
    balance_lcai = float(w3.from_wei(balance_wei, 'ether'))
    paid_count   = len(load_paid_wallets())
    return jsonify({
        'address':      relay.address,
        'balance':      balance_lcai,
        'enough':       balance_lcai > 0.001,
        'paid_wallets': paid_count,
        'fee_lcai':     RELAY_FEE_LCAI,
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8190))
    app.run(host='0.0.0.0', port=port)
