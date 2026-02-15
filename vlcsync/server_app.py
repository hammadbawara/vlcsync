import argparse
import asyncio
import json
from dataclasses import dataclass
from typing import Optional

from vlcsync.common import (
    DEFAULT_EVENT_TTL_SECONDS,
    DEFAULT_SEND_RETRIES,
    DEFAULT_SESSION_TTL_SECONDS,
    JANITOR_INTERVAL_SECONDS,
    SERVER_CONNECT_TIMEOUT_SECONDS,
    SERVER_SEND_RETRY_DELAY_SECONDS,
    now_seconds,
    send_json_with_retry,
    utc_ms_now,
)


@dataclass
class Session:
    client_id: str
    username: str
    peer: str | None
    writer: Optional[asyncio.StreamWriter]
    connected: bool
    last_seen: float


class SyncServer:
    SESSION_TTL_SECONDS = DEFAULT_SESSION_TTL_SECONDS
    EVENT_TTL_SECONDS = DEFAULT_EVENT_TTL_SECONDS

    def __init__(self, session_ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self.session_ttl_seconds = session_ttl_seconds
        self.sessions: dict[str, Session] = {}
        self.writer_to_client_id: dict[asyncio.StreamWriter, str] = {}
        self.event_log: list[dict] = []
        self.event_receipts: dict[tuple[str, str], dict[str, int | float]] = {}
        self.next_seq = 1
        self.lock = asyncio.Lock()

    @staticmethod
    def _now() -> float:
        return now_seconds()

    def connected_count(self) -> int:
        return sum(1 for session in self.sessions.values() if session.connected)

    async def _send_with_retry(
        self,
        writer: asyncio.StreamWriter,
        payload: dict,
        *,
        retries: int = DEFAULT_SEND_RETRIES,
        delay_seconds: float = SERVER_SEND_RETRY_DELAY_SECONDS,
    ) -> bool:
        return await send_json_with_retry(
            writer,
            payload,
            retries=retries,
            delay_seconds=delay_seconds,
            compact=True,
            on_error=lambda exc, attempt, total: print(
                f"[send] failed attempt={attempt}/{total} payload={payload.get('type')} error={exc}"
            ),
        )

    async def send(self, writer: asyncio.StreamWriter, payload: dict) -> bool:
        return await self._send_with_retry(writer, payload)

    def _prune_event_log(self) -> None:
        cutoff = self._now() - self.EVENT_TTL_SECONDS
        self.event_log = [entry for entry in self.event_log if entry["ts"] >= cutoff]
        self.event_receipts = {
            key: receipt for key, receipt in self.event_receipts.items() if receipt["ts"] >= cutoff
        }

    async def _prune_sessions(self) -> None:
        cutoff = self._now() - self.session_ttl_seconds
        expired_ids: list[str] = []
        async with self.lock:
            for client_id, session in self.sessions.items():
                if not session.connected and session.last_seen < cutoff:
                    expired_ids.append(client_id)

            for client_id in expired_ids:
                session = self.sessions.pop(client_id)
                print(
                    "[session] discarded after ttl "
                    f"username={session.username} id={session.client_id} last_seen={session.last_seen}"
                )

        if expired_ids:
            await self.broadcast_user_count()

    async def janitor_loop(self) -> None:
        while True:
            try:
                self._prune_event_log()
                await self._prune_sessions()
            except Exception as exc:
                print(f"[janitor] error: {exc}")
            await asyncio.sleep(JANITOR_INTERVAL_SECONDS)

    async def disconnect_writer(self, writer: asyncio.StreamWriter, reason: str = "disconnect") -> None:
        async with self.lock:
            client_id = self.writer_to_client_id.pop(writer, None)
            if client_id is None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                return

            session = self.sessions.get(client_id)
            if session and session.writer is writer:
                session.connected = False
                session.writer = None
                session.last_seen = self._now()
                print(
                    f"[-] {session.username} id={session.client_id} disconnected reason={reason}; "
                    f"retained_for={self.session_ttl_seconds}s"
                )

        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

        await self.broadcast_user_count()

    async def broadcast_user_count(self) -> None:
        await self.broadcast({"type": "user_count", "count": self.connected_count()})

    async def broadcast(self, payload: dict) -> None:
        async with self.lock:
            targets = [session for session in self.sessions.values() if session.connected and session.writer is not None]

        stale_writers: list[asyncio.StreamWriter] = []
        for session in targets:
            writer = session.writer
            if writer is None:
                continue
            ok = await self._send_with_retry(writer, payload)
            if not ok:
                print(
                    f"[broadcast] failed username={session.username} "
                    f"id={session.client_id} peer={session.peer}"
                )
                stale_writers.append(writer)

        if payload.get("type") == "sync":
            print(
                "[broadcast] "
                f"event={payload.get('event')} seq={payload.get('seq')} "
                f"from={payload.get('by')} to_targets={len(targets)}"
            )

        for writer in stale_writers:
            await self.disconnect_writer(writer, reason="broadcast-failed")

    async def _register_or_resume(
        self,
        writer: asyncio.StreamWriter,
        username: str,
        client_id: str,
        peer: str,
    ) -> Session:
        old_writer_to_close: asyncio.StreamWriter | None = None
        async with self.lock:
            existing = self.sessions.get(client_id)
            if existing:
                if existing.writer and existing.writer is not writer:
                    old_writer_to_close = existing.writer
                    self.writer_to_client_id.pop(existing.writer, None)
                existing.username = username
                existing.peer = peer
                existing.writer = writer
                existing.connected = True
                existing.last_seen = self._now()
                session = existing
            else:
                session = Session(
                    client_id=client_id,
                    username=username,
                    peer=peer,
                    writer=writer,
                    connected=True,
                    last_seen=self._now(),
                )
                self.sessions[client_id] = session

            self.writer_to_client_id[writer] = client_id

        if old_writer_to_close is not None:
            await self.disconnect_writer(old_writer_to_close, reason="session-takeover")

        return session

    async def _replay_since(self, writer: asyncio.StreamWriter, last_seq: int) -> int:
        self._prune_event_log()
        replayed = 0
        for entry in self.event_log:
            payload = entry["payload"]
            if int(payload.get("seq", 0)) <= last_seq:
                continue
            ok = await self.send(writer, payload)
            if not ok:
                raise ConnectionError("failed while replaying sync history")
            replayed += 1
        return replayed

    def _count_replay_since(self, last_seq: int) -> int:
        self._prune_event_log()
        return sum(1 for entry in self.event_log if int(entry["payload"].get("seq", 0)) > last_seq)

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_str = str(peer)
        client_id: str | None = None
        try:
            hello_line = await asyncio.wait_for(reader.readline(), timeout=SERVER_CONNECT_TIMEOUT_SECONDS)
            if not hello_line:
                writer.close()
                await writer.wait_closed()
                return

            hello = json.loads(hello_line.decode("utf-8").strip())
            if hello.get("type") != "hello" or not isinstance(hello.get("username"), str):
                await self.send(writer, {"type": "error", "message": "expected hello"})
                writer.close()
                await writer.wait_closed()
                return

            username = hello["username"].strip() or "anonymous"
            client_id = str(hello.get("client_id", "")).strip()
            if not client_id:
                client_id = f"anon-{id(writer)}"
            try:
                last_seq = int(hello.get("last_seq", 0))
            except (TypeError, ValueError):
                last_seq = 0

            session = await self._register_or_resume(writer, username, client_id, peer_str)
            print(f"[+] {username} id={client_id} connected from {peer_str}")
            print(f"[*] Connected users: {self.connected_count()}")

            replay_pending = self._count_replay_since(last_seq)
            ok = await self.send(
                writer,
                {
                    "type": "welcome",
                    "users": self.connected_count(),
                    "client_id": session.client_id,
                    "replayed": replay_pending,
                },
            )
            if not ok:
                raise ConnectionError("failed to send welcome")

            replayed = await self._replay_since(writer, last_seq)
            if replayed != replay_pending:
                print(f"[replay] pending={replay_pending} sent={replayed} id={client_id}")

            await self.broadcast_user_count()

            while True:
                line = await reader.readline()
                if not line:
                    break

                try:
                    msg = json.loads(line.decode("utf-8").strip())
                except json.JSONDecodeError:
                    print(f"[recv] invalid json from {username} id={client_id}")
                    continue

                async with self.lock:
                    session = self.sessions.get(client_id)
                    if session is not None:
                        session.last_seen = self._now()

                msg_type = msg.get("type")
                if msg_type == "heartbeat":
                    ok = await self.send(writer, {"type": "heartbeat_ack", "ts": utc_ms_now()})
                    if not ok:
                        raise ConnectionError("failed to send heartbeat_ack")
                    continue

                if msg_type != "event":
                    continue

                event = msg.get("event")
                if event not in {"play", "pause", "seek"}:
                    continue

                event_id = str(msg.get("event_id", "")).strip()
                if event_id:
                    duplicate_seq = None
                    async with self.lock:
                        receipt = self.event_receipts.get((client_id, event_id))
                        if receipt is not None:
                            duplicate_seq = int(receipt["seq"])
                    if duplicate_seq is not None:
                        ok = await self.send(
                            writer,
                            {
                                "type": "event_ack",
                                "event_id": event_id,
                                "seq": duplicate_seq,
                            },
                        )
                        if not ok:
                            raise ConnectionError("failed to send duplicate event_ack")
                        continue

                position = msg.get("position")
                playback_state = msg.get("playback_state")
                try:
                    event_utc_ms = int(msg.get("event_utc_ms"))
                except (TypeError, ValueError):
                    event_utc_ms = utc_ms_now()

                if playback_state not in {"playing", "paused"}:
                    playback_state = "playing" if event == "play" else "paused"

                server_recv_utc_ms = utc_ms_now()
                print(
                    "[recv] "
                    f"from={username} id={client_id} peer={peer_str} "
                    f"event={event} pos={position} event_utc_ms={event_utc_ms} "
                    f"server_recv_utc_ms={server_recv_utc_ms}"
                )

                async with self.lock:
                    seq = self.next_seq
                    self.next_seq += 1

                payload = {
                    "type": "sync",
                    "event": event,
                    "event_id": event_id,
                    "position": position,
                    "playback_state": playback_state,
                    "event_utc_ms": event_utc_ms,
                    "server_recv_utc_ms": server_recv_utc_ms,
                    "server_sent_utc_ms": utc_ms_now(),
                    "by": username,
                    "by_id": client_id,
                    "seq": seq,
                }
                self.event_log.append({"ts": self._now(), "payload": payload})
                if event_id:
                    async with self.lock:
                        self.event_receipts[(client_id, event_id)] = {
                            "seq": seq,
                            "ts": self._now(),
                        }

                if event_id:
                    ok = await self.send(
                        writer,
                        {
                            "type": "event_ack",
                            "event_id": event_id,
                            "seq": seq,
                        },
                    )
                    if not ok:
                        raise ConnectionError("failed to send event_ack")

                self._prune_event_log()
                await self.broadcast(payload)

        except asyncio.TimeoutError:
            print(f"[conn] handshake timeout peer={peer_str}")
        except (ConnectionError, OSError, asyncio.IncompleteReadError, json.JSONDecodeError) as exc:
            print(f"[conn] connection error peer={peer_str} error={exc}")
        except Exception as exc:
            print(f"[conn] unexpected error peer={peer_str} error={exc}")
        finally:
            await self.disconnect_writer(writer, reason="connection-closed")


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="Minimal VLC sync server")
    parser.add_argument("port", type=int, help="Port to listen on")
    parser.add_argument(
        "--session-ttl-seconds",
        type=int,
        default=SyncServer.SESSION_TTL_SECONDS,
        help="Offline session retention window in seconds (default: 1800)",
    )
    args = parser.parse_args()

    server_state = SyncServer(session_ttl_seconds=args.session_ttl_seconds)
    server = await asyncio.start_server(server_state.handle_client, host="0.0.0.0", port=args.port)
    janitor_task = asyncio.create_task(server_state.janitor_loop())

    sockets = server.sockets or []
    for sock in sockets:
        addr = sock.getsockname()
        print(f"[server] listening on {addr[0]}:{addr[1]}")

    try:
        async with server:
            await server.serve_forever()
    finally:
        janitor_task.cancel()
        try:
            await janitor_task
        except asyncio.CancelledError:
            pass


def run() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\n[server] stopped")
