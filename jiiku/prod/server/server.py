#!/usr/bin/python -u
"""
server.py
FastAPI + uvicorn + httptools server using native Python bridge bindings.

Bridge API (C++ pybind11 extension)
  bridge.init_module()                          init global state (once per process)
  bridge.send_request(path: str, req: bytes)    serialized protobuf in, serialized protobuf out
"""

import asyncio
import fcntl
import hashlib
import logging
import os
import re
import signal
import sys
import time
from collections import OrderedDict
from contextlib import asynccontextmanager

import orjson
from fastapi import FastAPI, Request, Response

# ==============================
#  CONFIG
# ==============================

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# Bazel runfiles root for the Python bridge target
BUILD_DIR = "/app/sekai-public/dist/bazel-bin/sekai/run_analysis/testing/analyze_main.runfiles/_main/"
UPDATE_SCRIPT = "/app/sekai-public/build_python.sh"
UPDATE_TIMEOUT = int(os.environ.get("UPDATE_TIMEOUT_MS", str(30 * 60 * 1000))) / 1000
LOG_DIR = "/tmp/build_logs"

try:
    with open("/etc/hostname") as _f:
        CONTAINER_ID = os.environ.get("HOSTNAME") or _f.read().strip()
except Exception:
    CONTAINER_ID = "unknown"

RUN_LENGTH_MAXIMUM = 24 * 12 * 21
CACHE_SIZE = int(os.environ.get("WASM_CACHE_SIZE", "1024"))
MAX_QUEUE = int(os.environ.get("WASM_MAX_QUEUE", "256"))
MAX_CONCURRENT = int(os.environ.get("WASM_MAX_CONCURRENT", "2"))  # mirrors JS WORKER_COUNT
REFUSE_UPDATE_WHEN_BUSY = os.environ.get("REFUSE_UPDATE_WHEN_BUSY", "false") == "true"
BRANCH_ALLOWLIST = {"refs/heads/dev", "refs/heads/main"}

# Patterns redacted from Discord-sent logs. Each entry: (compiled_regex, replacement).
# Add any secrets or sensitive strings you want scrubbed before upload.
LOG_SCRUB_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Discord webhook URLs
    (re.compile(r"https://discord(?:app)?\.com/[^\s\"']+"), "[REDACTED]"),
    # Generic key=value secrets
    (re.compile(r"(?i)\b(password|.internal|passwd|token|secret|api[_-]?key|auth[_-]?key|private[_-]?key)\s*[=:]\s*\S+"), r"\1=[REDACTED]"),
    # Bearer tokens in headers
    (re.compile(r"(?i)bearer\s+[a-zA-Z0-9\-._~+/]+=*"), "bearer [REDACTED]"),
    (re.compile(r"172\.30\.30\.\d+|[\w.-]+\.internal"), "[REDACTED]"),
]

# Bazel lines matching these are suppressed UNLESS they also match a keep pattern.
_BAZEL_NOISE_RE = re.compile(
    r"^(?:"
    r"INFO: (?!.*(?:error|fail))"  # INFO lines that aren't errors
    r"|\[\s*[\d,]+\s*/\s*[\d,]+\]"  # progress "[123 / 456] Compiling …"
    r"|Loading:"
    r"|Analyzing:"
    r")",
    re.IGNORECASE,
)
# Lines matching these are always kept regardless of noise filters.
_BAZEL_KEEP_RE = re.compile(r"(?i)error|warning|fail|assert|traceback|exception")

# ==============================
#  LOGGING
# ==============================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [worker:%(process)d] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)

# ==============================
#  BRIDGE (initialized post-fork in lifespan)
# ==============================

_bridge = None
_json_format = None
_ENDPOINT_TYPES: dict = {}
_bridge_ready = False
_bridge_sem: asyncio.Semaphore | None = None
_bridge_waiting = 0  # in-queue (waiting for semaphore) count


