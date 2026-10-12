"""Installs 1-click Windows Desktop shortcuts for Server, Client, and Hub.

Uses Windows Script Host (WScript.Shell) to create standard .lnk shortcut files
pointing to pythonw.exe so they open cleanly without flashing a command prompt.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def get_desktop_path() -> Path:
    """Find the user's active Windows Desktop directory."""
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        # Check standard desktop
        candidate = Path(user_profile) / "Desktop"
        if candidate.exists():
            return candidate
        # Check OneDrive desktop if OneDrive folder redirection is enabled
        candidate_onedrive = Path(user_profile) / "OneDrive" / "Desktop"
        if candidate_onedrive.exists():
            return candidate_onedrive

    # Fallback via PowerShell
    try:
        cmd = '[Environment]::GetFolderPath("Desktop")'
        out = subprocess.check_output(["powershell", "-NoProfile", "-Command", cmd], text=True).strip()
        if out:
            p = Path(out)
            if p.exists():
                return p
    except Exception:
        pass

    return Path.home() / "Desktop"


def find_pythonw() -> str:
    """Find pythonw.exe in the current Python environment for console-less launch."""
    py_dir = Path(sys.executable).parent
    pythonw = py_dir / "pythonw.exe"
    if pythonw.exists():
        return str(pythonw)
    return sys.executable


def create_shortcuts() -> list[str]:
    """Create Windows shortcuts on the desktop. Returns list of created shortcut paths."""
    desktop = get_desktop_path()
    if not desktop.exists():
        desktop.mkdir(parents=True, exist_ok=True)

    python_bin = find_pythonw()
    root_str = str(ROOT)

    shortcuts = [
        {
            "name": "Distributed System - Control Hub.lnk",
            "script": str(ROOT / "desktop_hub.py"),
            "desc": "Launch Distributed Computing Control Hub",
        },
        {
            "name": "Distributed System - Server Node.lnk",
            "script": str(ROOT / "server" / "app.py"),
            "desc": "Launch Remote GPU / Compute Server Node",
        },
        {
            "name": "Distributed System - Client Controller.lnk",
            "script": str(ROOT / "client" / "app.py"),
            "desc": "Launch Task Offloading Client Controller",
        },
    ]

    created = []
    for s in shortcuts:
        target_lnk = desktop / s["name"]
        ps_script = f"""
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut('{target_lnk}')
$Shortcut.TargetPath = '{python_bin}'
$Shortcut.Arguments = '"{s["script"]}"'
$Shortcut.WorkingDirectory = '{root_str}'
$Shortcut.Description = '{s["desc"]}'
$Shortcut.Save()
"""
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                check=True,
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            created.append(str(target_lnk))
        except Exception as exc:
            print(f"Failed to create {s['name']}: {exc}")

    return created


def main() -> int:
    print(f"Creating desktop shortcuts for Distributed Computing System in {ROOT}...")
    paths = create_shortcuts()
    if paths:
        print("\nCreated shortcuts on Desktop:")
        for p in paths:
            print(f"  + {p}")
        return 0
    print("Could not create shortcuts.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
