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

import os, json, time, uuid, base64, threading, urllib.request, concurrent.futures, queue
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct

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
LIGHTTUBE_HIDDEN_FILE    = os.environ.get("LIGHTTUBE_HIDDEN_FILE", "/data/lighttube_hidden.json")
LIGHTTUBE_OVERRIDES_FILE = os.environ.get("LIGHTTUBE_OVERRIDES_FILE", "/data/lighttube_overrides.json")
ORCAVAULT_HIDDEN_FILE    = os.environ.get("ORCAVAULT_HIDDEN_FILE", "/data/orcavault_hidden.json")
LIGHTTUBE_ADMIN_KEY      = os.environ.get("LIGHTTUBE_ADMIN_KEY", "")
# Comma-separated IDs that are ALWAYS hidden (survive redeploys without a volume)
LIGHTTUBE_HIDDEN_SEED    = {s.strip() for s in os.environ.get("LIGHTTUBE_HIDDEN_IDS", "").split(",") if s.strip()}
# Maintenance mode — set LIGHTTUBE_MAINTENANCE=true in Railway to block all uploads
LIGHTTUBE_MAINTENANCE    = os.environ.get("LIGHTTUBE_MAINTENANCE", "").lower() in ("true", "1", "yes")
# Permanently banned wallets — comma-separated, set BANNED_WALLETS in Railway
BANNED_WALLETS           = {w.strip().lower() for w in os.environ.get("BANNED_WALLETS", "").split(",") if w.strip()}
ORCAVAULT_HIDDEN_SEED    = {s.strip() for s in os.environ.get("ORCAVAULT_HIDDEN_IDS", "").split(",") if s.strip()}
LIGHTTUBE_V2_ADDRESS     = os.environ.get("LIGHTTUBE_V2_ADDRESS", "")
LIGHTTUBE_V3_ADDRESS     = os.environ.get("LIGHTTUBE_V3_ADDRESS", "")
LIGHTTUBE_THUMBS_DIR     = os.environ.get("LIGHTTUBE_THUMBS_DIR", "/data/lt_thumbs")
GITHUB_TOKEN             = os.environ.get("GITHUB_TOKEN", "")
GITHUB_THUMB_REPO        = "Keiko-Dev-LCAI/lighttube"
GITHUB_THUMB_BRANCH      = "main"
CHUNK_SIZE               = 90_000            # 90KB per chunk — Lightchain RPC hard limit is 128KB/tx
CHAIN_ID                 = 9200
CHUNK_BATCH_SIZE         = int(os.environ.get("CHUNK_BATCH_SIZE", "10"))  # parallel chunks per batch

# LightTubeV2 ABI (relay functions only)
LIGHTTUBE_V2_ABI = [
    {"inputs":[{"name":"uploader","type":"address"},{"name":"title","type":"string"},
               {"name":"description","type":"string"},{"name":"category","type":"string"},
               {"name":"totalChunks","type":"uint256"}],
     "name":"initVideoFor","outputs":[{"type":"uint256"}],
     "stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"videoId","type":"uint256"},{"name":"chunkIndex","type":"uint256"},
               {"name":"chunkData","type":"string"}],
     "name":"addVideoChunkFor","outputs":[],
     "stateMutability":"nonpayable","type":"function"},
    {"anonymous":False,"inputs":[
        {"indexed":True,"name":"videoId","type":"uint256"},
        {"indexed":True,"name":"uploader","type":"address"},
        {"indexed":False,"name":"title","type":"string"},
        {"indexed":False,"name":"description","type":"string"},
        {"indexed":False,"name":"category","type":"string"},
        {"indexed":False,"name":"totalChunks","type":"uint256"},
        {"indexed":False,"name":"timestamp","type":"uint256"}],
     "name":"VideoCreated","type":"event"},
    {"inputs":[{"name":"videoId","type":"uint256"},{"name":"title","type":"string"},
               {"name":"description","type":"string"},{"name":"category","type":"string"}],
     "name":"updateMetadata","outputs":[],
     "stateMutability":"nonpayable","type":"function"},
]

