"""The decision model (JevK5 via llama.cpp): a calibrated probability for every option, no generated text.

`Decider` uses BMK_DECIDER_URL if set, else a llama-server already listening on BMK_DECIDER_PORT, else
starts one with the cached GGUF and stops it when done.
"""

import json
import logging
import subprocess
import time
import urllib.request

from ..config import LOG_DIR
from . import AINotReady
from .models import decider_path

log = logging.getLogger(__name__)
MAX_OPTIONS_ONE_PASS = 16


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/health", timeout=2) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


class Decider:
    def __init__(self, config):
        self.config = config
        self.url = config.decider_url or f"http://127.0.0.1:{config.decider_port}"
        self._proc: subprocess.Popen | None = None
        self.model = None
        self.last_tokens, self.last_seconds = 0, 0.0
        self.name = config.decider_file

    def __enter__(self) -> "Decider":
        try:
            from jevk5.gguf import JevK5GGUF
        except ImportError as exc:
            raise AINotReady("the jevk5 client is not installed: scripts/install-ai.sh") from exc
        if not _healthy(self.url):
            if self.config.decider_url:
                raise AINotReady(f"no decision-model server at {self.url} (BMK_DECIDER_URL)")
            self._start()
        self.model = JevK5GGUF(self.url, temperature=self.config.decider_temperature,
                               knockout_temperature=self.config.decider_knockout_temperature)  # fmt: skip
        return self

    def _start(self) -> None:
        path = decider_path(self.config)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logfile = open(LOG_DIR / "llama-server.log", "ab")  # noqa: SIM115 - owned by the child process
        cmd = [self.config.llama_server, "-m", str(path), "-c", "8192", "-ngl", "99",
               "--host", "127.0.0.1", "--port", str(self.config.decider_port)]  # fmt: skip
        try:
            self._proc = subprocess.Popen(cmd, stdout=logfile, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise AINotReady("llama-server not found: brew install llama.cpp (or set BMK_LLAMA_SERVER)") from exc
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise AINotReady(f"llama-server exited ({self._proc.returncode}); see {LOG_DIR / 'llama-server.log'}")
            if _healthy(self.url):
                return
            time.sleep(0.5)
        self.close()
        raise AINotReady("llama-server did not become ready within 3 minutes")

    def choose(self, state: str, instructions: str, options: dict[str, str]) -> dict[str, float]:
        """Probability per option key."""
        result = self.model.decide(state, {"type": "choice", "instructions": instructions, "criteria": options})
        self.last_tokens = int(result.get("input_tokens") or 0)  # for job timing stats
        self.last_seconds = float(getattr(self.model, "last_seconds", 0.0) or 0.0)
        return result["probabilities"]

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def __exit__(self, *exc) -> None:
        self.close()
