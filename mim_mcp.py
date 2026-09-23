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

import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
    SMALL_SNIPPET_LIMIT,
    find_reciprocal_matches,
    group_snippets,
    scan_error_message,
    scan_file,
)
from mcp.server.fastmcp import FastMCP  # noqa: E402

log = logging.getLogger("mim_mcp")

APP_DIR = Path(__file__).resolve().parent
METRICS_FILE = os.environ.get("MIM_MCP_METRICS_FILE") or str(APP_DIR / "mcp_metrics.jsonl")
_METRICS_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _log_metric(record: dict) -> None:
    """Append one JSON line to the MCP metrics file. Writes are lock-serialized
    so concurrent scan_code calls don't interleave. Failures are logged but
    never raised — a metrics-write hiccup shouldn't kill a tool call."""
    if not METRICS_FILE:
        return
    try:
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with _METRICS_LOCK:
            with open(METRICS_FILE, "a") as f:
                f.write(line)
    except OSError as e:
        log.warning("metrics write to %s failed: %s", METRICS_FILE, e)


log.info(
    "starting bd_llm_traffic_scan MCP server (log_level=%s%s, metrics_file=%s)",
    LOG_LEVEL_NAME,
    f", log_file={LOG_FILE}" if LOG_FILE else "",
    METRICS_FILE or "off",
)
mcp = FastMCP("bd_llm_traffic_scan")


def _non_ws_len(s: str) -> int:
    return sum(1 for c in s if not c.isspace())


@mcp.tool()
def scan_code(code: str) -> dict:
    """Scan text against Black Duck's snippet-match KnowledgeBase and report
    reciprocal/copyleft license hits.

    The full input is scanned end-to-end — prose, fenced code, raw code
    without fences, and any text-file content the caller included are all
    checked. Inputs larger than the SCA endpoint's per-request cap
    (LARGE_SNIPPET_LIMIT non-ws chars) are split at line boundaries and
    scanned as several requests under one call.

    Returns a dict:
      clean          -- true if no RECIPROCAL / WEAK_RECIPROCAL matches.
      hits           -- list of {category, project, version, license, spdx,
                        ownership, path, source_start, source_end,
                        matched_start, matched_end}. Empty when clean.
      summary        -- one-line human-readable summary.
      http_status    -- HTTP status code from the SCA scan endpoint (int or
                        null; the last-observed status when the input was
                        split into several requests).
      skipped        -- present and true when the input was too small to
                        scan (<300 non-whitespace chars).
      error          -- present when the scan itself failed (missing bearer,
                        HTTP 4xx from SCA, malformed response, subprocess
                        timeout) or when every segment of a split input
                        errored out.
      partial_errors -- present when the input was split and some (but not
                        all) segments errored out; a list of
                        {idx, http_status, error} for each failed segment.

    Concurrent calls are safe: each invocation runs in its own tempdir.
    """
    call_id = uuid.uuid4().hex[:8]
    start_ts = _now_iso()
    func_t0 = time.monotonic()
    nws = _non_ws_len(code)
    log.info("scan_code called call_id=%s chars=%d nws=%d", call_id, len(code), nws)
    log.debug("scan_code input head: %r", code[:200])

    def _finish(outcome: str, hits: int = 0, http_status=None) -> None:
        _log_metric({
            "ts": start_ts,
            "call_id": call_id,
            "tool": "scan_code",
            "outcome": outcome,
            "duration_ms": int((time.monotonic() - func_t0) * 1000),
            "nws": nws,
            "hits": hits,
            "http_status": http_status,
        })

    if nws < SMALL_SNIPPET_LIMIT:
        log.info("scan_skipped reason=too_small nws=%d limit=%d", nws, SMALL_SNIPPET_LIMIT)
        _finish("skipped_small")
        return {
            "clean": True,
            "hits": [],
            "skipped": True,
            "summary": f"input too small to scan ({nws} < {SMALL_SNIPPET_LIMIT} non-ws chars)",
            "http_status": None,
        }

    # Split at line boundaries if the input exceeds the SCA endpoint's cap,
    # so a large blob (e.g. an entire assistant response) is scanned end to
    # end instead of rejected. Matches the proxy's behaviour.
    files = group_snippets([code])
    log.info("scan_start nws=%d files=%d", nws, len(files))
    t0 = time.monotonic()

    scan_results: list = []
    scan_errors: list = []
    last_http_status = None
    for idx, chunk in enumerate(files):
        try:
            r = scan_file(chunk)
        except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            elapsed = int((time.monotonic() - t0) * 1000)
            log.warning("scan_error idx=%d ms=%d err=%s", idx, elapsed, e)
            _finish("scan_error", http_status=last_http_status)
            return {
                "clean": False,
                "hits": [],
                "error": str(e),
                "summary": "scan failed",
                "http_status": last_http_status,
            }
        http_status = r.get("_http_status") if isinstance(r, dict) else None
        if http_status is not None:
            last_http_status = http_status
        err = scan_error_message(r)
        if err:
            log.info("scan_result_error idx=%d http_status=%s err=%s",
                     idx, http_status, err[:200])
            scan_errors.append({"idx": idx, "http_status": http_status, "error": err})
        else:
            scan_results.append(r)

    elapsed = int((time.monotonic() - t0) * 1000)
    if scan_errors and not scan_results:
        first = scan_errors[0]
        _finish("scan_result_error", http_status=last_http_status)
        return {
            "clean": False,
            "hits": [],
            "error": first["error"],
            "summary": "scan returned an error",
            "http_status": last_http_status,
        }

    hits = find_reciprocal_matches(scan_results)
    if hits:
        log.info("scan_ok ms=%d files=%d http_status=%s hits=%d",
                 elapsed, len(files), last_http_status, len(hits))
        sample = [(h.get("category"), h.get("project"), h.get("license")) for h in hits[:3]]
        log.info("rewrite_required hits=%d sample=%r", len(hits), sample)
        log.debug("hits detail: %r",
                  [(h.get("category"), h.get("project"), h.get("license")) for h in hits])
        _finish("hits", hits=len(hits), http_status=last_http_status)
    else:
        log.info("scan_ok ms=%d files=%d http_status=%s clean",
                 elapsed, len(files), last_http_status)
        _finish("clean", http_status=last_http_status)
    result_out = {
        "clean": not hits,
        "hits": hits,
        "summary": (
            f"{len(hits)} reciprocal/weak-reciprocal match(es)"
            if hits else "no reciprocal matches"
        ),
        "http_status": last_http_status,
    }
    if scan_errors:
        result_out["partial_errors"] = scan_errors
    return result_out


if __name__ == "__main__":
    _log_metric({
        "event": "startup",
        "ts": _now_iso(),
        "pid": os.getpid(),
        "log_level": LOG_LEVEL_NAME,
    })
    mcp.run()
