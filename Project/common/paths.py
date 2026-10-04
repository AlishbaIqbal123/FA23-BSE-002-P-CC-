"""Filesystem locations and hard limits shared by the client and the server.

Every tunable that both processes must agree on lives here, so that a mismatch
can never be introduced by editing two copies of a constant.
"""

from __future__ import annotations

import os
from pathlib import Path

PROTOCOL_VERSION = "1.0"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 7575
DEFAULT_ALLOWED_SUBNETS = ("127.0.0.0/8", "::1/128", "192.168.1.0/24")

MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024
MAX_FRAME_PAYLOAD = 1 * 1024 * 1024
STREAM_CHUNK = 256 * 1024
UPLOAD_ACK_INTERVAL = 4 * 1024 * 1024

SOCKET_CONNECT_TIMEOUT = 8.0
SOCKET_READ_TIMEOUT = 300.0
STALL_TIMEOUT = 180.0

HEARTBEAT_INTERVAL = 2.0
PROGRESS_MIN_INTERVAL = 0.12

ROOT = Path(__file__).resolve().parent.parent
SERVER_DATA = Path(os.environ.get("RDO_SERVER_DATA", ROOT / "server" / "data"))
CLIENT_DATA = Path(os.environ.get("RDO_CLIENT_DATA", ROOT / "client" / "data"))

DEFAULT_PORT_FILE = ROOT / "server" / "data" / "daemon.json"

# Keeps a console window from flashing on Windows when a subprocess is spawned.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0