"""Model files in the Hugging Face cache: where they are, whether they are there, and fetching them."""

import importlib.util
import shutil
from pathlib import Path
from typing import Any

from . import AINotReady


def _hub():
    try:
        import huggingface_hub
    except ImportError as exc:
        raise AINotReady('local AI needs the "ai" extra: uv pip install -e ".[ai]"') from exc
    return huggingface_hub


def llm_path(config) -> Path:
    hub = _hub()
    try:
        return Path(hub.snapshot_download(config.llm_model, local_files_only=True))
    except Exception as exc:  # LocalEntryNotFoundError and friends
        raise AINotReady(f"{config.llm_model} is not in the Hugging Face cache; run `bmk ai download`") from exc


def decider_path(config) -> Path:
    hub = _hub()
    try:
        return Path(hub.hf_hub_download(config.decider_repo, config.decider_file, local_files_only=True))
    except Exception as exc:
        raise AINotReady(f"{config.decider_repo}/{config.decider_file} is not in the Hugging Face cache; run `bmk ai download`") from exc


def status(config) -> list[dict[str, Any]]:
    """One row per piece: the LLM, the decision model, llama-server, the jevk5 client, mlx-lm."""
    rows = []
    for name, fn, ident in (("LLM (category list)", llm_path, config.llm_model),
                            ("Decision model (classifier)", decider_path, f"{config.decider_repo}/{config.decider_file}")):  # fmt: skip
        try:
            path = fn(config)
            size = sum(p.stat().st_size for p in (path.rglob("*") if path.is_dir() else [path]) if p.is_file())
            rows.append({"name": name, "id": ident, "ok": True, "detail": f"{size / 1e9:.1f} GB in the Hugging Face cache"})
        except AINotReady as exc:
            rows.append({"name": name, "id": ident, "ok": False, "detail": str(exc)})
    server = shutil.which(config.llama_server)
    rows.append({"name": "llama-server", "id": config.llama_server, "ok": bool(server or config.decider_url),
                 "detail": server or (config.decider_url and f"using {config.decider_url}") or "not found: brew install llama.cpp"})  # fmt: skip
    for name, module, hint in (("jevk5 client", "jevk5.gguf", "scripts/install-ai.sh"), ("mlx-lm", "mlx_lm", 'uv pip install -e ".[ai]"')):
        try:
            found = importlib.util.find_spec(module) is not None  # without importing: mlx takes seconds to load
        except ImportError:
            found = False
        rows.append({"name": name, "id": module, "ok": found, "detail": "installed" if found else f"not installed: {hint}"})
    return rows


def download(config, which: str = "all") -> list[str]:
    """Fetch missing model files into the Hugging Face cache (explicit; normal runs never download)."""
    hub = _hub()
    done = []
    if which in ("all", "llm"):
        done.append(hub.snapshot_download(config.llm_model))
    if which in ("all", "decider"):
        done.append(hub.hf_hub_download(config.decider_repo, config.decider_file))
    return done
