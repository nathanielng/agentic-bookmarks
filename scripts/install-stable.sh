#!/usr/bin/env bash
# Install the committed code (HEAD, or the commit given) into a separate venv and point the background
# scan at it, so work in progress in this checkout never runs against the real library.
#
#   scripts/install-stable.sh            # HEAD
#   scripts/install-stable.sh abc1234    # a specific commit
#
# Full Disk Access stays valid: the venv uses the same uv Python binary that was granted it.
set -euo pipefail

VENV="${BMK_STABLE_VENV:-$HOME/.local/share/bmk/venv}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
REV="$(git -C "$REPO" rev-parse --short "${1:-HEAD}")"

if [ -n "$(git -C "$REPO" status --porcelain -- src pyproject.toml)" ]; then
  echo "note: uncommitted changes in src/ are NOT installed (installing $REV)"
fi
[ -d "$VENV" ] || uv venv -q --python 3.13 "$VENV"
uv pip install -q --reinstall-package bmk --python "$VENV/bin/python" "bmk[web,enrich,files,macos,ai,mcp] @ git+file://$REPO@$REV"
"$REPO/scripts/install-ai.sh" "$VENV/bin/python" >/dev/null
cd "$HOME"  # so no project .env leaks into the agent's settings
"$VENV/bin/bmk" launchd install >/dev/null
echo "installed bmk $REV into $VENV; background scan restarted"
echo "daily use: $VENV/bin/bmk serve --open"
echo "MCP (Claude Code): claude mcp add bmk -- $VENV/bin/bmk mcp"
