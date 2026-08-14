#!/usr/bin/env python3
"""
MitM proxy: sits between a coding agent and an LLM (MCP gateway).

Flow per request:
  1. POST /proxy {"prompt": "..."}  ->  forward to $BLACKDUCK_MCP_GATEWAY_URL
  2. Extract fenced code blocks from the response; if none are present
     (e.g., Claude Opus emits raw code), fall back to the whole response.
  3. Snippets with < 300 non-whitespace chars are concatenated into a
     single file; larger blocks get their own file; blocks whose
     non-whitespace length exceeds 50000 are split at line boundaries
     into segments each within the cap. If the concatenated small file
     is itself still below the 300 threshold, it is dropped (too small
     to yield useful matches). Each file is scanned by
     run_snippet_hash.sh.
  4. If snippet_match.json reports RECIPROCAL or WEAK_RECIPROCAL matches,
     re-prompt the gateway to rewrite the code without those matches.
  5. Repeat until clean or MIM_MAX_RETRIES exhausted (default 6).

Env:
  BLACKDUCK_MCP_GATEWAY_URL   required   base URL of the LiteLLM gateway
  BLACKDUCK_MCP_GATEWAY_KEY   required   sk-... key sent as Bearer to the gateway
  BEARER_TOK                  required   used by run_snippet_hash.sh (SCA API)
  BLACKDUCK_HOST              required   used by run_snippet_hash.sh
  MIM_MODEL                   optional   default "gpt-4o-mini"
  MIM_MAX_RETRIES             optional   default 6
  MIM_PORT                    optional   default 8080
"""

import base64
import binascii
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections import deque
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, request

APP_DIR = Path(__file__).resolve().parent
SCRIPT = APP_DIR / "run_snippet_hash.sh"

GATEWAY_URL = (os.environ.get("BLACKDUCK_MCP_GATEWAY_URL") or "").rstrip("/")
GATEWAY_KEY = os.environ.get("BLACKDUCK_MCP_GATEWAY_KEY")
MODEL = os.environ.get("MIM_MODEL", "gpt-4o-mini")
MAX_RETRIES = int(os.environ.get("MIM_MAX_RETRIES", "6"))
LICENSE_DETAILS = os.environ.get("MIM_LICENSE_DETAILS", "").lower() in ("1", "true", "yes", "on")

SMALL_SNIPPET_LIMIT = 300
LARGE_SNIPPET_LIMIT = 50000
#TRIGGER_CATEGORIES = ("RECIPROCAL", "WEAK_RECIPROCAL", "PERMISSIVE", "UNKNOWN")  # for test purposes, trigger all categories
TRIGGER_CATEGORIES = ("RECIPROCAL", "WEAK_RECIPROCAL")
FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)

_LEVEL_MAP = {
    "off": logging.CRITICAL + 10,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}
LOG_LEVEL_NAME = os.environ.get("MIM_LOG_LEVEL", "info").lower()
LOG_LEVEL = _LEVEL_MAP.get(LOG_LEVEL_NAME, logging.INFO)
TRACE_KEEP = int(os.environ.get("MIM_TRACE_KEEP", "20"))

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mim")
if LOG_LEVEL_NAME == "off":
    logging.getLogger("werkzeug").setLevel(logging.CRITICAL + 10)

TRACES: "deque[dict]" = deque(maxlen=TRACE_KEEP)

# Tags identifying which leg of the pipeline a log line belongs to. All tags
# are the same width so the columns line up when tailing the log.
SIDE_CLIENT = "CLIENT "   # proxy <-> the coding agent (or curl) that called us
SIDE_SERVER = "SERVER "   # proxy <-> the upstream LLM gateway
SIDE_SCANNER = "SCANNER"  # proxy <-> the Black Duck snippet-matching endpoint
SIDE_NONE = "       "     # untagged lines (framework/startup output)


class Trace:
    """Per-request breadcrumbs: emits log lines and captures a record for /traces."""

    def __init__(self, prompt: str):
        self.id = uuid.uuid4().hex[:8]
        self.t0 = time.monotonic()
        self.prompt_preview = prompt[:200]
        self.events: list = []
        self.outcome: str = "pending"

    def event(self, kind: str, side: str = SIDE_NONE, **fields) -> None:
        entry = {
            "kind": kind,
            "side": side.strip(),
            "t_ms": int((time.monotonic() - self.t0) * 1000),
            **fields,
        }
        self.events.append(entry)
        if fields:
            parts = " ".join(f"{k}={_fmt(v)}" for k, v in fields.items())
            log.info("[%s] [%s] %s %s", side, self.id, kind, parts)
        else:
            log.info("[%s] [%s] %s", side, self.id, kind)

    def finish(self, outcome: str) -> None:
        self.outcome = outcome
        self.event("done", side=SIDE_CLIENT, outcome=outcome)
        TRACES.append({
            "trace_id": self.id,
            "prompt_preview": self.prompt_preview,
            "duration_ms": int((time.monotonic() - self.t0) * 1000),
            "outcome": outcome,
            "events": self.events,
        })


