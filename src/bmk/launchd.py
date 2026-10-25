"""launchd agent that polls iCloud Tabs in the background (PLAN05 §4.2).

The agent runs `python -m bmk ingest cloudtabs` every `poll_interval` seconds. Full Disk Access must be
granted to the interpreter launchd starts (the resolved binary, not the venv symlink), which is why
`interpreter()` is reported by `bmk launchd install` and `bmk doctor`.
"""

import os
import plistlib
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import LOG_DIR, Config

LABEL = "com.bmk.cloudtabs"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_PATH = LOG_DIR / "cloudtabs.log"

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def interpreter() -> Path:
    """The real Python binary (what launchd executes, and what needs Full Disk Access)."""
    return Path(os.path.realpath(sys.executable))


def _domain() -> str:
    return f"gui/{os.getuid()}"


def render_plist(config: Config, python: str | None = None, env: dict[str, str] | None = None) -> bytes:
    env = os.environ if env is None else env
    # Pass through bmk settings so the agent sees the same library and filters as the shell that installed it.
    passthrough = {k: v for k, v in env.items() if k.startswith("BMK_")}
    passthrough["BMK_DB_PATH"] = str(config.db_path)
    data = {
        "Label": LABEL,
        "ProgramArguments": [python or sys.executable, "-m", "bmk", "ingest", "cloudtabs"],
        "StartInterval": config.poll_interval,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "EnvironmentVariables": dict(sorted(passthrough.items())),
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
    }
    return plistlib.dumps(data)


@dataclass
class AgentStatus:
    installed: bool
    loaded: bool
    plist_path: Path
    last_exit: int | None = None
    detail: str = ""


def install(config: Config, run: Runner = _run, plist_path: Path | None = None) -> AgentStatus:
    plist_path = plist_path or PLIST_PATH
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(render_plist(config))
    run(["launchctl", "bootout", f"{_domain()}/{LABEL}"])  # replace any previous version; fails harmlessly if absent
    result = run(["launchctl", "bootstrap", _domain(), str(plist_path)])
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {(result.stderr or result.stdout).strip()}")
    return status(run, plist_path)


def uninstall(run: Runner = _run, plist_path: Path | None = None) -> None:
    run(["launchctl", "bootout", f"{_domain()}/{LABEL}"])
    (plist_path or PLIST_PATH).unlink(missing_ok=True)


def status(run: Runner = _run, plist_path: Path | None = None) -> AgentStatus:
    plist_path = plist_path or PLIST_PATH
    installed = plist_path.exists()
    result = run(["launchctl", "print", f"{_domain()}/{LABEL}"])
    if result.returncode != 0:
        return AgentStatus(installed=installed, loaded=False, plist_path=plist_path)
    match = re.search(r"last exit code = (-?\d+)", result.stdout)
    return AgentStatus(
        installed=installed,
        loaded=True,
        last_exit=int(match.group(1)) if match else None,
        plist_path=plist_path,
        detail="running" if re.search(r"state = running", result.stdout) else "",
    )
