#!/usr/bin/env bash
# install.sh — install claude-clean-sessions into ~/.claude/
#
# Copies the slash command and the Python helper to the right places.
# Safe to re-run (idempotent). Existing files are backed up with a .bak suffix.
#
# Refuses to operate through symlinks to avoid hijack attacks.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
CMD_DIR="$CLAUDE_DIR/commands"
LIB_DIR="$CMD_DIR/lib"
TARGET_CMD="$CMD_DIR/clean-sessions.md"
TARGET_LIB="$LIB_DIR/clean_sessions.py"

refuse_symlink() {
    local path="$1"
    if [ -L "$path" ]; then
        echo "ERROR: $path is a symlink; refusing to install through it." >&2
        echo "       Investigate and remove the symlink manually before retrying." >&2
        exit 1
    fi
}

backup_if_exists() {
    local path="$1"
    if [ -e "$path" ] && [ ! -L "$path" ]; then
        local bak="${path}.bak.$(date +%Y%m%d-%H%M%S).$$"
        # -p preserves mode/ownership; no dereference flag because we refuse symlinks above.
        cp -p "$path" "$bak"
        echo "  backed up existing file to $bak"
    fi
}

safe_install() {
    local src="$1"
    local dest="$2"
    refuse_symlink "$dest"
    backup_if_exists "$dest"
    # Remove any prior regular file so cp doesn't chase a newly-planted symlink race.
    rm -f "$dest"
    cp -p "$src" "$dest"
}

echo "Installing claude-clean-sessions..."

# Verify directories are not symlinks before creating/writing into them.
for dir in "$CLAUDE_DIR" "$CMD_DIR" "$LIB_DIR"; do
    refuse_symlink "$dir"
done

mkdir -p "$LIB_DIR"

safe_install "$SCRIPT_DIR/commands/clean-sessions.md" "$TARGET_CMD"
echo "  installed $TARGET_CMD"

safe_install "$SCRIPT_DIR/commands/lib/clean_sessions.py" "$TARGET_LIB"
chmod +x "$TARGET_LIB"
echo "  installed $TARGET_LIB"

if ! command -v python3 >/dev/null 2>&1; then
    echo "WARNING: python3 not found on PATH — you will need to install Python 3.9+"
fi

if [ ! -d /proc ] && ! command -v lsof >/dev/null 2>&1; then
    echo "ERROR: neither /proc nor lsof is available — running-session detection" >&2
    echo "       cannot function. Install lsof before using /clean-sessions." >&2
    exit 1
fi

echo ""
echo "Done. Open Claude Code and run:"
echo "  /clean-sessions"
