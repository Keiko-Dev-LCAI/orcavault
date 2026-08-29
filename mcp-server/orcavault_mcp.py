#!/usr/bin/env python3
"""
OrcaVault MCP Server
====================
Exposes OrcaVault (permanent on-chain memory storage on Lightchain) as MCP tools
so any AI agent can store and retrieve files WITHOUT holding a wallet of its own.

Custodial "house wallet" model
------------------------------
This server holds ONE pre-paid house wallet key and signs uploads on behalf of
any calling agent. The house wallet is registered once by paying the relay's
one-time fee (2 LCAI). After that the relay pays all on-chain gas — the house
wallet needs no ongoing balance.

Safety (why the guards exist)
-----------------------------
Because the relay spends the operator's gas on every upload, this server enforces
a max file size, a rolling per-minute rate limit, and a per-day quota BEFORE it
ever calls the relay. That way a runaway or abusive agent cannot drain the
operator's LCAI. All limits are configurable via env.

Config (env)
------------
  ORCAVAULT_RELAY_URL          relay base URL (default: public Railway URL)
  ORCAVAULT_AGENT_WALLET_KEY   house wallet private key (0x...). REQUIRED for uploads.
  ORCAVAULT_MAX_UPLOAD_MB      max file size in MB (default: 5)
  ORCAVAULT_RATE_PER_MIN       max uploads per rolling 60s (default: 3)
  ORCAVAULT_RATE_PER_DAY       max uploads per rolling 24h (default: 50)
  ORCAVAULT_HTTP_TIMEOUT       per-request timeout seconds (default: 60)
  ORCAVAULT_STREAM_VERSION     retrieval contract version tag (default: v3)

NOTE: never commit the wallet key. Set it via env / secret manager only.
"""
import os
import time
import base64
import binascii
import threading
from collections import deque

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from mcp.server.fastmcp import FastMCP

# ── Config ───────────────────────────────────────────────────────────────────
RELAY_URL      = os.environ.get("ORCAVAULT_RELAY_URL",
                                "https://orcavault-production.up.railway.app").rstrip("/")
AGENT_KEY      = os.environ.get("ORCAVAULT_AGENT_WALLET_KEY", "").strip()
MAX_UPLOAD_MB  = float(os.environ.get("ORCAVAULT_MAX_UPLOAD_MB", "5"))
RATE_PER_MIN   = int(os.environ.get("ORCAVAULT_RATE_PER_MIN", "3"))
RATE_PER_DAY   = int(os.environ.get("ORCAVAULT_RATE_PER_DAY", "50"))
HTTP_TIMEOUT   = int(os.environ.get("ORCAVAULT_HTTP_TIMEOUT", "60"))
STREAM_VERSION = os.environ.get("ORCAVAULT_STREAM_VERSION", "v3")

_ALLOWED_TYPES = {"photo", "video", "audio", "document"}

mcp = FastMCP("orcavault")

# ── House wallet (custodial signer) ──────────────────────────────────────────
_account = Account.from_key(AGENT_KEY) if AGENT_KEY else None


def _house_address() -> str:
    if _account is None:
        raise RuntimeError(
            "No house wallet configured — set ORCAVAULT_AGENT_WALLET_KEY. Uploads are disabled."
        )
    return _account.address


# ── Rate limiting (protects the operator's gas) ──────────────────────────────
_rl_lock  = threading.Lock()
_min_hits = deque()   # timestamps within the last 60s
_day_hits = deque()   # timestamps within the last 24h


def _rate_reserve():
    """Reserve one upload slot. Returns (ok, err). Roll back with _rate_rollback on failure."""
    now = time.time()
    with _rl_lock:
        while _min_hits and now - _min_hits[0] > 60:
            _min_hits.popleft()
        while _day_hits and now - _day_hits[0] > 86400:
            _day_hits.popleft()
        if len(_min_hits) >= RATE_PER_MIN:
            return False, f"Rate limit: max {RATE_PER_MIN} uploads per minute. Try again shortly."
        if len(_day_hits) >= RATE_PER_DAY:
            return False, f"Daily quota reached: max {RATE_PER_DAY} uploads per day."
        _min_hits.append(now)
        _day_hits.append(now)
        return True, None


def _rate_rollback():
    """Give back a reserved slot when the upload did not actually go through."""
    with _rl_lock:
        if _min_hits:
            _min_hits.pop()
        if _day_hits:
            _day_hits.pop()


# ── Tools ────────────────────────────────────────────────────────────────────
@mcp.tool()
def relay_status() -> dict:
    """Check whether the OrcaVault relay is online and healthy."""
    try:
        r = requests.get(f"{RELAY_URL}/health", timeout=HTTP_TIMEOUT)
        ct = r.headers.get("content-type", "")
        return {"ok": r.ok, "status": r.json() if ct.startswith("application/json") else r.text}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def check_access() -> dict:
    """Check if the OrcaVault house wallet is allowed to upload, and the current fee."""
    try:
        addr = _house_address()
    except Exception as e:
        return {"access": False, "error": str(e)}
    try:
        r = requests.get(f"{RELAY_URL}/api/check-access", params={"wallet": addr}, timeout=HTTP_TIMEOUT)
        data = r.json()
        data["house_wallet"] = addr
        return data
    except Exception as e:
        return {"access": False, "error": str(e), "house_wallet": addr}


