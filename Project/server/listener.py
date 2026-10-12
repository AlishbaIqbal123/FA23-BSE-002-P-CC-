"""TCP accept loop.

The main thread does nothing but ``accept()``. Every returned socket is handed
straight to a new :class:`~server.session.Session`, so a client that stops
reading - or a job that takes twenty minutes - can never stall the accept loop
and prevent the next client from being served. This is the same
thread-per-connection model as the lab, with the addition of the session's own
writer thread.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any, Callable

from common.messages import Capabilities
from .job_queue import JobPool
from .session import Session

LOG = logging.getLogger("rdo.listener")

READ_SIZE = 65_536


class Listener:
    def __init__(self, host: str, port: int, caps: Capabilities, pool: JobPool,
                 allowed: list[str], backlog: int = 16,
                 on_session_start: Callable[[Session], None] | None = None,
                 on_session_end: Callable[[Session], None] | None = None) -> None:
        self.host = host
        self.port = port
        self.caps = caps
        self.pool = pool
        self.allowed = allowed
        self.backlog = backlog
        self.on_session_start = on_session_start
        self.on_session_end = on_session_end
        self.sock: socket.socket | None = None
        self._stop = threading.Event()
        self._sessions: set[Session] = set()
        self._lock = threading.Lock()
        self.total_connections = 0
        self.active_connections = 0

    def bind(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(self.backlog)
        sock.settimeout(0.5)
        self.sock = sock
        self.port = sock.getsockname()[1]
        LOG.info("listening on %s:%d (backlog %d)", self.host, self.port, self.backlog)

    def serve_forever(self) -> None:
        if self.sock is None:
            self.bind()
        assert self.sock is not None
        LOG.info("accept loop started on thread %s", threading.current_thread().name)
        while not self._stop.is_set():
            try:
                client, address = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                LOG.exception("accept() failed")
                continue
            self._spawn(client, address)

    def _spawn(self, client: socket.socket, address: tuple[str, int]) -> None:
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        session = Session(client, address, self.caps, self.pool, self.allowed)

        with self._lock:
            self._sessions.add(session)
            self.total_connections += 1
            self.active_connections += 1
            ordinal = self.total_connections

        def serve() -> None:
            LOG.info("connection #%d open from %s:%d", ordinal, *address)
            if self.on_session_start:
                try:
                    self.on_session_start(session)
                except Exception:
                    pass
            try:
                session.run()
            finally:
                with self._lock:
                    self._sessions.discard(session)
                    self.active_connections -= 1
                if self.on_session_end:
                    try:
                        self.on_session_end(session)
                    except Exception:
                        pass
                LOG.info("connection #%d closed (%s:%d) | open=%d total=%d",
                         ordinal, *address, self.active_connections, self.total_connections)

        threading.Thread(target=serve, name=f"Session-{ordinal}-{address[0]}",
                         daemon=True).start()

    def get_active_sessions_info(self) -> list[dict[str, Any]]:
        """Return safe snapshot of active client sessions for UI monitoring."""
        with self._lock:
            sessions = list(self._sessions)
        info = []
        for s in sessions:
            info.append({
                "tag": getattr(s, "tag", f"{s.peer[0]}:{s.peer[1]}"),
                "peer_ip": s.peer[0],
                "peer_port": s.peer[1],
                "client_name": getattr(s, "client_name", "connecting"),
                "connected_at": getattr(s, "connected_at", time.time()),
                "active_jobs": len(getattr(s, "jobs", {})),
            })
        return info

    def stop(self) -> None:
        self._stop.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        with self._lock:
            sessions = list(self._sessions)
        for session in sessions:
            session.shutdown()
        LOG.info("listener stopped after %d connection(s)", self.total_connections)


__all__ = ["Listener"]