# In-memory upload job tracker  {jobId: {status, progress, total, videoId, error}}
lt_upload_jobs = {}

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

# ─── Web3 connection pool ─────────────────────────────────────────────────────
# 10 reusable connections shared across upload threads. Much cheaper than
# spawning a fresh HTTPS session per chunk transaction. Queue is thread-safe
# by design — no race conditions.
_W3_POOL_SIZE = 10
_w3_pool: queue.Queue = queue.Queue()
for _i in range(_W3_POOL_SIZE):
    _w3_pool.put(Web3(Web3.HTTPProvider(RPC_URL)))

def _borrow_w3() -> Web3:
    """Get a Web3 connection from the pool. Blocks up to 60 s if all are busy."""
    return _w3_pool.get(timeout=60)

def _return_w3(conn: Web3) -> None:
    """Return a connection to the pool."""
    _w3_pool.put(conn)

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

# ─── GitHub thumbnail storage ────────────────────────────────────────────────

def save_thumbnail_github(filename, data_b64):
    """Commit a thumbnail JPEG to the lighttube GitHub repo. Returns the raw URL."""
    if not GITHUB_TOKEN:
        raise Exception("GITHUB_TOKEN not configured")
    # Strip data URI prefix
    if ',' in data_b64:
        data_b64 = data_b64.split(',', 1)[1]
    path    = f"thumbs/{filename}"
    api_url = f"https://api.github.com/repos/{GITHUB_THUMB_REPO}/contents/{path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    # Get existing SHA (needed for update)
    sha = None
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            sha = json.loads(r.read()).get("sha")
    except Exception:
        pass
    body = {"message": f"Thumbnail {filename}", "content": data_b64, "branch": GITHUB_THUMB_BRANCH}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(api_url, data=json.dumps(body).encode(), headers=headers, method="PUT")
    with urllib.request.urlopen(req, timeout=20) as r:
        result = json.loads(r.read())
        return f"https://raw.githubusercontent.com/{GITHUB_THUMB_REPO}/{GITHUB_THUMB_BRANCH}/thumbs/{filename}"

# ─── LightTube relay upload ───────────────────────────────────────────────────

def _send_one_chunk_tx(video_id, chunk_index, chunk_data, nonce, gas_price, contract_address):
    """Send a single addVideoChunkFor transaction. Borrows a pooled Web3 connection."""
    w3t = _borrow_w3()
    try:
        ct         = w3t.eth.contract(address=Web3.to_checksum_address(contract_address), abi=LIGHTTUBE_V2_ABI)
        relay_acct = Account.from_key(RELAY_PRIVATE_KEY)
        tx = ct.functions.addVideoChunkFor(video_id, chunk_index, chunk_data).build_transaction({
            'from':     relay_acct.address,
            'nonce':    nonce,
            'gas':      12_000_000,
            'gasPrice': gas_price,
            'chainId':  CHAIN_ID,
        })
        signed  = relay_acct.sign_transaction(tx)
        tx_hash = w3t.eth.send_raw_transaction(signed.raw_transaction)
        return w3t.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    finally:
        _return_w3(w3t)


