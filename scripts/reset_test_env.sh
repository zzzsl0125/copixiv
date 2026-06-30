#!/usr/bin/env bash
# Reset the test environment — delete test DB and download directory.
# Shortcut for: python scripts/test_ingest.py --reset
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf test_env/
echo "Test environment reset. test_env/ deleted."