def _fmt(v) -> str:
    s = repr(v) if isinstance(v, (list, tuple, dict)) else str(v)
    return s if len(s) <= 120 else s[:117] + "..."


def _prompt_debug_view(s: str) -> str:
    """Trim embedded prior prompt+response from debug output.

    The upstream LLM still receives the full text; this just keeps the log
    focused on the header and match list.
    """
    marker = "Original request:"
    idx = s.find(marker)
    if idx == -1:
        return s
    return s[:idx].rstrip() + "\n[previous prompt + response omitted]"


app = Flask(__name__)


def _decode_text(data: bytes, filename: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"attachment '{filename}' is not valid UTF-8 text: {e}")


def _parse_json_prefix(raw: bytes):
    """Parse a JSON value from the head of `raw`; return (obj, trailing_text).

    Supports the `curl -d '{...}' --data-binary @file.txt` pattern, where
    curl concatenates the two chunks with an `&` separator. Any leading
    `&` and whitespace on the trailing side are stripped.
    """
    if not raw:
        return {}, ""
    text = raw.decode("utf-8", errors="replace")
    head = text.lstrip()
    if not head.startswith(("{", "[")):
        return None, text
    leading = len(text) - len(head)
    try:
        obj, idx = json.JSONDecoder().raw_decode(head)
    except json.JSONDecodeError:
        return None, text
    remainder = text[leading + idx:]
    remainder = remainder.lstrip("&").lstrip()
    return obj, remainder


def extract_attachments(req) -> list:
    """Pull text attachments from multipart, JSON `attachments`, or trailing body.

    Returns a list of {"filename", "text"} dicts. Attachments are expected to
    be UTF-8 text; anything else raises ValueError → 400.
    """
    parts: list = []
    ctype = (req.content_type or "").split(";", 1)[0].strip().lower()

    if ctype == "multipart/form-data":
        for _, fs in req.files.items(multi=True):
            data = fs.read()
            if not data:
                continue
            name = fs.filename or "attachment"
            parts.append({"filename": name, "text": _decode_text(data, name)})
        return parts

    body, trailing = _parse_json_prefix(req.get_data())
    body = body if isinstance(body, dict) else {}
    for a in body.get("attachments") or []:
        name = a.get("filename") or "attachment"
        if "text" in a and a["text"] is not None:
            parts.append({"filename": name, "text": str(a["text"])})
            continue
        raw = a.get("data_b64") or a.get("data")
        if not raw:
            continue
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"attachment '{name}' has invalid base64: {e}")
        parts.append({"filename": name, "text": _decode_text(data, name)})
    if trailing:
        parts.append({"filename": "attached", "text": trailing})
    return parts


def inline_attachments(prompt: str, attachments: list) -> str:
    """Concatenate text attachments after the prompt with labeled fences."""
    if not attachments:
        return prompt
    chunks = [prompt.rstrip(), ""]
    for a in attachments:
        chunks.append(f"--- attachment: {a['filename']} ---")
        chunks.append(a["text"].rstrip("\n"))
        chunks.append("--- end attachment ---")
        chunks.append("")
    return "\n".join(chunks).rstrip() + "\n"


# The upstream LiteLLM gateway validates request bodies with a Pydantic model
# and returns `Extra inputs are not permitted` for any field outside its schema.
# Newer Claude Code releases send fields (e.g. `output_config`) the gateway's
# `/v1/messages` schema doesn't accept, so we whitelist what we forward.
ANTHROPIC_ALLOWED_FIELDS = frozenset({
    "model", "messages", "system", "max_tokens", "tools", "tool_choice",
    "temperature", "top_p", "top_k", "stop_sequences", "metadata", "stream",
})

OPENAI_ALLOWED_FIELDS = frozenset({
    "model", "messages", "temperature", "top_p", "n", "stream", "stop",
    "max_tokens", "max_completion_tokens", "presence_penalty",
    "frequency_penalty", "logit_bias", "user", "response_format", "seed",
    "tools", "tool_choice", "parallel_tool_calls", "logprobs", "top_logprobs",
    "reasoning_effort", "service_tier",
})


def _sanitize_upstream_body(body: dict, channel: str) -> tuple:
    """Drop fields the upstream gateway doesn't accept. Returns
    (kept_body, dropped_keys)."""
    allowed = ANTHROPIC_ALLOWED_FIELDS if channel == "anthropic" else OPENAI_ALLOWED_FIELDS
    kept, dropped = {}, []
    for k, v in body.items():
        if k in allowed:
            kept[k] = v
        else:
            dropped.append(k)
    return kept, sorted(dropped)


