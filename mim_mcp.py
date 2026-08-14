#!/usr/bin/env python3
"""MCP stdio server exposing Black Duck snippet-match as a tool.

Wraps the same scan primitive that mim_proxy.py uses (`scan_file`) so a
Claude Code session can check code for reciprocal/copyleft license matches
without needing to be routed through the HTTP proxy. Claude decides when
to call the tool and how to react to the result.

Register once:
  claude mcp add bd_llm_traffic_scan /abs/path/run_mcp.sh

Env:
  BEARER_TOK, BLACKDUCK_HOST -- required for scans (server still starts
                                without them; each scan_code call returns
                                a structured error instead of crashing).
"""

import logging
import os
import subprocess
import sys
import time

# Log to stderr BEFORE importing mim_proxy — its module-level logging.basicConfig
# call would otherwise default to stdout in some setups, and stdout is the
# MCP JSON-RPC channel. Any leaked byte here breaks the transport.
_LEVEL_MAP = {
    "off": logging.CRITICAL + 10,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}
LOG_LEVEL_NAME = os.environ.get("MIM_MCP_LOG_LEVEL", "info").lower()
LOG_LEVEL = _LEVEL_MAP.get(LOG_LEVEL_NAME, logging.INFO)
LOG_FILE = os.environ.get("MIM_MCP_LOG_FILE") or None

_handlers = [logging.StreamHandler(sys.stderr)]
if LOG_FILE:
    _handlers.append(logging.FileHandler(LOG_FILE))
logging.basicConfig(
    level=LOG_LEVEL,
    handlers=_handlers,
    format="%(asctime)s %(levelname)-5s [mcp] %(message)s",
    datefmt="%H:%M:%S",
)

from mim_proxy import (  # noqa: E402
    LARGE_SNIPPET_LIMIT,
    SMALL_SNIPPET_LIMIT,
    find_reciprocal_matches,
    scan_error_message,
    scan_file,
)
from mcp.server.fastmcp import FastMCP  # noqa: E402

log = logging.getLogger("mim_mcp")
log.info(
    "starting bd_llm_traffic_scan MCP server (log_level=%s%s)",
    LOG_LEVEL_NAME,
    f", log_file={LOG_FILE}" if LOG_FILE else "",
)
mcp = FastMCP("bd_llm_traffic_scan")


def _non_ws_len(s: str) -> int:
    return sum(1 for c in s if not c.isspace())


@mcp.tool()
def scan_code(code: str) -> dict:
    """Scan a code snippet against Black Duck's snippet-match KnowledgeBase
    and report reciprocal/copyleft license hits.

    Returns a dict:
      clean         -- true if no RECIPROCAL / WEAK_RECIPROCAL matches.
      hits          -- list of {category, project, version, license, spdx,
                       ownership, path, source_start, source_end,
                       matched_start, matched_end}. Empty when clean.
      summary       -- one-line human-readable summary.
      http_status   -- HTTP status code from the SCA scan endpoint (int or null).
      skipped       -- present and true when the input was too small to scan
                       (<300 non-whitespace chars).
      error         -- present when the scan itself failed (missing bearer,
                       HTTP 4xx from SCA, malformed response, subprocess timeout).

    Concurrent calls are safe: each invocation runs in its own tempdir.
    """
    nws = _non_ws_len(code)
    log.info("scan_code called chars=%d nws=%d", len(code), nws)
    log.debug("scan_code input head: %r", code[:200])

    if nws < SMALL_SNIPPET_LIMIT:
        log.info("scan_skipped reason=too_small nws=%d limit=%d", nws, SMALL_SNIPPET_LIMIT)
        return {
            "clean": True,
            "hits": [],
            "skipped": True,
            "summary": f"input too small to scan ({nws} < {SMALL_SNIPPET_LIMIT} non-ws chars)",
            "http_status": None,
        }
    if nws > LARGE_SNIPPET_LIMIT:
        log.info("scan_skipped reason=too_large nws=%d limit=%d", nws, LARGE_SNIPPET_LIMIT)
        return {
            "clean": False,
            "hits": [],
            "error": (
                f"input too large ({nws} non-ws chars > {LARGE_SNIPPET_LIMIT}); "
                "split the code into smaller chunks and call scan_code once per chunk"
            ),
            "summary": "input rejected: too large",
            "http_status": None,
        }

    log.info("scan_start nws=%d", nws)
    t0 = time.monotonic()
    try:
        result = scan_file(code)
    except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        log.warning("scan_error ms=%d err=%s", elapsed, e)
        return {
            "clean": False,
            "hits": [],
            "error": str(e),
            "summary": "scan failed",
            "http_status": None,
        }

    elapsed = int((time.monotonic() - t0) * 1000)
    http_status = result.get("_http_status") if isinstance(result, dict) else None
    err = scan_error_message(result)
    if err:
        log.info("scan_result_error ms=%d http_status=%s err=%s",
                 elapsed, http_status, err[:200])
        return {
            "clean": False,
            "hits": [],
            "error": err,
            "summary": "scan returned an error",
            "http_status": http_status,
        }

    hits = find_reciprocal_matches([result])
    if hits:
        log.info("scan_ok ms=%d http_status=%s hits=%d",
                 elapsed, http_status, len(hits))
        sample = [(h.get("category"), h.get("project"), h.get("license")) for h in hits[:3]]
        log.info("rewrite_required hits=%d sample=%r", len(hits), sample)
        log.debug("hits detail: %r",
                  [(h.get("category"), h.get("project"), h.get("license")) for h in hits])
    else:
        log.info("scan_ok ms=%d http_status=%s clean", elapsed, http_status)
    return {
        "clean": not hits,
        "hits": hits,
        "summary": (
            f"{len(hits)} reciprocal/weak-reciprocal match(es)"
            if hits else "no reciprocal matches"
        ),
        "http_status": http_status,
    }


if __name__ == "__main__":
    mcp.run()
