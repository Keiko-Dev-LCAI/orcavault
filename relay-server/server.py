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

import os, json, time, uuid, base64, threading, urllib.request, urllib.parse, concurrent.futures, queue, secrets, tempfile
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
# ── AIVM (Lightchain decentralized inference) ────────────────────────────────
_AIVM_GATEWAY  = "https://chat-api.mainnet.lightchain.ai"
_AIVM_RELAY    = "wss://relay.mainnet.lightchain.ai/ws"
_AIVM_RPC      = "https://rpc.mainnet.lightchain.ai"
_AIVM_JOB_REG  = "0xfB15F90298e4CcD7106E76fFB5e520315cC42B0b"
_AIVM_JOB_FEE  = 20_000_000_000_000_000   # 0.02 LCAI in wei
_AIVM_CHAIN_ID = 9200
GITHUB_THUMB_REPO        = "Keiko-Dev-LCAI/lighttube"
GITHUB_THUMB_BRANCH      = "main"
CHUNK_SIZE               = 90_000            # 90KB per chunk — Lightchain RPC hard limit is 128KB/tx
CHAIN_ID                 = 9200
CHUNK_BATCH_SIZE         = int(os.environ.get("CHUNK_BATCH_SIZE", "10"))  # parallel chunks per batch

# Global nonce lock — ensures only one relay job claims the relay wallet's nonce at a time.
# Prevents concurrent jobs from grabbing the same nonce and silently dropping chunks.
_nonce_lock = threading.Lock()
# Only one LightTube blockchain job (upload or repair) at a time on the relay wallet.
_relay_job_lock = threading.Lock()
_ACTIVE_LT_JOB_STATUSES = frozenset({'receiving', 'initializing', 'repairing', 'uploading', 'pending'})

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
# Must be >= _MAX_BATCH (25) to avoid pool-exhaustion timeouts when a full
# parallel batch borrows connections simultaneously.
_W3_POOL_SIZE = 30
_w3_pool: queue.Queue = queue.Queue()
for _i in range(_W3_POOL_SIZE):
    _w3_pool.put(Web3(Web3.HTTPProvider(RPC_URL)))

def _borrow_w3() -> Web3:
    """Get a Web3 connection from the pool. Blocks up to 90 s if all are busy."""
    return _w3_pool.get(timeout=90)

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

def _github_read_json(path):
    """Read a JSON file from the lighttube GitHub repo. Returns parsed data or None on error."""
    if not GITHUB_TOKEN:
        return None
    api_url = f"https://api.github.com/repos/{GITHUB_THUMB_REPO}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            content = base64.b64decode(data["content"].replace("\n", "")).decode()
            return json.loads(content)
    except Exception:
        return None

def _github_write_json(path, data, message):
    """Write a JSON file to the lighttube GitHub repo. Creates or updates."""
    if not GITHUB_TOKEN:
        return False
    api_url = f"https://api.github.com/repos/{GITHUB_THUMB_REPO}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json", "Content-Type": "application/json"}
    sha = None
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            sha = json.loads(r.read()).get("sha")
    except Exception:
        pass
    content_b64 = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
    body = {"message": message, "content": content_b64, "branch": GITHUB_THUMB_BRANCH}
    if sha:
        body["sha"] = sha
    try:
        req = urllib.request.Request(api_url, data=json.dumps(body).encode(), headers=headers, method="PUT")
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        return True
    except Exception as e:
        print(f"Warning: could not write {path} to GitHub: {e}")
        return False

# ─── GitHub-backed moderation lists (survive all redeploys) ──────────────────

_perm_hidden_cache    = None   # set of permanently hidden video IDs
_banned_wallets_cache = None   # set of permanently banned wallet addresses

def get_permanent_hidden():
    """Return the GitHub-backed permanent hidden set. Loaded once per process startup."""
    global _perm_hidden_cache
    if _perm_hidden_cache is None:
        from_github = _github_read_json("moderation/permanent_hidden.json") or []
        _perm_hidden_cache = set(str(x) for x in from_github) | LIGHTTUBE_HIDDEN_SEED
    return _perm_hidden_cache

def add_permanent_hidden(video_id):
    """Mark a video as permanently hidden. Updates GitHub in background."""
    perm = get_permanent_hidden()
    perm.add(str(video_id))
    # Immediately add to temp hidden file too for fast effect
    hidden = load_hidden_videos()
    hidden.add(str(video_id))
    save_hidden_videos(hidden)
    # Persist to GitHub in background
    snapshot = list(perm)
    threading.Thread(
        target=_github_write_json,
        args=("moderation/permanent_hidden.json", snapshot, f"Permanently hide {video_id}"),
        daemon=True
    ).start()

def get_banned_wallets_gh():
    """Return the GitHub-backed banned wallet set. Loaded once per process startup."""
    global _banned_wallets_cache
    if _banned_wallets_cache is None:
        from_github = _github_read_json("moderation/banned_wallets.json") or []
        _banned_wallets_cache = set(w.lower() for w in from_github) | BANNED_WALLETS
    return _banned_wallets_cache

def add_banned_wallet_gh(wallet):
    """Permanently ban a wallet. Updates GitHub in background."""
    banned = get_banned_wallets_gh()
    banned.add(wallet.lower())
    snapshot = list(banned)
    threading.Thread(
        target=_github_write_json,
        args=("moderation/banned_wallets.json", snapshot, f"Ban wallet {wallet[:10]}"),
        daemon=True
    ).start()

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

def _mime_for_filename(fn):
    fn = (fn or 'video.mp4').lower()
    if fn.endswith('.mp4'):
        return 'video/mp4'
    if fn.endswith('.mov'):
        return 'video/quicktime'
    if fn.endswith('.webm'):
        return 'video/webm'
    if fn.endswith('.gif'):
        return 'image/gif'
    return 'video/mp4'


def _stream_b64_to_file(raw_path, b64_path, read_size=3 * 1024 * 1024):
    """Base64-encode a large file to disk without holding the full payload in RAM."""
    read_size = max(read_size - (read_size % 3), 3)
    with open(raw_path, 'rb') as inf, open(b64_path, 'w', encoding='ascii') as outf:
        while True:
            block = inf.read(read_size)
            if not block:
                break
            outf.write(base64.b64encode(block).decode('ascii'))


def _data_uri_chunk_at(ci, prefix, b64_path, b64_len):
    """Return one on-chain data-URI chunk from prefix + streamed base64 file."""
    start = ci * CHUNK_SIZE
    end   = min(start + CHUNK_SIZE, len(prefix) + b64_len)
    if end <= start:
        return ''
    if end <= len(prefix):
        return prefix[start:end]
    if start >= len(prefix):
        with open(b64_path, 'r', encoding='ascii') as f:
            f.seek(start - len(prefix))
            return f.read(end - start)
    with open(b64_path, 'r', encoding='ascii') as f:
        return prefix[start:] + f.read(end - len(prefix))


def _active_lt_job():
    """Return an in-flight LightTube upload/repair job, if any."""
    for job in lt_upload_jobs.values():
        if job.get('status') in _ACTIVE_LT_JOB_STATUSES:
            return job
    return None


def _get_video_total_chunks_on_chain(w3, contract_address, video_id):
    """Read totalChunks from the VideoCreated event for a video."""
    addr        = Web3.to_checksum_address(contract_address)
    event_topic = '0x' + w3.keccak(text='VideoCreated(uint256,address,string,string,string,uint256,uint256)').hex()
    video_topic = '0x' + hex(video_id)[2:].zfill(64)
    logs = w3.eth.get_logs({
        'fromBlock': 0,
        'toBlock':   'latest',
        'address':   addr,
        'topics':    [event_topic, video_topic],
    })
    if not logs:
        raise Exception(f'Video {video_id} not found on-chain')
    data = logs[0]['data']
    if isinstance(data, str):
        data = bytes.fromhex(data[2:])
    from eth_abi import decode as abi_decode
    _title, _desc, _cat, total_chunks, _ts = abi_decode(
        ['string', 'string', 'string', 'uint256', 'uint256'], data
    )
    return int(total_chunks)


def _flush_stuck_relay_nonces(w3, relay_acct, max_cancel=400):
    """
    Replace stuck pending relay-wallet txs with 0-value self-transfers so new
    chunk submissions can mine. Needed when a bad repair (wrong video ID, etc.)
    fills the mempool with reverting transactions.
    """
    confirmed = w3.eth.get_transaction_count(relay_acct.address, 'latest')
    pending   = w3.eth.get_transaction_count(relay_acct.address, 'pending')
    stuck     = pending - confirmed
    if stuck <= 0:
        return 0
    print(f"[relay] flushing {stuck} stuck mempool nonce(s) from {confirmed}…")
    cleared = 0
    gas_price = int(w3.eth.gas_price * 10)
    for nonce in range(confirmed, min(pending, confirmed + max_cancel)):
        try:
            tx = {
                'to':       relay_acct.address,
                'value':    0,
                'nonce':    nonce,
                'gas':      21_000,
                'gasPrice': gas_price,
                'chainId':  CHAIN_ID,
            }
            signed  = relay_acct.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.get('status') == 1:
                cleared += 1
        except Exception as err:
            err_str = str(err).lower()
            if 'nonce too low' in err_str or 'already known' in err_str:
                cleared += 1
                continue
            print(f"[relay] flush nonce {nonce} failed: {err}")
            break
    print(f"[relay] flushed {cleared} stuck nonce(s)")
    return cleared


