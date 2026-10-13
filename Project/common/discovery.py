"""UDP LAN auto-discovery and network diagnostic utilities.

Provides zero-configuration server discovery across a local area network (LAN)
and connectivity probing so users don't have to manually look up IP addresses
or debug shell commands.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import subprocess
import threading
import time
from typing import Any, Callable

from common.paths import CREATE_NO_WINDOW

LOG = logging.getLogger("rdo.discovery")

DEFAULT_DISCOVERY_PORT = 7576
MAGIC_PING = b"RDO_DISCOVERY_PING_V1"
MAGIC_PONG_PREFIX = b"RDO_DISCOVERY_PONG_V1:"


def _windows_adapter_map() -> dict[str, str]:
    """Query Windows for IPv4 address to interface alias mapping."""
    if os.name != "nt":
        return {}
    try:
        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-NetIPAddress -AddressFamily IPv4 | Select-Object -Property IPAddress, InterfaceAlias"
            ],
            capture_output=True, text=True, timeout=3.0,
            creationflags=CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            return {}
        result: dict[str, str] = {}
        for line in proc.stdout.strip().splitlines()[2:]:
            parts = line.split(None, 1)
            if len(parts) == 2:
                result[parts[0].strip()] = parts[1].strip()
        return result
    except Exception:
        return {}


def get_local_ip_addresses() -> list[dict[str, Any]]:
    """Enumerate IPv4 addresses available on local network adapters."""
    addresses: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1. Try socket.getaddrinfo on local hostname
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in seen:
                seen.add(ip)
    except OSError:
        pass

    # 2. Try routing probe to common gateway ranges
    for probe_target in ("8.8.8.8", "1.1.1.1", "10.0.0.1", "192.168.1.1"):
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect((probe_target, 9))
            ip = probe.getsockname()[0]
            probe.close()
            if ip not in seen:
                seen.add(ip)
        except OSError:
            pass

    adapter_map = _windows_adapter_map()

    for ip in sorted(seen):
        try:
            addr = ipaddress.ip_address(ip)
            is_loopback = addr.is_loopback
            is_private = addr.is_private
            is_link_local = addr.is_link_local
        except ValueError:
            is_loopback = ip.startswith("127.")
            is_private = False
            is_link_local = False

        if is_loopback:
            desc = "Loopback (Local Only)"
        elif is_link_local:
            desc = "Direct Cable (Link-Local)"
        elif ip in adapter_map:
            alias = adapter_map[ip]
            is_virt = any(
                k in alias.lower()
                for k in ("vmware", "virtualbox", "vethernet", "default switch", "bluetooth")
            )
            desc = f"{alias} (Virtual)" if is_virt else alias
        else:
            desc = "LAN / Wi-Fi"

        addresses.append({
            "ip": ip,
            "is_loopback": is_loopback,
            "is_private": is_private,
            "is_link_local": is_link_local,
            "description": desc,
        })

    # Ensure 127.0.0.1 is always present as fallback
    if not any(a["ip"] == "127.0.0.1" for a in addresses):
        addresses.insert(0, {
            "ip": "127.0.0.1",
            "is_loopback": True,
            "is_private": True,
            "is_link_local": False,
            "description": "Loopback (Local Only)",
        })

    return addresses


def get_primary_lan_ip() -> str:
    """Return the active routable LAN IP address to share with other computers."""
    # 1. Active routing probe: queries OS kernel for the interface that connects outbound
    for probe_target in ("8.8.8.8", "1.1.1.1", "10.0.0.1", "192.168.1.1"):
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect((probe_target, 80))
            ip = probe.getsockname()[0]
            probe.close()
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            pass

    # 2. Fallback: inspect adapters, filtering out virtual adapters (VMware, WSL, VirtualBox)
    addrs = get_local_ip_addresses()
    physical = [
        a for a in addrs
        if not a["is_loopback"] and not a["is_link_local"] and "virtual" not in a["description"].lower()
    ]
    if physical:
        for a in physical:
            if a["ip"].startswith("192.168.") or a["ip"].startswith("10."):
                return a["ip"]
        return physical[0]["ip"]

    # 3. Fallback to any non-loopback
    for a in addrs:
        if not a["is_loopback"]:
            return a["ip"]
    return "127.0.0.1"


def test_tcp_connection(host: str, port: int, timeout: float = 2.0) -> tuple[bool, float, str]:
    """Test TCP connectivity to a host and port.

    Returns:
        (success, latency_ms, message)
    """
    if not host:
        return False, 0.0, "Worker IP address cannot be empty"

    t0 = time.perf_counter()
    sock = None
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.settimeout(0.5)
        latency = (time.perf_counter() - t0) * 1000.0
        return True, latency, f"Successfully connected to {host}:{port} in {latency:.1f} ms"
    except socket.timeout:
        return False, 0.0, f"Connection timed out ({timeout}s). Check IP address or Windows Firewall on the server."
    except ConnectionRefusedError:
        return False, 0.0, f"Connection refused at {host}:{port}. Is the server application started and listening?"
    except OSError as exc:
        return False, 0.0, f"Network error connecting to {host}:{port}: {exc}"
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass


class DiscoveryService:
    """Listens for UDP discovery requests on the LAN and replies with server details."""

    def __init__(
        self,
        tcp_port: int,
        get_info_callback: Callable[[], dict[str, Any]] | None = None,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
    ) -> None:
        self.tcp_port = tcp_port
        self.get_info_callback = get_info_callback
        self.discovery_port = discovery_port
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass
            sock.bind(("", self.discovery_port))
            sock.settimeout(0.6)
            self._sock = sock
        except OSError as exc:
            LOG.warning("Could not bind UDP discovery port %d: %s", self.discovery_port, exc)
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="DiscoveryServer", daemon=True)
        self._thread.start()
        LOG.info("LAN auto-discovery service listening on UDP port %d", self.discovery_port)
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._sock is None:
                break
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue

            if not data.startswith(MAGIC_PING):
                continue

            info: dict[str, Any] = {
                "hostname": socket.gethostname(),
                "tcp_port": self.tcp_port,
                "gpu": "Unknown",
                "lan_ip": get_primary_lan_ip(),
            }
            if self.get_info_callback:
                try:
                    custom = self.get_info_callback()
                    info.update(custom)
                except Exception as exc:
                    LOG.debug("Error in get_info_callback: %s", exc)

            response = MAGIC_PONG_PREFIX + json.dumps(info).encode("utf-8")
            try:
                self._sock.sendto(response, addr)
                LOG.debug("Responded to discovery ping from %s:%d", addr[0], addr[1])
            except OSError as exc:
                LOG.debug("Failed to send discovery response to %s: %s", addr, exc)

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            self._thread = None


def discover_servers(
    timeout: float = 1.2,
    discovery_port: int = DEFAULT_DISCOVERY_PORT,
) -> list[dict[str, Any]]:
    """Scan the LAN for running GPU/Compute servers using UDP broadcast."""
    servers: list[dict[str, Any]] = []
    seen: set[str] = set()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.3)

    targets = ["255.255.255.255", "127.0.0.1"]
    for ip_info in get_local_ip_addresses():
        ip = ip_info["ip"]
        if ip != "127.0.0.1" and not ip_info["is_loopback"]:
            parts = ip.split(".")
            if len(parts) == 4:
                targets.append(f"{parts[0]}.{parts[1]}.{parts[2]}.255")
                if ip.startswith("169.254."):
                    targets.append("169.254.255.255")
                elif ip.startswith("10."):
                    targets.append(f"{parts[0]}.255.255.255")

    # Broadcast discovery ping
    for target in set(targets):
        try:
            sock.sendto(MAGIC_PING, (target, discovery_port))
        except OSError:
            pass

    deadline = time.perf_counter() + timeout
    by_host: dict[str, dict[str, Any]] = {}

    while time.perf_counter() < deadline:
        try:
            data, addr = sock.recvfrom(4096)
        except (socket.timeout, OSError):
            continue

        if data.startswith(MAGIC_PONG_PREFIX):
            raw_json = data[len(MAGIC_PONG_PREFIX):]
            try:
                payload = json.loads(raw_json.decode("utf-8"))
                responding_ip = addr[0]
                port = payload.get("tcp_port", 7575)
                hostname = payload.get("hostname", responding_ip)
                lan_ip = payload.get("lan_ip", responding_ip)
                key = f"{hostname}:{port}"

                # Prefer LAN IP over 127.0.0.1 or virtual switches if available
                chosen_ip = lan_ip if lan_ip else responding_ip
                if key not in by_host:
                    payload["ip"] = chosen_ip
                    payload["port"] = port
                    payload["responding_ip"] = responding_ip
                    by_host[key] = payload
                else:
                    # If existing entry has a loopback or virtual IP, upgrade to physical LAN IP
                    current_ip = by_host[key]["ip"]
                    if (current_ip.startswith("127.") or current_ip.startswith("172.")) and responding_ip.startswith("192.168."):
                        by_host[key]["ip"] = responding_ip
            except Exception:
                pass

    try:
        sock.close()
    except OSError:
        pass

    return list(by_host.values())