def forward_to_gateway(body: dict, endpoint: str) -> dict:
    """Forward the client's request body verbatim to `endpoint` on the
    upstream gateway (e.g. "/v1/messages" or "/v1/chat/completions") and
    return the parsed response JSON. Streaming to the upstream is always
    disabled — we need the full body buffered so we can scan it; if the
    downstream client asked for SSE we re-emit our own stream after."""
    if not GATEWAY_URL:
        raise RuntimeError("BLACKDUCK_MCP_GATEWAY_URL is not set")
    if not GATEWAY_KEY:
        raise RuntimeError("BLACKDUCK_MCP_GATEWAY_KEY is not set")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GATEWAY_KEY}",
    }
    channel = "anthropic" if endpoint.endswith("/messages") else "openai"
    payload, dropped = _sanitize_upstream_body({**body, "stream": False}, channel)
    if dropped:
        log.info("[%s] dropped %d field(s) not in %s upstream schema: %s",
                 SIDE_SERVER, len(dropped), channel, dropped)
    resp = requests.post(
        f"{GATEWAY_URL}{endpoint}",
        json=payload,
        headers=headers,
        timeout=120,
        verify=False,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"gateway {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def _extract_response_text(response_body: dict, channel: str) -> str:
    """Concatenate every text block from a completion response for the
    snippet scanner. Non-text blocks (tool_use, etc.) are intentionally
    ignored — they pass through the pipeline untouched. The `\\n` fix-up
    matches the prior behaviour: some models emit backslash-n instead of
    a real newline, which breaks the fence regex."""
    if channel == "anthropic":
        parts = []
        for b in response_body.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
        return "\n".join(parts).replace("\\n", "\n")
    if channel == "openai":
        try:
            content = response_body["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            content = ""
        return content.replace("\\n", "\n")
    return ""


def _last_user_text(body: dict) -> str:
    """Text of the most recent user turn — used only for the trace preview
    and debug logging. Never fed back to the upstream (we forward the whole
    body verbatim)."""
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            return _extract_user_text(m).strip()
    return ""


def extract_code_blocks(text: str) -> list:
    return [m.group(1) for m in FENCE_RE.finditer(text)]


def _non_ws_len(s: str) -> int:
    """Count of non-whitespace characters in `s`."""
    return sum(1 for c in s if not c.isspace())


def _split_by_nonws(content: str, max_nonws: int) -> list:
    """Split `content` into segments each with <= max_nonws non-whitespace
    chars, preferring line boundaries. If a single line already exceeds the
    limit (e.g. minified code), hard-split it at the first char that pushes
    the running non-whitespace count over the ceiling."""
    if _non_ws_len(content) <= max_nonws:
        return [content]
    segments = []
    buf = []
    buf_nonws = 0
    for line in content.splitlines(keepends=True):
        ln = _non_ws_len(line)
        if buf and buf_nonws + ln > max_nonws:
            segments.append("".join(buf))
            buf = []
            buf_nonws = 0
        while ln > max_nonws:
            cut_end = 0
            count = 0
            for i, ch in enumerate(line):
                cut_end = i + 1
                if not ch.isspace():
                    count += 1
                    if count == max_nonws:
                        break
            segments.append(line[:cut_end])
            line = line[cut_end:]
            ln = _non_ws_len(line)
        buf.append(line)
        buf_nonws += ln
    if buf:
        segments.append("".join(buf))
    return segments


def group_snippets(blocks: list) -> list:
    """A snippet must have at least SMALL_SNIPPET_LIMIT non-whitespace chars
    to be worth scanning. Blocks that meet the threshold scan individually;
    smaller blocks are merged and only sent if their combined non-whitespace
    length reaches the threshold. Blocks whose non-whitespace length exceeds
    LARGE_SNIPPET_LIMIT are split at line boundaries into segments each
    within the cap."""
    kept = [b for b in blocks if b]
    files, small = [], []
    for b in kept:
        if _non_ws_len(b) >= SMALL_SNIPPET_LIMIT:
            files.extend(_split_by_nonws(b, LARGE_SNIPPET_LIMIT))
        else:
            small.append(b)
    if small:
        merged = "\n\n".join(small)
        if _non_ws_len(merged) >= SMALL_SNIPPET_LIMIT:
            files.extend(_split_by_nonws(merged, LARGE_SNIPPET_LIMIT))
    return files


def scan_file(content: str) -> dict:
    """Run run_snippet_hash.sh in an isolated tempdir; return parsed snippet_match.json."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        input_path = td_path / "input.txt"
        input_path.write_text(content)
        result = subprocess.run(
            [str(SCRIPT), str(input_path)],
            cwd=td_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out_json = td_path / "snippet_match.json"
        if not out_json.exists():
            raise RuntimeError(
                f"snippet_match.json not produced (exit={result.returncode}): {result.stderr[-500:]}"
            )

        http_status = None
        status_file = td_path / "snippet_status.txt"
        if status_file.exists():
            try:
                http_status = int(status_file.read_text().strip())
            except ValueError:
                http_status = None

        raw = out_json.read_text()
        if not raw.strip():
            raise RuntimeError(
                f"snippet_match.json is empty (HTTP {http_status}, exit={result.returncode}): "
                f"{result.stderr[-500:]}"
            )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"snippet_match.json is not valid JSON (HTTP {http_status}): {e}; "
                f"head={raw[:200]!r}"
            )
        if isinstance(parsed, dict):
            parsed.setdefault("_http_status", http_status)
        return parsed


def scan_error_message(result) -> str:
    """Return a short error string if the snippet-match JSON reports a failure,
    else ''. Includes the upstream HTTP status code (when run_snippet_hash.sh
    captured one) plus Black Duck's `errorMessage`/`errors[]` body, and flags
    responses that look successful but lack `snippetMatches`."""
    if not isinstance(result, dict):
        return f"unexpected response shape: {type(result).__name__}"

    status = result.get("_http_status")
    status_prefix = f"HTTP {status}" if isinstance(status, int) else ""
    http_failed = isinstance(status, int) and status >= 400

    body_msg = ""
    msg = result.get("errorMessage") or result.get("message")
    code = result.get("errorCode") or result.get("statusCode")
    if msg:
        body_msg = f"[{code or '?'}] {msg}"
    else:
        errs = result.get("errors")
        if isinstance(errs, list) and errs:
            first = errs[0] if isinstance(errs[0], dict) else {}
            m = first.get("errorMessage") or first.get("message") or str(errs[0])
            c = first.get("errorCode") or "?"
            body_msg = f"[{c}] {m}"

    if body_msg:
        return f"{status_prefix} {body_msg}".strip()
    if http_failed:
        return f"{status_prefix}: no error body"
    if "snippetMatches" not in result:
        return f"{status_prefix} response missing 'snippetMatches' key".strip()
    return ""


def _first(lst):
    return lst[0] if isinstance(lst, list) and lst else None


def find_reciprocal_matches(scan_results: list) -> list:
    hits = []
    for r in scan_results:
        matches = r.get("snippetMatches", {}) or {}
        for cat in TRIGGER_CATEGORIES:
            for m in matches.get(cat, []) or []:
                lic = m.get("licenseDefinition") or {}
                regions = m.get("regions") or {}
                hits.append({
                    "category": cat,
                    "project": m.get("projectName"),
                    "version": m.get("releaseVersion"),
                    "license": lic.get("licenseDisplayName") or lic.get("name"),
                    "spdx": lic.get("spdxId"),
                    "ownership": lic.get("ownership"),
                    "path": m.get("matchedFilePath"),
                    "source_start": _first(regions.get("sourceStartLines")),
                    "source_end": _first(regions.get("sourceEndLines")),
                    "matched_start": _first(regions.get("matchedStartLines")),
                    "matched_end": _first(regions.get("matchedEndLines")),
                })
    return hits


def _license_hit_lines(hits: list) -> list:
    """One human-readable line per unique license identified across `hits`.
    Grouped by (category, SPDX id or license name) so the same license from
    several projects collapses into one entry."""
    seen = {}
    for h in hits:
        key = (h["category"], h.get("spdx") or "", h.get("license") or "")
        seen.setdefault(key, h)
    lines = []
    for h in sorted(seen.values(), key=lambda x: (x["category"], x.get("license") or "")):
        parts = [f"[{h['category']}]", h.get("license") or "?"]
        spdx = h.get("spdx")
        own = h.get("ownership")
        if spdx or own:
            parts.append(f"(SPDX: {spdx or '-'}, ownership: {own or '-'})")
        proj = h.get("project")
        if proj:
            parts.append(f"<- {proj} {h.get('version') or ''}".rstrip())
        lines.append(" ".join(parts))
    return lines


def _format_hit(h: dict, detailed: bool) -> str:
    header = f"  - [{h['category']}] {h['project']} {h['version']} -> {h['license']}"
    if not detailed:
        return header
    extras = []
    spdx = h.get("spdx")
    own = h.get("ownership")
    if spdx or own:
        extras.append(f"      spdx: {spdx or '-'}  ({own or '-'})")
    if h.get("path"):
        extras.append(f"      matched file: {h['path']}")
    if h.get("matched_start") is not None:
        extras.append(f"      matched lines: {h['matched_start']}-{h['matched_end']}")
    if h.get("source_start") is not None:
        extras.append(f"      your lines:    {h['source_start']}-{h['source_end']}")
    return "\n".join([header] + extras)


def _rewrite_user_turn(hits: list, detailed: bool) -> str:
    """Content of the follow-up user message appended to the conversation
    when the scan finds reciprocal matches. Naming the matched projects /
    licenses in the message body lets the model actually target them —
    the previous prompt-rewriting hack computed this list but never sent
    it upstream."""
    listed = "\n".join(_format_hit(h, detailed) for h in hits[:20])
    return (
        #"The previous response contained code that matches reciprocal / "
        #"copyleft licensed source:\n" + listed + "\n\n"
        "Please rewrite the code with a materially different algorithm and "
        "control flow, significantly reworded comments (including banner "
        "text), and renamed identifiers, taking a clean-room approach not "
        "copied from open source."
    )


def _append_rewrite_turn(body: dict, channel: str, response_body: dict,
                          hits: list) -> dict:
    """Build a new request body by appending the assistant's prior response
    plus a user turn asking for a rewrite. Preserves everything else the
    client sent (system prompt, tools, model, etc.)."""
    messages = list(body.get("messages") or [])
    if channel == "anthropic":
        messages.append({
            "role": "assistant",
            "content": response_body.get("content") or [],
        })
    else:  # openai
        try:
            assistant_content = response_body["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            assistant_content = ""
        messages.append({"role": "assistant", "content": assistant_content})
    messages.append({
        "role": "user",
        "content": _rewrite_user_turn(hits, detailed=LICENSE_DETAILS),
    })
    return {**body, "messages": messages}


def run_pipeline(body: dict, channel: str, request_fields: dict = None) -> tuple:
    """Run the LLM + scan + rewrite loop against the client's FULL request
    body — preserving model, system, tools, and the full messages array.

    channel: "anthropic" (upstream /v1/messages) or "openai" (upstream
    /v1/chat/completions).

    Returns (result, status). Result includes:
      response_body -- the final upstream response body (dict), preserving
                       content blocks including tool_use so the handler can
                       re-emit them verbatim.
      attempts, history, clean, hits, message, note, trace_id, error --
                       as before.
    """
    endpoint = "/v1/messages" if channel == "anthropic" else "/v1/chat/completions"
    preview = _last_user_text(body)
    trace = Trace(preview)
    trace.event("request", side=SIDE_CLIENT, prompt_chars=len(preview), **(request_fields or {}))
    log.debug("[%s] [%s] initial prompt:\n%s", SIDE_CLIENT, trace.id, _prompt_debug_view(preview))
    current_body = body
    history: list = []

    for attempt in range(MAX_RETRIES + 1):
        msgs = current_body.get("messages") or []
        trace.event(
            "attempt_start",
            side=SIDE_SERVER,
            n=attempt + 1,
            endpoint=endpoint,
            model=current_body.get("model"),
            msg_count=len(msgs),
        )
        log.debug(
            "[%s] [%s] attempt %d body-preview (last user):\n%s",
            SIDE_SERVER, trace.id, attempt + 1, _prompt_debug_view(_last_user_text(current_body)),
        )
        t = time.monotonic()
        try:
            response_body = forward_to_gateway(current_body, endpoint)
        except Exception as e:
            trace.event("gateway_error", side=SIDE_SERVER, err=str(e)[:200])
            trace.finish("gateway_error")
            return {"error": f"gateway request failed: {e}", "trace_id": trace.id}, 502

        response_text = _extract_response_text(response_body, channel)
        trace.event(
            "upstream_ok",
            side=SIDE_SERVER,
            ms=int((time.monotonic() - t) * 1000),
            resp_chars=len(response_text),
        )

        blocks = extract_code_blocks(response_text)
        source = "fenced"
        if not blocks and response_text.strip():
            blocks = [response_text]
            source = "whole_response"
        if not blocks:
            trace.event("empty_response", side=SIDE_SERVER)
            trace.finish("no_code")
            return {
                "response_body": response_body,
                "attempts": attempt + 1,
                "history": history,
                "trace_id": trace.id,
                "note": "no scannable text in assistant response",
            }, 200

        files = group_snippets(blocks)
        if source == "whole_response":
            trace.event("no_fences_using_whole_response", side=SIDE_SERVER, chars=len(response_text))
        trace.event(
            "code_blocks",
            side=SIDE_SCANNER,
            found=len(blocks),
            files=len(files),
            sizes=[len(f) for f in files],
        )
        try:
            scan_results = []
            scan_errors: list = []
            for i, content in enumerate(files):
                log.debug(
                    "[%s] [%s] scan input %d (%d bytes):\n%s",
                    SIDE_SCANNER, trace.id, i, len(content), content,
                )
                trace.event("scan_start", side=SIDE_SCANNER, idx=i, bytes=len(content))
                st = time.monotonic()
                scan_result = scan_file(content)
                scan_results.append(scan_result)
                elapsed = int((time.monotonic() - st) * 1000)
                http_status = scan_result.get("_http_status") if isinstance(scan_result, dict) else None
                err = scan_error_message(scan_result)
                if err:
                    scan_errors.append({
                        "idx": i,
                        "bytes": len(content),
                        "http_status": http_status,
                        "error": err,
                    })
                    trace.event(
                        "scan_result_error",
                        side=SIDE_SCANNER,
                        idx=i,
                        bytes=len(content),
                        ms=elapsed,
                        http_status=http_status,
                        err=err[:200],
                    )
                else:
                    trace.event(
                        "scan_ok",
                        side=SIDE_SCANNER,
                        idx=i,
                        bytes=len(content),
                        ms=elapsed,
                        http_status=http_status,
                    )
        except Exception as e:
            trace.event("scan_error", side=SIDE_SCANNER, err=str(e)[:200])
            trace.finish("scan_error")
            return {
                "error": f"snippet scan failed: {e}",
                "response_body": response_body,
                "history": history,
                "trace_id": trace.id,
            }, 500

        if scan_errors and len(scan_errors) == len(files):
            trace.event("scan_all_failed", side=SIDE_SCANNER, count=len(scan_errors))
            trace.finish("scan_error")
            return {
                "error": "all snippet-match scans returned errors",
                "response_body": response_body,
                "history": history,
                "scan_errors": scan_errors,
                "trace_id": trace.id,
            }, 502

        hits = find_reciprocal_matches(scan_results)
        history_entry = {
            "attempt": attempt + 1,
            "code_blocks": len(blocks),
            "files_scanned": len(files),
            "hits": len(hits),
        }
        if scan_errors:
            history_entry["scan_errors"] = scan_errors
        history.append(history_entry)

        if hits:
            unique = sorted({(h["category"], h["project"], h["license"]) for h in hits})
            trace.event(
                "hits",
                side=SIDE_SCANNER,
                count=len(hits),
                unique=len(unique),
                sample=[f"{c}:{p}" for c, p, _ in unique[:3]],
            )
            if LICENSE_DETAILS:
                license_lines = _license_hit_lines(hits)
                trace.event("licenses", side=SIDE_SCANNER, count=len(license_lines))
                for line in license_lines:
                    log.info("[%s] [%s]   %s", SIDE_SCANNER, trace.id, line)
        else:
            trace.event("clean", side=SIDE_SCANNER)

        if not hits:
            trace.finish("clean")
            return {
                "response_body": response_body,
                "attempts": attempt + 1,
                "history": history,
                "trace_id": trace.id,
                "clean": True,
            }, 200

        if attempt >= MAX_RETRIES:
            trace.finish("give_up")
            return {
                "response_body": response_body,
                "attempts": attempt + 1,
                "history": history,
                "hits": hits,
                "clean": False,
                "trace_id": trace.id,
                "message": (
                    f"Gave up after {MAX_RETRIES} rewrite attempts — the model "
                    "keeps producing code with reciprocal/copyleft matches. "
                    "Please try a different prompt."
                ),
            }, 200

        trace.event("rewrite_prompt", side=SIDE_SERVER)
        current_body = _append_rewrite_turn(current_body, channel, response_body, hits)
        log.debug(
            "[%s] [%s] rewrite user turn appended (messages=%d)",
            SIDE_SERVER, trace.id, len(current_body.get("messages") or []),
        )

    trace.finish("unreachable")
    return {"error": "unreachable", "trace_id": trace.id}, 500


HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
    "content-encoding",
}


def _passthrough(subpath: str):
    """Transparently forward the current request to the upstream gateway."""
    if not GATEWAY_URL:
        return jsonify({"error": "BLACKDUCK_MCP_GATEWAY_URL not set"}), 502
    target = f"{GATEWAY_URL}/{subpath.lstrip('/')}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    if "authorization" not in {k.lower() for k in headers} and GATEWAY_KEY:
        headers["Authorization"] = f"Bearer {GATEWAY_KEY}"
    try:
        upstream = requests.request(
            method=request.method,
            url=target,
            headers=headers,
            data=request.get_data(),
            params=request.args,
            timeout=120,
            verify=False,
            allow_redirects=False,
        )
    except Exception as e:
        log.warning("[%s] passthrough %s %s failed: %s", SIDE_SERVER, request.method, subpath, e)
        return jsonify({"error": f"upstream passthrough failed: {e}"}), 502
    resp_headers = [(k, v) for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP]
    log.info("[%s] passthrough %s /%s -> %d (%d bytes)",
             SIDE_SERVER, request.method, subpath, upstream.status_code, len(upstream.content))
    return upstream.content, upstream.status_code, resp_headers


def _extract_user_text(msg: dict) -> str:
    """Get plaintext from a chat message whose content is a string or a list of parts."""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [p.get("text", "") for p in c
                 if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(parts)
    return ""


def _sse_frame(event_name: str, data: dict) -> str:
    """One SSE frame: `event: <name>\\ndata: <json>\\n\\n`."""
    return f"event: {event_name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


ANTHROPIC_SSE_CHUNK_CHARS = 256


def _anthropic_sse_stream(response_body: dict, trace_id: str):
    """Generate the Anthropic Messages SSE frame sequence for an
    already-buffered response body. Preserves every content block the
    upstream produced — `text` blocks stream as `text_delta`s (chunked so
    the client's parser sees incremental progress), `tool_use` blocks
    stream as an `input_json_delta` carrying the tool's input, and any
    other block type is echoed inside the initial `content_block_start`
    frame. Without this, tools declared by Claude Code would never fire."""
    msg_id = response_body.get("id") or (f"msg_{trace_id}" if trace_id else "msg_")
    model_name = response_body.get("model", "unknown")
    usage = response_body.get("usage") or {}
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    stop_reason = response_body.get("stop_reason") or "end_turn"
    stop_sequence = response_body.get("stop_sequence")
    content = response_body.get("content") or []

    yield _sse_frame("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model_name,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
    })

    for idx, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text") or ""
            yield _sse_frame("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            })
            for i in range(0, len(text), ANTHROPIC_SSE_CHUNK_CHARS):
                yield _sse_frame("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {
                        "type": "text_delta",
                        "text": text[i:i + ANTHROPIC_SSE_CHUNK_CHARS],
                    },
                })
            yield _sse_frame("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            })
        elif btype == "tool_use":
            yield _sse_frame("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {
                    "type": "tool_use",
                    "id": block.get("id") or f"toolu_{idx}",
                    "name": block.get("name") or "",
                    "input": {},
                },
            })
            input_json = json.dumps(block.get("input") or {}, separators=(",", ":"))
            yield _sse_frame("content_block_delta", {
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "input_json_delta", "partial_json": input_json},
            })
            yield _sse_frame("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            })
        else:
            # Unknown block type — echo the whole block in the start frame
            # so no fields are lost, then stop it immediately.
            yield _sse_frame("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": block,
            })
            yield _sse_frame("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            })

    yield _sse_frame("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": stop_sequence},
        "usage": {"output_tokens": output_tokens},
    })
    yield _sse_frame("message_stop", {"type": "message_stop"})


def _anthropic_sse_error(err_type: str, message: str, trace_id: str = ""):
    """Single-frame SSE error stream matching Anthropic's error event shape."""
    yield _sse_frame("error", {
        "type": "error",
        "error": {"type": err_type, "message": message},
        "trace_id": trace_id,
    })


@app.route("/proxy", methods=["POST"])
def proxy():
    ctype = (request.content_type or "").split(";", 1)[0].strip().lower()
    if ctype == "multipart/form-data":
        raw_prompt = request.form.get("prompt")
    else:
        body, _ = _parse_json_prefix(request.get_data())
        raw_prompt = (body or {}).get("prompt") if isinstance(body, dict) else None
    if not raw_prompt:
        return jsonify({"error": "missing 'prompt' in request body"}), 400

    try:
        attachments = extract_attachments(request)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    prompt = inline_attachments(raw_prompt, attachments)
    upstream_body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}]}
    request_fields = {
        "channel": "proxy",
        "total_chars": len(prompt),
        "attachments": [
            {"filename": a["filename"], "chars": len(a["text"])}
            for a in attachments
        ] if attachments else [],
    }
    result, status = run_pipeline(upstream_body, "openai", request_fields=request_fields)
    if "response_body" in result:
        result["response"] = _extract_response_text(result.pop("response_body"), "openai")
    return jsonify(result), status


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    body = request.get_json(silent=True) or {}
    messages = body.get("messages") or []
    user_msgs = [m for m in messages if m.get("role") == "user"]
    prompt = _extract_user_text(user_msgs[-1]).strip() if user_msgs else ""
    if not prompt:
        log.info("[%s] chat_completions passthrough (no user prompt)", SIDE_CLIENT)
        return _passthrough("v1/chat/completions")

    request_fields = {
        "channel": "chat_completions",
        "msg_count": len(messages),
        "user_msgs": len(user_msgs),
    }
    result, status = run_pipeline(body, "openai", request_fields=request_fields)
    if "error" in result:
        return jsonify({
            "error": {"message": result["error"], "type": "proxy_error"},
            "trace_id": result.get("trace_id"),
        }), status
    # Return the upstream response verbatim (preserves choices, tool_calls,
    # usage counts, etc.) so tools declared by the client keep working.
    return jsonify(result.get("response_body") or {}), status