def _scan_video_chunk_indices(w3, contract_address, video_id):
    """
    Return the set of chunk indices already stored on-chain for a video.

    Uses adaptive block-range pagination: walks the chain in 50k-block pages,
    subdividing any page that fails or returns a dense burst of events (large
    uploads cluster in a narrow block window and crash single get_logs calls).
    """
    addr           = Web3.to_checksum_address(contract_address)
    event_topic    = '0x' + w3.keccak(text='VideoChunkStored(uint256,uint256,uint256,string)').hex()
    video_id_topic = '0x' + hex(video_id)[2:].zfill(64)
    latest_block   = w3.eth.block_number
    present_set    = set()
    MIN_SPAN       = 100    # smallest block window to attempt
    DENSE_LIMIT    = 800    # subdivide if a page returns this many logs

    def _scan_range(start, end, depth=0):
        if start > end:
            return
        span = end - start + 1
        try:
            logs = w3.eth.get_logs({
                'fromBlock': start,
                'toBlock':   end,
                'address':   addr,
                'topics':    [event_topic, video_id_topic],
            })
            for log in logs:
                present_set.add(int(log['topics'][2].hex(), 16))
            if len(logs) >= DENSE_LIMIT and span > MIN_SPAN:
                mid = start + span // 2
                print(f"[repair] scan blocks {start}-{end}: {len(logs)} logs — subdividing at {mid}")
                _scan_range(start, mid, depth + 1)
                _scan_range(mid + 1, end, depth + 1)
        except Exception as err:
            if span <= MIN_SPAN:
                print(f"[repair] scan blocks {start}-{end} failed at min span: {err}")
                return
            mid = start + span // 2
            print(f"[repair] scan blocks {start}-{end} failed ({err}) — splitting at {mid}")
            _scan_range(start, mid, depth + 1)
            _scan_range(mid + 1, end, depth + 1)

    SCAN_STEP = 50_000
    total_pages = (latest_block // SCAN_STEP) + 1
    page_num = 0
    print(f"[repair] video {video_id}: adaptive scan 0-{latest_block:,} ({total_pages} top-level pages)")
    for start in range(0, latest_block + 1, SCAN_STEP):
        end = min(start + SCAN_STEP - 1, latest_block)
        page_num += 1
        _scan_range(start, end)
        if page_num % 5 == 0 or end >= latest_block:
            print(f"[repair] scan page {page_num}/{total_pages} done — {len(present_set)} unique chunks so far")
    return present_set


def _wait_tx_receipt(w3_conn, tx_hash, timeout=900):
    """Poll for a transaction receipt; Lightchain can take several minutes per tx."""
    deadline = time.time() + timeout
    tx_hex   = tx_hash.hex() if hasattr(tx_hash, 'hex') else str(tx_hash)
    while time.time() < deadline:
        try:
            return w3_conn.eth.get_transaction_receipt(tx_hash)
        except Exception:
            pass
        try:
            return w3_conn.eth.wait_for_transaction_receipt(tx_hash, timeout=10)
        except Exception:
            time.sleep(5)
    raise Exception(f'Transaction {tx_hex} not in the chain after {timeout} seconds')


def _send_one_chunk_tx(video_id, chunk_index, chunk_data, nonce, gas_price, contract_address, receipt_timeout=900):
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
        return _wait_tx_receipt(w3t, tx_hash, timeout=receipt_timeout)
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
        with _nonce_lock:
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
                            print(f"[WARNING] chunk {ci} got 'nonce too low' — may have been dropped by concurrent job. Marking as done.")
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

    # Permanent wallet ban check — env var + GitHub-backed list
    if wallet in BANNED_WALLETS or wallet in get_banned_wallets_gh():
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


@app.route('/api/lighttube/upload-init', methods=['POST'])
def lighttube_upload_init():
    """
    Initialize a chunked upload session (for large files that would timeout as a single POST).
    JSON body: {wallet, signature, timestamp, title, description, category, totalPieces, fileName, thumbnail}
    Returns: {jobId}
    """
    if LIGHTTUBE_MAINTENANCE:
        return jsonify({'error': 'LightTube is temporarily offline for maintenance. Please check back soon.'}), 503

    # Support both JSON body (normal upload) and multipart/form-data (repair mode)
    data = request.get_json(force=True, silent=True) or {}
    form = request.form

    def _field(key, default=''):
        """Read from form data first, fall back to JSON body."""
        v = form.get(key) or data.get(key) or default
        return (v if isinstance(v, str) else str(v or default)).strip()

    wallet       = _field('wallet').lower()
    signature    = _field('signature')
    title        = _field('title')
    description  = _field('description')
    category     = _field('category', 'Other') or 'Other'
    timestamp    = _field('timestamp')
    total_pieces = int(form.get('totalPieces', data.get('totalPieces', 0)) or 0)
    file_name    = _field('fileName', 'video.mp4') or 'video.mp4'
    thumbnail    = _field('thumbnail') or None
    repair_video_id_str = _field('repairVideoId')
    repair_video_id = int(repair_video_id_str) if repair_video_id_str else None

    if repair_video_id is not None:
        # Repair mode — admin-only; skip wallet/title/sig checks
        if not LIGHTTUBE_ADMIN_KEY:
            return jsonify({'error': 'Admin not configured on server'}), 500
        admin_key = _field('adminKey')
        if admin_key != LIGHTTUBE_ADMIN_KEY:
            return jsonify({'error': 'Unauthorized'}), 401
        if not total_pieces:
            return jsonify({'error': 'totalPieces required'}), 400
        active = _active_lt_job()
        if active:
            return jsonify({
                'error': 'Another upload/repair is already running on the relay. '
                         'Wait for it to finish, or hard-refresh and retry in a few minutes.'
            }), 409
    else:
        # Normal upload mode — validate wallet, title, and signature
        if not wallet or not signature or not title or not total_pieces:
            return jsonify({'error': 'Missing required fields'}), 400

        # Permanent wallet ban check — same as existing upload endpoint
        if wallet in BANNED_WALLETS or wallet in get_banned_wallets_gh():
            return jsonify({'error': 'This wallet has been banned from LightTube.'}), 403

        # Verify wallet signature — same message format as existing upload endpoint
        message = f"Upload to LightTube\nTitle: {title}\nWallet: {wallet}\nTimestamp: {timestamp}"
        try:
            msg       = encode_defunct(text=message)
            recovered = Account.recover_message(msg, signature=signature).lower()
            if recovered != wallet:
                return jsonify({'error': 'Signature does not match wallet'}), 401
        except Exception as e:
            return jsonify({'error': f'Signature error: {e}'}), 401

    # Create a temp file to receive the incoming pieces
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.tmp')
    tmp.close()

    job_id = str(uuid.uuid4())
    lt_upload_jobs[job_id] = {
        'status':          'receiving',
        'progress':        0,
        'total':           0,
        'videoId':         None,
        'error':           None,
        'pieces_received': 0,
        'total_pieces':    total_pieces,
        'tmp_path':        tmp.name,
        'wallet':          wallet,
        'title':           title,
        'description':     description,
        'category':        category,
        'file_name':       file_name,
        'thumbnail':       thumbnail,
        'repair_video_id': repair_video_id,
    }
    return jsonify({'jobId': job_id})


@app.route('/api/lighttube/upload-piece', methods=['POST'])
def lighttube_upload_piece():
    """
    Append one binary piece to a chunked upload session.
    Form fields: jobId (string), pieceIndex (int), piece (file/blob)
    Returns: {ok: true, piecesReceived: N}
    """
    job_id     = request.form.get('jobId', '').strip()
    piece_file = request.files.get('piece')

    if not job_id or not piece_file:
        return jsonify({'error': 'Missing jobId or piece'}), 400

    job = lt_upload_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    if job['status'] != 'receiving':
        return jsonify({'error': 'Job not in receiving state'}), 400

    # Append piece to the temp file
    with open(job['tmp_path'], 'ab') as f:
        f.write(piece_file.read())

    job['pieces_received'] += 1

    # All pieces received — kick off background processing
    if job['pieces_received'] >= job['total_pieces']:
        job['status'] = 'initializing'
        t = threading.Thread(target=_process_chunked_upload, args=(job_id,), daemon=True)
        t.start()

    return jsonify({'ok': True, 'piecesReceived': job['pieces_received']})


def _process_chunked_upload(job_id):
    """
    Background thread: base64-encode the assembled temp file and submit it to the
    blockchain exactly the same way _do_lt_upload() does — same contract calls,
    same adaptive parallel batching, same thumbnail handling.
    """
    job      = lt_upload_jobs[job_id]
    tmp_path = job['tmp_path']
    try:
        active_address = LIGHTTUBE_V3_ADDRESS or LIGHTTUBE_V2_ADDRESS
        if not active_address:
            raise Exception("No LightTube contract address configured (set LIGHTTUBE_V3_ADDRESS or LIGHTTUBE_V2_ADDRESS)")

        w3_local   = Web3(Web3.HTTPProvider(RPC_URL))
        relay_acct = Account.from_key(RELAY_PRIVATE_KEY)
        contract   = w3_local.eth.contract(
            address=Web3.to_checksum_address(active_address),
            abi=LIGHTTUBE_V2_ABI  # relay ABI is identical for V2 and V3
        )

        repair_video_id = job.get('repair_video_id')

        # ── REPAIR MODE: scan blockchain BEFORE loading 2 GB into RAM ────────
        if repair_video_id is not None:
            job['status'] = 'repairing'
            job['phase']  = 'scanning'
            print(f"[repair] job {job_id}: scanning blockchain before file encode…")
            try:
                present_set = _scan_video_chunk_indices(w3_local, active_address, repair_video_id)
            except Exception as e:
                raise Exception(f'Failed to query blockchain events: {e}')

            job['phase'] = 'encoding'
            print(f"[repair] job {job_id}: streaming base64 encode to disk (no 2 GB RAM load)…")
            mime     = _mime_for_filename(job.get('file_name'))
            prefix   = 'data:' + mime + ';base64,'
            b64_path = tmp_path + '.b64'
            _stream_b64_to_file(tmp_path, b64_path)
            b64_len      = os.path.getsize(b64_path)
            data_uri_len = len(prefix) + b64_len
            num_chunks   = (data_uri_len + CHUNK_SIZE - 1) // CHUNK_SIZE

            def _chunk_at(ci):
                return _data_uri_chunk_at(ci, prefix, b64_path, b64_len)

            chain_total = _get_video_total_chunks_on_chain(w3_local, active_address, repair_video_id)
            print(f"[repair] video {repair_video_id}: found {len(present_set)}/{num_chunks} chunks on-chain after adaptive scan (chain totalChunks={chain_total})")
            if num_chunks != chain_total:
                raise Exception(
                    f'File produces {num_chunks} chunks but video {repair_video_id} expects '
                    f'{chain_total} on-chain. Wrong file or wrong video ID?'
                )

            missing_indices = sorted(i for i in (set(range(num_chunks)) - present_set) if i < chain_total)
            job['total']         = len(missing_indices)
            job['missing_count'] = len(missing_indices)
            job['present_count'] = num_chunks - len(missing_indices)
            job['total_chunks']  = num_chunks
            job['videoId']       = repair_video_id

            if not missing_indices:
                job['status'] = 'complete'
                print(f"[repair] job {job_id}: all {num_chunks} chunks present — nothing to repair")
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                return

            print(f"[repair] job {job_id}: {len(missing_indices)} missing chunks for video {repair_video_id}")

            # Pre-flight: relay wallet must have enough LCAI for gas
            relay_bal_wei = w3_local.eth.get_balance(relay_acct.address)
            gas_price_est = int(w3_local.eth.gas_price * 1.2)
            cost_per_chunk = 12_000_000 * gas_price_est
            total_cost_wei = cost_per_chunk * len(missing_indices)
            bal_lcai  = float(w3_local.from_wei(relay_bal_wei, 'ether'))
            need_lcai = float(w3_local.from_wei(total_cost_wei, 'ether'))
            affordable = relay_bal_wei // cost_per_chunk if cost_per_chunk else 0
            print(f"[repair] relay balance {bal_lcai:.4f} LCAI — need ~{need_lcai:.2f} LCAI for {len(missing_indices)} chunks (~{affordable} affordable)")
            if affordable < 1:
                raise Exception(
                    f'Relay wallet has only {bal_lcai:.4f} LCAI but repair needs ~{need_lcai:.2f} LCAI gas '
                    f'for {len(missing_indices)} chunks. Fund {relay_acct.address} with LCAI and retry.'
                )

            job['phase'] = 'uploading'
            _do_repair_upload(job_id, repair_video_id, _chunk_at, missing_indices, active_address)
            for path in (tmp_path, b64_path):
                try:
                    os.unlink(path)
                except Exception:
                    pass
            return

        # ── NORMAL UPLOAD MODE ────────────────────────────────────────────────
        with open(tmp_path, 'rb') as f:
            raw = f.read()
        fn = job.get('file_name', 'video.mp4').lower()
        if fn.endswith('.mp4'):
            mime = 'video/mp4'
        elif fn.endswith('.mov'):
            mime = 'video/quicktime'
        elif fn.endswith('.webm'):
            mime = 'video/webm'
        elif fn.endswith('.gif'):
            mime = 'image/gif'
        else:
            mime = 'video/mp4'
        data_uri = 'data:' + mime + ';base64,' + base64.b64encode(raw).decode('ascii')
        del raw
        chunks = [data_uri[i:i+CHUNK_SIZE] for i in range(0, len(data_uri), CHUNK_SIZE)]

        job['total']  = len(chunks)
        job['status'] = 'initializing'

        # ── initVideoFor ──────────────────────────────────────────────────────
        user_wallet = job['wallet']
        title       = job['title']
        description = job['description']
        category    = job['category']

        with _nonce_lock:
            nonce = w3_local.eth.get_transaction_count(relay_acct.address, 'pending')
            tx = contract.functions.initVideoFor(
                Web3.to_checksum_address(user_wallet), title, description, category, len(chunks)
            ).build_transaction({
                'from':     relay_acct.address,
                'nonce':    nonce,
                'gas':      300_000,
                'gasPrice': w3_local.eth.gas_price,
                'chainId':  CHAIN_ID,
            })
            signed  = w3_local.eth.account.sign_transaction(tx, RELAY_PRIVATE_KEY)
            tx_hash = w3_local.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3_local.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        # Parse videoId from VideoCreated event
        ct_with_events = w3_local.eth.contract(
            address=Web3.to_checksum_address(active_address),
            abi=LIGHTTUBE_V2_ABI
        )
        logs     = ct_with_events.events.VideoCreated().process_receipt(receipt)
        video_id = int(logs[0]['args']['videoId'])
        job['videoId'] = video_id
        job['status']  = 'uploading'
        nonce += 1

        # Save thumbnail immediately after we have the videoId (non-fatal if it fails)
        thumbnail_b64 = job.get('thumbnail')
        if thumbnail_b64:
            try:
                prefix   = "v3" if LIGHTTUBE_V3_ADDRESS else "v2"
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

        # ── addVideoChunkFor × N (adaptive parallel batches) — identical to _do_lt_upload ──
        _MAX_BATCH = 25
        _MIN_BATCH = 8
        batch_size = max(CHUNK_BATCH_SIZE, 15)
        chunk_idx  = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_BATCH) as pool:
            while chunk_idx < len(chunks):
                batch          = chunks[chunk_idx : chunk_idx + batch_size]
                gas_price      = int(w3_local.eth.gas_price * 1.2)
                future_map     = {}
                had_real_error = False

                for j, chunk in enumerate(batch):
                    ci = chunk_idx + j
                    cn = nonce + j
                    f  = pool.submit(_send_one_chunk_tx, video_id, ci, chunk, cn, gas_price, active_address)
                    future_map[f] = (ci, cn, chunk)

                for f in concurrent.futures.as_completed(future_map):
                    ci, cn, chunk = future_map[f]
                    try:
                        f.result()
                    except Exception as e:
                        err_str = str(e).lower()
                        if 'nonce too low' in err_str or 'already known' in err_str:
                            print(f"[WARNING] chunk {ci} got 'nonce too low' — may have been dropped by concurrent job. Marking as done.")
                        else:
                            had_real_error = True
                            _send_one_chunk_tx(video_id, ci, chunk, cn, int(w3_local.eth.gas_price * 1.2), active_address)
                    job['progress'] += 1

                nonce     += len(batch)
                chunk_idx += len(batch)

                if had_real_error:
                    batch_size = max(batch_size - 5, _MIN_BATCH)
                    print(f"[chunked-upload] batch error — backing off to {batch_size} chunks/batch")
                else:
                    batch_size = min(batch_size + 3, _MAX_BATCH)
                    print(f"[chunked-upload] clean batch — stepping up to {batch_size} chunks/batch")

        job['status'] = 'complete'
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    except Exception as e:
        job['status'] = 'error'
        job['error']  = str(e)
        print(f"Chunked LightTube upload error [{job_id}]: {e}")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _do_repair_upload(job_id, video_id, chunk_source, missing_indices, active_address):
    """
    Background thread: submit only the missing chunks for a video repair job.
    chunk_source is either a list of chunk strings or a callable chunk_source(ci).
    """
    job = lt_upload_jobs[job_id]
    try:
        with _relay_job_lock:
            job['status']   = 'uploading'
            job['phase']    = 'uploading'
            job['repaired'] = 0
            job['failed']   = 0
            relay_acct = Account.from_key(RELAY_PRIVATE_KEY)
            w3_local   = Web3(Web3.HTTPProvider(RPC_URL))
            contract   = w3_local.eth.contract(
                address=Web3.to_checksum_address(active_address),
                abi=LIGHTTUBE_V2_ABI,
            )
            chain_total = _get_video_total_chunks_on_chain(w3_local, active_address, video_id)

            job['phase'] = 'flushing'
            flushed = _flush_stuck_relay_nonces(w3_local, relay_acct)
            if flushed:
                print(f"[repair] job {job_id}: cleared {flushed} stuck relay nonce(s) before upload")
            job['phase'] = 'uploading'

            def _get_chunk(ci):
                if callable(chunk_source):
                    return chunk_source(ci)
                return chunk_source[ci]

            for ci in missing_indices:
                if ci >= chain_total:
                    raise Exception(
                        f'Chunk {ci} is out of range for video {video_id} (totalChunks={chain_total}). '
                        f'Wrong video ID?'
                    )
                chunk_data = _get_chunk(ci)
                try:
                    contract.functions.addVideoChunkFor(video_id, ci, chunk_data).call(
                        {'from': relay_acct.address}
                    )
                except Exception as sim_err:
                    raise Exception(
                        f'Chunk {ci} would revert on-chain for video {video_id}: {sim_err}'
                    ) from sim_err

                with _nonce_lock:
                    # Use confirmed nonce — never stack txs in mempool when the chain is slow.
                    nonce     = w3_local.eth.get_transaction_count(relay_acct.address, 'latest')
                    gas_price = int(w3_local.eth.gas_price * 3)
                    receipt   = _send_one_chunk_tx(
                        video_id, ci, chunk_data, nonce, gas_price, active_address,
                        receipt_timeout=900,
                    )
                if receipt.get('status') != 1:
                    job['failed'] = job.get('failed', 0) + 1
                    raise Exception(f'Chunk {ci} transaction reverted on-chain')
                job['repaired'] = job.get('repaired', 0) + 1
                job['progress'] = job.get('progress', 0) + 1
                if job['progress'] % 25 == 0:
                    print(f"[repair] job {job_id}: {job['progress']}/{len(missing_indices)} chunks stored for video {video_id}")

            job['status'] = 'complete'
            print(f"[repair] job {job_id} complete — repaired {job['repaired']} chunks for video {video_id}")
    except Exception as e:
        job['status'] = 'error'
        job['error']  = str(e)
        print(f"Repair upload error [{job_id}]: {e}")



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
    """Load hidden video IDs — merged from three sources:
    1. Disk file  (fast, resets on redeploy if volume is lost)
    2. Env var seed (survives redeploys, set manually in Railway)
    3. GitHub permanent list (survives everything, set by admin moderation actions)
    """
    try:
        os.makedirs(os.path.dirname(LIGHTTUBE_HIDDEN_FILE), exist_ok=True)
        with open(LIGHTTUBE_HIDDEN_FILE, 'r') as f:
            from_disk = set(str(x) for x in json.load(f))
    except Exception:
        from_disk = set()
    return from_disk | LIGHTTUBE_HIDDEN_SEED | get_permanent_hidden()

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
        'status':              'ok',
        'relay_address':       relay.address,
        'relay_balance':       str(balance_lcai) + ' LCAI',
        'relay_fee_lcai':      current_fee_lcai(balance_lcai),
        'v3_contract':         V3_CONTRACT_ADDRESS,
        'lighttube_v3':        LIGHTTUBE_V3_ADDRESS or None,
        'lighttube_v2':        LIGHTTUBE_V2_ADDRESS or None,
        'lighttube_scan_fix':  'adaptive-50k-subdivide',
        'lighttube_repair_fix': 'latest-nonce-900s-wait',
        'chain_id':            CHAIN_ID,
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
        'fee_lcai':     BASE_FEE_LCAI,
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

