#!/usr/bin/env bash
# Forge installer for macOS (Apple Silicon / Intel) and Linux. One command, fully set up:
#
#   curl -fsSL https://raw.githubusercontent.com/eshangulati1234-a11y/forge-agent/main/install.sh | bash
#
# Idempotent: safe to re-run to update or repair. Environment knobs:
#   FORGE_APP_DIR=~/.forge-app   where the code lives when installing from the curl one-liner
#   FORGE_NO_PULL=1              skip the model downloads (they can be pulled later with `forge setup`)
#   FORGE_NO_STARTUP=1           do not install the start-at-login item
set -euo pipefail

REPO_URL="${FORGE_REPO_URL:-https://github.com/eshangulati1234-a11y/forge-agent.git}"
APP_DIR="${FORGE_APP_DIR:-$HOME/.forge-app}"
BIN_DIR="$HOME/.local/bin"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

OS="$(uname -s)"
case "$OS" in Darwin|Linux) ;; *) die "Unsupported OS '$OS'. On Windows use install.ps1." ;; esac
export PATH="$BIN_DIR:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# 1. uv (manages Python 3.12 and the virtualenv; nothing else needs to be pre-installed)
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$BIN_DIR:$PATH"
fi
command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH; open a new terminal and re-run."

# 2. the code: use this checkout if the script runs from one, otherwise clone/update APP_DIR
SRC=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/pyproject.toml" ] \
   && grep -q '^name = "forge"' "$(dirname "${BASH_SOURCE[0]}")/pyproject.toml" 2>/dev/null; then
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -z "$SRC" ]; then
  command -v git >/dev/null 2>&1 || die "git is required (macOS: xcode-select --install)."
  if [ -d "$APP_DIR/.git" ]; then
    say "Updating Forge in $APP_DIR"
    git -C "$APP_DIR" pull --ff-only
  else
    say "Downloading Forge to $APP_DIR"
    git clone --depth 1 "$REPO_URL" "$APP_DIR"
  fi
  SRC="$APP_DIR"
fi
cd "$SRC"

# 3. Python dependencies (uv picks the Mac-native torch build on macOS, CUDA torch on Linux/Windows)
say "Installing Python dependencies"
uv sync --python 3.12

# 4. put `forge` on PATH
mkdir -p "$BIN_DIR"
ln -sf "$SRC/.venv/bin/forge" "$BIN_DIR/forge"
SHELL_RC=""
case "${SHELL:-}" in */zsh) SHELL_RC="$HOME/.zshrc" ;; */bash) SHELL_RC="$HOME/.bashrc" ;; esac
if [ -n "$SHELL_RC" ] && ! grep -qs '\.local/bin' "$SHELL_RC" 2>/dev/null; then
  printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$SHELL_RC"
  say "Added ~/.local/bin to PATH in $SHELL_RC"
fi

# 5. Ollama (local model runtime). Homebrew if present, otherwise the official app/installer.
if ! command -v ollama >/dev/null 2>&1 && [ ! -x /Applications/Ollama.app/Contents/Resources/ollama ] \
   && [ ! -x "$HOME/Applications/Ollama.app/Contents/Resources/ollama" ]; then
  if [ "$OS" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
      say "Installing Ollama with Homebrew"
      brew install ollama
    else
      say "Installing Ollama from ollama.com"
      TMP="$(mktemp -d)"
      curl -fL https://ollama.com/download/Ollama-darwin.zip -o "$TMP/ollama.zip"
      mkdir -p "$HOME/Applications"
      ditto -x -k "$TMP/ollama.zip" "$HOME/Applications"
      rm -rf "$TMP"
    fi
  else
    say "Installing Ollama"
    curl -fsSL https://ollama.com/install.sh | sh
  fi
fi

# 6. models, browser, Laya checkpoint, signing key, start-at-login, health check
ARGS=(setup --yes --install-ollama)
[ "${FORGE_NO_PULL:-0}" = "1" ] && ARGS+=(--no-pull)
if [ "${FORGE_NO_STARTUP:-0}" = "1" ]; then ARGS+=(--no-startup); else ARGS+=(--startup); fi
say "Running Forge setup (downloads models; this can take a while the first time)"
"$SRC/.venv/bin/forge" "${ARGS[@]}"

echo
say "Done. Open a new terminal (or run: export PATH=\"\$HOME/.local/bin:\$PATH\") and start with:  forge"