def _do_lt_upload(job_id, user_wallet, title, description, category, data_uri, thumbnail_b64=None):
    """Background thread: chunk and store a video via LightTube relay functions (V3 preferred, V2 fallback)."""
    job = lt_upload_jobs[job_id]
    try:
        active_address = LIGHTTUBE_V3_ADDRESS or LIGHTTUBE_V2_ADDRESS
        if not active_address:
            raise Exception("No LightTube contract address configured (set LIGHTTUBE_V3_ADDRESS or LIGHTTUBE_V2_ADDRESS)")
        w3 = Web3(Web3.HTTPProvider(RPC_URL))
        relay_acct = Account.from_key(RELAY_PRIVATE_KEY)
        contract   = w3.eth.contract(
            address=Web3.to_checksum_address(active_address),
            abi=LIGHTTUBE_V2_ABI  # relay ABI identical for V2 and V3
        )
        chunks = [data_uri[i:i+CHUNK_SIZE] for i in range(0, len(data_uri), CHUNK_SIZE)]
        job['total'] = len(chunks)

        # ── initVideoFor ──────────────────────────────────────────────
        job['status'] = 'initializing'
        nonce = w3.eth.get_transaction_count(relay_acct.address, 'pending')
        tx = contract.functions.initVideoFor(
            Web3.to_checksum_address(user_wallet), title, description, category, len(chunks)
        ).build_transaction({
            'from':     relay_acct.address,
            'nonce':    nonce,
            'gas':      300_000,
            'gasPrice': w3.eth.gas_price,
            'chainId':  CHAIN_ID,
        })
        signed  = w3.eth.account.sign_transaction(tx, RELAY_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        # Parse videoId from VideoCreated event
        ct_with_events = w3.eth.contract(
            address=Web3.to_checksum_address(active_address),
            abi=LIGHTTUBE_V2_ABI
        )
        logs    = ct_with_events.events.VideoCreated().process_receipt(receipt)
        video_id = int(logs[0]['args']['videoId'])
        job['videoId'] = video_id
        job['status']  = 'uploading'
        nonce += 1

        # Save thumbnail immediately after we know the videoId (non-fatal if it fails)
        if thumbnail_b64:
            try:
                prefix = "v3" if LIGHTTUBE_V3_ADDRESS else "v2"
                filename = f"{prefix}_{video_id}.jpg"
                if GITHUB_TOKEN:
                    save_thumbnail_github(filename, thumbnail_b64)
                else:
                    os.makedirs(LIGHTTUBE_THUMBS_DIR, exist_ok=True)
                    thumb_path = os.path.join(LIGHTTUBE_THUMBS_DIR, filename)
                    thumb_data = thumbnail_b64.split(',', 1)[1] if ',' in thumbnail_b64 else thumbnail_b64
                    with open(thumb_path, 'wb') as tf:
                        tf.write(base64.b64decode(thumb_data))
            except Exception as te:
                print(f"Thumbnail save failed (non-fatal): {te}")

        # ── addVideoChunkFor × N (adaptive parallel batches) ──────────────────
        _MAX_BATCH = 25   # never go above this
        _MIN_BATCH = 8    # never go below this
        batch_size = max(CHUNK_BATCH_SIZE, 15)  # start at 15 (or env var if higher)
        chunk_idx  = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_BATCH) as pool:
            while chunk_idx < len(chunks):
                batch          = chunks[chunk_idx : chunk_idx + batch_size]
                gas_price      = int(w3.eth.gas_price * 1.2)  # 20% bump avoids underpricing
                future_map     = {}
                had_real_error = False

                # Fire all chunks in this batch simultaneously with pre-assigned nonces
                for j, chunk in enumerate(batch):
                    ci = chunk_idx + j
                    cn = nonce + j
                    f  = pool.submit(_send_one_chunk_tx, video_id, ci, chunk, cn, gas_price, active_address)
                    future_map[f] = (ci, cn, chunk)

                # Wait for all confirmations; retry any that failed
                for f in concurrent.futures.as_completed(future_map):
                    ci, cn, chunk = future_map[f]
                    try:
                        f.result()
                    except Exception as e:
                        err_str = str(e).lower()
                        if 'nonce too low' in err_str or 'already known' in err_str:
                            pass  # tx was already mined — treat as success
                        else:
                            had_real_error = True
                            # One retry with a fresh gas price
                            _send_one_chunk_tx(video_id, ci, chunk, cn, int(w3.eth.gas_price * 1.2), active_address)
                    job['progress'] += 1

                nonce     += len(batch)
                chunk_idx += len(batch)

                # Adaptive sizing: grow on clean batch, shrink on real errors
                if had_real_error:
                    batch_size = max(batch_size - 5, _MIN_BATCH)
                    print(f"[upload] batch error — backing off to {batch_size} chunks/batch")
                else:
                    batch_size = min(batch_size + 3, _MAX_BATCH)
                    print(f"[upload] clean batch — stepping up to {batch_size} chunks/batch")

        job['status'] = 'complete'
    except Exception as e:
        job['status'] = 'error'
        job['error']  = str(e)
        print(f"LightTube upload error [{job_id}]: {e}")


