import argparse
import asyncio
import json
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from vlcsync.common import (
    BURST_READ_TIMEOUT_SECONDS,
    CLIENT_EVENT_ACK_TIMEOUT_SECONDS,
    CLIENT_HEARTBEAT_INTERVAL_SECONDS,
    CLIENT_HEARTBEAT_TIMEOUT_SECONDS,
    CLIENT_RECONNECT_BACKOFF_MULTIPLIER,
    CLIENT_RECONNECT_INITIAL_BACKOFF_SECONDS,
    CLIENT_RECONNECT_MAX_BACKOFF_SECONDS,
    CLIENT_SEND_RETRY_DELAY_SECONDS,
    DEFAULT_SEND_RETRIES,
    LOCALHOST,
    LOCAL_WATCHER_ERROR_SLEEP_SECONDS,
    LOCAL_WATCHER_SLEEP_SECONDS,
    LUA_PORT_SCAN_ATTEMPTS,
    LUA_PORT_START,
    OUTBOUND_QUEUE_SIZE,
    SERVER_CONNECT_TIMEOUT_SECONDS,
    SUPPRESS_REMOTE_SECONDS,
    VLC_CONNECT_RETRY_SLEEP_SECONDS,
    VLC_CONNECT_TIMEOUT_SECONDS,
    generate_default_username,
    get_vlcsync_config_dir,
    load_or_create_client_id,
    now_seconds,
    safe_float,
    safe_int,
    send_json_with_retry,
    seq_of,
    utc_ms_now,
)


class ConfigError(Exception):
    pass


REQUIRED_CONFIG_FIELDS = ("username", "server_ip", "port", "lua_port")


def _client_config_path(default_path: str | None = None) -> Path:
    if default_path:
        return Path(default_path).expanduser()
    return get_vlcsync_config_dir() / "client_config.json"


def _default_config() -> dict[str, Any]:
    return {
        "username": generate_default_username(),
        "server_ip": None,
        "port": None,
        "lua_port": None,
    }


def _validate_config_types(config: dict[str, Any], config_path: Path) -> None:
    if not isinstance(config["username"], str) or not config["username"].strip():
        raise ConfigError(
            f"Invalid 'username' in config: {config_path}. Expected a non-empty string."
        )
    if config["server_ip"] is not None and not isinstance(config["server_ip"], str):
        raise ConfigError(
            f"Invalid 'server_ip' in config: {config_path}. Expected string or null."
        )
    if config["port"] is not None and not isinstance(config["port"], int):
        raise ConfigError(
            f"Invalid 'port' in config: {config_path}. Expected integer or null."
        )
    if config["lua_port"] is not None and not isinstance(config["lua_port"], int):
        raise ConfigError(
            f"Invalid 'lua_port' in config: {config_path}. Expected integer or null."
        )

    for field in ("port", "lua_port"):
        value = config[field]
        if value is not None and not (1 <= value <= 65535):
            raise ConfigError(
                f"Invalid '{field}' in config: {config_path}. Expected value between 1 and 65535."
            )


def load_or_create_client_config(config_path: Path) -> dict[str, Any]:
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if not config_path.exists():
        defaults = _default_config()
        config_path.write_text(json.dumps(defaults, indent=2) + "\n", encoding="utf-8")
        print(f"[config] created default config at {config_path}")
        print("[config] server_ip and port are null by default; set them in config or pass CLI args")
        return defaults

    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Unable to read config file {config_path}: {exc}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON format in config file {config_path}: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ConfigError(f"Invalid config format in {config_path}: expected a JSON object.")

    missing_fields = [field for field in REQUIRED_CONFIG_FIELDS if field not in parsed]
    if missing_fields:
        raise ConfigError(
            f"Missing required config fields in {config_path}: {', '.join(missing_fields)}"
        )

    config: dict[str, Any] = {
        "username": parsed["username"],
        "server_ip": parsed["server_ip"],
        "port": parsed["port"],
        "lua_port": parsed["lua_port"],
    }
    _validate_config_types(config, config_path)
    return config


