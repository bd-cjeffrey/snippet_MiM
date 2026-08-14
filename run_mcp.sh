#!/bin/bash
# Launch the Black Duck snippet-scan MCP stdio server.
# Registered with: claude mcp add bd_llm_traffic_scan /abs/path/run_mcp.sh
cd "$(dirname "$0")" || exit 1
source ./source_bearer_demo.sh || exit 1
export PYTHONWARNINGS="ignore:Unverified HTTPS request"
exec ./.venv/bin/python mim_mcp.py