def _setup_bridge() -> None:
    """Load and initialize the C++ bridge. Must run after forking (in lifespan)."""
    global _bridge, _json_format, _ENDPOINT_TYPES, _bridge_ready

    sys.path.insert(0, BUILD_DIR)

    from google.protobuf import json_format as _jf  # type: ignore
    from sekai.run_analysis import bridge as _b      # type: ignore
    from sekai.run_analysis.proto import service_pb2 as _pb2  # type: ignore

    _bridge = _b
    _json_format = _jf

    _ENDPOINT_TYPES = {
        "/analyze_player":      (_pb2.AnalyzePlayerRequest,      _pb2.AnalyzePlayerResponse),
        "/analyze_graph":       (_pb2.AnalyzeGraphRequest,       _pb2.AnalyzeGraphResponse),
        "/batch_analyze_graph": (_pb2.BatchAnalyzeGraphRequest,  _pb2.BatchAnalyzeGraphResponse),
        "/analyze_team":        (_pb2.AnalyzeTeamRequest,        _pb2.AnalyzeTeamResponse),
        "/compute_stats":       (_pb2.ComputeStatsRequest,       _pb2.ComputeStatsResponse),
    }

    _bridge.init_module()
    _bridge_ready = True
    logging.info("Bridge ready (pid=%d)", os.getpid())


# ==============================
#  LRU CACHE
# ==============================

class LRUCache:
    def __init__(self, max_size: int):
        self._max = max_size
        self._data: OrderedDict = OrderedDict()

    def get(self, key: str):
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def set(self, key: str, val) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        elif len(self._data) >= self._max:
            self._data.popitem(last=False)
        self._data[key] = val

    def clear(self) -> None:
        self._data.clear()


_response_cache = LRUCache(CACHE_SIZE)

# ==============================
#  BACKGROUND TASKS
# ==============================

_bg_tasks: set = set()

def _bg(coro) -> None:
    """Schedule a fire-and-forget coroutine, keeping a strong reference so the
    task isn't garbage-collected before it completes (Python 3.12+ requirement)."""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)

# ==============================
#  SERVER STATE
# ==============================

_update_in_progress = False
_last_update: dict = {
    "startedAt": None, "finishedAt": None,
    "ok": None, "error": None, "exitCode": None,
}
_in_flight = 0

# ==============================
#  HELPERS
# ==============================

_STATUS_MAP = {0: 200, 1: 500, 3: 400, 5: 404, 7: 403, 12: 501, 16: 401}
_VALID_PATH_RE = re.compile(r"^[a-zA-Z0-9_]+(/[a-zA-Z0-9_]+)*$")


def _cache_key(path: str, body_bytes: bytes) -> str:
    return hashlib.sha256(path.encode() + b"\x00" + body_bytes).hexdigest()


def _sanitize_commit(commit: str) -> str:
    if not commit:
        return ""
    return re.sub(r"[^a-zA-Z0-9\-_]", "", commit)[:100]


def _truncate_log(src: str, dst: str, max_bytes: int) -> str:
    size = os.path.getsize(src)
    if size <= max_bytes:
        return src
    with open(src, "rb") as f:
        f.seek(size - max_bytes)
        data = f.read(max_bytes)
    with open(dst, "wb") as f:
        f.write(data)
    return dst


def _scrub_and_filter_log(src: str, dst: str) -> str | None:
    """Return a Discord-safe copy of the build log at *dst*, or None on failure.

    Steps applied (raw log at *src* is never modified):
      1. Drop noisy Bazel progress/info lines (unless they contain an error keyword).
      2. Redact secrets via LOG_SCRUB_PATTERNS.

    Returns None (no log attached) rather than falling back to the raw file,
    so secrets are never accidentally sent to Discord on error.
    """
    try:
        with open(src, "r", errors="replace") as f_in, open(dst, "w") as f_out:
            for line in f_in:
                if _BAZEL_NOISE_RE.match(line) and not _BAZEL_KEEP_RE.search(line):
                    continue
                for pattern, replacement in LOG_SCRUB_PATTERNS:
                    line = pattern.sub(replacement, line)
                f_out.write(line)
        return dst
    except OSError as exc:
        logging.error("Failed to scrub build log, sending no attachment: %s", exc)
        return None

