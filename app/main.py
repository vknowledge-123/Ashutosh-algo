# app/main.py
from __future__ import annotations

import asyncio
import os
import time
import pytz
import datetime
import subprocess
import sys

from typing import Any, Dict, List, Optional, Set, Tuple
from fastapi import HTTPException
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Template
from kiteconnect import KiteConnect, KiteTicker 
from .redis_store import RedisStore
from .chartink_client import (
    parse_chartink_payload,
    normalize_alert_name,
    normalize_symbols,
    normalize_symbol,
)
from .trade_engine import TradeEngine
from .websocket_manager import WebSocketManager
from .stock_sector import STOCK_INDEX_MAPPING
from .auth import AuthService
from .middleware import AuthMiddleware, get_current_user, SecurityHeadersMiddleware
from .custom_middleware import SelectiveHostMiddleware
import logging

# Security Imports
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from .security_config import (
    ALLOWED_HOSTS, 
    get_csp_header_value, 
    RATE_LIMIT_AUTH_OTP, 
    RATE_LIMIT_AUTH_VERIFY, 
    RATE_LIMIT_LOGIN
)

# Initialize Limiter
limiter = Limiter(key_func=get_remote_address)


# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()  # Load from .env file
except ImportError:
    pass  # dotenv not installed, use system env vars

# Import encryption module
try:
    from .crypto import init_encryption
    ENCRYPTION_AVAILABLE = True
except ImportError:
    ENCRYPTION_AVAILABLE = False
    print("⚠️  Encryption module not available. Install cryptography: pip install cryptography")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# make sure your module loggers show INFO
logging.getLogger("trade_engine").setLevel(logging.INFO)
logging.getLogger("uvicorn").setLevel(logging.INFO)
logging.getLogger("uvicorn.error").setLevel(logging.INFO)
# Filter out spammy health check logs
class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Return False to filter OUT the record if it matches our path
        return record.args and len(record.args) >= 3 and "/api/zerodha-status" not in str(record.args[2])

# Apply filter to suppress only the specific status endpoint
logging.getLogger("uvicorn.access").setLevel(logging.INFO)  # Keep INFO for other requests
logging.getLogger("uvicorn.access").addFilter(EndpointFilter())

# Optional stdout filter to suppress verbose middleware prints
_SUPPRESS_MW = (os.getenv("SUPPRESS_MIDDLEWARE_LOGS", "1") or "").strip().lower() in {"1", "true", "yes", "on"}
if _SUPPRESS_MW:
    class _FilteredStdout:
        def __init__(self, stream, drop_prefixes):
            self._stream = stream
            self._drop_prefixes = tuple(drop_prefixes)
            self._buf = ""

        def write(self, s):
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if not line.startswith(self._drop_prefixes):
                    self._stream.write(line + "\n")
            return len(s)

        def flush(self):
            if self._buf:
                if not self._buf.startswith(self._drop_prefixes):
                    self._stream.write(self._buf)
                self._buf = ""
            self._stream.flush()

        def __getattr__(self, name):
            return getattr(self._stream, name)

    sys.stdout = _FilteredStdout(sys.stdout, ["[MIDDLEWARE]"])

# -----------------------------
# Config
# -----------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
ENABLE_SERVICE_RESTART = (os.getenv("ENABLE_SERVICE_RESTART") or "").strip().lower() in {"1", "true", "yes", "on"}
SERVICE_RESTART_TOKEN = (os.getenv("SERVICE_RESTART_TOKEN") or "").strip()
TRADING_SYSTEMD_UNIT = (os.getenv("TRADING_SYSTEMD_UNIT") or "trading").strip()
TRADING_RESTART_CMD = (os.getenv("TRADING_RESTART_CMD") or f"systemctl restart --no-block {TRADING_SYSTEMD_UNIT}").strip()
TRADING_RESTART_TIMEOUT_SEC = float((os.getenv("TRADING_RESTART_TIMEOUT_SEC") or "15").strip() or "15")


def _is_test_mode() -> bool:
    v = (os.getenv("APP_TESTING") or "").strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    return (os.getenv("APP_ENV") or "").strip().lower() == "test"


