import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Callable

MSG_TERMINATOR = "\n"
JSON_COMPACT_SEPARATORS = (",", ":")

LOCALHOST = "127.0.0.1"

DEFAULT_SESSION_TTL_SECONDS = 30 * 60
DEFAULT_EVENT_TTL_SECONDS = 30 * 60
JANITOR_INTERVAL_SECONDS = 30

DEFAULT_SEND_RETRIES = 4
SERVER_SEND_RETRY_DELAY_SECONDS = 0.35
CLIENT_SEND_RETRY_DELAY_SECONDS = 0.4

SERVER_CONNECT_TIMEOUT_SECONDS = 20
VLC_CONNECT_TIMEOUT_SECONDS = 15.0
VLC_CONNECT_RETRY_SLEEP_SECONDS = 0.25

CLIENT_RECONNECT_INITIAL_BACKOFF_SECONDS = 1.0
CLIENT_RECONNECT_MAX_BACKOFF_SECONDS = 10.0
CLIENT_RECONNECT_BACKOFF_MULTIPLIER = 1.8
CLIENT_HEARTBEAT_INTERVAL_SECONDS = 2.5
CLIENT_HEARTBEAT_TIMEOUT_SECONDS = 12.0

OUTBOUND_QUEUE_SIZE = 2000
SUPPRESS_REMOTE_SECONDS = 1.2

LOCAL_WATCHER_SLEEP_SECONDS = 0.35
LOCAL_WATCHER_ERROR_SLEEP_SECONDS = 0.5
BURST_READ_TIMEOUT_SECONDS = 0.03

LUA_PORT_START = 4213
LUA_PORT_SCAN_ATTEMPTS = 200


def get_vlcsync_config_dir() -> Path:
    path = Path.home() / "config/vlcsync"
    path.mkdir(parents=True, exist_ok=True)
    return path


def generate_default_username() -> str:
    return f"user-{uuid.uuid4().hex[:8]}"


def now_seconds() -> float:
    return time.time()


def utc_ms_now() -> int:
    return int(time.time() * 1000)


def safe_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def seq_of(message: dict) -> int:
    try:
        return int(message.get("seq", 0))
    except (TypeError, ValueError):
        return 0


def dump_json_line(payload: dict, compact: bool) -> bytes:
    if compact:
        data = json.dumps(payload, separators=JSON_COMPACT_SEPARATORS)
    else:
        data = json.dumps(payload)
    return (data + MSG_TERMINATOR).encode("utf-8")


async def send_json_with_retry(
    writer: asyncio.StreamWriter,
    payload: dict,
    *,
    retries: int,
    delay_seconds: float,
    compact: bool,
    on_error: Callable[[Exception, int, int], None],
) -> bool:
    data = dump_json_line(payload, compact=compact)
    for attempt in range(1, retries + 1):
        try:
            writer.write(data)
            await writer.drain()
            return True
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            on_error(exc, attempt, retries)
            if attempt < retries:
                await asyncio.sleep(delay_seconds)
    return False


def load_or_create_client_id(username: str) -> str:
    safe_username = "".join(ch for ch in username if ch.isalnum() or ch in {"-", "_"}) or "default"
    path = get_vlcsync_config_dir()
    identity_file = path / f"client_id_{safe_username}.txt"
    if identity_file.exists():
        value = identity_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = uuid.uuid4().hex
    identity_file.write_text(value, encoding="utf-8")
    return value