@app.route('/api/lighttube/upload', methods=['POST'])
def lighttube_upload():
    """
    Relay-upload a video to LightTubeV2 on behalf of a user wallet.
    Form fields: wallet, signature, title, description, category, timestamp
    File field:  video  (multipart/form-data)
    """
    # Maintenance mode — block all uploads
    if LIGHTTUBE_MAINTENANCE:
        return jsonify({'error': 'LightTube is temporarily offline for maintenance. Please check back soon.'}), 503

    wallet     = request.form.get('wallet', '').strip().lower()
    signature  = request.form.get('signature', '').strip()
    title      = request.form.get('title', '').strip()
    description= request.form.get('description', '').strip()
    category   = request.form.get('category', 'Other').strip()
    timestamp  = request.form.get('timestamp', '').strip()
    video_file = request.files.get('video')
    thumbnail  = request.form.get('thumbnail', '').strip() or None

    if not wallet or not signature or not title or not video_file:
        return jsonify({'error': 'Missing required fields'}), 400

    # Permanent wallet ban check
    if wallet in BANNED_WALLETS:
        return jsonify({'error': 'This wallet has been banned from LightTube.'}), 403

    # Verify wallet signature
    message = f"Upload to LightTube\nTitle: {title}\nWallet: {wallet}\nTimestamp: {timestamp}"
    try:
        msg       = encode_defunct(text=message)
        recovered = Account.recover_message(msg, signature=signature).lower()
        if recovered != wallet:
            return jsonify({'error': 'Signature does not match wallet'}), 401
    except Exception as e:
        return jsonify({'error': f'Signature error: {e}'}), 401

    # Build base64 data URI
    mime      = video_file.content_type or 'video/mp4'
    raw_bytes = video_file.read()
    data_uri  = f"data:{mime};base64,{base64.b64encode(raw_bytes).decode()}"

    # Launch background upload
    job_id = str(uuid.uuid4())
    lt_upload_jobs[job_id] = {
        'status': 'pending', 'progress': 0, 'total': 0, 'videoId': None, 'error': None
    }
    t = threading.Thread(target=_do_lt_upload,
                         args=(job_id, wallet, title, description, category, data_uri, thumbnail),
                         daemon=True)
    t.start()
    return jsonify({'jobId': job_id})


@app.route('/api/lighttube/upload-progress/<job_id>', methods=['GET'])
def lighttube_upload_progress(job_id):
    """Poll upload job status. Returns {status, progress, total, videoId, error}."""
    job = lt_upload_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


# ─── LightTube thumbnail endpoints ───────────────────────────────────────────

@app.route('/api/lighttube/thumb/<version>/<video_id>', methods=['GET'])
def lighttube_get_thumb(version, video_id):
    """Serve stored thumbnail for a LightTube video. version = v2 or v3."""
    if version not in ('v2', 'v3'):
        return '', 400
    thumb_path = os.path.join(LIGHTTUBE_THUMBS_DIR, f"{version}_{video_id}.jpg")
    if not os.path.exists(thumb_path):
        return '', 404
    return send_file(thumb_path, mimetype='image/jpeg')


