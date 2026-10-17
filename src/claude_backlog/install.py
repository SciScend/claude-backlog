"""Install and remove the systemd user service, the /backlog skill and a launcher."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = ["install", "uninstall", "unit_text"]

UNIT_NAME = "claude-backlog.service"
SKILL_MARKER = "installed by claude-backlog"


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def unit_path() -> Path:
    return _config_home() / "systemd" / "user" / UNIT_NAME


def skill_path() -> Path:
    return Path.home() / ".claude" / "skills" / "backlog" / "SKILL.md"


def desktop_path() -> Path:
    return _data_home() / "applications" / "claude-backlog.desktop"


def unit_text(python=None, path_env=None) -> str:
    python = python or sys.executable
    path_env = path_env if path_env is not None else os.environ.get("PATH", "")
    return f"""[Unit]
Description=claude-backlog: board and runner for unattended Claude Code tasks
After=network-online.target

[Service]
ExecStart={python} -m claude_backlog serve
# A user service does not see the login shell's PATH; claude and git must be
# found as they are in a terminal.
Environment=PATH={path_env}
Restart=on-failure
RestartSec=5
# Stop only the service itself at first; it stops the running task cleanly
# (the task goes back to the queue) before anything else is killed.
KillMode=mixed
TimeoutStopSec=60

[Install]
WantedBy=default.target
"""


def desktop_text(port) -> str:
    return f"""[Desktop Entry]
Type=Application
Name=Claude Backlog
Comment=Tasks Claude Code works on while you are away
Exec=xdg-open http://127.0.0.1:{port}/
Icon=view-list-symbolic
Terminal=false
Categories=Development;Utility;
"""


def _systemctl(*args) -> bool:
    result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  systemctl --user {' '.join(args)}: {result.stderr.strip()}")
    return result.returncode == 0


def _install_skill() -> None:
    source = Path(__file__).parent / "skill" / "SKILL.md"
    target = skill_path()
    if target.exists() and SKILL_MARKER not in target.read_text(encoding="utf-8"):
        print(f"  skipped the /backlog skill: {target} exists and is not ours")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    print(f"  /backlog skill: {target}")


def _check_linger() -> None:
    if not shutil.which("loginctl"):
        return
    out = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p",
                          "Linger", "--value"], capture_output=True, text=True)
    if out.stdout.strip() == "no":
        print("  note: lingering is off, so the service stops when you log out. "
              "Turn it on with: loginctl enable-linger")


def setup_phone(port) -> int:
    if not shutil.which("tailscale"):
        print("Phone access needs Tailscale: https://tailscale.com/download")
        return 1
    target = f"http://127.0.0.1:{port + 1}"
    try:
        result = subprocess.run(["tailscale", "serve", "--bg", "--https=443", target],
                                capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        # tailscale serve waits for ever when Serve is not enabled on the
        # tailnet; its output then holds the page that enables it.
        out = exc.stdout or b""
        out = out.decode(errors="replace") if isinstance(out, bytes) else out
        print("tailscale serve did not finish:")
        print("  " + (out.strip() or "(no output)").replace("\n", "\n  "))
        print("Enable Serve (and HTTPS certificates) for your tailnet at the address "
              "above, then run 'claude-backlog install --phone' again.")
        return 1
    if result.returncode != 0:
        print("tailscale serve failed:")
        print("  " + (result.stderr.strip() or result.stdout.strip()))
        print("Is Tailscale up ('tailscale up')? If it asks for permission, run "
              "'sudo tailscale set --operator=$USER' once.")
        return 1
    print(result.stdout.strip())
    print("Phone access: enter your Tailscale login in the board's Settings, then open "
          "the address above on your phone (the Settings page shows a QR code).")
    return 0


def install(port, phone=False) -> int:
    print("Installing claude-backlog:")
    if not shutil.which("claude"):
        print("  warning: 'claude' is not on PATH; install Claude Code and log in first")
    unit = unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(unit_text(), encoding="utf-8")
    print(f"  service: {unit}")
    _install_skill()
    desktop = desktop_path()
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text(desktop_text(port), encoding="utf-8")
    print(f"  launcher: {desktop}")
    ok = _systemctl("daemon-reload") and _systemctl("enable", "--now", UNIT_NAME)
    if ok:
        _systemctl("restart", UNIT_NAME)
        print(f"  started; the board is at http://127.0.0.1:{port}/")
    _check_linger()
    if phone:
        return setup_phone(port)
    return 0 if ok else 1


def uninstall() -> int:
    print("Removing claude-backlog (your tasks and data stay):")
    _systemctl("disable", "--now", UNIT_NAME)
    for path in (unit_path(), desktop_path()):
        if path.exists():
            path.unlink()
            print(f"  removed {path}")
    skill = skill_path()
    if skill.exists() and SKILL_MARKER in skill.read_text(encoding="utf-8"):
        skill.unlink()
        with contextlib.suppress(OSError):
            skill.parent.rmdir()
        print(f"  removed {skill}")
    _systemctl("daemon-reload")
    print("If you set up phone access, turn it off with: tailscale serve --https=443 off")
    return 0
