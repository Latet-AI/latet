"""Start the daemon at login: HKCU Run key (Windows), launchd agent (macOS), systemd user unit (Linux).
None of them needs administrator rights."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from latet.service.daemon import _daemon_command

_NAME = "Latet"
_LABEL = "dev.latet.daemon"


def _command() -> list[str]:
    return _daemon_command()


def install() -> str:
    command = _command()
    if os.name == "nt":
        import winreg

        line = subprocess.list2cmdline(command)
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, _NAME, 0, winreg.REG_SZ, line)
        return f"HKCU\\...\\Run\\{_NAME} = {line}"
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"<string>{a}</string>" for a in command)
        text = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            f'<plist version="1.0"><dict><key>Label</key><string>{_LABEL}</string>'
            f"<key>ProgramArguments</key><array>{args}</array>"
            # Started again when it fails, as systemd's Restart=on-failure: never after `latet stop`, a restore under
            # way or a Latet already running there, which exit 0.
            "<key>RunAtLoad</key><true/><key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>"
            "</dict></plist>\n"
        )
        domain = f"gui/{os.getuid()}"
        if plist.exists() and plist.read_text() != text:
            # launchd keeps the agent it loaded until it is booted out, which stops the daemon it started: only an
            # agent that changes is, so that bootstrap does not leave the earlier one in effect until the next login.
            subprocess.run(["launchctl", "bootout", domain, str(plist)], check=False, capture_output=True)  # noqa: S603,S607
        plist.write_text(text)
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=False, capture_output=True)  # noqa: S603,S607
        return str(plist)
    unit = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / "latet.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\nDescription=Latet privacy firewall for AI agents\n\n"
        f"[Service]\nExecStart={' '.join(command)}\nRestart=on-failure\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)  # noqa: S607
        subprocess.run(["systemctl", "--user", "enable", "latet.service"], check=False, capture_output=True)  # noqa: S607
    return str(unit)


def uninstall() -> str:
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, _NAME)
        except FileNotFoundError:
            return "not installed"
        return "removed"
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], check=False, capture_output=True)  # noqa: S603,S607
        plist.unlink(missing_ok=True)
        return "removed"
    unit = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / "latet.service"
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "latet.service"], check=False, capture_output=True)  # noqa: S607
    unit.unlink(missing_ok=True)
    return "removed"


def installed() -> bool:
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                winreg.QueryValueEx(key, _NAME)
        except FileNotFoundError:
            return False
        return True
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist").exists()
    return (
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / "latet.service"
    ).exists()