@app.route('/api/lighttube/permanent-remove', methods=['POST'])
def lighttube_permanent_remove():
    """
    Admin — permanently remove a video. Writes to GitHub so it survives all future redeploys.
    Cannot be restored via the UI (use Railway env var LIGHTTUBE_HIDDEN_IDS to manually override).
    Body: { videoId: "v3-2", adminKey: "secret" }
    """
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured on server'}), 500
    body = request.get_json()
    if not body:
        return jsonify({'error': 'No JSON body'}), 400
    if body.get('adminKey') != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    video_id = body.get('videoId')
    if not video_id:
        return jsonify({'error': 'videoId required'}), 400
    add_permanent_hidden(str(video_id))
    print(f"[MODERATION] Permanently removed video {video_id}")
    return jsonify({'success': True, 'message': f'Video {video_id} permanently removed and written to GitHub'})

@app.route('/api/lighttube/ban-wallet', methods=['POST'])
def lighttube_ban_wallet():
    """
    Admin — permanently ban a wallet from uploading + optionally permanently remove their video.
    Ban list written to GitHub — survives all future redeploys.
    Body: { wallet: "0x...", videoId: "v3-2" (optional), adminKey: "secret" }
    """
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured on server'}), 500
    body = request.get_json()
    if not body:
        return jsonify({'error': 'No JSON body'}), 400
    if body.get('adminKey') != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    wallet   = body.get('wallet', '').strip().lower()
    video_id = body.get('videoId')
    if not wallet:
        return jsonify({'error': 'wallet required'}), 400
    add_banned_wallet_gh(wallet)
    if video_id:
        add_permanent_hidden(str(video_id))
    print(f"[MODERATION] Banned wallet {wallet[:10]}... video={video_id or 'none'}")
    return jsonify({
        'success': True,
        'message': f'Wallet banned forever. Video {"permanently removed" if video_id else "not changed"}.'
    })

