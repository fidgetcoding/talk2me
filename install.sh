#!/usr/bin/env bash
# talk2me installer — clone, venv, install, put `t2m` on your PATH.
#
#   curl -fsSL https://raw.githubusercontent.com/fidgetcoding/talk2me/v2.7.0/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/fidgetcoding/talk2me/v2.7.0/install.sh | bash -s -- --parakeet
#   curl -fsSL https://raw.githubusercontent.com/fidgetcoding/talk2me/v2.7.0/install.sh | bash -s -- --whisper-gpu
#   curl -fsSL https://raw.githubusercontent.com/fidgetcoding/talk2me/v2.7.0/install.sh | bash -s -- --v1
#
# Installs the newest release tag, not whatever is on main right now; pass
# --ref main for the bleeding edge. Everything lands in ~/.talk2me (override with
# TALK2ME_HOME). Re-running updates to the newest tag (and re-running with a
# different --ref switches versions in place).
set -euo pipefail

REPO="https://github.com/fidgetcoding/talk2me.git"
DIR="${TALK2ME_HOME:-$HOME/.talk2me}"
BIN="$HOME/.local/bin"
REF=""          # any branch or tag: --ref main, --ref v1.0.0 (--v1 is shorthand)
EXTRA=""

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h)
      echo "talk2me installer. Installs the newest release into ~/.talk2me and puts t2m on your PATH."
      echo "  --ref <branch|tag>  a specific version (--ref main for the bleeding edge, --v1 for v1)"
      echo "  --whisper-gpu       also install Whisper large-v3-turbo for the GPU (Apple Silicon)"
      echo "  --parakeet          also install Parakeet for the GPU (Apple Silicon)"
      exit 0 ;;
    --parakeet|--whisper-gpu)
      [ "$(uname -s)-$(uname -m)" = "Darwin-arm64" ] || { echo "❌ $1 needs Apple Silicon; the default ears work fine without it"; exit 1; }
      EXTRA="[${1#--}]" ;;
    --v1) REF="v1" ;;
    --ref) shift; REF="${1:-}"; [ -n "$REF" ] || { echo "❌ --ref needs a value"; exit 1; } ;;
    *) echo "❌ unknown flag: $1 (known: --parakeet, --whisper-gpu, --ref <branch|tag>, --v1)"; exit 1 ;;
  esac
  shift
done

echo "🎙️  talk2me installer"

command -v git >/dev/null 2>&1 || { echo "❌ need git first"; exit 1; }

latest_tag() {
  # awk reads to the end on purpose: with pipefail, anything that closes the
  # pipe early (head, awk's exit) can kill the script on SIGPIPE.
  git ls-remote --tags --refs --sort=-v:refname "$REPO" 'v*' \
    | awk -F/ '!found && $NF ~ /^v[0-9]+\.[0-9]+\.[0-9]+$/ { print $NF; found = 1 }'
}

if [ -z "$REF" ]; then
  REF="$(latest_tag || true)"
  [ -n "$REF" ] || { echo "⚠️  couldn't find a release tag, installing main"; REF="main"; }
fi
echo "→ version: $REF"

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
[ -z "$EXTRA" ] || echo "→ installing with the GPU ears $EXTRA (Apple Silicon). Big dependencies, so a few minutes on a slow line…"
"$DIR/venv/bin/pip" install -q -e "$DIR/repo$EXTRA"

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