@app.route('/api/lighttube/set-thumbnail', methods=['POST'])
def lighttube_set_thumbnail():
    """
    Allow a video uploader to set/update their video thumbnail.
    Body JSON: {videoId, contractVersion, wallet, signature, timestamp, thumbnail}
    thumbnail = base64 data URI (image/jpeg recommended, max ~200KB)
    """
    data           = request.get_json(force=True) or {}
    video_id       = str(data.get('videoId', '')).strip()
    contract_ver   = str(data.get('contractVersion', 'v3')).strip().lower()
    wallet         = (data.get('wallet', '') or '').strip().lower()
    signature      = (data.get('signature', '') or '').strip()
    timestamp      = (data.get('timestamp', '') or '').strip()
    thumbnail      = (data.get('thumbnail', '') or '').strip()

    if not all([video_id, wallet, signature, thumbnail]):
        return jsonify({'error': 'Missing required fields'}), 400
    if contract_ver not in ('v2', 'v3'):
        return jsonify({'error': 'contractVersion must be v2 or v3'}), 400

    # Verify wallet signature
    message = f"Set thumbnail for LightTube video {video_id}\nWallet: {wallet}\nTimestamp: {timestamp}"
    try:
        msg       = encode_defunct(text=message)
        recovered = Account.recover_message(msg, signature=signature).lower()
        if recovered != wallet:
            return jsonify({'error': 'Signature does not match wallet'}), 401
    except Exception as e:
        return jsonify({'error': f'Signature error: {e}'}), 401

    # Verify ownership on-chain
    VIDEOS_ABI = [{"inputs":[{"name":"","type":"uint256"}],"name":"videos","outputs":[
        {"name":"uploader","type":"address"},{"name":"totalChunks","type":"uint256"},{"name":"exists","type":"bool"}
    ],"stateMutability":"view","type":"function"}]
    try:
        w3_local = Web3(Web3.HTTPProvider(RPC_URL))
        addr = LIGHTTUBE_V3_ADDRESS if contract_ver == 'v3' else LIGHTTUBE_V2_ADDRESS
        if not addr:
            return jsonify({'error': f'Contract {contract_ver} not configured'}), 400
        ct   = w3_local.eth.contract(address=Web3.to_checksum_address(addr), abi=VIDEOS_ABI)
        vid  = ct.functions.videos(int(video_id)).call()
        if not vid[2]:
            return jsonify({'error': 'Video not found on chain'}), 404
        if vid[0].lower() != wallet:
            return jsonify({'error': 'Not the video uploader'}), 403
    except Exception as e:
        return jsonify({'error': f'Chain verification failed: {e}'}), 500

    # Save thumbnail (GitHub preferred, disk fallback)
    try:
        filename = f"{contract_ver}_{video_id}.jpg"
        if GITHUB_TOKEN:
            save_thumbnail_github(filename, thumbnail)
        else:
            os.makedirs(LIGHTTUBE_THUMBS_DIR, exist_ok=True)
            thumb_path = os.path.join(LIGHTTUBE_THUMBS_DIR, filename)
            thumb_data = thumbnail.split(',', 1)[1] if ',' in thumbnail else thumbnail
            with open(thumb_path, 'wb') as f:
                f.write(base64.b64decode(thumb_data))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': f'Save failed: {e}'}), 500


# ─── LightTube hidden video registry ─────────────────────────────────────────

def load_hidden_videos():
    """Load hidden video IDs from disk, merged with the always-hidden seed from env var."""
    try:
        os.makedirs(os.path.dirname(LIGHTTUBE_HIDDEN_FILE), exist_ok=True)
        with open(LIGHTTUBE_HIDDEN_FILE, 'r') as f:
            from_disk = set(str(x) for x in json.load(f))
    except Exception:
        from_disk = set()
    return from_disk | LIGHTTUBE_HIDDEN_SEED  # always include seeded IDs

def save_hidden_videos(hidden):
    """Persist hidden video IDs to disk."""
    try:
        os.makedirs(os.path.dirname(LIGHTTUBE_HIDDEN_FILE), exist_ok=True)
        with open(LIGHTTUBE_HIDDEN_FILE, 'w') as f:
            json.dump(list(hidden), f)
    except Exception as e:
        print(f"Warning: could not save hidden_videos: {e}")

