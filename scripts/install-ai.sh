#!/usr/bin/env bash
# The parts of the local AI that pip extras can't express (PLAN05 §11):
# - llama.cpp's llama-server (Homebrew), which runs the decision model;
# - the jevk5 client, pinned and installed without its declared dependencies (they pull in torch; the
#   GGUF client used here needs only the standard library).
# Models are not downloaded here: run `bmk ai download` (into the Hugging Face cache), then `bmk ai status`.
#
#   scripts/install-ai.sh                 # into the active venv
#   scripts/install-ai.sh path/to/python  # into another venv (install-stable.sh does this)
set -euo pipefail

JEVK5="jevk5 @ git+https://github.com/allebee/jevk5@f26426d16f59e8bbe1470e5b162cc89329e29b29"
PYTHON="${1:-$(command -v python)}"

if ! command -v llama-server >/dev/null; then
  echo "llama-server not found; install it with: brew install llama.cpp" >&2
fi
uv pip install -q --python "$PYTHON" --no-deps "$JEVK5"
echo "jevk5 client installed into $PYTHON"
