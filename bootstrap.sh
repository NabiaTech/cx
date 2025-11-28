#!/bin/bash
# cx bootstrap - One-line installer for Codex Suite
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/NabiaTech/cx/main/bootstrap.sh | bash
#
# Or clone and run:
#   git clone https://github.com/NabiaTech/cx.git ~/.cx
#   ~/.cx/bootstrap.sh

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}▸${NC} $*"; }
warn() { echo -e "${YELLOW}▸${NC} $*"; }
error() { echo -e "${RED}▸${NC} $*" >&2; }

INSTALL_DIR="${CX_INSTALL_DIR:-$HOME/.cx}"
REPO_URL="https://github.com/NabiaTech/cx.git"

echo ""
echo "┌──────────────────────────────────────┐"
echo "│  cx - Codex Suite Bootstrap          │"
echo "│  Logging • Analytics • Federation    │"
echo "└──────────────────────────────────────┘"
echo ""

# Check dependencies
check_deps() {
    local missing=()

    command -v git >/dev/null || missing+=("git")
    command -v python3 >/dev/null || missing+=("python3")

    if [[ ${#missing[@]} -gt 0 ]]; then
        error "Missing dependencies: ${missing[*]}"
        echo ""
        echo "Install with:"
        echo "  macOS:  brew install ${missing[*]}"
        echo "  Ubuntu: sudo apt install ${missing[*]}"
        exit 1
    fi

    info "Dependencies OK (git, python3)"
}

# Check for codex CLI
check_codex() {
    if command -v codex >/dev/null; then
        info "Found codex: $(which codex)"
    else
        warn "codex CLI not found"
        echo ""
        echo "  Install codex first:"
        echo "    npm install -g @openai/codex"
        echo ""
        echo "  Or continue anyway (cx will work once codex is installed)"
        echo ""
    fi
}

# Clone or update repo
install_repo() {
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        info "Updating existing installation..."
        cd "$INSTALL_DIR"
        git pull --quiet
    else
        info "Installing to $INSTALL_DIR..."
        rm -rf "$INSTALL_DIR"
        git clone --quiet "$REPO_URL" "$INSTALL_DIR"
    fi

    chmod +x "$INSTALL_DIR/bin/cx"
    chmod +x "$INSTALL_DIR/lib"/*.py 2>/dev/null || true
    chmod +x "$INSTALL_DIR/lib/codex-log" 2>/dev/null || true
}

# Setup shell integration
setup_shell() {
    local shell_name=$(basename "$SHELL")
    local shell_rc=""

    case "$shell_name" in
        zsh)  shell_rc="$HOME/.zshrc" ;;
        bash) shell_rc="$HOME/.bashrc" ;;
        *)    shell_rc="$HOME/.profile" ;;
    esac

    local path_line="export PATH=\"$INSTALL_DIR/bin:\$PATH\""

    if grep -q "\.cx/bin" "$shell_rc" 2>/dev/null; then
        info "PATH already configured in $shell_rc"
    else
        echo "" >> "$shell_rc"
        echo "# Codex Suite (cx)" >> "$shell_rc"
        echo "$path_line" >> "$shell_rc"
        info "Added to PATH in $shell_rc"
    fi
}

# Create log directory
setup_logs() {
    mkdir -p "$HOME/.codexlogs"
    info "Log directory: ~/.codexlogs"
}

# Main
main() {
    check_deps
    check_codex
    install_repo
    setup_shell
    setup_logs

    echo ""
    echo "────────────────────────────────────────"
    echo ""
    info "Installation complete!"
    echo ""
    echo "  Next steps:"
    echo "    1. Reload shell:  source ~/.zshrc  (or restart terminal)"
    echo "    2. Verify:        cx status"
    echo "    3. Run:           cx --help"
    echo ""
    echo "  Quick start:"
    echo "    cx                     # Interactive session with logging"
    echo "    cx -m gpt-4o           # Use specific model"
    echo "    cx resume --last       # Resume last session"
    echo "    cx headless 'prompt'   # Non-interactive mode"
    echo ""
}

main "$@"