@app.route('/api/lighttube/moderation-lists', methods=['GET'])
def lighttube_moderation_lists():
    """
    Admin — return permanent hidden IDs and banned wallets. Admin key required.
    """
    admin_key = request.args.get('adminKey', '')
    if not LIGHTTUBE_ADMIN_KEY or admin_key != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({
        'permanent_hidden': list(get_permanent_hidden()),
        'banned_wallets':   list(get_banned_wallets_gh()),
    })

# ─── OrcaMint moderation (GitHub-backed, survives all redeploys) ──────────────

ORCAMINT_GITHUB_REPO   = "Keiko-Dev-LCAI/orcamint"
ORCAMINT_GITHUB_BRANCH = "main"

_om_perm_hidden_cache    = None   # set of permanently hidden token IDs
_om_banned_wallets_cache = None   # set of permanently banned wallet addresses

def _github_read_json_repo(repo, branch, path):
    """Read a JSON file from any GitHub repo. Returns parsed data or None on error."""
    if not GITHUB_TOKEN:
        return None
    api_url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            content = base64.b64decode(data["content"].replace("\n", "")).decode()
            return json.loads(content)
    except Exception:
        return None

def _github_write_json_repo(repo, branch, path, data, message):
    """Write a JSON file to any GitHub repo. Creates or updates."""
    if not GITHUB_TOKEN:
        return False
    api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json", "Content-Type": "application/json"}
    sha = None
    try:
        req = urllib.request.Request(f"{api_url}?ref={branch}", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            sha = json.loads(r.read()).get("sha")
    except Exception:
        pass
    content_b64 = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
    body = {"message": message, "content": content_b64, "branch": branch}
    if sha:
        body["sha"] = sha
    try:
        req = urllib.request.Request(api_url, data=json.dumps(body).encode(), headers=headers, method="PUT")
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        return True
    except Exception as e:
        print(f"Warning: could not write {path} to {repo}: {e}")
        return False

def get_om_permanent_hidden():
    """Return OrcaMint permanent hidden set. Loaded once per process startup."""
    global _om_perm_hidden_cache
    if _om_perm_hidden_cache is None:
        from_github = _github_read_json_repo(ORCAMINT_GITHUB_REPO, ORCAMINT_GITHUB_BRANCH, "moderation/permanent_hidden.json") or []
        _om_perm_hidden_cache = set(str(x) for x in from_github)
    return _om_perm_hidden_cache

def add_om_permanent_hidden(token_id):
    """Permanently hide an OrcaMint NFT by token ID. Writes to GitHub in background."""
    perm = get_om_permanent_hidden()
    perm.add(str(token_id))
    snapshot = list(perm)
    threading.Thread(
        target=_github_write_json_repo,
        args=(ORCAMINT_GITHUB_REPO, ORCAMINT_GITHUB_BRANCH, "moderation/permanent_hidden.json", snapshot, f"Permanently hide token {token_id}"),
        daemon=True
    ).start()

def get_om_banned_wallets():
    """Return OrcaMint banned wallets set. Loaded once per process startup."""
    global _om_banned_wallets_cache
    if _om_banned_wallets_cache is None:
        from_github = _github_read_json_repo(ORCAMINT_GITHUB_REPO, ORCAMINT_GITHUB_BRANCH, "moderation/banned_wallets.json") or []
        _om_banned_wallets_cache = set(w.lower() for w in from_github)
    return _om_banned_wallets_cache

def add_om_banned_wallet(wallet):
    """Permanently ban a wallet from OrcaMint. Writes to GitHub in background."""
    banned = get_om_banned_wallets()
    banned.add(wallet.lower())
    snapshot = list(banned)
    threading.Thread(
        target=_github_write_json_repo,
        args=(ORCAMINT_GITHUB_REPO, ORCAMINT_GITHUB_BRANCH, "moderation/banned_wallets.json", snapshot, f"Ban wallet {wallet[:10]}"),
        daemon=True
    ).start()

@app.route('/api/orcamint/permanent-remove', methods=['POST'])
def orcamint_permanent_remove():
    """
    Admin — permanently remove an OrcaMint NFT. Writes to GitHub so it survives all future redeploys.
    Body: { tokenId: "5", adminKey: "secret" }
    """
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured on server'}), 500
    body = request.get_json()
    if not body:
        return jsonify({'error': 'No JSON body'}), 400
    if body.get('adminKey') != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    token_id = body.get('tokenId')
    if token_id is None:
        return jsonify({'error': 'tokenId required'}), 400
    add_om_permanent_hidden(str(token_id))
    print(f"[ORCAMINT MOD] Permanently removed token {token_id}")
    return jsonify({'success': True, 'message': f'Token {token_id} permanently removed and written to GitHub'})

@app.route('/api/orcamint/ban-wallet', methods=['POST'])
def orcamint_ban_wallet():
    """
    Admin — permanently ban a wallet + optionally permanently remove their token.
    Body: { wallet: "0x...", tokenId: "5" (optional), adminKey: "secret" }
    """
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured on server'}), 500
    body = request.get_json()
    if not body:
        return jsonify({'error': 'No JSON body'}), 400
    if body.get('adminKey') != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    wallet   = body.get('wallet', '').strip().lower()
    token_id = body.get('tokenId')
    if not wallet:
        return jsonify({'error': 'wallet required'}), 400
    add_om_banned_wallet(wallet)
    if token_id is not None:
        add_om_permanent_hidden(str(token_id))
    print(f"[ORCAMINT MOD] Banned wallet {wallet[:10]}... token={token_id or 'none'}")
    return jsonify({
        'success': True,
        'message': f'Wallet banned forever. Token {"permanently removed" if token_id is not None else "not changed"}.'
    })

@app.route('/api/orcamint/moderation-lists', methods=['GET'])
def orcamint_moderation_lists():
    """Admin — return permanent hidden token IDs and banned wallets. Admin key required."""
    admin_key = request.args.get('adminKey', '')
    if not LIGHTTUBE_ADMIN_KEY or admin_key != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({
        'permanent_hidden': list(get_om_permanent_hidden()),
        'banned_wallets':   list(get_om_banned_wallets()),
    })

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
    for field in ('title', 'description', 'category', 'uploader'):
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

@app.route('/api/orcavault/report', methods=['POST'])
def orcavault_report():
    """Anyone can report a memory. Stores report for admin review."""
    body = request.get_json() or {}
    memory_id = body.get('memoryId', 'unknown')
    vault_id  = body.get('vaultId', 'unknown')
    reason    = body.get('reason', 'No reason given')
    reporter  = body.get('reporter', 'anonymous')
    report_entry = {
        'memoryId': memory_id,
        'vaultId':  vault_id,
        'reason':   reason,
        'reporter': reporter,
        'timestamp': time.time(),
    }
    # Append to reports file
    reports_file = os.environ.get("ORCAVAULT_REPORTS_FILE", "/data/orcavault_reports.json")
    try:
        try:
            with open(reports_file, 'r') as f:
                reports = json.load(f)
        except Exception:
            reports = []
        reports.append(report_entry)
        with open(reports_file, 'w') as f:
            json.dump(reports, f, indent=2)
    except Exception as e:
        print(f"Warning: could not save report: {e}")
    print(f"OrcaVault report: vault={vault_id} memory={memory_id} reason={reason} reporter={reporter}")
    return jsonify({'success': True})

@app.route('/api/orcavault/creator-delete', methods=['POST'])
def orcavault_creator_delete():
    """Creator self-delete — wallet must sign a message to prove ownership.
    Body: { memoryId: "N", wallet: "0x...", signature: "0x...", message: "..." }"""
    body = request.get_json()
    if not body:
        return jsonify({'error': 'No JSON body'}), 400
    memory_id = body.get('memoryId')
    wallet_addr = body.get('wallet', '').strip().lower()
    signature   = body.get('signature', '')
    message     = body.get('message', '')
    if not memory_id or not wallet_addr or not signature or not message:
        return jsonify({'error': 'memoryId, wallet, signature, and message are required'}), 400
    # Verify signature — recover signer from the signed message
    try:
        msg_hash = encode_defunct(text=message)
        recovered = Account.recover_message(msg_hash, signature=signature)
        if recovered.lower() != wallet_addr:
            return jsonify({'error': 'Signature mismatch — could not verify wallet ownership'}), 401
    except Exception as e:
        return jsonify({'error': f'Signature verification failed: {str(e)}'}), 401
    # Add to hidden list
    hidden = load_orcavault_hidden()
    hidden.add(str(memory_id))
    save_orcavault_hidden(hidden)
    return jsonify({'success': True})

# ═══════════════════════════════════════════════════════════════════════════════
#  LIGHTTUNES
# ═══════════════════════════════════════════════════════════════════════════════

LIGHTTUNES_V1_ADDRESS    = os.environ.get("LIGHTTUNES_V1_ADDRESS", "")
LIGHTTUNES_HIDDEN_FILE   = os.environ.get("LIGHTTUNES_HIDDEN_FILE", "/data/lighttunes_hidden.json")
LIGHTTUNES_OVERRIDES_FILE= os.environ.get("LIGHTTUNES_OVERRIDES_FILE", "/data/lighttunes_overrides.json")
LIGHTTUNES_HIDDEN_SEED   = {s.strip() for s in os.environ.get("LIGHTTUNES_HIDDEN_IDS", "").split(",") if s.strip()}
LIGHTTUNES_GITHUB_REPO   = "Keiko-Dev-LCAI/lighttunes"
LIGHTTUNES_GITHUB_BRANCH = "main"
LIGHTTUNES_THUMBS_DIR    = os.environ.get("LIGHTTUNES_THUMBS_DIR", "/data/lt_thumbs2")
LIGHTTUNES_FEE_USD       = float(os.environ.get("LIGHTTUNES_FEE_USD", "0.50"))
LIGHTTUNES_FEE_WALLET    = os.environ.get("LIGHTTUNES_FEE_WALLET", "").strip().lower()
LIGHTTUNES_USED_TX_FILE  = os.environ.get("LIGHTTUNES_USED_TX_FILE", "/data/lighttunes_used_tx.json")

# ─── LCAI price feed (cached 5 min) ─────────────────────────────────────────
_lt_price_cache = {'price': None, 'ts': 0}

def _get_lcai_price_usd():
    """Fetch LCAI/USD price. Tries CoinGecko then DexScreener; caches 5 min."""
    global _lt_price_cache
    import urllib.request as _ur
    if time.time() - _lt_price_cache['ts'] < 300 and _lt_price_cache['price']:
        return _lt_price_cache['price']
    for cg_id in ('lightchain-ai', 'lightchain'):
        try:
            req = _ur.Request(
                f'https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd',
                headers={'Accept': 'application/json', 'User-Agent': 'LightTunes/1.0'})
            with _ur.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            price = data.get(cg_id, {}).get('usd')
            if price and float(price) > 0:
                _lt_price_cache = {'price': float(price), 'ts': time.time()}
                return float(price)
        except Exception:
            pass
    try:
        req = _ur.Request('https://api.dexscreener.com/latest/dex/search?q=LCAI',
                          headers={'Accept': 'application/json', 'User-Agent': 'LightTunes/1.0'})
        with _ur.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        for pair in data.get('pairs', []):
            if pair.get('baseToken', {}).get('symbol', '').upper() == 'LCAI':
                price = float(pair.get('priceUsd', 0))
                if price > 0:
                    _lt_price_cache = {'price': price, 'ts': time.time()}
                    return price
    except Exception:
        pass
    if _lt_price_cache['price']:
        return _lt_price_cache['price']
    return 0.004  # last-resort fallback

# ─── Used-tx tracking (prevents replay attacks) ──────────────────────────────
def _load_used_tx():
    try:
        with open(LIGHTTUNES_USED_TX_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()

def _save_used_tx(hashes: set):
    try:
        os.makedirs(os.path.dirname(LIGHTTUNES_USED_TX_FILE), exist_ok=True)
        with open(LIGHTTUNES_USED_TX_FILE, 'w') as f:
            json.dump(list(hashes), f)
    except Exception as e:
        print(f"Warning: could not save used_tx: {e}")

# ─── LightTunesV1 ABI (relay functions only) ──────────────────────────────────
LIGHTTUNES_ABI = [
    {"inputs":[{"name":"uploader","type":"address"},{"name":"title","type":"string"},
               {"name":"artist","type":"string"},{"name":"genre","type":"string"},
               {"name":"description","type":"string"},{"name":"isPublic","type":"bool"},
               {"name":"totalChunks","type":"uint256"}],
     "name":"initSongFor","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"songId","type":"uint256"},{"name":"chunkIndex","type":"uint256"},
               {"name":"chunkData","type":"string"}],
     "name":"addSongChunkFor","outputs":[],
     "stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"relay","type":"address"}],
     "name":"setRelayWallet","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"anonymous":False,"inputs":[
        {"indexed":True,"name":"songId","type":"uint256"},
        {"indexed":True,"name":"uploader","type":"address"},
        {"indexed":False,"name":"title","type":"string"},
        {"indexed":False,"name":"artist","type":"string"},
        {"indexed":False,"name":"genre","type":"string"},
        {"indexed":False,"name":"description","type":"string"},
        {"indexed":False,"name":"isPublic","type":"bool"},
        {"indexed":False,"name":"totalChunks","type":"uint256"},
        {"indexed":False,"name":"timestamp","type":"uint256"}],
     "name":"SongCreated","type":"event"},
    {"anonymous":False,"inputs":[
        {"indexed":True,"name":"songId","type":"uint256"},
        {"indexed":True,"name":"chunkIndex","type":"uint256"},
        {"indexed":False,"name":"totalChunks","type":"uint256"},
        {"indexed":False,"name":"chunkData","type":"string"}],
     "name":"SongChunkStored","type":"event"},
    {"anonymous":False,"inputs":[
        {"indexed":True,"name":"songId","type":"uint256"},
        {"indexed":True,"name":"uploader","type":"address"},
        {"indexed":False,"name":"title","type":"string"},
        {"indexed":False,"name":"artist","type":"string"},
        {"indexed":False,"name":"genre","type":"string"},
        {"indexed":False,"name":"description","type":"string"},
        {"indexed":False,"name":"isPublic","type":"bool"},
        {"indexed":False,"name":"timestamp","type":"uint256"}],
     "name":"SongMetadataUpdated","type":"event"},
]

