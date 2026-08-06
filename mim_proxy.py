#!/usr/bin/env python3
"""
MitM proxy: sits between a coding agent and an LLM (MCP gateway).

Flow per request:
  1. POST /proxy {"prompt": "..."}  ->  forward to $BLACKDUCK_MCP_GATEWAY_URL
  2. Extract fenced code blocks from the response; if none are present
     (e.g., Claude Opus emits raw code), fall back to the whole response.
  3. Snippets < 300 chars are concatenated into a single file; snippets
     300-50000 chars each get their own file; snippets > 50000 chars are
     split at line boundaries into chunks under the limit. Each file is
     scanned by run_snippet_hash.sh.
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
from flask import Flask, jsonify, request

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


class Trace:
    """Per-request breadcrumbs: emits log lines and captures a record for /traces."""

    def __init__(self, prompt: str):
        self.id = uuid.uuid4().hex[:8]
        self.t0 = time.monotonic()
        self.prompt_preview = prompt[:200]
        self.events: list = []
        self.outcome: str = "pending"

    def event(self, kind: str, **fields) -> None:
        entry = {
            "kind": kind,
            "t_ms": int((time.monotonic() - self.t0) * 1000),
            **fields,
        }
        self.events.append(entry)
        if fields:
            parts = " ".join(f"{k}={_fmt(v)}" for k, v in fields.items())
            log.info("[%s] %s %s", self.id, kind, parts)
        else:
            log.info("[%s] %s", self.id, kind)

    def finish(self, outcome: str) -> None:
        self.outcome = outcome
        self.event("done", outcome=outcome)
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


def forward_to_gateway(prompt: str) -> str:
    if not GATEWAY_URL:
        raise RuntimeError("BLACKDUCK_MCP_GATEWAY_URL is not set")
    if not GATEWAY_KEY:
        raise RuntimeError("BLACKDUCK_MCP_GATEWAY_KEY is not set")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GATEWAY_KEY}",
    }
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = requests.post(
        f"{GATEWAY_URL}/v1/chat/completions",
        json=payload,
        headers=headers,
        timeout=120,
        verify=False,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"gateway {resp.status_code}: {resp.text[:400]}")
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    # Some models return literal "\n" (backslash-n) rather than real newlines;
    # normalize so the fence regex and downstream scan see actual line breaks.
    return content.replace("\\n", "\n")


def extract_code_blocks(text: str) -> list:
    return [m.group(1) for m in FENCE_RE.finditer(text)]


def split_oversize(content: str, limit: int) -> list:
    """Split `content` into chunks no larger than `limit`, cutting at line
    boundaries when possible."""
    if len(content) <= limit:
        return [content]
    chunks = []
    remaining = content
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def group_snippets(blocks: list) -> list:
    """Blocks 300-50000 chars scan individually; smaller blocks merge into
    one file; blocks over 50000 chars are split at line boundaries."""
    kept = [b for b in blocks if b]
    files, small = [], []
    for b in kept:
        if len(b) >= SMALL_SNIPPET_LIMIT:
            files.extend(split_oversize(b, LARGE_SNIPPET_LIMIT))
        else:
            small.append(b)
    if small:
        files.extend(split_oversize("\n\n".join(small), LARGE_SNIPPET_LIMIT))
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
        return json.loads(out_json.read_text())


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


def build_rewrite_prompt(
    original_prompt: str,
    prior_response: str,
    hits: list,
    detailed: bool = False,
) -> str:
    listed = "\n".join(_format_hit(h, detailed) for h in hits[:20])
    return (
        "Rewrite the all the code returned to me, change the "
        "algorithm and control flow, significantly change every comment, including banner details, and rename all code identifiers and take a clean-room "
        "approach, not copying directly from open source. \n\n"
        f"Original request:\n{original_prompt}\n\n"
        f"Previous (rejected) response:\n{prior_response}\n"
    )


def run_pipeline(prompt: str, request_fields: dict = None) -> tuple:
    """Run the LLM + scan + rewrite loop for `prompt`.

    Returns (result_dict, status_code). Result includes fields expected by
    the /proxy endpoint (response, attempts, history, clean, hits, message,
    note, trace_id, error) — the caller shapes them for its own protocol.
    """
    trace = Trace(prompt)
    trace.event("request", prompt_chars=len(prompt), **(request_fields or {}))
    log.debug("[%s] initial prompt:\n%s", trace.id, _prompt_debug_view(prompt))
    current_prompt = prompt
    history: list = []

    for attempt in range(MAX_RETRIES + 1):
        trace.event("attempt_start", n=attempt + 1, prompt_chars=len(current_prompt))
        log.debug("[%s] attempt %d prompt:\n%s", trace.id, attempt + 1, _prompt_debug_view(current_prompt))
        t = time.monotonic()
        try:
            response_text = forward_to_gateway(current_prompt)
        except Exception as e:
            trace.event("gateway_error", err=str(e)[:200])
            trace.finish("gateway_error")
            return {"error": f"gateway request failed: {e}", "trace_id": trace.id}, 502
        trace.event(
            "upstream_ok",
            ms=int((time.monotonic() - t) * 1000),
            resp_chars=len(response_text),
        )

        blocks = extract_code_blocks(response_text)
        source = "fenced"
        if not blocks and response_text.strip():
            blocks = [response_text]
            source = "whole_response"
        if not blocks:
            trace.event("empty_response")
            trace.finish("no_code")
            return {
                "response": response_text,
                "attempts": attempt + 1,
                "history": history,
                "trace_id": trace.id,
                "note": "response was empty; nothing scanned",
            }, 200

        files = group_snippets(blocks)
        if source == "whole_response":
            trace.event("no_fences_using_whole_response", chars=len(response_text))
        trace.event(
            "code_blocks",
            found=len(blocks),
            files=len(files),
            sizes=[len(f) for f in files],
        )
        try:
            scan_results = []
            for i, content in enumerate(files):
                log.debug(
                    "[%s] scan input %d (%d bytes):\n%s",
                    trace.id, i, len(content), content,
                )
                st = time.monotonic()
                scan_results.append(scan_file(content))
                trace.event(
                    "scan_ok",
                    idx=i,
                    bytes=len(content),
                    ms=int((time.monotonic() - st) * 1000),
                )
        except Exception as e:
            trace.event("scan_error", err=str(e)[:200])
            trace.finish("scan_error")
            return {
                "error": f"snippet scan failed: {e}",
                "response": response_text,
                "history": history,
                "trace_id": trace.id,
            }, 500

        hits = find_reciprocal_matches(scan_results)
        history.append({
            "attempt": attempt + 1,
            "code_blocks": len(blocks),
            "files_scanned": len(files),
            "hits": len(hits),
        })

        if hits:
            unique = sorted({(h["category"], h["project"], h["license"]) for h in hits})
            trace.event(
                "hits",
                count=len(hits),
                unique=len(unique),
                sample=[f"{c}:{p}" for c, p, _ in unique[:3]],
            )
        else:
            trace.event("clean")

        if not hits:
            trace.finish("clean")
            return {
                "response": response_text,
                "attempts": attempt + 1,
                "history": history,
                "trace_id": trace.id,
                "clean": True,
            }, 200

        if attempt >= MAX_RETRIES:
            trace.finish("give_up")
            return {
                "response": response_text,
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

        trace.event("rewrite_prompt")
        current_prompt = build_rewrite_prompt(prompt, response_text, hits, detailed=LICENSE_DETAILS)
        log.debug("[%s] rewrite prompt:\n%s", trace.id, _prompt_debug_view(current_prompt))

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
        log.warning("passthrough %s %s failed: %s", request.method, subpath, e)
        return jsonify({"error": f"upstream passthrough failed: {e}"}), 502
    resp_headers = [(k, v) for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP]
    log.info("passthrough %s /%s -> %d (%d bytes)",
             request.method, subpath, upstream.status_code, len(upstream.content))
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


def _openai_response(text: str, model_name: str, trace_id: str) -> dict:
    """Wrap `text` in an OpenAI /v1/chat/completions response envelope."""
    return {
        "id": f"chatcmpl-{trace_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _anthropic_response(text: str, model_name: str, trace_id: str) -> dict:
    """Wrap `text` in an Anthropic /v1/messages response envelope."""
    return {
        "id": f"msg_{trace_id}",
        "type": "message",
        "role": "assistant",
        "model": model_name,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


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
    request_fields = {
        "channel": "proxy",
        "total_chars": len(prompt),
        "attachments": [
            {"filename": a["filename"], "chars": len(a["text"])}
            for a in attachments
        ] if attachments else [],
    }
    result, status = run_pipeline(prompt, request_fields=request_fields)
    return jsonify(result), status


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    body = request.get_json(silent=True) or {}
    messages = body.get("messages") or []
    user_msgs = [m for m in messages if m.get("role") == "user"]
    prompt = _extract_user_text(user_msgs[-1]).strip() if user_msgs else ""
    if not prompt:
        log.info("chat_completions passthrough (no user prompt)")
        return _passthrough("v1/chat/completions")

    request_fields = {
        "channel": "chat_completions",
        "msg_count": len(messages),
        "user_msgs": len(user_msgs),
    }
    result, status = run_pipeline(prompt, request_fields=request_fields)
    if "error" in result:
        return jsonify({
            "error": {"message": result["error"], "type": "proxy_error"},
            "trace_id": result.get("trace_id"),
        }), status
    return jsonify(_openai_response(
        result["response"],
        body.get("model") or MODEL,
        result.get("trace_id", ""),
    )), status


@app.route("/v1/messages", methods=["POST"])
def anthropic_messages():
    """Anthropic Messages API entry — used by Claude Code and other clients."""
    body = request.get_json(silent=True) or {}
    messages = body.get("messages") or []
    user_msgs = [m for m in messages if m.get("role") == "user"]
    # Anthropic user messages can carry tool_result content (no `type:"text"`
    # parts); those aren't user prompts, so _extract_user_text returns empty
    # and we fall through to passthrough.
    prompt = _extract_user_text(user_msgs[-1]).strip() if user_msgs else ""
    if not prompt:
        log.info("anthropic_messages passthrough (no user text)")
        return _passthrough("v1/messages")

    request_fields = {
        "channel": "anthropic_messages",
        "msg_count": len(messages),
        "user_msgs": len(user_msgs),
    }
    result, status = run_pipeline(prompt, request_fields=request_fields)
    if "error" in result:
        return jsonify({
            "type": "error",
            "error": {"type": "proxy_error", "message": result["error"]},
            "trace_id": result.get("trace_id"),
        }), status
    return jsonify(_anthropic_response(
        result["response"],
        body.get("model") or MODEL,
        result.get("trace_id", ""),
    )), status


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
