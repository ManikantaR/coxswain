#!/usr/bin/env bash
# Launch the dashboard against a seeded demo home for visual verification.
set -euo pipefail
cd "$(dirname "$0")/.."
export COX_HOME="${COX_HOME:-/tmp/cox-demo}"
exec .venv/bin/python -u -m cox.cli serve --host 127.0.0.1 --port 8792 --token demo