async def _send_discord(message: str, file_path: str | None = None) -> None:
    import urllib.error
    import urllib.request

    def _post() -> None:
        has_file = bool(file_path and os.path.exists(file_path))

        if has_file:
            # Multipart with file attachment — use "file" field name (same as prod server.js)
            boundary = "SEKAI_MP_BOUNDARY"
            with open(file_path, "rb") as f:
                fdata = f.read()
            body = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="content"\r\n\r\n'
                f"{message}\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="build-log.txt"\r\n'
                f"Content-Type: text/plain\r\n\r\n"
            ).encode() + fdata + f"\r\n--{boundary}--\r\n".encode()
            content_type = f"multipart/form-data; boundary={boundary}"
        else:
            # Plain JSON for text-only messages
            body = orjson.dumps({"content": message})
            content_type = "application/json"

        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL, data=body, method="POST",
            headers={
                "Content-Type": content_type,
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30):
                pass
        except urllib.error.HTTPError as exc:
            logging.error("Discord webhook HTTP %d: %s", exc.code,
                          exc.read().decode(errors="replace"))
        except Exception as exc:
            logging.error("Discord webhook error: %s", exc)

    await asyncio.to_thread(_post)


_BUILD_PIDFILE = "/tmp/build.pid"
_BUILD_LOCKFILE = "/tmp/build.lock"


def _kill_stale_build() -> None:
    """Kill any build process left over from a previous container instance."""
    try:
        with open(_BUILD_PIDFILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGKILL)
        logging.warning("Killed stale build process (pid=%d)", pid)
    except (FileNotFoundError, ProcessLookupError, ValueError):
        pass
    except Exception as exc:
        logging.warning("Could not kill stale build: %s", exc)
    finally:
        try:
            os.unlink(_BUILD_PIDFILE)
        except FileNotFoundError:
            pass


async def _run_update_script(commit: str = "") -> int:
    global _last_update

    # Cross-process exclusive lock — prevents two workers from running builds simultaneously.
    lock_fd = open(_BUILD_LOCKFILE, "w")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fd.close()
        logging.warning("Another worker is already running a build, skipping")
        return -1

    safe_commit = _sanitize_commit(commit)
    _last_update = {
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "finishedAt": None, "ok": None, "error": None, "exitCode": None,
    }

    os.makedirs(LOG_DIR, exist_ok=True)

    # Prune old logs (keep 5 most recent)
    try:
        entries = sorted(
            [{"n": f, "t": os.path.getmtime(os.path.join(LOG_DIR, f))}
             for f in os.listdir(LOG_DIR)],
            key=lambda x: x["t"], reverse=True,
        )[5:]
        for e in entries:
            os.unlink(os.path.join(LOG_DIR, e["n"]))
    except Exception:
        pass

    ts = time.strftime("%Y-%m-%dT%H-%M-%S")
    log_path = os.path.join(LOG_DIR, f"build-{ts}.txt")
    cmd = [UPDATE_SCRIPT] + ([safe_commit] if safe_commit else [])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=os.path.dirname(UPDATE_SCRIPT),
    )
    logging.info("Update started (pid=%d)", proc.pid)

    # Write pidfile so a server restart can kill this orphaned build
    try:
        with open(_BUILD_PIDFILE, "w") as f:
            f.write(str(proc.pid))
    except Exception:
        pass

    async def _stream():
        with open(log_path, "wb") as log_file:
            async for line in proc.stdout:
                log_file.write(line)
                log_file.flush()

    stream_task = asyncio.create_task(_stream())
    try:
        await asyncio.wait_for(proc.wait(), timeout=UPDATE_TIMEOUT)
    except asyncio.TimeoutError:
        logging.error("Update timeout — killing process")
        proc.kill()
        await proc.wait()
    finally:
        stream_task.cancel()
        try:
            await stream_task
        except asyncio.CancelledError:
            pass
        try:
            os.unlink(_BUILD_PIDFILE)
        except FileNotFoundError:
            pass
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        lock_fd.close()

    code = proc.returncode if proc.returncode is not None else -1
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _last_update.update({"finishedAt": now, "exitCode": code, "ok": code == 0})

    # Discord notification with log attachment
    try:
        MAX_LOG = 8 * 1024 * 1024
        final_log = log_path
        suffix = ""
        if os.path.exists(log_path) and os.path.getsize(log_path) > MAX_LOG:
            trunc = log_path.replace(".txt", "-truncated.txt")
            final_log = _truncate_log(log_path, trunc, MAX_LOG)
            suffix = "\n⚠️ Log was truncated (too large)."
        # Scrub secrets and strip noisy Bazel lines before sending to Discord.
        # The raw log on disk is left intact for local debugging.
        safe_log = _scrub_and_filter_log(final_log, final_log.replace(".txt", "-discord.txt"))
        if safe_log is None:
            suffix += "\n⚠️ Log scrubbing failed — attachment withheld."
        emoji = "🟢" if code == 0 else "🔴"
        await _send_discord(
            f"{emoji} Build finished (return code: `{code}`){suffix}", safe_log
        )
    except Exception as exc:
        logging.error("Discord log notification failed: %s", exc)

    return code