# ─── LightTunes in-memory job tracker ─────────────────────────────────────────
lt_song_jobs = {}   # {jobId: {status, progress, total, songId, error}}

# ─── LightTunes permanent hidden (GitHub-backed) ──────────────────────────────
_lt_perm_hidden_cache    = None
_lt_banned_wallets_cache = None

def get_lt_permanent_hidden():
    global _lt_perm_hidden_cache
    if _lt_perm_hidden_cache is None:
        _lt_perm_hidden_cache = set(str(x) for x in (_github_read_json_repo(LIGHTTUNES_GITHUB_REPO, LIGHTTUNES_GITHUB_BRANCH, "moderation/permanent_hidden.json") or []))
    return _lt_perm_hidden_cache

def add_lt_permanent_hidden(song_id):
    perm = get_lt_permanent_hidden()
    perm.add(str(song_id))
    snapshot = sorted(perm)
    threading.Thread(target=_github_write_json_repo,
        args=(LIGHTTUNES_GITHUB_REPO, LIGHTTUNES_GITHUB_BRANCH,
              "moderation/permanent_hidden.json", snapshot, f"Permanently hide {song_id}"),
        daemon=True).start()

def get_lt_banned_wallets():
    global _lt_banned_wallets_cache
    if _lt_banned_wallets_cache is None:
        _lt_banned_wallets_cache = set(str(x).lower() for x in (_github_read_json_repo(LIGHTTUNES_GITHUB_REPO, LIGHTTUNES_GITHUB_BRANCH, "moderation/banned_wallets.json") or []))
    return _lt_banned_wallets_cache

def add_lt_banned_wallet(wallet):
    bans = get_lt_banned_wallets()
    bans.add(wallet.lower())
    snapshot = sorted(bans)
    threading.Thread(target=_github_write_json_repo,
        args=(LIGHTTUNES_GITHUB_REPO, LIGHTTUNES_GITHUB_BRANCH,
              "moderation/banned_wallets.json", snapshot, f"Ban wallet {wallet[:10]}"),
        daemon=True).start()

# ─── LightTunes hidden file helpers ───────────────────────────────────────────
def load_lt_hidden():
    try:
        os.makedirs(os.path.dirname(LIGHTTUNES_HIDDEN_FILE), exist_ok=True)
        with open(LIGHTTUNES_HIDDEN_FILE, 'r') as f:
            from_disk = set(str(x) for x in json.load(f))
    except Exception:
        from_disk = set()
    return from_disk | LIGHTTUNES_HIDDEN_SEED | get_lt_permanent_hidden()

def save_lt_hidden(hidden):
    try:
        os.makedirs(os.path.dirname(LIGHTTUNES_HIDDEN_FILE), exist_ok=True)
        with open(LIGHTTUNES_HIDDEN_FILE, 'w') as f:
            json.dump(sorted(hidden), f)
    except Exception as e:
        print(f"Warning: could not save lt_hidden: {e}")

