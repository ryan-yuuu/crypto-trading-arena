#!/bin/bash
#
# Trading Arena Startup Script (Shell Wrapper)
#
# Thin wrapper around start_arena.py. All logic lives in the Python script
# to avoid duplication and ensure feature parity (config support, multi-LLM, etc.).
#
# Usage:
#   ./start_arena.sh
#   ./start_arena.sh --cloud-broker broker.example.com:9092
#   ./start_arena.sh --with-viewer
#   ./start_arena.sh --help
#

set -euo pipefail

# Check for uv
if ! command -v uv &> /dev/null; then
    echo "Error: uv is not installed. Install from https://docs.astral.sh/uv/"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

exec uv run python start_arena.py "$@"