def _run_restart_command(cmd: str) -> Dict[str, Any]:
    """
    Execute the configured restart command and wait briefly for acceptance/failure.
    The command should return quickly, for example by using `systemctl --no-block`.
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return {"ok": False, "error": "TRADING_RESTART_CMD_EMPTY"}

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=max(1.0, TRADING_RESTART_TIMEOUT_SEC),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"RESTART_TIMEOUT_AFTER_{int(max(1.0, TRADING_RESTART_TIMEOUT_SEC))}S",
        }
    except Exception as e:
        return {"ok": False, "error": f"RESTART_SPAWN_FAILED:{e}"}

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if detail:
            detail = detail.replace("\r", " ").replace("\n", " ")[:400]
        return {
            "ok": False,
            "error": f"RESTART_CMD_FAILED:{proc.returncode}",
            "detail": detail or "Command returned non-zero exit code",
        }

    detail = (proc.stdout or "").strip()
    return {"ok": True, "detail": detail[:400] if detail else ""}

app = FastAPI(title="AlgoEdge Ultra-Low Latency")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# 1. Selective Host Middleware (Strict for dashboard, permissive for webhooks)
app.add_middleware(
    SelectiveHostMiddleware,
    allowed_hosts=ALLOWED_HOSTS,
    bypass_paths=["/webhook/"]  # Webhook endpoints bypass host validation
)

# 2. Security Headers (XSS, CSP, etc.)
app.add_middleware(
    SecurityHeadersMiddleware,
    csp_header=get_csp_header_value()
)

# 3. SlowAPI Middleware (Rate Limiting)
app.add_middleware(SlowAPIMiddleware)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
    "https://clicktrade.live",
    "https://www.clicktrade.live"],  # change in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize encryption manager (will be set in startup)
encryption_manager = None

ws_mgr = WebSocketManager()
# store will be initialized in startup after encryption is ready
store = None
# auth_service will be initialized after store is ready
auth_service = None

# Engines per user
ENGINE: Dict[int, TradeEngine] = {}

# -----------------------------
# KiteTicker globals (single ticker)
# -----------------------------
KT: Optional[KiteTicker] = None
KT_CONNECTED: bool = False
KT_TASK: Optional[asyncio.Future] = None
KT_LOCK = asyncio.Lock()

KT_USER_ID: Optional[int] = None
KT_ACCESS_TOKEN: str = ""

APP_LOOP: Optional[asyncio.AbstractEventLoop] = None

# Subscriptions + token map
SUB_TOKENS: Set[int] = set()
TOKEN_TO_SYMBOL: Dict[int, str] = {}
SYMBOL_TOKEN: Dict[str, int] = {}

# If webhook arrives before instruments map is loaded, we queue symbols here
PENDING_SYMBOLS: Dict[int, Set[str]] = {}
INSTR_LOCK = asyncio.Lock()

# Zerodha session validity cache (avoid calling profile() every 5s)
_SESSION_CACHE: Dict[int, Dict[str, Any]] = {}  # user_id -> {"ok": bool, "ts": float}
_SESSION_CACHE_TTL = 30.0  # seconds

# Throttle Redis position writes (per symbol)
_LAST_POS_SAVE: Dict[Tuple[int, str], float] = {}
_POS_SAVE_THROTTLE_SEC = 0.8

# Throttle instrument reload
_LAST_INSTR_RELOAD = 0.0
_INSTR_RELOAD_INTERVAL = 300.0  # 5 minutes


# -----------------------------
# Helpers
# -----------------------------
def _read_dashboard_template(user_id: int, username: str) -> str:
    with open("app/static/dashboard.html", "r", encoding="utf-8") as f:
        html = f.read()
    t = Template(html)
    return t.render(USER_ID=user_id, USERNAME=username)


def _kite_client(api_key: str, access_token: str) -> KiteConnect:
    k = KiteConnect(api_key=api_key)
    k.set_access_token(access_token)
    return k


def _sym_safe(x: Any) -> str:
    """
    Strong symbol normalizer (extra-safe).
    Uses redis_store.norm_symbol as the single source of truth.
    """
    return normalize_symbol(x)


async def is_session_valid(user_id: int) -> bool:
    """
    Dashboard polls every 5s. Cache validity for short TTL.
    """
    now = time.time()
    cached = _SESSION_CACHE.get(user_id)
    if cached and (now - float(cached.get("ts", 0.0)) < _SESSION_CACHE_TTL):
        return bool(cached.get("ok", False))

    creds = await store.load_credentials(user_id)
    at = (await store.load_access_token(user_id)).strip()
    api_key = (creds.get("api_key") or "").strip()

    if not api_key or not at:
        _SESSION_CACHE[user_id] = {"ok": False, "ts": now}
        return False

    try:
        kite = _kite_client(api_key, at)
        kite.profile()  # validates access_token
        _SESSION_CACHE[user_id] = {"ok": True, "ts": now}
        return True
    except Exception:
        _SESSION_CACHE[user_id] = {"ok": False, "ts": now}
        return False


# async def ensure_engine(user_id: int) -> TradeEngine:
#     user_id = int(user_id)
#     if user_id not in ENGINE:
#         ENGINE[user_id] = TradeEngine(user_id=user_id, store=store)
#         await ENGINE[user_id].configure_kite()
#     return ENGINE[user_id]
async def ensure_engine(user_id: int) -> TradeEngine:
    user_id = int(user_id)
    if user_id not in ENGINE:
        ENGINE[user_id] = TradeEngine(user_id=user_id, store=store, broadcast_cb=ws_mgr.broadcast_nowait)
        await ENGINE[user_id].configure_kite()

        # ✅ Restore open positions after restart
        restored = await ENGINE[user_id].rehydrate_open_positions()
        if restored:
            # ✅ Ensure ticks come for these symbols
            asyncio.create_task(subscribe_symbols_for_user(user_id, restored))

    return ENGINE[user_id]



# -----------------------------
# Instruments (symbol -> token)
# -----------------------------
async def build_symbol_token_map_from_kite(user_id: int) -> bool:
    """
    Download NSE instruments once after login and keep in memory.
    Heavy operation: never do this in the webhook hot path unless unavoidable.
    """
    if _is_test_mode():
        return False
    user_id = int(user_id)

    creds = await store.load_credentials(user_id)
    api_key = (creds.get("api_key") or "").strip()
    access_token = (await store.load_access_token(user_id)).strip()
    if not api_key or not access_token:
        print("[INSTR] Missing api_key/access_token; cannot load instruments")
        return False

    try:
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        print("[INSTR] Downloading NSE instruments...")
        all_instruments = kite.instruments("NSE")

        if not all_instruments:
            print("[INSTR] ❌ No instruments returned from Kite")
            return False

        # Clear maps
        temp_sym_tok = {}
        temp_tok_sym = {}

        for ins in all_instruments:
            # We want BOTH original tradingsymbol and normalized one to be safe
            raw_sym = ins.get("tradingsymbol", "")
            norm_sym = _sym_safe(raw_sym)
            tok = ins.get("instrument_token")
            
            if tok:
                itok = int(tok)
                temp_tok_sym[itok] = norm_sym
                
                # Store under both raw and normalized if different
                if raw_sym:
                    temp_sym_tok[raw_sym] = itok
                    # Also store base without common suffixes (e.g., "-EQ", ".NS")
                    if raw_sym.endswith("-EQ"):
                        temp_sym_tok[raw_sym[:-3]] = itok
                    if raw_sym.endswith(".NS"):
                        temp_sym_tok[raw_sym[:-3]] = itok
                if norm_sym:
                    temp_sym_tok[norm_sym] = itok

        SYMBOL_TOKEN.clear()
        SYMBOL_TOKEN.update(temp_sym_tok)
        TOKEN_TO_SYMBOL.clear()
        TOKEN_TO_SYMBOL.update(temp_tok_sym)

        print(f"[INSTR] ✅ Loaded {len(SYMBOL_TOKEN)} symbols into memory (Source: NSE)")
        
        # Debug: check if common symbols are present - Explicit check for M&M and friends
        for test_sym in ["TATAMOTORS", "PEL", "SBIN", "RELIANCE", "M&M", "NIVABUPA"]:
            found = False
            if test_sym in SYMBOL_TOKEN:
                print(f"[INSTR] Verified: {test_sym} -> {SYMBOL_TOKEN[test_sym]}")
                found = True
            if f"{test_sym}-EQ" in SYMBOL_TOKEN:
                 print(f"[INSTR] Verified: {test_sym} found as {test_sym}-EQ -> {SYMBOL_TOKEN[f'{test_sym}-EQ']}")
                 found = True
            
            if not found:
                print(f"[INSTR] ⚠️ Not found in NSE map: {test_sym}")

        return True
    except Exception as e:
        print("[INSTR] instruments download failed:", e)
        return False


async def _ensure_token_map_ready(user_id: int) -> None:
    """
    Ensures SYMBOL_TOKEN is available.
    If webhook comes early, we build map in background and then subscribe pending symbols.
    """
    user_id = int(user_id)

    if SYMBOL_TOKEN:
        # already ready
        return

    async with INSTR_LOCK:
        # double-check after acquiring lock
        if SYMBOL_TOKEN:
            return
        ok = await is_session_valid(user_id)
        if not ok:
            return
        built = await build_symbol_token_map_from_kite(user_id)
        if not built:
            return
    # after map is ready, subscribe pending symbols
    pending = list(PENDING_SYMBOLS.get(user_id, set()))
    if pending:
        await subscribe_symbols_for_user(user_id, pending)
        PENDING_SYMBOLS[user_id] = set()
        # 🔥 FIX: resubscribe tokens if ticker is already running
    if KT and KT_CONNECTED and SUB_TOKENS:
        try:
            KT.subscribe(list(SUB_TOKENS))
            KT.set_mode(KT.MODE_FULL, list(SUB_TOKENS))
            print("[KT] re-subscribed after token map ready:", len(SUB_TOKENS))
        except Exception as e:
            print("[KT] re-subscribe failed:", e)



# -----------------------------
# Subscriptions
# -----------------------------
async def subscribe_symbols_for_user(user_id: int, symbols: List[str]) -> None:
    """
    Adds tokens to SUB_TOKENS and subscribes if KiteTicker is running.

    Key behaviors:
    - If token map is not ready, queue symbols and build map in background.
    - Uses MODE_FULL to receive OHLC (close/high/low) and quantities.
    """
    if _is_test_mode():
        return
    user_id = int(user_id)
    if not symbols:
        return

    # Normalize symbols up-front
    norm_syms: List[str] = []
    for s in symbols:
        sym = _sym_safe(s)
        if sym:
            norm_syms.append(sym)

    if not norm_syms:
        return

    # If token map is not ready, queue and kick off background build (non-blocking).
    if not SYMBOL_TOKEN:
        PENDING_SYMBOLS.setdefault(user_id, set()).update(norm_syms)
        asyncio.create_task(_ensure_token_map_ready(user_id))
        # Do not block webhook here.
        return

    changed = False
    missing_syms: List[str] = []
    for sym in norm_syms:
        tok = SYMBOL_TOKEN.get(sym)
        if not tok and "-" not in sym:
            alt = f"{sym}-EQ"
            tok = SYMBOL_TOKEN.get(alt)
            if tok:
                sym = alt
        if not tok:
            print(f"[TOKEN MISSING] {sym}  (common cause: symbol format like SBIN-EQ)")
            missing_syms.append(sym)
            continue

        if tok not in SUB_TOKENS:
            SUB_TOKENS.add(tok)
            changed = True
        else:
             # Already subscribed
             pass
             
        # Validation Log
        if tok:
             # print(f"[SUB_CHECK] ✅ {sym} -> {tok}")
             pass
    
    if missing_syms:
        print(f"⚠️ [SUB_WARNING] Could not resolve tokens for: {missing_syms}. (Total Map: {len(SYMBOL_TOKEN)})")
        # Trigger reload if enough time has passed
        global _LAST_INSTR_RELOAD
        now = time.time()
        if now - _LAST_INSTR_RELOAD > _INSTR_RELOAD_INTERVAL:
            _LAST_INSTR_RELOAD = now
            print("[INSTR] 🔄 Triggering periodic instrument reload due to missing symbols...")
            asyncio.create_task(build_symbol_token_map_from_kite(user_id))

    # Update live ticker subscriptions if running
    if changed:
        if KT and KT_CONNECTED:
            try:
                KT.subscribe(list(SUB_TOKENS))
                # FULL mode gives ohlc.close/high/low etc
                KT.set_mode(KT.MODE_FULL, list(SUB_TOKENS))
                print(f"[SUB] ✅ SUBSCRIBED to {len(SUB_TOKENS)} tokens. New: {len(norm_syms)} -> {[s for s in norm_syms if s not in missing_syms]}")
            except Exception as e:
                print(f"[SUB] ❌ subscribe failed: {e}")
        else:
             print(f"[SUB] ⚠️ Added to set, but KT not connected/ready. Count={len(SUB_TOKENS)}. KT={KT is not None} CONN={KT_CONNECTED}")


# -----------------------------
# KiteTicker start / restart
# -----------------------------
async def _stop_kite_ticker() -> None:
    global KT, KT_CONNECTED, KT_TASK, KT_USER_ID, KT_ACCESS_TOKEN
    try:
        if KT is not None:
            try:
                KT.close()  # KiteTicker supports close()
            except Exception:
                pass
    finally:
        KT = None
        KT_CONNECTED = False
        KT_TASK = None
        KT_USER_ID = None
        KT_ACCESS_TOKEN = ""


async def start_kite_ticker(user_id: int) -> None:
    """
    Starts a single KiteTicker (threaded=True) and routes ticks back into FastAPI loop.
    Uses MODE_FULL for OHLC + quantities.
    """
    global KT, KT_TASK, KT_CONNECTED, KT_USER_ID, KT_ACCESS_TOKEN

    if _is_test_mode():
        return

    user_id = int(user_id)

    async with KT_LOCK:
        creds = await store.load_credentials(user_id)
        api_key = (creds.get("api_key") or "").strip()
        access_token = (await store.load_access_token(user_id)).strip()

        if not api_key or not access_token:
            print("[KT] missing api_key/access_token; ticker not started")
            return

        # If ticker already running but token changed, restart it
        if KT is not None:
            if (KT_USER_ID != user_id) or (KT_ACCESS_TOKEN != access_token):
                print("[KT] access token changed -> restarting ticker")
                await _stop_kite_ticker()
            else:
                return  # already running with same creds

        kt = KiteTicker(api_key, access_token)
        KT = kt
        KT_USER_ID = user_id
        KT_ACCESS_TOKEN = access_token

        def on_connect(ws, response):
            global KT_CONNECTED
            KT_CONNECTED = True
            try:
                if SUB_TOKENS:
                    ws.subscribe(list(SUB_TOKENS))
                    ws.set_mode(ws.MODE_FULL, list(SUB_TOKENS))
            except Exception as e:
                print("[KT] subscribe on_connect failed:", e)
            print("[KT] connected, subs:", len(SUB_TOKENS), "mode=FULL")

        def on_close(ws, code, reason):
            global KT_CONNECTED
            KT_CONNECTED = False
            print("[KT] closed", code, reason)

        def on_error(ws, code, reason):
            print("[KT] error", code, reason)

        def on_ticks(ws, ticks):
            if ticks:
                 # Debug: print first few tokens to verify we get data
                 sample = [t.get('instrument_token') for t in ticks[:3]]
                 # print(f"[KT] TICKS RECEIVED: {len(ticks)} sample={sample}")

            loop = APP_LOOP
            if loop is None:
                return

            async def _handle():
                eng = await ensure_engine(user_id)

                for t in ticks or []:

                    try:
                        tok = int(t.get("instrument_token", 0))
                        sym = TOKEN_TO_SYMBOL.get(tok)
                        if not sym:
                            continue

                        ltp = float(t.get("last_price") or 0.0)

                        ohlc = t.get("ohlc") or {}
                        close = float(ohlc.get("close") or 0.0)
                        high = float(ohlc.get("high") or ltp)
                        low = float(ohlc.get("low") or ltp)

                        tbq = float(t.get("buy_quantity") or 0.0)
                        tsq = float(t.get("sell_quantity") or 0.0)
                        # ✅ PROPER PER-STOCK LOG
                        # Feed engine with proper OHLC (important for sector ranking)
                        pos = await eng.on_tick(sym, ltp, close, high, low, tbq, tsq)

                        # UI tick push (non-blocking)
                        ws_mgr.broadcast_nowait(
                            user_id,
                            {
                                "type": "tick",
                                "symbol": sym,
                                "ltp": ltp,
                                "close": close,
                                "high": high,
                                "low": low,
                                "tbq": tbq,
                                "tsq": tsq,
                            },
                        )

                        # Throttle Redis writes for positions
                        if pos:
                            key = (user_id, sym)
                            now = time.time()
                            last = _LAST_POS_SAVE.get(key, 0.0)
                            if now - last >= _POS_SAVE_THROTTLE_SEC:
                                _LAST_POS_SAVE[key] = now
                                asyncio.create_task(store.upsert_position(user_id, sym, pos.to_public()))
                                ws_mgr.broadcast_nowait(user_id, {"type": "pos", "position": pos.to_public()})

                    except Exception as e:
                        print("[KT] tick handle error:", e)

            asyncio.run_coroutine_threadsafe(_handle(), loop)

        kt.on_connect = on_connect
        kt.on_close = on_close
        kt.on_error = on_error
        kt.on_ticks = on_ticks

        def on_order_update(ws, data):
            loop = APP_LOOP
            if loop is None:
                return

            async def _handle_ou():
                try:
                    eng = await ensure_engine(user_id)
                    await eng.on_order_update(data)  # <-- add this method in TradeEngine
                except Exception as e:
                    print("[KT] order_update handle error:", e)

            asyncio.run_coroutine_threadsafe(_handle_ou(), loop)

        kt.on_order_update = on_order_update
        def _run():
            try:
                kt.connect(threaded=True)
            except Exception as e:
                print("[KT] connect thread error:", e)

        loop = asyncio.get_running_loop()
        KT_TASK = loop.run_in_executor(None, _run)
        print("[KT] connect thread started")


# -----------------------------
# Auto Square Off Scheduler
# -----------------------------
async def schedule_auto_squareoff():
    """
    Runs every 30s. Checks if time >= 15:15 IST.
    If yes, and enabled, and not run yet today -> triggers exit_all.
    """
    while True:
        try:
            await asyncio.sleep(20) # check freq
            
            # Simple IST check
            tz = pytz.timezone("Asia/Kolkata")
            now = datetime.datetime.now(tz)
            
            # Target: 15:20 (3:20 PM)
            if now.hour == 15 and now.minute >= 20:
                # Check all users (currently only 1 supported primarily, but loop capable)
                user_ids = [1] 
                
                for uid in user_ids:
                    if await store.is_auto_sq_off_enabled(uid):
                        if not await store.has_auto_sq_off_run(uid):
                            print(f"⏰ [AUTO_SQ_OFF] Triggering for user={uid} at {now}")
                            eng = await ensure_engine(uid)
                            # Passing reason AUTO_SQ_OFF_320 to differentiate
                            cnt = await eng.exit_all_open_positions(reason="AUTO_SQ_OFF_320")
                            await store.mark_auto_sq_off_run(uid)
                            
                            # Notify UI
                            ws_mgr.broadcast_nowait(uid, {
                                "type": "toast", 
                                "text": f"⏰ Auto Square Off Triggered ({cnt} positions)",
                                "error": False
                            })
        except Exception as e:
            print("[SCHED] Auto sq off error:", e)
            await asyncio.sleep(10)


# -----------------------------
# Startup
# -----------------------------
@app.on_event("startup")
async def startup():
    global APP_LOOP, encryption_manager, store, auth_service
    APP_LOOP = asyncio.get_running_loop()
    ws_mgr.set_loop(APP_LOOP)

    if _is_test_mode():
        from .memory_store import InMemoryStore

        store = InMemoryStore()
        auth_service = AuthService(store)
        return

    # Initialize encryption
    if ENCRYPTION_AVAILABLE:
        try:
            encryption_manager = init_encryption()
        except Exception as e:
            print(f"⚠️  Encryption initialization failed: {e}")
            encryption_manager = None
    
    # Initialize Redis store with encryption
    store = RedisStore(REDIS_URL, encryption_manager)
    if not await store.ping():
        raise RuntimeError(f"Redis is not reachable at {REDIS_URL}")
    await store.init_scripts()
    
    # Initialize auth service
    auth_service = AuthService(store)
    print("✅ Authentication service initialized")
    
    # Start Scheduler
    asyncio.create_task(schedule_auto_squareoff())

    # Auto-start for all users found in Redis
    try:
        all_uids = await store.list_all_user_ids()
        print(f"🔄 [STARTUP] Found {len(all_uids)} users. Rehydrating...")

        for uid in all_uids:
            try:
                # ✅ Auto-enable Auto Square Off if not set (Default: ON)
                if not await store.is_auto_sq_off_enabled(uid):
                    await store.set_auto_sq_off_enabled(uid, True)

                ok = await is_session_valid(uid)
                if ok:
                    print(f"🚀 [STARTUP] Re-connecting User {uid}...")
                    async with INSTR_LOCK:
                        # Build per-user symbol token map if needed
                        # (Note: SYMBOL_TOKEN is global, but let's ensure it's loaded)
                        if not SYMBOL_TOKEN:
                            await build_symbol_token_map_from_kite(uid)

                    base_symbols = list(STOCK_INDEX_MAPPING.keys())
                    await subscribe_symbols_for_user(uid, base_symbols)
                    await start_kite_ticker(uid)

                    eng = await ensure_engine(uid)
                    await eng.configure_kite()
                    print(f"✅ [STARTUP] User {uid} Rehydrated")
                else:
                    print(f"⚠️ [STARTUP] Skipping User {uid} (Session invalid/expired)")
            except Exception as ue:
                print(f"❌ [STARTUP] Failed to rehydrate User {uid}: {ue}")

    except Exception as e:
        print("[startup] user listing/rehydration failed:", e)



# -----------------------------
# Authentication Endpoints
# -----------------------------
@app.get("/", response_class=RedirectResponse)
@limiter.limit(RATE_LIMIT_LOGIN)
async def root(request: Request):
    """Redirect to dashboard (Auth managed by Cloudflare)"""
    return RedirectResponse(url="/dashboard")


# Auth endpoints removed as requested (Cloudflare Zero Trust managed)


# -----------------------------
# Dashboard
# -----------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """
    Serve the main trading dashboard.
    Auth is bypassed here as it is handled by Cloudflare Zero Trust.
    Defaulting to User ID 1.
    """
    # Simply render for default user (Ashutosh)
    return _read_dashboard_template(user_id=1, username="Ashutosh")


# -----------------------------
# Credentials + Kite login
# -----------------------------
@app.post("/api/save-credentials")
async def save_credentials(payload: Dict[str, Any]) -> Dict[str, Any]:
    user_id = int(payload.get("user_id", 1))
    api_key = str(payload.get("api_key", "")).strip()
    api_secret = str(payload.get("api_secret", "")).strip()
    if not api_key or not api_secret:
        return {"error": "API_KEY_SECRET_REQUIRED"}

    await store.save_credentials(user_id, api_key, api_secret)
    _SESSION_CACHE.pop(user_id, None)
    return {"ok": True}


@app.get("/connect/zerodha")
async def connect_zerodha(user_id: int = 1):
    user_id = int(user_id)
    creds = await store.load_credentials(user_id)
    api_key = (creds.get("api_key") or "").strip()
    api_secret = (creds.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        return RedirectResponse(url=f"/dashboard?user_id={user_id}&error=missing_creds")

    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()
    return RedirectResponse(url=login_url)


@app.get("/zerodha/callback")
async def zerodha_callback(request: Request, user_id: Optional[int] = None):
    # 1) Try user_id from query params
    if user_id is None:
        try:
             uid_q = request.query_params.get("user_id")
             if uid_q: user_id = int(uid_q)
        except: pass
        
    # 2) Fallback to session cookie (crucial for preserving context after redirect)
    if user_id is None:
        token = request.cookies.get("session_token")
        if token and auth_service:
            session_data = await auth_service.verify_session(token)
            if session_data:
                user_id = session_data.get("user_id")
                
    # 3) Final fallback
    user_id = int(user_id or 1)

    creds = await store.load_credentials(user_id)
    api_key = (creds.get("api_key") or "").strip()
    api_secret = (creds.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        return RedirectResponse(url=f"/dashboard?user_id={user_id}")

    request_token = request.query_params.get("request_token", "") or ""
    if not request_token.strip():
        return RedirectResponse(url=f"/dashboard?user_id={user_id}")

    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token.strip(), api_secret=api_secret)
    access_token = str(data.get("access_token") or "").strip()

    await store.save_access_token(user_id, access_token)
    _SESSION_CACHE.pop(user_id, None)

    # Build instruments map
    async with INSTR_LOCK:
        await build_symbol_token_map_from_kite(user_id)

    # Subscribe base universe (for sector ranking)
    base_symbols = list(STOCK_INDEX_MAPPING.keys())
    await subscribe_symbols_for_user(user_id, base_symbols)

    # Subscribe any pending symbols that arrived via webhook earlier
    pending = list(PENDING_SYMBOLS.get(user_id, set()))
    if pending:
        await subscribe_symbols_for_user(user_id, pending)
        PENDING_SYMBOLS[user_id] = set()

    # Start / restart ticker
    await start_kite_ticker(user_id)
    # Ensure engine has latest access token
    eng = await ensure_engine(user_id)
    await eng.configure_kite()

    return RedirectResponse(url=f"/dashboard?user_id={user_id}")


@app.get("/api/zerodha-status")
async def zerodha_status(user_id: int = 1):
    user_id = int(user_id)

    session_ok = await is_session_valid(user_id)
    kill = await store.is_kill(user_id)

    ticker_connected = bool(KT_CONNECTED and KT_USER_ID == user_id)

    # A valid auth session means Kite login succeeded, even if the background
    # ticker thread is still reconnecting. When that happens, try to self-heal.
    if session_ok and not ticker_connected and not _is_test_mode():
        try:
            eng = await ensure_engine(user_id)
            await eng.configure_kite()
            await _stop_kite_ticker()
            await start_kite_ticker(user_id)
        except Exception as e:
            print(f"[KT] status self-heal failed for user={user_id}: {e}")

        ticker_connected = bool(KT_CONNECTED and KT_USER_ID == user_id)

    return {
        "connected": bool(session_ok),
        "ticker_connected": ticker_connected,
        "kill_switch": kill
    }


# -----------------------------
# Alert Config
# -----------------------------
@app.get("/api/alert-config")
async def list_alert_config(user_id: int = 1) -> Dict[str, Any]:
    user_id = int(user_id)
    cfg = await store.list_alert_configs(user_id)
    return {"configs": cfg}


@app.post("/api/alert-config")
async def save_alert_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    user_id = int(payload.get("user_id", 1))
    raw_name = payload.get("alert_name", "")
    if not raw_name or not str(raw_name).strip():
        return {"error": "ALERT_NAME_REQUIRED"}

    # Normalize key consistently
    alert_name = normalize_alert_name(raw_name)

    # Guard: normalized-name collisions overwrite configs silently.
    # Example: "My_Strategy" vs "My-Strategy" normalize to the same key.
    try:
        existing = await store.get_alert_config(user_id, alert_name)
    except Exception:
        existing = None
    if existing:
        existing_raw = str(existing.get("alert_name_raw") or existing.get("alert_name") or "").strip()
        incoming_raw = str(raw_name or "").strip()
        if existing_raw and incoming_raw and existing_raw.lower() != incoming_raw.lower():
            return {
                "error": "ALERT_NAME_COLLISION",
                "message": "This strategy name normalizes to an existing saved strategy key; rename it to avoid overwriting.",
                "normalized_key": alert_name,
                "existing_alert_name_raw": existing_raw,
                "incoming_alert_name_raw": incoming_raw,
            }

    payload2 = dict(payload)
    payload2["alert_name"] = alert_name
    payload2["alert_name_raw"] = str(raw_name)

    await store.save_alert_config(user_id, payload2)
    
    # Log top sectors if sector filter is enabled
    if str(payload2.get("sector_on", "false")).lower() == "true":
         try:
             eng = await ensure_engine(user_id)
             ranks = eng.get_sector_rank()
             top_n = int(payload2.get("topn", 3))
             
             # Get top N sectors
             top_sectors = ranks[:top_n]
             
             # Format for log
             sector_str = ", ".join([f"{s[0]} ({s[1]:+.2f}%)" for s in top_sectors])
             
             print("\n" + "="*60)
             print(f"✅ ALERT CONFIG SAVED: '{alert_name}'")
             print(f"🔍 Sector Filter: TOP {top_n}")
             print(f"📊 Current Top {top_n}: {sector_str}")
             print("="*60 + "\n")
         except Exception as e:
             print(f"⚠️ Failed to log top sectors: {e}")

    return {"status": "saved", "config": payload2}


@app.delete("/api/alert-config")
async def delete_alert_config_api(payload: Dict[str, Any]) -> Dict[str, Any]:
    user_id = int(payload.get("user_id", 1))
    alert_name = str(payload.get("alert_name", "")).strip()
    deleted = await store.delete_alert_config(user_id, alert_name)
    if not deleted:
        return {"status": "not_found", "deleted": False}
    return {"status": "deleted", "deleted": True}


# -----------------------------
# MTM P&L Exit Config (Daily)
# -----------------------------
@app.get("/api/pnl-exit-config")
async def get_pnl_exit_config_api(user_id: int = 1) -> Dict[str, Any]:
    user_id = int(user_id)
    cfg = await store.get_pnl_exit_config(user_id)
    return {"config": cfg}


@app.post("/api/pnl-exit-config")
async def set_pnl_exit_config_api(payload: Dict[str, Any]) -> Dict[str, Any]:
    user_id = int(payload.get("user_id", 1))
    cfg = await store.set_pnl_exit_config(user_id, payload or {})
    return {"status": "saved", "config": cfg}


# -----------------------------
# Position Management
# -----------------------------
@app.post("/api/position/exit-all")
async def exit_all_positions_api(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Exit all open positions"""
    user_id = int(payload.get("user_id", 1))
    
    eng = await ensure_engine(user_id)
    try:
        count = await eng.exit_all_open_positions(reason="MANUAL_EXIT_ALL")
        return {"status": "ok", "count": count, "message": f"Exit orders sent for {count} positions"}
    except Exception as e:
        return {"error": str(e)}