def load_lt_overrides():
    try:
        with open(LIGHTTUNES_OVERRIDES_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def save_lt_overrides(overrides):
    try:
        os.makedirs(os.path.dirname(LIGHTTUNES_OVERRIDES_FILE), exist_ok=True)
        with open(LIGHTTUNES_OVERRIDES_FILE, 'w') as f:
            json.dump(overrides, f, indent=2)
    except Exception as e:
        print(f"Warning: could not save lt_overrides: {e}")

# ─── LightTunes chunk upload helpers ──────────────────────────────────────────
def _send_one_lt_chunk_tx(song_id, chunk_index, chunk_data, nonce, gas_price, contract_address):
    w3t = _borrow_w3()
    try:
        ct  = w3t.eth.contract(address=Web3.to_checksum_address(contract_address), abi=LIGHTTUNES_ABI)
        relay_acct = Account.from_key(RELAY_PRIVATE_KEY)
        tx = ct.functions.addSongChunkFor(song_id, chunk_index, chunk_data).build_transaction({
            'from': relay_acct.address, 'nonce': nonce, 'gas': 12_000_000,
            'gasPrice': gas_price, 'chainId': CHAIN_ID,
        })
        signed  = relay_acct.sign_transaction(tx)
        tx_hash = w3t.eth.send_raw_transaction(signed.raw_transaction)
        return w3t.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    finally:
        _return_w3(w3t)

def _do_lt_upload(job_id, user_wallet, title, artist, genre, description, is_public, data_uri, thumbnail_b64=None):
    """Background thread: chunk and store a song via LightTunes relay."""
    job = lt_song_jobs[job_id]
    try:
        if not LIGHTTUNES_V1_ADDRESS:
            raise Exception("LIGHTTUNES_V1_ADDRESS not configured in Railway")
        w3 = Web3(Web3.HTTPProvider(RPC_URL))
        relay_acct = Account.from_key(RELAY_PRIVATE_KEY)
        contract   = w3.eth.contract(address=Web3.to_checksum_address(LIGHTTUNES_V1_ADDRESS), abi=LIGHTTUNES_ABI)
        chunks = [data_uri[i:i+CHUNK_SIZE] for i in range(0, len(data_uri), CHUNK_SIZE)]
        job['total'] = len(chunks)

        # ── initSongFor ───────────────────────────────────────────────────────
        job['status'] = 'initializing'
        nonce = w3.eth.get_transaction_count(relay_acct.address, 'pending')
        gas_price = int(w3.eth.gas_price * 1.2)
        tx = contract.functions.initSongFor(
            Web3.to_checksum_address(user_wallet), title, artist, genre, description, is_public, len(chunks)
        ).build_transaction({'from': relay_acct.address, 'nonce': nonce, 'gas': 500_000,
                              'gasPrice': gas_price, 'chainId': CHAIN_ID})
        signed  = relay_acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        song_id_raw = contract.events.SongCreated().process_receipt(receipt)
        song_id = int(song_id_raw[0]['args']['songId']) if song_id_raw else None
        if song_id is None:
            raise Exception("Could not determine songId from initSongFor receipt")
        job['songId'] = song_id
        nonce += 1

        # ── Save thumbnail to GitHub (LightTunes repo) ───────────────────────
        if thumbnail_b64:
            try:
                import base64 as _b64
                b64_content = thumbnail_b64.split(',', 1)[1] if ',' in thumbnail_b64 else thumbnail_b64
                path    = f"thumbs/v1_{song_id}.jpg"
                api_url = f"https://api.github.com/repos/{LIGHTTUNES_GITHUB_REPO}/contents/{path}"
                headers = {
                    "Authorization": f"token {GITHUB_TOKEN}",
                    "Accept":        "application/vnd.github.v3+json",
                    "Content-Type":  "application/json",
                }
                # get existing SHA for update
                sha = None
                try:
                    req = urllib.request.Request(api_url, headers=headers)
                    with urllib.request.urlopen(req, timeout=10) as r:
                        sha = json.loads(r.read()).get("sha")
                except Exception:
                    pass
                body = {"message": f"LightTunes thumbnail song {song_id}", "content": b64_content, "branch": LIGHTTUNES_GITHUB_BRANCH}
                if sha:
                    body["sha"] = sha
                req = urllib.request.Request(api_url, data=json.dumps(body).encode(), headers=headers, method="PUT")
                with urllib.request.urlopen(req, timeout=20) as r:
                    r.read()
                print(f"LightTunes thumbnail saved: {path}")
            except Exception as te:
                print(f"LightTunes thumbnail save failed: {te}")

        # ── addSongChunkFor × N ───────────────────────────────────────────────
        _MAX_BATCH = 25
        _MIN_BATCH = 8
        job['status'] = 'uploading'
        batch_size = max(CHUNK_BATCH_SIZE, 15)
        chunk_idx  = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_BATCH) as pool:
            while chunk_idx < len(chunks):
                batch      = chunks[chunk_idx : chunk_idx + batch_size]
                gas_price  = int(w3.eth.gas_price * 1.2)
                future_map = {}
                had_real_error = False
                for j, chunk in enumerate(batch):
                    ci = chunk_idx + j
                    cn = nonce + j
                    f  = pool.submit(_send_one_lt_chunk_tx, song_id, ci, chunk, cn, gas_price, LIGHTTUNES_V1_ADDRESS)
                    future_map[f] = (ci, cn, chunk)
                for f in concurrent.futures.as_completed(future_map):
                    ci, cn, chunk = future_map[f]
                    try:
                        f.result()
                    except Exception as e:
                        err_str = str(e).lower()
                        if 'nonce too low' in err_str or 'already known' in err_str:
                            pass
                        else:
                            had_real_error = True
                            _send_one_lt_chunk_tx(song_id, ci, chunk, cn, int(w3.eth.gas_price * 1.2), LIGHTTUNES_V1_ADDRESS)
                    job['progress'] += 1
                nonce     += len(batch)
                chunk_idx += len(batch)
                if had_real_error:
                    batch_size = max(batch_size - 5, _MIN_BATCH)
                else:
                    batch_size = min(batch_size + 3, _MAX_BATCH)

        job['status'] = 'complete'
        print(f"LightTunes upload complete [{job_id}]: songId={song_id}, chunks={len(chunks)}")
    except Exception as e:
        print(f"LightTunes upload error [{job_id}]: {e}")
        job['status'] = 'error'
        job['error']  = str(e) or repr(e)

# ─── LightTunes API endpoints ─────────────────────────────────────────────────

@app.route('/api/lighttunes/fee', methods=['GET'])
def lighttunes_fee():
    """Return current upload fee in LCAI and USD."""
    price = _get_lcai_price_usd()
    fee_lcai = round(LIGHTTUNES_FEE_USD / price, 2) if price > 0 else None
    return jsonify({
        'fee_usd':    LIGHTTUNES_FEE_USD,
        'fee_lcai':   fee_lcai,
        'lcai_price': price,
        'fee_wallet': LIGHTTUNES_FEE_WALLET,
    })

@app.route('/api/lighttunes/upload', methods=['POST'])
def lighttunes_upload():
    """Relay upload endpoint for LightTunes songs."""
    if not RELAY_PRIVATE_KEY:
        return jsonify({'error': 'Relay not configured'}), 500
    if LIGHTTUBE_MAINTENANCE:
        return jsonify({'error': 'LightTunes is in maintenance mode'}), 503

    user_wallet   = request.form.get('wallet', '').strip()
    signature     = request.form.get('signature', '').strip()
    title         = request.form.get('title', '').strip() or 'Untitled'
    artist        = request.form.get('artist', '').strip()
    genre         = request.form.get('genre', '').strip() or 'Other'
    description   = request.form.get('description', '').strip()
    is_public     = request.form.get('isPublic', 'true').lower() in ('true', '1', 'yes')
    thumbnail_b64 = request.form.get('thumbnail', '')
    payment_tx    = request.form.get('paymentTxHash', '').strip()

    if not user_wallet:
        return jsonify({'error': 'wallet required'}), 400

    # Verify wallet signature
    try:
        msg = encode_defunct(text=f"LightTunes upload: {user_wallet.lower()}")
        recovered = Account.recover_message(msg, signature=signature)
        if recovered.lower() != user_wallet.lower():
            return jsonify({'error': 'Signature verification failed'}), 401
    except Exception as e:
        return jsonify({'error': f'Signature error: {e}'}), 401

    # Check banned wallet
    if user_wallet.lower() in get_lt_banned_wallets():
        return jsonify({'error': 'Wallet is banned from LightTunes'}), 403

    # ── Fee verification (skip for owner wallets) ────────────────────────────
    if user_wallet.lower() not in OWNER_WALLETS:
        if not LIGHTTUNES_FEE_WALLET:
            return jsonify({'error': 'Upload fee not configured — contact admin'}), 503
        if not payment_tx:
            return jsonify({'error': 'Upload fee required — paymentTxHash missing'}), 402

        # Check tx hasn't been used before
        used = _load_used_tx()
        if payment_tx.lower() in used:
            return jsonify({'error': 'This payment transaction has already been used'}), 400

        # Verify on-chain
        try:
            tx = w3.eth.get_transaction(payment_tx)
        except Exception:
            return jsonify({'error': 'Payment transaction not found on chain — wait a moment and retry'}), 404

        if tx['from'].lower() != user_wallet.lower():
            return jsonify({'error': 'Payment was not sent from your wallet'}), 400
        if tx.get('to', '').lower() != LIGHTTUNES_FEE_WALLET.lower():
            return jsonify({'error': 'Payment was not sent to the correct fee wallet'}), 400

        price    = _get_lcai_price_usd()
        required = Web3.to_wei(LIGHTTUNES_FEE_USD / price * 0.90, 'ether')  # 10% slippage tolerance
        if tx['value'] < required:
            paid = float(w3.from_wei(tx['value'], 'ether'))
            needed = round(LIGHTTUNES_FEE_USD / price, 2)
            return jsonify({'error': f'Payment too small: sent {paid:.4f} LCAI, required ~{needed} LCAI'}), 400

        # Mark tx as used
        used.add(payment_tx.lower())
        _save_used_tx(used)

    # Read audio file
    audio_file = request.files.get('audio')
    if not audio_file:
        return jsonify({'error': 'audio file required'}), 400

    import base64 as _b64
    audio_bytes = audio_file.read()
    mime_type   = audio_file.content_type or 'audio/mpeg'
    data_uri    = f"data:{mime_type};base64," + _b64.b64encode(audio_bytes).decode()

    job_id = str(uuid.uuid4())[:8]
    lt_song_jobs[job_id] = {'status': 'queued', 'progress': 0, 'total': 0, 'songId': None, 'error': None}
    threading.Thread(target=_do_lt_upload, daemon=True,
        args=(job_id, user_wallet, title, artist, genre, description, is_public, data_uri, thumbnail_b64)).start()
    return jsonify({'jobId': job_id})

@app.route('/api/lighttunes/upload-progress/<job_id>', methods=['GET'])
def lighttunes_upload_progress(job_id):
    job = lt_song_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)