@app.route("/v1/messages", methods=["POST"])
def anthropic_messages():
    """Anthropic Messages API entry — used by Claude Code and other clients.

    Forwards the client's model/system/tools/messages verbatim to the
    upstream `/v1/messages` endpoint; buffers the response for scanning;
    if the scan is clean, re-emits the upstream response body as-is
    (content blocks including tool_use are preserved). If the client
    asked for `stream: true`, we re-emit the buffered body as an
    Anthropic SSE stream so the media type matches what Claude Code
    asked for."""
    body = request.get_json(silent=True) or {}
    stream = bool(body.get("stream"))
    messages = body.get("messages") or []
    user_msgs = [m for m in messages if m.get("role") == "user"]
    # Anthropic user messages can carry tool_result content (no `type:"text"`
    # parts); those aren't user prompts, so _extract_user_text returns empty
    # and we fall through to passthrough.
    prompt = _extract_user_text(user_msgs[-1]).strip() if user_msgs else ""
    if not prompt:
        log.info("[%s] anthropic_messages passthrough (no user text)", SIDE_CLIENT)
        return _passthrough("v1/messages")

    request_fields = {
        "channel": "anthropic_messages",
        "msg_count": len(messages),
        "user_msgs": len(user_msgs),
        "stream": stream,
        "has_tools": bool(body.get("tools")),
        "has_system": bool(body.get("system")),
    }
    result, status = run_pipeline(body, "anthropic", request_fields=request_fields)
    trace_id = result.get("trace_id", "")

    if "error" in result:
        if stream:
            return Response(
                _anthropic_sse_error("proxy_error", result["error"], trace_id),
                status=status,
                mimetype="text/event-stream",
            )
        return jsonify({
            "type": "error",
            "error": {"type": "proxy_error", "message": result["error"]},
            "trace_id": trace_id,
        }), status

    response_body = result.get("response_body") or {}
    if stream:
        return Response(
            _anthropic_sse_stream(response_body, trace_id),
            status=status,
            mimetype="text/event-stream",
        )
    return jsonify(response_body), status


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def catchall(path):
    return _passthrough(path)


