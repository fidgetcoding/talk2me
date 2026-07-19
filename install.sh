#!/usr/bin/env bash
# talk2me installer — clone, venv, install, put `t2m` on your PATH.
#
#   curl -fsSL https://raw.githubusercontent.com/fidgetcoding/talk2me/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/fidgetcoding/talk2me/main/install.sh | bash -s -- --parakeet
#
# Everything lands in ~/.talk2me (override with TALK2ME_HOME). Re-running updates.
set -euo pipefail

REPO="https://github.com/fidgetcoding/talk2me.git"
DIR="${TALK2ME_HOME:-$HOME/.talk2me}"
BIN="$HOME/.local/bin"

echo "🎙️  talk2me installer"

command -v git >/dev/null 2>&1 || { echo "❌ need git first"; exit 1; }
PY="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3 || true)"
[ -n "$PY" ] || { echo "❌ need Python 3.11+"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "❌ Python 3.11+ required (found $("$PY" -V))"; exit 1; }

if [ -d "$DIR/repo/.git" ]; then
  echo "→ updating existing install…"
  git -C "$DIR/repo" pull --ff-only
else
  mkdir -p "$DIR"
  echo "→ cloning…"
  git clone --depth 1 "$REPO" "$DIR/repo"
fi

echo "→ building venv…"
"$PY" -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
if [ "${1:-}" = "--parakeet" ]; then
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
