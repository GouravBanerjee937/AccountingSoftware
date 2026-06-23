#!/bin/bash
# Start the accounting app + in-app assistant with all keys loaded.
# Usage:  bash run_server.sh     (Ctrl+C to stop)
cd "$(dirname "$0")"
source /Users/gourav/Work/invoice-mcp/set_keys.sh
exec ./.venv/bin/python server.py