@app.route("/health")
def health():
    return jsonify({
        "gateway_url_set": bool(GATEWAY_URL),
        "gateway_key_set": bool(GATEWAY_KEY),
        "sca_bearer_set": bool(os.environ.get("BEARER_TOK")),
        "model": MODEL,
        "script_present": SCRIPT.exists(),
        "max_retries": MAX_RETRIES,
        "log_level": LOG_LEVEL_NAME,
        "trace_keep": TRACE_KEEP,
        "license_details": LICENSE_DETAILS,
        "traces_held": len(TRACES),
    })


@app.route("/traces")
def traces_index():
    """Compact list of recent traces (newest first)."""
    items = [
        {
            "trace_id": t["trace_id"],
            "outcome": t["outcome"],
            "duration_ms": t["duration_ms"],
            "events": len(t["events"]),
            "prompt_preview": t["prompt_preview"],
        }
        for t in list(TRACES)
    ]
    items.reverse()
    return jsonify(items)


@app.route("/traces/<trace_id>")
def traces_detail(trace_id):
    for t in TRACES:
        if t["trace_id"] == trace_id:
            return jsonify(t)
    return jsonify({"error": "not found"}), 404


def _parse_args(argv):
    import argparse
    p = argparse.ArgumentParser(
        prog="mim_proxy",
        description="MitM proxy: scans LLM code for reciprocal/copyleft matches "
                    "and re-prompts on hits.",
    )
    p.add_argument(
        "--log-level", "-l",
        choices=["off", "warn", "info", "debug"],
        default=None,
        help="verbosity of pipeline logs (overrides MIM_LOG_LEVEL; default: info)",
    )
    p.add_argument(
        "--port", "-p", type=int, default=None,
        help="TCP port to bind (overrides MIM_PORT; default: 8080)",
    )
    p.add_argument(
        "--model", "-m", default=None,
        help="LLM model name (overrides MIM_MODEL; default: gpt-4o-mini)",
    )
    p.add_argument(
        "--max-retries", "-r", type=int, default=None,
        help="rewrite attempts before giving up (overrides MIM_MAX_RETRIES; default: 6)",
    )
    p.add_argument(
        "--trace-keep", type=int, default=None,
        help="recent traces to keep in memory for /traces (overrides MIM_TRACE_KEEP; default: 20)",
    )
    p.add_argument(
        "--license-details", "-L", action="store_true", default=False,
        help="include SPDX id, matched file path, and line ranges from the snippet scan "
             "in the rewrite prompt (overrides MIM_LICENSE_DETAILS; default: off)",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])

    if args.log_level is not None:
        LOG_LEVEL_NAME = args.log_level
        log.setLevel(_LEVEL_MAP[LOG_LEVEL_NAME])
        logging.getLogger().setLevel(_LEVEL_MAP[LOG_LEVEL_NAME])
        if LOG_LEVEL_NAME == "off":
            logging.getLogger("werkzeug").setLevel(logging.CRITICAL + 10)
    if args.model is not None:
        MODEL = args.model
    if args.max_retries is not None:
        MAX_RETRIES = args.max_retries
    if args.trace_keep is not None:
        TRACE_KEEP = args.trace_keep
        # replace the deque with one of the new size, preserving current items
        _existing = list(TRACES)
        TRACES = deque(_existing[-TRACE_KEEP:], maxlen=TRACE_KEEP)
    if args.license_details:
        LICENSE_DETAILS = True

    if not GATEWAY_URL:
        print("warning: BLACKDUCK_MCP_GATEWAY_URL is not set", file=sys.stderr)
    if not SCRIPT.exists():
        print(f"error: {SCRIPT} not found", file=sys.stderr)
        sys.exit(1)

    port = args.port if args.port is not None else int(os.environ.get("MIM_PORT", "8080"))
    app.run(host="127.0.0.1", port=port, debug=False)