@app.route('/api/lighttunes/hidden', methods=['GET'])
def lighttunes_get_hidden():
    hidden = load_lt_hidden()
    return jsonify({'hidden': list(hidden)})

@app.route('/api/lighttunes/hide', methods=['POST'])
def lighttunes_hide():
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured'}), 500
    body = request.get_json()
    if not body or body.get('adminKey', '') != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    song_id = body.get('songId')
    if song_id is None:
        return jsonify({'error': 'songId required'}), 400
    hidden = load_lt_hidden()
    hidden.add(str(song_id))
    save_lt_hidden(hidden)
    return jsonify({'success': True})

@app.route('/api/lighttunes/unhide', methods=['POST'])
def lighttunes_unhide():
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured'}), 500
    body = request.get_json()
    if not body or body.get('adminKey', '') != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    song_id = body.get('songId')
    if song_id is None:
        return jsonify({'error': 'songId required'}), 400
    hidden = load_lt_hidden()
    hidden.discard(str(song_id))
    save_lt_hidden(hidden)
    return jsonify({'success': True})

@app.route('/api/lighttunes/permanent-remove', methods=['POST'])
def lighttunes_permanent_remove():
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured'}), 500
    body = request.get_json()
    if not body or body.get('adminKey', '') != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    song_id = str(body.get('songId', ''))
    if not song_id:
        return jsonify({'error': 'songId required'}), 400
    add_lt_permanent_hidden(song_id)
    hidden = load_lt_hidden()
    hidden.add(song_id)
    save_lt_hidden(hidden)
    return jsonify({'success': True, 'message': f'Song {song_id} permanently removed and written to GitHub'})

@app.route('/api/lighttunes/ban-wallet', methods=['POST'])
def lighttunes_ban_wallet():
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured'}), 500
    body = request.get_json()
    if not body or body.get('adminKey', '') != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    wallet  = body.get('wallet', '').strip()
    song_id = str(body.get('songId', '')) if body.get('songId') else None
    if not wallet:
        return jsonify({'error': 'wallet required'}), 400
    add_lt_banned_wallet(wallet)
    if song_id:
        add_lt_permanent_hidden(song_id)
        hidden = load_lt_hidden()
        hidden.add(song_id)
        save_lt_hidden(hidden)
    return jsonify({'success': True, 'message': f'Wallet banned. Song {"removed" if song_id else "unchanged"}.'})

@app.route('/api/lighttunes/moderation-lists', methods=['GET'])
def lighttunes_moderation_lists():
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured'}), 500
    if request.args.get('adminKey', '') != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({
        'permanent_hidden': list(get_lt_permanent_hidden()),
        'banned_wallets':   list(get_lt_banned_wallets()),
    })

@app.route('/api/lighttunes/overrides', methods=['GET'])
def lighttunes_get_overrides():
    return jsonify(load_lt_overrides())

@app.route('/api/lighttunes/set-override', methods=['POST'])
def lighttunes_set_override():
    if not LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Admin not configured'}), 500
    body = request.get_json()
    if not body or body.get('adminKey', '') != LIGHTTUBE_ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    sid = str(body.get('songId', ''))
    if not sid:
        return jsonify({'error': 'songId required'}), 400
    overrides = load_lt_overrides()
    entry = overrides.get(sid, {})
    for field in ('title', 'artist', 'genre', 'description', 'uploader'):
        if field in body:
            entry[field] = body[field]
    if 'forceExplicit' in body:
        entry['forceExplicit'] = bool(body['forceExplicit'])
    overrides[sid] = entry
    save_lt_overrides(overrides)
    return jsonify({'success': True, 'songId': sid, 'override': entry})

@app.route('/api/lighttunes/thumbnail/<path:filename>', methods=['GET'])
def lighttunes_thumbnail(filename):
    """Serve thumbnails from disk if present."""
    thumb_path = os.path.join(LIGHTTUNES_THUMBS_DIR, filename)
    if os.path.exists(thumb_path):
        from flask import send_file
        return send_file(thumb_path, mimetype='image/jpeg')
    return '', 404

# ─── end LightTunes ───────────────────────────────────────────────────────────

# ─── AIVM Client ──────────────────────────────────────────────────────────────

_AIVM_ABI = [
    {
        "name": "createSession", "type": "function", "stateMutability": "payable",
        "inputs": [
            {"name": "paramsHash",      "type": "bytes32"},
            {"name": "worker",          "type": "address"},
            {"name": "encWorkerKey",    "type": "bytes"},
            {"name": "ephemeralPubKey", "type": "bytes"},
            {"name": "initState",       "type": "bytes"},
            {"name": "expiry",          "type": "uint256"},
        ],
        "outputs": [{"name": "sessionId", "type": "uint256"}],
    },
    {
        "name": "submitJob", "type": "function", "stateMutability": "payable",
        "inputs": [
            {"name": "sessionId",  "type": "uint256"},
            {"name": "promptHash", "type": "bytes32"},
        ],
        "outputs": [{"name": "jobId", "type": "uint256"}],
    },
    {
        "anonymous": False, "name": "SessionCreated", "type": "event",
        "inputs": [
            {"indexed": True,  "name": "sessionId",      "type": "uint256"},
            {"indexed": True,  "name": "user",            "type": "address"},
            {"indexed": True,  "name": "paramsHash",      "type": "bytes32"},
            {"indexed": False, "name": "worker",          "type": "address"},
            {"indexed": False, "name": "encWorkerKey",    "type": "bytes"},
            {"indexed": False, "name": "ephemeralPubKey", "type": "bytes"},
        ],
    },
    {
        "anonymous": False, "name": "JobSubmitted", "type": "event",
        "inputs": [
            {"indexed": True,  "name": "jobId",     "type": "uint256"},
            {"indexed": True,  "name": "sessionId", "type": "uint256"},
            {"indexed": False, "name": "worker",    "type": "address"},
        ],
    },
    {
        "anonymous": False, "name": "JobCompleted", "type": "event",
        "inputs": [
            {"indexed": True,  "name": "jobId",         "type": "uint256"},
            {"indexed": True,  "name": "worker",         "type": "address"},
            {"indexed": False, "name": "responseHash",   "type": "bytes32"},
            {"indexed": False, "name": "ciphertextHash", "type": "bytes32"},
        ],
    },
]


def _aivm_decode_pubkey(s):
    """Accept hex (with/without 0x) or base64; return 65-byte uncompressed P-256 point."""
    if isinstance(s, (bytes, bytearray)):
        return bytes(s)
    s = s.strip()
    if s.startswith('0x') or s.startswith('0X'):
        b = bytes.fromhex(s[2:])
    elif len(s) == 130 and all(c in '0123456789abcdefABCDEF' for c in s):
        b = bytes.fromhex(s)
    else:
        b = base64.b64decode(s)
    if len(b) != 65:
        raise ValueError(f"pubkey decode: expected 65 bytes, got {len(b)}")
    return b


