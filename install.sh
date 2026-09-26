#!/usr/bin/env bash
# Install into the selected Hermes home; no configuration or vault data is changed.
set -euo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DEST="$INSTALL_HOME/plugins/instinct-memory"
ALIAS_DEST="$INSTALL_HOME/plugins/instinct"

# Hermes resolves memory.provider by directory name. Keep the canonical package
# directory and provide the shorter activation key without duplicating the code.
if [[ -e "$ALIAS_DEST" || -L "$ALIAS_DEST" ]]; then
    if [[ ! -L "$ALIAS_DEST" ]] || [[ "$(readlink "$ALIAS_DEST")" != instinct-memory ]]; then
        printf '%s\n' 'Cannot install: plugins/instinct is already used by another plugin.' >&2
        exit 1
    fi
fi

mkdir -p "$PLUGIN_DEST" "$INSTALL_HOME/scripts" "$INSTALL_HOME/skills/productivity/instinct-memory"
for source in "$SOURCE_DIR/plugin/instinct-memory/"*.py "$SOURCE_DIR/plugin/instinct-memory/plugin.yaml"; do
    cp -- "$source" "$PLUGIN_DEST/"
done
cp -- "$SOURCE_DIR/scripts/instinct_reconcile.py" "$SOURCE_DIR/scripts/instinct_reconcile.sh" "$INSTALL_HOME/scripts/"
cp -- "$SOURCE_DIR/skill/SKILL.md" "$INSTALL_HOME/skills/productivity/instinct-memory/SKILL.md"
chmod +x "$INSTALL_HOME/scripts/instinct_reconcile.py" "$INSTALL_HOME/scripts/instinct_reconcile.sh"
if [[ ! -L "$ALIAS_DEST" ]]; then
    ln -s instinct-memory "$ALIAS_DEST"
fi

printf '%s\n' 'Installed instinct-memory. Next, with the same HERMES_HOME:' \
    '  hermes config set memory.provider instinct' \
    'Restart Hermes to activate the provider.'