# -----------------------------
# Chartink webhook
# -----------------------------
@app.api_route("/webhook/chartink", methods=["POST", "GET"])
async def chartink_webhook(request: Request, user_id: int = 1) -> Dict[str, Any]:
    user_id = int(user_id)
    eng = await ensure_engine(user_id)

    payload: Dict[str, Any] = {}
    content_type = (request.headers.get("content-type") or "").lower()

    # Allow GET webhooks (some providers misconfigure and send query params).
    if request.method == "GET":
        try:
            qp = request.query_params
            for k in qp.keys():
                vals = qp.getlist(k)
                payload[k] = vals if len(vals) > 1 else (vals[0] if vals else "")
        except Exception:
            payload = {}
    else:
        # 1) JSON
        if "application/json" in content_type:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
        else:
            # 2) form-data / urlencoded (preserve multi-values)
            try:
                form = await request.form()
                if form:
                    tmp: Dict[str, Any] = {}
                    for k, v in form.multi_items():
                        if k in tmp:
                            if not isinstance(tmp[k], list):
                                tmp[k] = [tmp[k]]
                            tmp[k].append(v)
                        else:
                            tmp[k] = v
                    payload = tmp
            except Exception:
                payload = {}

            # 3) raw text might be JSON
            if not payload:
                try:
                    raw = (await request.body() or b"").decode("utf-8", errors="ignore").strip()
                    if raw.startswith("{") and raw.endswith("}"):
                        import json as _json
                        payload = _json.loads(raw)
                except Exception:
                    payload = {}

    alert_name_raw, symbols_raw, ts = parse_chartink_payload(payload)
    alert_name = normalize_alert_name(alert_name_raw)

    # normalize symbols (and also force extra-safe cleanup)
    symbols0 = normalize_symbols(symbols_raw)
    symbols = [_sym_safe(s) for s in symbols0 if _sym_safe(s)]

    logging.getLogger("trade_engine").info(
        "🌐 WEBHOOK_RECEIVED | user=%s method=%s ct=%s alert=%s symbols=%s keys=%s",
        user_id,
        request.method,
        content_type,
        alert_name,
        len(symbols),
        sorted([str(k) for k in (payload or {}).keys()])[:30],
    )

    # If symbols couldn't be parsed, show it on dashboard and stop.
    if not symbols:
        res = [{"symbol": "", "status": "ERROR", "reason": "NO_SYMBOLS_PARSED"}]
        await store.save_alert(user_id, {
            "alert_name": alert_name,
            "time": ts,
            "symbols": [],
            "result": res,
        })
        await ws_mgr.broadcast(user_id, {
            "type": "alert",
            "alert_name": alert_name,
            "time": ts,
            "symbols": [],
            "result": res,
        })
        return {
            "ok": True,
            "warning": "NO_SYMBOLS_PARSED",
            "alert": alert_name,
            "symbols": [],
            "result": res,
            "content_type": content_type,
        }

    # Subscribe symbols for ticks (non-blocking)
    asyncio.create_task(subscribe_symbols_for_user(user_id, symbols))

    # 1) INITIALIZE Alert History IMMEDIATELY (Prevents race with trade motor)
    initial_res = [{"symbol": s, "status": "RECEIVED"} for s in symbols]
    await store.save_alert(user_id, {
        "alert_name": alert_name,
        "time": ts,
        "symbols": symbols,
        "result": initial_res
    })

    # 2) Process alert -> orders
    try:
        res = await eng.on_chartink_alert(alert_name, symbols, ts=ts)
    except Exception as e:
        print(f"🔥 [WEBHOOK_PANIC] Critical Trade Engine Error: {e}")
        await store.set_kill(user_id, True)
        res = [{"symbol": s, "status": "ERROR", "reason": f"CRITICAL_FAIL:{e}"} for s in symbols]

    # 3) UPDATE Alert History with Entry Results
    await store.save_alert(user_id, {
        "alert_name": alert_name,
        "time": ts,
        "result": res
    })

    # Alert data for UI
    alert_data = {
        "type": "alert",
        "alert_name": alert_name,
        "time": ts,
        "symbols": symbols,
        "result": res,
    }
    print(f"📥 Chartink alert_data → {alert_data}")

    # Push to UI (which triggers reload)
    await ws_mgr.broadcast(user_id, alert_data)

    return {
        "ok": True,
        "alert": alert_name,
        "symbols": symbols,
        "result": res,
        "content_type": content_type,
    }