def _aivm_ecdh_wrap(session_key: bytes, peer_pub_bytes: bytes) -> bytes:
    """ECDH-wrap session_key for peer P-256 pubkey. Returns ephemPub(65)||nonce(12)||ct||tag(16)."""
    from cryptography.hazmat.primitives.asymmetric.ec import (
        generate_private_key, ECDH, EllipticCurvePublicNumbers, SECP256R1
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.backends import default_backend

    x = int.from_bytes(peer_pub_bytes[1:33], 'big')
    y = int.from_bytes(peer_pub_bytes[33:65], 'big')
    peer_pub = EllipticCurvePublicNumbers(x, y, SECP256R1()).public_key(default_backend())

    ephem_priv = generate_private_key(SECP256R1(), default_backend())
    shared = ephem_priv.exchange(ECDH(), peer_pub)

    pub_nums = ephem_priv.public_key().public_numbers()
    ephem_pub_bytes = (b'\x04' +
                       pub_nums.x.to_bytes(32, 'big') +
                       pub_nums.y.to_bytes(32, 'big'))

    nonce  = secrets.token_bytes(12)
    ct_tag = AESGCM(shared).encrypt(nonce, session_key, None)
    return ephem_pub_bytes + nonce + ct_tag


def _aivm_aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """AES-256-GCM encrypt. Returns nonce(12) || ct || tag(16)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def _aivm_aes_decrypt(key: bytes, blob: bytes) -> bytes:
    """AES-256-GCM decrypt nonce(12) || ct || tag(16)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(blob) < 28:
        raise ValueError("ciphertext too short")
    return AESGCM(key).decrypt(blob[:12], blob[12:], None)


class AIVMClient:
    """
    Runs LLM inference through the Lightchain decentralized worker network.
    Cost: ~0.022 LCAI per inference (0.02 worker fee + ~0.002 gas).
    Uses RELAY_PRIVATE_KEY wallet (already funded for uploads).
    """

    def __init__(self, private_key: str):
        import requests as _req
        from web3 import Web3
        from eth_account import Account

        self._req      = _req
        self._w3       = Web3(Web3.HTTPProvider(_AIVM_RPC))
        self._account  = Account.from_key(private_key)
        self._registry = self._w3.eth.contract(
            address=Web3.to_checksum_address(_AIVM_JOB_REG),
            abi=_AIVM_ABI,
        )
        self._jwt     = None
        self._jwt_exp = 0
        print(f"  [AIVM] relay wallet: {self._account.address}")

    def _get_jwt(self) -> str:
        from eth_account.messages import encode_defunct
        if self._jwt and time.time() < self._jwt_exp - 30:
            return self._jwt
        req = self._req
        r = req.get(
            f"{_AIVM_GATEWAY}/api/auth/challenge",
            params={"address": self._account.address}, timeout=15,
        )
        r.raise_for_status()
        message = r.json()["message"]
        sig = self._account.sign_message(encode_defunct(text=message))
        r2 = req.post(
            f"{_AIVM_GATEWAY}/api/auth/verify",
            json={"message": message, "signature": "0x" + sig.signature.hex()},
            timeout=15,
        )
        r2.raise_for_status()
        v = r2.json()
        self._jwt = v["token"]
        exp_str = v["expiresAt"][:19].replace("T", " ")
        self._jwt_exp = time.mktime(time.strptime(exp_str, "%Y-%m-%d %H:%M:%S"))
        return self._jwt

    def _auth_headers(self):
        return {
            "Authorization": f"Bearer {self._get_jwt()}",
            "Accept":        "application/json",
            "Content-Type":  "application/json",
        }

    def run_inference(self, prompt: str, timeout_secs: int = 360) -> str:
        import websocket as _ws
        from web3 import Web3

        req = self._req
        print(f"  [AIVM] starting inference ({len(prompt)} chars)")

        # 1-2. Auth + pick model
        r = req.get(f"{_AIVM_GATEWAY}/api/models", timeout=15)
        r.raise_for_status()
        models = r.json().get("models", [])
        model  = next((m for m in models if m["name"] == "llama3-8b"), models[0] if models else None)
        if not model:
            raise RuntimeError("No models available from AIVM gateway")
        model_id = model["id"]
        print(f"  [AIVM] model: {model['name']} id={model_id[:10]}…")

        # 3. Select worker
        r = req.post(
            f"{_AIVM_GATEWAY}/api/sessions/select",
            json={"modelId": model_id},
            headers=self._auth_headers(), timeout=15,
        )
        r.raise_for_status()
        sel = r.json()
        print(f"  [AIVM] worker: {sel['worker']}")

        # 4-5. Session key + ECDH wrap
        session_key  = secrets.token_bytes(32)
        enc_worker   = _aivm_ecdh_wrap(session_key, _aivm_decode_pubkey(sel["workerEncryptionKey"]))
        enc_disputer = _aivm_ecdh_wrap(session_key, _aivm_decode_pubkey(sel["disputerEncryptionKey"]))

        # 6. Prepare
        r = req.post(
            f"{_AIVM_GATEWAY}/api/sessions/prepare",
            json={
                "modelId":        model_id,
                "encWorkerKey":   base64.b64encode(enc_worker).decode(),
                "encDisputerKey": base64.b64encode(enc_disputer).decode(),
            },
            headers=self._auth_headers(), timeout=15,
        )
        r.raise_for_status()
        prep = r.json()

        # 7. createSession on-chain
        params_hash = bytes.fromhex(model_id[2:].zfill(64) if model_id[:2].lower() == "0x" else model_id.zfill(64))
        sig_bytes   = bytes.fromhex(prep["signature"][2:] if prep["signature"][:2].lower() == "0x" else prep["signature"])

        gas_price = self._w3.eth.gas_price
        nonce_val = self._w3.eth.get_transaction_count(self._account.address)

        tx = self._registry.functions.createSession(
            params_hash,
            Web3.to_checksum_address(prep["worker"]),
            enc_worker,
            enc_disputer,
            sig_bytes,
            prep["expiry"],
        ).build_transaction({
            "from":     self._account.address,
            "nonce":    nonce_val,
            "gas":      1_000_000,
            "gasPrice": gas_price,
            "value":    0,
            "chainId":  _AIVM_CHAIN_ID,
        })
        signed  = self._account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  [AIVM] createSession tx: {tx_hash.hex()}")
        receipt1 = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
        if receipt1.status != 1:
            raise RuntimeError("createSession reverted on-chain")

        session_id = None
        for log in receipt1.logs:
            try:
                evt = self._registry.events.SessionCreated().process_log(log)
                session_id = evt["args"]["sessionId"]
                break
            except Exception:
                pass
        if session_id is None:
            raise RuntimeError("SessionCreated event not found in receipt")
        print(f"  [AIVM] sessionId: {session_id}")

        # 8. Open relay WebSocket
        relay_token = None
        deadline = time.time() + 30
        while time.time() < deadline:
            r = req.get(
                f"{_AIVM_GATEWAY}/api/sessions/{session_id}/token",
                headers=self._auth_headers(), timeout=10,
            )
            if r.status_code == 200:
                d = r.json()
                if d.get("token"):
                    relay_token = d["token"]
                    break
            time.sleep(1)
        if not relay_token:
            raise RuntimeError("Relay token not ready within 30s")

        chunks   = []
        ws_ready = threading.Event()
        ws_err   = [None]

        def _on_message(ws_obj, message):
            try:
                frame = json.loads(message)
                payload = frame.get("payload")
                if not payload:
                    return
                blob = base64.b64decode(payload)
                try:
                    pt = _aivm_aes_decrypt(session_key, blob)
                    chunks.append(pt.decode("utf-8", errors="replace"))
                except Exception:
                    pass
            except Exception:
                pass

        def _on_open(ws_obj):
            ws_ready.set()

        def _on_error(ws_obj, err):
            ws_err[0] = err
            ws_ready.set()

        ws = _ws.WebSocketApp(
            f"{_AIVM_RELAY}?token={urllib.parse.quote(relay_token)}",
            on_message=_on_message,
            on_open=_on_open,
            on_error=_on_error,
        )
        ws_thread = threading.Thread(target=ws.run_forever, daemon=True)
        ws_thread.start()
        ws_ready.wait(timeout=15)
        if ws_err[0]:
            raise RuntimeError(f"WebSocket failed: {ws_err[0]}")
        print("  [AIVM] relay connected")

        # 9. Encrypt prompt + upload blob
        cipher = _aivm_aes_encrypt(session_key, prompt.encode("utf-8"))
        r = req.post(
            f"{_AIVM_GATEWAY}/api/blobs",
            json={"data": base64.b64encode(cipher).decode()},
            headers=self._auth_headers(), timeout=15,
        )
        r.raise_for_status()
        blob_hashes = r.json().get("blobHashes", [])
        if not blob_hashes:
            raise RuntimeError("No blob hash returned from gateway")
        _bh = blob_hashes[0]
        prompt_hash = bytes.fromhex(_bh[2:].zfill(64) if _bh[:2].lower() == "0x" else _bh.zfill(64))

        # 10. submitJob (pay 0.02 LCAI)
        nonce_val2 = self._w3.eth.get_transaction_count(self._account.address)
        tx2 = self._registry.functions.submitJob(
            session_id,
            prompt_hash,
        ).build_transaction({
            "from":     self._account.address,
            "nonce":    nonce_val2,
            "gas":      500_000,
            "gasPrice": gas_price,
            "value":    _AIVM_JOB_FEE,
            "chainId":  _AIVM_CHAIN_ID,
        })
        signed2  = self._account.sign_transaction(tx2)
        tx_hash2 = self._w3.eth.send_raw_transaction(signed2.raw_transaction)
        print(f"  [AIVM] submitJob tx: {tx_hash2.hex()}")
        receipt2 = self._w3.eth.wait_for_transaction_receipt(tx_hash2, timeout=90)
        if receipt2.status != 1:
            raise RuntimeError("submitJob reverted — check LCAI balance")

        job_id = None
        for log in receipt2.logs:
            try:
                evt = self._registry.events.JobSubmitted().process_log(log)
                job_id = evt["args"]["jobId"]
                break
            except Exception:
                pass
        if job_id is None:
            raise RuntimeError("JobSubmitted event not found in receipt")
        print(f"  [AIVM] jobId: {job_id}")

        # 11. Poll for JobCompleted
        job_completed_topic = "0x" + Web3.keccak(
            text="JobCompleted(uint256,address,bytes32,bytes32)"
        ).hex()
        job_id_topic = "0x" + hex(job_id)[2:].zfill(64)

        done     = False
        deadline = time.time() + timeout_secs
        while time.time() < deadline and not done:
            time.sleep(5)
            try:
                head = self._w3.eth.block_number
                logs = self._w3.eth.get_logs({
                    "address":   Web3.to_checksum_address(_AIVM_JOB_REG),
                    "fromBlock": receipt2.blockNumber,
                    "toBlock":   head,
                    "topics":    [job_completed_topic, job_id_topic],
                })
                if logs:
                    done = True
                    print(f"  [AIVM] JobCompleted! worker: {logs[0].get('address')}")
            except Exception as e:
                print(f"  [AIVM] log poll error (retrying): {e}")

        time.sleep(4)
        ws.close()

        result = "".join(chunks)
        if result:
            print(f"  [AIVM] inference done (relay data), {len(result)} chars")
            return result

        if not done:
            raise RuntimeError(f"Timeout after {timeout_secs}s waiting for JobCompleted")

        print(f"  [AIVM] inference done, {len(result)} chars received")
        return result


_aivm_relay_client = None
_aivm_relay_lock   = threading.Lock()


def _get_relay_aivm_client():
    """Return AIVMClient singleton using RELAY_PRIVATE_KEY, or None if not configured."""
    global _aivm_relay_client
    pk = RELAY_PRIVATE_KEY.strip()
    if not pk:
        return None
    with _aivm_relay_lock:
        if _aivm_relay_client is None:
            try:
                _aivm_relay_client = AIVMClient(pk)
            except Exception as e:
                print(f"  [AIVM] Failed to init relay client: {e}")
                return None
    return _aivm_relay_client


# ─── AI Description Generator ─────────────────────────────────────────────────

def call_aivm_describe(prompt):
    """Call Lightchain AIVM to generate a short description."""
    client = _get_relay_aivm_client()
    if not client:
        raise ValueError("RELAY_PRIVATE_KEY not configured — AIVM unavailable")
    return client.run_inference(prompt)


@app.route('/api/describe-upload', methods=['POST'])
def describe_upload():
    """Generate a 2-sentence AI description for a video or audio upload."""
    data = request.get_json() or {}
    title = data.get('title', '').strip()
    category = data.get('category', '')
    media_type = data.get('type', 'video')
    if not title:
        return jsonify({'error': 'Title is required'}), 400
    if media_type == 'audio':
        prompt = (
            f"Write a 2-sentence description for a song called '{title}'"
            f" in the {category} genre. Be descriptive and engaging."
            f" Keep it under 100 words. Return only the description, no quotes or extra text."
        )
    else:
        prompt = (
            f"Write a 2-sentence description for a video called '{title}'"
            f" in the {category} category. Be descriptive and engaging."
            f" Keep it under 100 words. Return only the description, no quotes or extra text."
        )
    try:
        description = call_aivm_describe(prompt)
        return jsonify({'description': description})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── end AI Description Generator ─────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8190))
    app.run(host='0.0.0.0', port=port)
