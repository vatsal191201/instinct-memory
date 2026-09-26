#!/bin/bash
# Cron wrapper for the instinct memory reconciler.
#
# The reconciler only needs the plugin's own modules, but it resolves the vault and the
# plugin through HERMES_HOME and uses the Hermes venv for PyYAML — so pin both here rather
# than depending on the cron environment's PATH.
set -uo pipefail

export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export PATH="$HOME/.local/bin:$PATH"

PY="$HERMES_HOME/hermes-agent/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

exec "$PY" "$HERMES_HOME/scripts/instinct_reconcile.py" "$@"