# -----------------------------
# Sectors
# -----------------------------
@app.post("/api/subscribe-symbols")
async def api_subscribe_symbols(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Force subscription for a batch of symbols (used by UI)"""
    user_id = int(payload.get("user_id", 1))
    symbols = payload.get("symbols", [])
    if not symbols:
        return {"ok": False, "error": "NO_SYMBOLS"}
    await subscribe_symbols_for_user(user_id, symbols)
    return {"ok": True, "count": len(symbols), "subscribed": symbols}


@app.get("/api/sectors/top")
async def get_top_sectors(user_id: int = Query(..., alias="user_id"), limit: int = 10):
    """
    Get current top N performing sectors.
    """
    eng = await ensure_engine(user_id)
    ranks = eng.get_sector_rank()
    
    # Format for display: [{"name": "NIFTY AUTO", "pct": 1.23}, ...]
    top = [{"name": r[0], "pct": r[1]} for r in ranks[:limit]]
    return {"sectors": top}


# -----------------------------
# Alerts
# -----------------------------
@app.get("/api/alerts")
async def api_alerts(user_id: int = 1, limit: int = 100) -> Dict[str, Any]:
    user_id = int(user_id)
    alerts = await store.get_recent_alerts(user_id, int(limit))
    return {"alerts": alerts}


@app.delete("/api/alerts")
async def api_clear_alerts(user_id: int = 1) -> Dict[str, Any]:
    user_id = int(user_id)
    await store.delete_alerts(user_id)
    return {"ok": True, "message": "All alerts cleared"}


# -----------------------------
# Positions
# -----------------------------
@app.get("/api/positions")
async def api_positions(user_id: int = 1) -> Dict[str, Any]:
    user_id = int(user_id)
    rows = await store.list_positions(user_id)
    return {"positions": rows}


#-----------------------------
# Square Off positions
# -----------------------------
@app.post("/api/position/squareoff")
async def api_squareoff(payload: Dict[str, Any]) -> Dict[str, Any]:
    user_id = int(payload.get("user_id", 1))
    raw_symbol = payload.get("symbol", "")
    symbol = _sym_safe(raw_symbol)
    reason = str(payload.get("reason", "MANUAL") or "MANUAL").strip().upper()

    if not symbol:
        return {"error": f"Invalid symbol: {raw_symbol}"}

    eng = await ensure_engine(user_id)

    print(f"🖱️ [SQUAREOFF_CLICK] user={user_id} raw='{raw_symbol}' sym='{symbol}' reason={reason}")
    ok = await is_session_valid(user_id)
    if not ok:
        return {"error": "Zerodha not connected. Please login first."}

    # ✅ Works even after restart (memory -> Zerodha fallback)
    r = await eng.manual_squareoff_zerodha(symbol, reason=reason)

    print(f"🧾 [SQUAREOFF_RESULT] user={user_id} sym={symbol} -> {r}")
    
    # Convert response format to match frontend expectations
    if r.get("status") == "ERROR":
        return {"error": r.get("reason", "Square off failed")}
    elif r.get("status") == "NOT_FOUND":
        return {"error": f"No open position found for {symbol}"}
    
    ws_mgr.broadcast_nowait(user_id, {"type": "pos_refresh"})
    return {"ok": True, "message": f"Exit order sent for {symbol}"}


# -----------------------------
# Kill switch
# -----------------------------
@app.post("/api/kill-switch")
async def api_kill(payload: Dict[str, Any]) -> Dict[str, Any]:
    user_id = int(payload.get("user_id", 1))
    enabled = bool(payload.get("enabled", True))
    if not enabled:
        await store.set_kill(user_id, False)
        return {"ok": True, "enabled": False}

    # Enabling kill switch: square-off first, then activate kill switch.
    eng = await ensure_engine(user_id)
    try:
        sq = await eng.squareoff_all_positions(reason="KILL_SWITCH_MANUAL")
    except Exception as e:
        sq = {"ok": False, "error": str(e), "count": 0, "results": []}

    await store.set_kill(user_id, True)
    try:
        ws_mgr.broadcast_nowait(user_id, {"type": "kill_switch", "enabled": True})
    except Exception:
        pass
    return {"ok": True, "enabled": True, "squareoff": sq}


# -----------------------------
# Admin: service restart
# -----------------------------
@app.post("/api/service/restart")
async def api_restart_service(
    request: Request,
    payload: Dict[str, Any],
    x_restart_token: Optional[str] = Header(None, alias="X-Restart-Token"),
) -> Dict[str, Any]:
    """
    Trigger a server-side restart command (typically `systemctl restart trading`).

    Safety:
      - Disabled by default; set ENABLE_SERVICE_RESTART=1 to enable.
      - If SERVICE_RESTART_TOKEN is set, clients must provide X-Restart-Token header.
    """
    if not ENABLE_SERVICE_RESTART:
        # Fallback: soft restart ticker to pick up new tokens without full service restart.
        user_id = int(payload.get("user_id", 1))
        eng = await ensure_engine(user_id)
        await eng.configure_kite()
        await _stop_kite_ticker()
        await start_kite_ticker(user_id)
        return {"ok": True, "message": "Ticker restarted (service restart disabled)"}

    if SERVICE_RESTART_TOKEN and (x_restart_token or "") != SERVICE_RESTART_TOKEN:
        raise HTTPException(status_code=403, detail="INVALID_RESTART_TOKEN")

    if sys.platform.startswith("win"):
        # Allow Windows only if TRADING_RESTART_CMD is explicitly set to a non-systemctl command.
        if not TRADING_RESTART_CMD or "systemctl" in TRADING_RESTART_CMD.lower():
            user_id = int(payload.get("user_id", 1))
            eng = await ensure_engine(user_id)
            await eng.configure_kite()
            await _stop_kite_ticker()
            await start_kite_ticker(user_id)
            return {"ok": True, "message": "Ticker restarted (Windows fallback)"}

    cmd = TRADING_RESTART_CMD

    result = _run_restart_command(cmd)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "detail": result.get("detail", "")}

    return {"ok": True, "message": "Restart requested", "detail": result.get("detail", "")}


# -----------------------------
# Auto Square Off Config
# -----------------------------
@app.get("/api/auto-sq-off/status")
async def get_auto_sq_off(user_id: int = 1) -> Dict[str, Any]:
    enabled = await store.is_auto_sq_off_enabled(int(user_id))
    return {"enabled": enabled}

@app.post("/api/auto-sq-off/toggle")
async def toggle_auto_sq_off(payload: Dict[str, Any]) -> Dict[str, Any]:
    user_id = int(payload.get("user_id", 1))
    enabled = bool(payload.get("enabled", False))
    await store.set_auto_sq_off_enabled(user_id, enabled)
    return {"enabled": enabled}


# -----------------------------
# WebSocket feed
# -----------------------------
@app.get("/ws/feed")
async def ws_feed_http(user_id: int = 1) -> Dict[str, Any]:
    # If someone hits this endpoint via normal HTTP, FastAPI will not match the WebSocket route
    # and you'll see 404s in logs. Return a clear response instead.
    raise HTTPException(
        status_code=426,
        detail="Upgrade Required: connect using WebSocket (ws:// or wss://) to /ws/feed",
    )

@app.websocket("/ws/feed")
async def ws_feed(ws: WebSocket, user_id: int = 1):
    user_id = int(user_id)
    await ws_mgr.connect(user_id, ws)
    try:
        while True:
            # Keep-alive from client (dashboard sends ping)
            await ws.receive_text()
    except WebSocketDisconnect:
        await ws_mgr.disconnect(user_id, ws)
    except Exception:
        await ws_mgr.disconnect(user_id, ws)


