#!/usr/bin/env bash
# talk2me installer — clone, venv, install, put `t2m` on your PATH.
#
#   curl -fsSL https://raw.githubusercontent.com/fidgetcoding/talk2me/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/fidgetcoding/talk2me/main/install.sh | bash -s -- --parakeet
#   curl -fsSL https://raw.githubusercontent.com/fidgetcoding/talk2me/main/install.sh | bash -s -- --v1
#
# Everything lands in ~/.talk2me (override with TALK2ME_HOME). Re-running updates
# (and re-running with a different --ref switches versions in place).
set -euo pipefail

REPO="https://github.com/fidgetcoding/talk2me.git"
DIR="${TALK2ME_HOME:-$HOME/.talk2me}"
BIN="$HOME/.local/bin"
REF="main"      # any branch or tag: --ref v1, --ref v1.0.0 (--v1 is shorthand)
PARAKEET=0

while [ $# -gt 0 ]; do
  case "$1" in
    --parakeet) PARAKEET=1 ;;
    --v1) REF="v1" ;;
    --ref) shift; REF="${1:-}"; [ -n "$REF" ] || { echo "❌ --ref needs a value"; exit 1; } ;;
    *) echo "❌ unknown flag: $1 (known: --parakeet, --ref <branch|tag>, --v1)"; exit 1 ;;
  esac
  shift
done

echo "🎙️  talk2me installer"
[ "$REF" = "main" ] || echo "→ version: $REF"

command -v git >/dev/null 2>&1 || { echo "❌ need git first"; exit 1; }
PY="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3 || true)"
[ -n "$PY" ] || { echo "❌ need Python 3.11+"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "❌ Python 3.11+ required (found $("$PY" -V))"; exit 1; }

if [ -d "$DIR/repo/.git" ]; then
  echo "→ updating existing install…"
  # fetch + detach instead of pull: works identically for branches and tags,
  # and re-running with a different --ref switches versions cleanly.
  git -C "$DIR/repo" fetch --depth 1 origin "$REF"
  git -C "$DIR/repo" checkout -q --detach FETCH_HEAD
else
  mkdir -p "$DIR"
  echo "→ cloning…"
  git clone --depth 1 --branch "$REF" "$REPO" "$DIR/repo"
fi

echo "→ building venv…"
"$PY" -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
if [ "$PARAKEET" = "1" ]; then
  echo "→ installing with the Parakeet GPU ears (Apple Silicon)…"
  "$DIR/venv/bin/pip" install -q -e "$DIR/repo[parakeet]"
else
  "$DIR/venv/bin/pip" install -q -e "$DIR/repo"
fi

mkdir -p "$BIN"
ln -sf "$DIR/venv/bin/talk2me" "$BIN/talk2me"
ln -sf "$DIR/venv/bin/t2m" "$BIN/t2m"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "⚠️  $BIN isn't on your PATH — add this to your shell profile:"
     echo '     export PATH="$HOME/.local/bin:$PATH"' ;;
esac

echo "✅ installed. Say hi:"
echo "     t2m"
