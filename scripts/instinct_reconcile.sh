#!/usr/bin/env bash
# Works both from the checkout and after install.sh, including under cron.
set -euo pipefail

export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export PATH="$HOME/.local/bin:${PATH:-/usr/bin:/bin}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RECONCILE_PYTHON="$HERMES_HOME/hermes-agent/venv/bin/python"
[[ -x "$RECONCILE_PYTHON" ]] || RECONCILE_PYTHON="$(command -v python3)"
exec "$RECONCILE_PYTHON" "$SCRIPT_DIR/instinct_reconcile.py" "$@"