def load_lt_overrides():
    """Load LightTube metadata overrides: {videoId: {title, description, category, ...}}"""
    try:
        with open(LIGHTTUBE_OVERRIDES_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def save_lt_overrides(overrides):
    try:
        os.makedirs(os.path.dirname(LIGHTTUBE_OVERRIDES_FILE), exist_ok=True)
        with open(LIGHTTUBE_OVERRIDES_FILE, 'w') as f:
            json.dump(overrides, f, indent=2)
    except Exception as e:
        print(f"Warning: could not save lt_overrides: {e}")

def load_orcavault_hidden():
    """Load hidden OrcaVault memory IDs from disk, merged with always-hidden seed."""
    try:
        os.makedirs(os.path.dirname(ORCAVAULT_HIDDEN_FILE), exist_ok=True)
        with open(ORCAVAULT_HIDDEN_FILE, 'r') as f:
            from_disk = set(str(x) for x in json.load(f))
    except Exception:
        from_disk = set()
    return from_disk | ORCAVAULT_HIDDEN_SEED

def save_orcavault_hidden(hidden):
    """Persist hidden OrcaVault memory IDs to disk."""
    try:
        os.makedirs(os.path.dirname(ORCAVAULT_HIDDEN_FILE), exist_ok=True)
        with open(ORCAVAULT_HIDDEN_FILE, 'w') as f:
            json.dump(list(hidden), f)
    except Exception as e:
        print(f"Warning: could not save orcavault_hidden: {e}")

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

@app.route('/api/lighttube/hidden', methods=['GET'])
def lighttube_get_hidden():
    """
    Public endpoint — returns list of hidden video IDs for LightTube feed filtering.
    No auth required: the list itself isn't sensitive.
    """
    hidden = load_hidden_videos()
    return jsonify({'hidden': list(hidden)})

@app.route('/api/lighttube/hide', methods=['POST'])
def lighttube_hide():
    """
    Admin endpoint — hide a video from the LightTube feed.
    Body: { videoId: "N", adminKey: "secret" }
    """
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured on server'}), 500
    body = request.get_json()
    if not body:
        return jsonify({'error': 'No JSON body'}), 400
    video_id = body.get('videoId')
    admin_key = body.get('adminKey', '')
    if admin_key != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    if video_id is None:
        return jsonify({'error': 'videoId required'}), 400
    hidden = load_hidden_videos()
    hidden.add(str(video_id))
    save_hidden_videos(hidden)
    return jsonify({'success': True, 'hidden_count': len(hidden)})

@app.route('/api/lighttube/unhide', methods=['POST'])
def lighttube_unhide():
    """
    Admin endpoint — restore a hidden video to the LightTube feed.
    Body: { videoId: "N", adminKey: "secret" }
    """
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured on server'}), 500
    body = request.get_json()
    if not body:
        return jsonify({'error': 'No JSON body'}), 400
    video_id = body.get('videoId')
    admin_key = body.get('adminKey', '')
    if admin_key != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    if video_id is None:
        return jsonify({'error': 'videoId required'}), 400
    hidden = load_hidden_videos()
    hidden.discard(str(video_id))
    save_hidden_videos(hidden)
    return jsonify({'success': True, 'hidden_count': len(hidden)})

@app.route('/api/lighttube/update-metadata', methods=['POST'])
def lighttube_update_metadata():
    """
    Admin endpoint — update title/description/category for a relay-uploaded video.
    The relay wallet was the uploader on-chain, so it can sign the updateMetadata tx.
    Body: { videoId: 4, title: "...", description: "...", category: "...", adminKey: "secret" }
    """
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured on server'}), 500
    if not LIGHTTUBE_V3_ADDRESS:
        return jsonify({'error': 'V3 contract address not configured'}), 500
    if not RELAY_PRIVATE_KEY:
        return jsonify({'error': 'Relay key not configured'}), 500
    body = request.get_json()
    if not body:
        return jsonify({'error': 'No JSON body'}), 400
    if body.get('adminKey', '') != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    video_id   = body.get('videoId')
    title      = body.get('title', '')
    desc       = body.get('description', '')
    category   = body.get('category', '')
    if video_id is None:
        return jsonify({'error': 'videoId required'}), 400
    try:
        relay_acct = Account.from_key(RELAY_PRIVATE_KEY)
        ct = w3.eth.contract(
            address=Web3.to_checksum_address(LIGHTTUBE_V3_ADDRESS),
            abi=LIGHTTUBE_V2_ABI
        )
        nonce     = w3.eth.get_transaction_count(relay_acct.address)
        gas_price = int(w3.eth.gas_price * 1.2)
        tx = ct.functions.updateMetadata(int(video_id), title, desc, category).build_transaction({
            'from': relay_acct.address, 'nonce': nonce,
            'gas': 500_000, 'gasPrice': gas_price, 'chainId': CHAIN_ID,
        })
        signed  = relay_acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        return jsonify({'success': True, 'txHash': tx_hash.hex(), 'status': receipt.status})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lighttube/overrides', methods=['GET'])
def lighttube_get_overrides():
    """Public — returns metadata overrides so frontend can apply them over on-chain data."""
    return jsonify(load_lt_overrides())

@app.route('/api/lighttube/set-override', methods=['POST'])
def lighttube_set_override():
    """Admin — set a metadata override for a video. Merges with existing on-chain data client-side.
    Body: { videoId: "v3-4", title: "...", description: "...", category: "...", adminKey: "..." }
    Any field can be omitted — only provided fields are overridden.
    """
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured'}), 500
    body = request.get_json()
    if not body or body.get('adminKey', '') != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    vid = str(body.get('videoId', ''))
    if not vid:
        return jsonify({'error': 'videoId required'}), 400
    overrides = load_lt_overrides()
    entry = overrides.get(vid, {})
    for field in ('title', 'description', 'category'):
        if field in body:
            entry[field] = body[field]
    overrides[vid] = entry
    save_lt_overrides(overrides)
    return jsonify({'success': True, 'videoId': vid, 'override': entry})

# ─── OrcaVault hidden memory registry ────────────────────────────────────────

@app.route('/api/orcavault/hidden', methods=['GET'])
def orcavault_get_hidden():
    """Public endpoint — returns list of hidden OrcaVault memory IDs."""
    hidden = load_orcavault_hidden()
    return jsonify({'hidden': list(hidden)})

@app.route('/api/orcavault/hide', methods=['POST'])
def orcavault_hide():
    """Admin endpoint — hide a memory from OrcaVault.
    Body: { memoryId: "N", adminKey: "secret" }"""
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured on server'}), 500
    body = request.get_json()
    if not body:
        return jsonify({'error': 'No JSON body'}), 400
    memory_id = body.get('memoryId')
    admin_key = body.get('adminKey', '')
    if admin_key != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    if memory_id is None:
        return jsonify({'error': 'memoryId required'}), 400
    hidden = load_orcavault_hidden()
    hidden.add(str(memory_id))
    save_orcavault_hidden(hidden)
    return jsonify({'success': True, 'hidden_count': len(hidden)})

@app.route('/api/orcavault/unhide', methods=['POST'])
def orcavault_unhide():
    """Admin endpoint — restore a hidden memory to OrcaVault.
    Body: { memoryId: "N", adminKey: "secret" }"""
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured on server'}), 500
    body = request.get_json()
    if not body:
        return jsonify({'error': 'No JSON body'}), 400
    memory_id = body.get('memoryId')
    admin_key = body.get('adminKey', '')
    if admin_key != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    if memory_id is None:
        return jsonify({'error': 'memoryId required'}), 400
    hidden = load_orcavault_hidden()
    hidden.discard(str(memory_id))
    save_orcavault_hidden(hidden)
    return jsonify({'success': True, 'hidden_count': len(hidden)})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8190))
    app.run(host='0.0.0.0', port=port)