# ==============================
#  BRIDGE CALL
# ==============================

async def _call_bridge(path: str, req_bytes: bytes) -> bytes:
    global _bridge_waiting

    if _bridge_waiting >= MAX_QUEUE:
        raise OverflowError("queue full")

    _bridge_waiting += 1
    try:
        async with _bridge_sem:
            return await asyncio.to_thread(_bridge.send_request, path, req_bytes)
    finally:
        _bridge_waiting -= 1


# ==============================
#  REQUEST LIMITS
# ==============================

def _check_limits(path: str, body: dict, internal: bool) -> str | None:
    if path == "/batch_analyze_graph":
        reqs = body.get("requests", [])
        if len(reqs) >= 5:
            return "too many batch requests"
        for r in reqs:
            if len(r.get("points", [])) >= RUN_LENGTH_MAXIMUM:
                return "too many batch requests"
            if len(r.get("timestamps", [])) >= RUN_LENGTH_MAXIMUM:
                return "too many batch requests"

    elif path == "/analyze_graph":
        if len(body.get("points", [])) >= RUN_LENGTH_MAXIMUM:
            return "too many batch requests"
        if len(body.get("timestamps", [])) >= RUN_LENGTH_MAXIMUM:
            return "too many batch requests"

    elif path == "/analyze_player":
        players = body.get("players", [])
        if not isinstance(players, list) or (len(players) > 2 and not internal):
            return "too many players"
        for p in players:
            graph = p.get("graph", {})
            if len(graph.get("points", [])) >= RUN_LENGTH_MAXIMUM:
                return "too many data points"
            if len(graph.get("timestamps", [])) >= RUN_LENGTH_MAXIMUM:
                return "too many data points"

    return None


# ==============================
#  RESPONSE HELPER
# ==============================

class ORJSONResponse(Response):
    media_type = "application/json"

    def render(self, content) -> bytes:
        return orjson.dumps(content, option=orjson.OPT_NON_STR_KEYS)


def _err(msg: str, status: int) -> Response:
    return Response(
        content=orjson.dumps({"error": msg}),
        status_code=status,
        media_type="application/json",
    )


# ==============================
#  LIFESPAN
# ==============================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bridge_sem
    _kill_stale_build()
    _bridge_sem = asyncio.Semaphore(MAX_CONCURRENT)
    try:
        await asyncio.to_thread(_setup_bridge)
    except Exception as exc:
        # Bridge artifacts not built yet — start in degraded mode, /api/* returns 503
        logging.warning("Bridge not available at startup (not built yet?): %s", exc)
    yield


# ==============================
#  APP
# ==============================

app = FastAPI(
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def _track_in_flight(request: Request, call_next):
    global _in_flight
    _in_flight += 1
    try:
        return await call_next(request)
    finally:
        _in_flight = max(0, _in_flight - 1)


# ==============================
#  ROUTES
# ==============================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "wasmReady": _bridge_ready,
        "updating": _update_in_progress,
        "containerId": CONTAINER_ID,
    }


@app.get("/update/status")
async def update_status():
    return {
        "running": _update_in_progress,
        "inFlightRequests": _in_flight,
        "lastUpdate": _last_update,
    }