@mcp.tool()
def store_memory(title: str, content_base64: str, mime_type: str = "application/octet-stream",
                 caption: str = "", mem_type: str = "document", template: str = "") -> dict:
    """
    Store a file permanently on-chain via OrcaVault.

    Provide the file as base64 in content_base64. Returns the on-chain memory id
    plus a public retrieval URL. One-time cost is paid at house-wallet setup; the
    relay covers all gas.

    mem_type must be one of: photo, video, audio, document.
    """
    if _account is None:
        return {"success": False, "error": "Uploads disabled: no house wallet configured."}

    mem_type = (mem_type or "document").lower().strip()
    if mem_type not in _ALLOWED_TYPES:
        return {"success": False, "error": f"mem_type must be one of {sorted(_ALLOWED_TYPES)}"}
    if not title or not content_base64:
        return {"success": False, "error": "title and content_base64 are required"}

    # Validate base64 + enforce size cap (each chunk is a gas-paid on-chain tx).
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        return {"success": False, "error": "content_base64 is not valid base64"}
    size_mb = len(raw) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        return {"success": False,
                "error": f"File too large: {size_mb:.2f} MB exceeds cap of {MAX_UPLOAD_MB} MB"}

    ok, err = _rate_reserve()
    if not ok:
        return {"success": False, "error": err}

    try:
        addr    = _account.address
        ts      = str(int(time.time() * 1000))
        message = f"OrcaVault one-click upload\nWallet: {addr.lower()}\nTimestamp: {ts}"
        signed  = _account.sign_message(encode_defunct(text=message))
        sig     = signed.signature.hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig

        body = {
            "ownerAddress": addr,
            "title":        title,
            "caption":      caption,
            "memType":      mem_type,
            "template":     template,
            "dataURI":      f"data:{mime_type};base64,{content_base64}",
            "signature":    sig,
            "timestamp":    ts,
        }
        # Uploads can be slow (many on-chain chunk txs) — allow a generous timeout.
        r = requests.post(f"{RELAY_URL}/api/relay-upload", json=body, timeout=max(HTTP_TIMEOUT, 300))
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}

        if not r.ok or not data.get("success"):
            _rate_rollback()
            return {"success": False, "status_code": r.status_code,
                    "error": data.get("error") or data.get("message") or "upload failed",
                    "detail": data}

        mid = data.get("memoryId")
        data["retrieveUrl"] = f"{RELAY_URL}/api/media/stream/orcavault/{STREAM_VERSION}/{mid}"
        data["statusUrl"]   = f"{RELAY_URL}/api/media/stream/orcavault/{STREAM_VERSION}/{mid}/status"
        return data
    except Exception as e:
        _rate_rollback()
        return {"success": False, "error": str(e)}


@mcp.tool()
def get_memory_status(memory_id: int, version: str = STREAM_VERSION) -> dict:
    """Check whether a stored memory has finished writing and is ready to retrieve."""
    try:
        r = requests.get(
            f"{RELAY_URL}/api/media/stream/orcavault/{version}/{int(memory_id)}/status",
            timeout=HTTP_TIMEOUT,
        )
        try:
            return r.json()
        except Exception:
            return {"raw": r.text, "status_code": r.status_code}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_memory(memory_id: int, version: str = STREAM_VERSION, inline_max_mb: float = 1.0) -> dict:
    """
    Retrieve a stored memory. Always returns a public URL. If the file is small
    (<= inline_max_mb), the bytes are also returned inline as base64.
    """
    url = f"{RELAY_URL}/api/media/stream/orcavault/{version}/{int(memory_id)}"
    out = {"memoryId": int(memory_id), "url": url}
    try:
        r = requests.get(url, timeout=max(HTTP_TIMEOUT, 120))
        if not r.ok:
            out["error"] = f"HTTP {r.status_code}"
            return out
        content = r.content
        out["content_type"] = r.headers.get("content-type")
        out["size_bytes"]   = len(content)
        if len(content) <= inline_max_mb * 1024 * 1024:
            out["content_base64"] = base64.b64encode(content).decode()
        else:
            out["note"] = "File too large to inline; fetch it from the url."
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


if __name__ == "__main__":
    # Default stdio (local). On Railway set MCP_TRANSPORT=sse (or streamable-http)
    # and PORT — binds 0.0.0.0 so remote MCP clients can connect.
    # See GROK-MCP-DEPLOY-RUNBOOK.md.
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower() or "stdio"
    if transport in ("sse", "streamable-http"):
        mcp.settings.host = os.environ.get("HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("PORT", "8000"))
        # Public Railway hostname must pass transport security checks.
        try:
            from mcp.server.transport_security import TransportSecuritySettings
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
            )
        except Exception:
            pass
    mcp.run(transport=transport)