def _resolve_runtime_value(
    field: str,
    config_value: Any,
    positional_value: Any,
    option_value: Any,
    config_path: Path,
) -> Any:
    if option_value is not None:
        return option_value
    if positional_value is not None:
        return positional_value
    print(f"[config] using {field} from {config_path}: {config_value}")
    return config_value


def _positive_port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not (1 <= parsed <= 65535):
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return parsed


class VlcBridge:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.lock = asyncio.Lock()

    async def connect(self, timeout_seconds: float = VLC_CONNECT_TIMEOUT_SECONDS) -> None:
        deadline = now_seconds() + timeout_seconds
        while True:
            try:
                self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
                await self.command("ping")
                return
            except Exception:
                if now_seconds() >= deadline:
                    raise RuntimeError("could not connect to VLC Lua interface")
                await asyncio.sleep(VLC_CONNECT_RETRY_SLEEP_SECONDS)

    async def command(self, cmd: str) -> str:
        if self.reader is None or self.writer is None:
            raise RuntimeError("VLC bridge is not connected")

        async with self.lock:
            self.writer.write((cmd + "\n").encode("utf-8"))
            await self.writer.drain()
            line = await self.reader.readline()
            if not line:
                raise RuntimeError("VLC bridge disconnected")
            return line.decode("utf-8").strip()

    async def get_state(self) -> tuple[str, float | None]:
        raw = await self.command("state")
        if not raw.startswith("state:"):
            return "paused", None

        parts = raw.split(";")
        state = "paused"
        pos: float | None = None
        for part in parts:
            if part.startswith("state:"):
                state = part.split(":", 1)[1]
            elif part.startswith("position:"):
                value = part.split(":", 1)[1]
                if value != "no-input":
                    try:
                        pos = float(value)
                    except ValueError:
                        pos = None
        return state, pos

    async def play(self) -> None:
        await self.command("play")

    async def pause(self) -> None:
        await self.command("pause")

    async def seek(self, position: float) -> None:
        await self.command(f"seek:{position:.3f}")