@app.post("/update")
async def trigger_update(request: Request):
    global _update_in_progress

    try:
        payload = await request.json()
    except Exception:
        return _err("invalid body", 400)

    if payload.get("event") != "push":
        return {
            "status": "ignored", "reason": "Not push event",
            "request": {k: payload.get(k) for k in ("event", "ref", "commit")},
        }

    ref = payload.get("ref", "")
    if ref not in BRANCH_ALLOWLIST:
        return {
            "status": "ignored", "reason": f"Branch not allowlisted ({ref})",
            "request": {k: payload.get(k) for k in ("event", "ref", "commit")},
        }

    if _update_in_progress:
        return Response(
            content=orjson.dumps({"status": "busy", "message": "Update already running"}),
            status_code=409, media_type="application/json",
        )

    if REFUSE_UPDATE_WHEN_BUSY and _in_flight > 1:
        return Response(
            content=orjson.dumps({
                "status": "busy",
                "message": "Refusing update while requests are in flight",
                "inFlightRequests": _in_flight,
            }),
            status_code=409, media_type="application/json",
        )

    commit = payload.get("commit") or ""
    logging.info("Update requested (commit=%s ref=%s)", commit or "latest", ref)
    _update_in_progress = True

    _bg(_send_discord(
        f"🚀 Build started on `{CONTAINER_ID}` @ `{commit or 'latest'}` (`{ref}`)"
    ))

    async def _do_update():
        global _update_in_progress
        try:
            code = await _run_update_script(commit)
            _update_in_progress = False
            if code == 0:
                await _send_discord("✅ Build successful. Reloading...")
                logging.info("Build succeeded — signalling container init for restart")
                # SIGTERM to PID 1 (tini) — the process Docker monitors.
                # Tini forwards the signal to uvicorn and waits for it to exit,
                # then exits itself, triggering Docker's restart policy.
                os.kill(1, signal.SIGTERM)
        except Exception as exc:
            _update_in_progress = False
            logging.error("Update task failed: %s", exc)

    _bg(_do_update())
    return Response(
        content=orjson.dumps({"status": "started", "message": "Update started"}),
        status_code=202, media_type="application/json",
    )


@app.post("/api/{path:path}")
async def api_endpoint(path: str, request: Request):
    if not _bridge_ready:
        return _err("Bridge not ready", 503)

    # Validate path format and allowlist
    if not _VALID_PATH_RE.match(path):
        return _err("invalid path", 400)

    req_path = "/" + path
    if req_path not in _ENDPOINT_TYPES:
        return _err("endpoint not allowed", 418)

    # Enforce body size limit before reading into memory (fastify had 100 MB)
    content_length = request.headers.get("content-length")
    try:
        if content_length and int(content_length) > 10 * 1024 * 1024:
            return _err("request too large", 413)
    except ValueError:
        return _err("invalid content-length", 400)

    try:
        body_bytes = await request.body()
        if len(body_bytes) > 10 * 1024 * 1024:
            return _err("request too large", 413)
        body: dict = orjson.loads(body_bytes) if body_bytes else {}
    except Exception:
        return _err("invalid body", 400)

    internal = bool(body.pop("internal", False))

    err = _check_limits(req_path, body, internal)
    if err:
        return _err(err, 418)

    # Canonical body bytes for cache key
    body_canonical = orjson.dumps(body, option=orjson.OPT_SORT_KEYS)
    key = _cache_key(req_path, body_canonical)

    cached = _response_cache.get(key)
    if cached:
        return Response(content=cached["data"], status_code=cached["status"], media_type="application/json")

    # Serialize JSON body → protobuf
    ReqType, RespType = _ENDPOINT_TYPES[req_path]
    try:
        req_proto = ReqType()
        _json_format.ParseDict(body, req_proto, ignore_unknown_fields=True)
        req_bytes = req_proto.SerializeToString()
    except Exception as exc:
        logging.warning("Proto serialize error (%s): %s", req_path, exc)
        return _err("invalid request", 400)

    # Call bridge
    try:
        resp_bytes = await _call_bridge(req_path, req_bytes)
    except OverflowError:
        return _err("Service overloaded, try again later", 503)
    except Exception as exc:
        logging.error("Bridge call failed (%s): %s", req_path, exc)
        return _err("Internal error", 500)

    # Deserialize protobuf → JSON dict
    try:
        resp_proto = RespType()
        resp_proto.ParseFromString(resp_bytes)

        http_code = 200
        if resp_proto.HasField("status"):
            code_val = resp_proto.status.code if resp_proto.status.HasField("code") else 0
            http_code = _STATUS_MAP.get(code_val, 500)

        resp_data = orjson.dumps(
            _json_format.MessageToDict(resp_proto),
            option=orjson.OPT_NON_STR_KEYS,
        )
    except Exception as exc:
        logging.error("Proto deserialize error (%s): %s", req_path, exc)
        return _err("Internal error", 500)

    if http_code == 200:
        _response_cache.set(key, {"data": resp_data, "status": http_code})

    return Response(content=resp_data, status_code=http_code, media_type="application/json")