class SyncClient:
    def __init__(self, username: str, server_ip: str, port: int, lua_port: int | None) -> None:
        self.username = username
        self.server_ip = server_ip
        self.port = port
        self.lua_port = lua_port
        self.client_id = load_or_create_client_id(username)

        self.server_reader: asyncio.StreamReader | None = None
        self.server_writer: asyncio.StreamWriter | None = None
        self.vlc: VlcBridge | None = None
        self.vlc_process: subprocess.Popen | None = None

        self.connected_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.outbound_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=OUTBOUND_QUEUE_SIZE)
        self.last_applied_seq = 0

        self.last_state: str | None = None
        self.last_position: float | None = None
        self.suppress_until = 0.0
        self.last_server_message_at = 0.0
        self.server_send_lock = asyncio.Lock()
        self.next_local_event_id = 1
        self.pending_event_acks: dict[str, asyncio.Event] = {}

    @staticmethod
    def _utc_ms_now() -> int:
        return utc_ms_now()

    @staticmethod
    def _safe_int(value: object) -> int | None:
        return safe_int(value)

    @staticmethod
    def _safe_float(value: object) -> float | None:
        return safe_float(value)

    @staticmethod
    def _seq_of(msg: dict) -> int:
        return seq_of(msg)

    def install_lua_script(self) -> Path:
        source = Path(__file__).resolve().parent.parent / "vlcsync.lua"
        target_dir = Path.home() / ".local/share/vlc/lua/intf"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "vlcsync.lua"
        shutil.copy2(source, target)
        return target

    @staticmethod
    def _is_port_free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((LOCALHOST, port))
                return True
            except OSError:
                return False

    @classmethod
    def _find_free_port(cls, start: int = LUA_PORT_START, attempts: int = LUA_PORT_SCAN_ATTEMPTS) -> int:
        for candidate in range(start, start + attempts):
            if cls._is_port_free(candidate):
                return candidate
        raise RuntimeError("could not find a free local Lua port")

    def _get_vlc(self) -> VlcBridge:
        if self.vlc is None:
            raise RuntimeError("VLC bridge not initialized")
        return self.vlc

    def launch_vlc(self) -> None:
        self.install_lua_script()
        if self.lua_port is None:
            self.lua_port = self._find_free_port()
        elif not self._is_port_free(self.lua_port):
            raise RuntimeError(
                f"Lua port {self.lua_port} is already in use. Choose another with --lua-port."
            )

        self.vlc = VlcBridge(LOCALHOST, self.lua_port)
        cmd = [
            "vlc",
            "--extraintf",
            "luaintf",
            "--lua-intf",
            "vlcsync",
            "--lua-config",
            f"vlcsync={{port={self.lua_port}}}",
        ]
        self.vlc_process = subprocess.Popen(cmd)
        print(f"[client] VLC launched (Lua port {self.lua_port})")

    async def _close_server_connection(self) -> None:
        writer = self.server_writer
        self.server_reader = None
        self.server_writer = None
        self.connected_event.clear()
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as exc:
                print(f"[client] close connection error: {exc}")

    async def connect_server(self) -> None:
        print(f"[client] connecting to server {self.server_ip}:{self.port}")
        self.server_reader, self.server_writer = await asyncio.wait_for(
            asyncio.open_connection(self.server_ip, self.port),
            timeout=SERVER_CONNECT_TIMEOUT_SECONDS,
        )
        hello = {
            "type": "hello",
            "username": self.username,
            "client_id": self.client_id,
            "last_seq": self.last_applied_seq,
        }
        self.server_writer.write((json.dumps(hello) + "\n").encode("utf-8"))
        await self.server_writer.drain()

        first = await asyncio.wait_for(self.server_reader.readline(), timeout=SERVER_CONNECT_TIMEOUT_SECONDS)
        if not first:
            raise RuntimeError("server closed connection during handshake")
        msg = json.loads(first.decode("utf-8").strip())
        if msg.get("type") == "welcome":
            print(f"[client] Connected to server {self.server_ip}:{self.port}")
            print(f"[client] Client id: {self.client_id}")
            print(f"[client] Current connected users: {msg.get('users')}")
            print(f"[client] Replayed events after reconnect: {msg.get('replayed', 0)}")
            self.last_server_message_at = now_seconds()
            self.connected_event.set()
        else:
            raise RuntimeError("invalid welcome from server")

    def _mark_server_message(self) -> None:
        self.last_server_message_at = now_seconds()

    def _seconds_since_server_message(self) -> float:
        if self.last_server_message_at <= 0:
            return 0.0
        return max(0.0, now_seconds() - self.last_server_message_at)

    async def _send_to_server_with_retry(
        self,
        payload: dict,
        retries: int = DEFAULT_SEND_RETRIES,
        delay_seconds: float = CLIENT_SEND_RETRY_DELAY_SECONDS,
    ) -> bool:
        if self.server_writer is None:
            print("[client] send failed: not connected to server")
            return False

        async with self.server_send_lock:
            writer = self.server_writer
            if writer is None:
                print("[client] send failed: not connected to server")
                return False

            return await send_json_with_retry(
                writer,
                payload,
                retries=retries,
                delay_seconds=delay_seconds,
                compact=False,
                on_error=lambda exc, attempt, total: print(
                    f"[client] send attempt {attempt}/{total} failed: {exc}"
                ),
            )

    async def send_event(self, event: str, position: float | None, playback_state: str) -> None:
        event_id = f"{self.client_id}:{self.next_local_event_id}"
        self.next_local_event_id += 1
        payload = {
            "type": "event",
            "event_id": event_id,
            "event": event,
            "position": position,
            "playback_state": playback_state,
            "event_utc_ms": self._utc_ms_now(),
        }
        try:
            self.outbound_queue.put_nowait(payload)
            print(
                f"[local->queue] event={event} pos={position} "
                f"state={playback_state} event_utc_ms={payload['event_utc_ms']} id={event_id}"
            )
        except asyncio.QueueFull:
            print("[local->queue] outbound queue full, dropping oldest event")
            try:
                _ = self.outbound_queue.get_nowait()
                self.outbound_queue.task_done()
            except asyncio.QueueEmpty:
                pass
            self.outbound_queue.put_nowait(payload)

    def _compute_latency_seconds(self, msg: dict) -> float:
        now_ms = self._utc_ms_now()
        event_utc_ms = self._safe_int(msg.get("event_utc_ms"))
        server_sent_utc_ms = self._safe_int(msg.get("server_sent_utc_ms"))

        if event_utc_ms is not None:
            delta = now_ms - event_utc_ms
            if 0 <= delta <= 15 * 60 * 1000:
                return delta / 1000.0

        if server_sent_utc_ms is not None:
            delta = now_ms - server_sent_utc_ms
            if delta >= 0:
                return delta / 1000.0

        return 0.0

    async def apply_remote(self, msg: dict) -> None:
        event = str(msg.get("event", ""))
        by = str(msg.get("by", ""))
        position = self._safe_float(msg.get("position"))
        playback_state = str(msg.get("playback_state", "paused"))
        seq_num = self._seq_of(msg)

        latency_seconds = self._compute_latency_seconds(msg)
        compensated_position: float | None = position
        if position is not None and event in {"play", "seek"} and playback_state == "playing":
            compensated_position = position + latency_seconds

        print(
            f"[server->{self.username}] seq={seq_num} event={event} by={by} "
            f"pos={position} compensated={compensated_position} latency={latency_seconds:.3f}s"
        )

        self.suppress_until = time.time() + SUPPRESS_REMOTE_SECONDS
        vlc = self._get_vlc()

        if event == "play":
            if compensated_position is not None:
                await vlc.seek(compensated_position)
            await vlc.play()
        elif event == "pause":
            if position is not None:
                await vlc.seek(position)
            await vlc.pause()
        elif event == "seek" and compensated_position is not None:
            await vlc.seek(compensated_position)
            if playback_state == "playing":
                await vlc.play()
            else:
                await vlc.pause()

    async def _handle_non_sync_message(self, msg: dict) -> None:
        msg_type = msg.get("type")
        if msg_type == "user_count":
            print(f"[client] Connected users: {msg.get('count')}")
        elif msg_type == "event_ack":
            event_id = str(msg.get("event_id", "")).strip()
            if event_id:
                ack_event = self.pending_event_acks.get(event_id)
                if ack_event is not None:
                    ack_event.set()
                print(f"[client] ack received event_id={event_id} seq={msg.get('seq')}")

    async def keepalive_loop(self) -> None:
        while not self.stop_event.is_set():
            await self.connected_event.wait()

            silent_for = self._seconds_since_server_message()
            if silent_for > CLIENT_HEARTBEAT_TIMEOUT_SECONDS:
                raise RuntimeError(
                    f"server heartbeat timeout after {silent_for:.1f}s of silence"
                )

            ok = await self._send_to_server_with_retry({"type": "heartbeat", "ts": self._utc_ms_now()})
            if not ok:
                raise RuntimeError("failed sending heartbeat to server")

            await asyncio.sleep(CLIENT_HEARTBEAT_INTERVAL_SECONDS)

    async def _read_latest_sync_from_burst(self, first_sync: dict) -> dict:
        if self.server_reader is None:
            return first_sync

        latest = first_sync
        while True:
            try:
                line = await asyncio.wait_for(self.server_reader.readline(), timeout=BURST_READ_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                break

            if not line:
                raise RuntimeError("server disconnected")

            try:
                msg = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                continue

            if msg.get("type") == "sync":
                if self._seq_of(msg) >= self._seq_of(latest):
                    latest = msg
            else:
                await self._handle_non_sync_message(msg)

        return latest

    async def server_listener(self) -> None:
        if self.server_reader is None:
            raise RuntimeError("server not connected")

        while True:
            line = await self.server_reader.readline()
            if not line:
                raise RuntimeError("server disconnected")
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                continue

            self._mark_server_message()

            msg_type = msg.get("type")
            if msg_type == "sync":
                msg = await self._read_latest_sync_from_burst(msg)
                by_id = str(msg.get("by_id", ""))
                if by_id == self.client_id:
                    continue
                seq_num = self._seq_of(msg)

                if seq_num and seq_num <= self.last_applied_seq:
                    continue

                try:
                    await self.apply_remote(msg)
                    if seq_num > 0:
                        self.last_applied_seq = max(self.last_applied_seq, seq_num)
                except Exception as exc:
                    print(f"[client] failed applying remote event seq={seq_num}: {exc}")
            else:
                await self._handle_non_sync_message(msg)

    async def sender_loop(self) -> None:
        while not self.stop_event.is_set():
            payload = await self.outbound_queue.get()
            event_id = str(payload.get("event_id", "")).strip()
            ack_event: asyncio.Event | None = None
            if event_id:
                ack_event = self.pending_event_acks.setdefault(event_id, asyncio.Event())
            try:
                while not self.stop_event.is_set():
                    await self.connected_event.wait()
                    ok = await self._send_to_server_with_retry(payload)
                    if ok:
                        print(f"[queue->server] sent event={payload.get('event')} pos={payload.get('position')} id={event_id}")

                        if ack_event is None:
                            break

                        try:
                            await asyncio.wait_for(
                                ack_event.wait(),
                                timeout=CLIENT_EVENT_ACK_TIMEOUT_SECONDS,
                            )
                            break
                        except asyncio.TimeoutError:
                            print(
                                f"[queue->server] ack timeout for event_id={event_id}; forcing reconnect and retry"
                            )
                            await self._close_server_connection()
                            await asyncio.sleep(0.2)
                            continue

                    print("[queue->server] send failed, waiting for reconnect")
                    self.connected_event.clear()
                    await asyncio.sleep(1.0)
            finally:
                if event_id:
                    self.pending_event_acks.pop(event_id, None)
                self.outbound_queue.task_done()

    async def connection_supervisor(self) -> None:
        backoff = CLIENT_RECONNECT_INITIAL_BACKOFF_SECONDS
        while not self.stop_event.is_set():
            listener_task: asyncio.Task | None = None
            keepalive_task: asyncio.Task | None = None
            try:
                await self.connect_server()
                backoff = CLIENT_RECONNECT_INITIAL_BACKOFF_SECONDS
                listener_task = asyncio.create_task(self.server_listener())
                keepalive_task = asyncio.create_task(self.keepalive_loop())
                done, pending = await asyncio.wait(
                    {listener_task, keepalive_task},
                    return_when=asyncio.FIRST_EXCEPTION,
                )

                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        raise exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[client] server connection lost/error: {exc}")
            finally:
                for task in (listener_task, keepalive_task):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (listener_task, keepalive_task) if task is not None),
                    return_exceptions=True,
                )
                await self._close_server_connection()

            if self.stop_event.is_set():
                break

            wait_time = min(backoff, CLIENT_RECONNECT_MAX_BACKOFF_SECONDS)
            print(f"[client] reconnecting in {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            backoff = min(backoff * CLIENT_RECONNECT_BACKOFF_MULTIPLIER, CLIENT_RECONNECT_MAX_BACKOFF_SECONDS)

    async def local_watcher(self) -> None:
        vlc = self._get_vlc()
        while True:
            try:
                state, position = await vlc.get_state()
            except Exception as exc:
                print(f"[client] local watcher read error: {exc}")
                await asyncio.sleep(LOCAL_WATCHER_ERROR_SLEEP_SECONDS)
                continue

            if self.last_state is None:
                self.last_state = state
                self.last_position = position
                await asyncio.sleep(LOCAL_WATCHER_SLEEP_SECONDS)
                continue

            now = now_seconds()
            if now < self.suppress_until:
                self.last_state = state
                self.last_position = position
                await asyncio.sleep(LOCAL_WATCHER_SLEEP_SECONDS)
                continue

            if state in {"playing", "paused"} and state != self.last_state:
                try:
                    await self.send_event(
                        "play" if state == "playing" else "pause",
                        position,
                        state,
                    )
                except Exception as exc:
                    print(f"[client] failed queueing playstate event: {exc}")
            elif (
                position is not None
                and self.last_position is not None
                and abs(position - self.last_position) > 1.5
            ):
                try:
                    await self.send_event("seek", position, state if state in {"playing", "paused"} else "paused")
                except Exception as exc:
                    print(f"[client] failed queueing seek event: {exc}")

            self.last_state = state
            self.last_position = position
            await asyncio.sleep(LOCAL_WATCHER_SLEEP_SECONDS)

    async def run(self) -> None:
        self.launch_vlc()
        vlc = self._get_vlc()
        await vlc.connect()
        print("[client] Connected to VLC Lua interface")
        await asyncio.gather(
            self.connection_supervisor(),
            self.sender_loop(),
            self.local_watcher(),
        )

    async def shutdown(self) -> None:
        self.stop_event.set()
        self.connected_event.set()
        await self._close_server_connection()
        if self.vlc_process is not None and self.vlc_process.poll() is None:
            self.vlc_process.terminate()


async def async_main() -> None:
    parser = argparse.ArgumentParser(
        description="Minimal VLC sync client",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "username",
        nargs="?",
        help="Username (positional override). If omitted, config value is used.",
    )
    parser.add_argument(
        "server_ip",
        nargs="?",
        help="Server IP/host (positional override). If omitted, config value is used.",
    )
    parser.add_argument(
        "port",
        nargs="?",
        type=_positive_port,
        help="Server port (positional override). If omitted, config value is used.",
    )
    parser.add_argument("--config", help="Path to client config JSON file.")
    parser.add_argument("--username", dest="username_opt", help="Username (overrides config and positional).")
    parser.add_argument("--server-ip", dest="server_ip_opt", help="Server IP/host (overrides config and positional).")
    parser.add_argument(
        "--port",
        dest="port_opt",
        type=_positive_port,
        help="Server port (overrides config and positional).",
    )
    parser.add_argument(
        "--lua-port",
        dest="lua_port_opt",
        type=_positive_port,
        default=None,
        help="Local Lua interface port. If omitted, auto-selects a free port.",
    )
    args = parser.parse_args()

    config_path = _client_config_path(args.config)
    config = load_or_create_client_config(config_path)

    username = _resolve_runtime_value(
        "username",
        config["username"],
        args.username,
        args.username_opt,
        config_path,
    )
    server_ip = _resolve_runtime_value(
        "server_ip",
        config["server_ip"],
        args.server_ip,
        args.server_ip_opt,
        config_path,
    )
    port = _resolve_runtime_value(
        "port",
        config["port"],
        args.port,
        args.port_opt,
        config_path,
    )
    lua_port = _resolve_runtime_value(
        "lua_port",
        config["lua_port"],
        None,
        args.lua_port_opt,
        config_path,
    )

    if server_ip is None:
        parser.error(
            f"server_ip is not set. Set it in config file {config_path} or pass via CLI argument."
        )
    if port is None:
        parser.error(
            f"port is not set. Set it in config file {config_path} or pass via CLI argument."
        )

    if not isinstance(username, str) or not username.strip():
        parser.error("username must be a non-empty string.")
    if not isinstance(server_ip, str) or not server_ip.strip():
        parser.error("server_ip must be a non-empty string.")
    if not isinstance(port, int):
        parser.error("port must be an integer.")
    if lua_port is not None and not isinstance(lua_port, int):
        parser.error("lua_port must be an integer when provided.")

    client = SyncClient(username.strip(), server_ip.strip(), port, lua_port)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    runner = asyncio.create_task(client.run())
    stopper = asyncio.create_task(stop_event.wait())

    done, pending = await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()

    if stopper in done:
        runner.cancel()

    try:
        await runner
    except asyncio.CancelledError:
        pass
    finally:
        await client.shutdown()


def run() -> None:
    try:
        asyncio.run(async_main())
    except ConfigError as exc:
        print(f"[config] error: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(0)
