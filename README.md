# MiM — Man-in-the-Middle License Guard

A small HTTP proxy that sits between a coding agent and a LiteLLM-compatible
LLM gateway (`$BLACKDUCK_MCP_GATEWAY_URL`). It forwards prompts upstream and,
for every fenced code block in the response, runs BlackDuck's snippet-matching
API via `run_snippet_hash.sh`. If any match is classified as `RECIPROCAL` or
`WEAK_RECIPROCAL`, the proxy re-prompts the LLM to rewrite the code without
those matches. It gives up after 6 attempts and asks the caller to try a
different prompt.

## Requirements

- Python 3.13+
- `curl` and `jq` on `PATH` (used by `source_bearer_demo.sh` and `run_snippet_hash.sh`)
- Network access to both the BlackDuck SCA host and the MCP/LiteLLM gateway
- A BlackDuck personal access token and a LiteLLM API key

## Install

```bash
cd /path/to/snippet_MiM
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
chmod +x run_snippet_hash.sh source_bearer_demo.sh
```

## Configure environment

Two sets of credentials are needed:

1. **BlackDuck SCA** (used by `run_snippet_hash.sh` to call `/api/snippet-matching`).
   `source_bearer_demo.sh` exchanges a BlackDuck token for a short-lived bearer
   and exports `BEARER_TOK` and `BLACKDUCK_HOST`:

   ```bash
   source ./source_bearer_demo.sh
   ```

   Edit `source_bearer_demo.sh` if you need to swap in a different `BLACKDUCK_TOK`
   or `BLACKDUCK_HOST`.

2. **LLM gateway** (LiteLLM at `$BLACKDUCK_MCP_GATEWAY_URL`):

   ```bash
   export BLACKDUCK_MCP_GATEWAY_URL="https://llm.core.blackduck.com"
   export BLACKDUCK_MCP_GATEWAY_KEY="sk-...your-litellm-key..."
   ```

3. **JAVA** modify the source_bearer_demo.sh script to set the JAVA_HOME
   and PATH environment variables

Optional tuning:

| Variable | Default | Purpose |
|---|---|---|
| `MIM_MODEL` | `gpt-4o-mini` | Model name sent to LiteLLM |
| `MIM_MAX_RETRIES` | `6` | Rewrite attempts before giving up |
| `MIM_PORT` | `8080` | Local port the proxy binds to |
| `MIM_LOG_LEVEL` | `info` | `off` \| `warn` \| `info` \| `debug` — see [Instrumentation](#instrumentation) |
| `MIM_TRACE_KEEP` | `20` | Number of recent traces kept in memory for `/traces` |

## Run

```bash
./.venv/bin/python mim_proxy.py
```

Command-line flags (each also mirrors an env var):

```bash
./.venv/bin/python mim_proxy.py --help
./.venv/bin/python mim_proxy.py --log-level debug          # or -l debug
./.venv/bin/python mim_proxy.py -l debug -p 9090 -r 3
```

| Flag | Env var | Default |
|---|---|---|
| `-l`, `--log-level {off,warn,info,debug}` | `MIM_LOG_LEVEL` | `info` |
| `-p`, `--port` | `MIM_PORT` | `8080` |
| `-m`, `--model` | `MIM_MODEL` | `gpt-4o-mini` |
| `-r`, `--max-retries` | `MIM_MAX_RETRIES` | `6` |
| `--trace-keep` | `MIM_TRACE_KEEP` | `20` |
| `-L`, `--license-details` | `MIM_LICENSE_DETAILS` | off |

The proxy listens on `127.0.0.1:$MIM_PORT`. `GET /health` reports whether
each dependency (gateway URL/key, SCA bearer, snippet script) is wired up,
and `./test_snippet_match.sh` runs an end-to-end check against a known OSS
sample.

## Use

The proxy exposes three shapes of endpoint:

| Route | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible entry point. If the request carries a user message, the assistant text is scanned and possibly rewritten before being returned. If it only has system/tool messages, or the last user content is empty, the whole request is forwarded upstream unchanged. |
| `POST /v1/messages` | Anthropic Messages API — used by Claude Code and other Anthropic-native clients. Same scan/rewrite behavior as `/v1/chat/completions`, but the response is wrapped in Anthropic shape (`{id: "msg_...", type: "message", content: [{type:"text", text:...}], stop_reason: "end_turn", ...}`). When the request carries `"stream": true`, the pipeline still runs to completion and the finished assistant text is re-emitted as an Anthropic-shape SSE stream (`message_start` → `content_block_start` → `content_block_delta` chunks → `content_block_stop` → `message_delta` → `message_stop`) so clients that require `text/event-stream` — Claude Code included — accept the reply on the first try. Tool-result follow-ups (last user message contains only `tool_result` blocks) are forwarded upstream unchanged. |
| Any other path/method | Transparent reverse proxy to the upstream gateway. Handles `GET /v1/models`, session init, embeddings, tool-only chat completions, etc. Client `Authorization` is forwarded; if the client didn't send one, the proxy substitutes `BLACKDUCK_MCP_GATEWAY_KEY`. |
| `POST /proxy` | Simple test entry — same scan pipeline as `/v1/chat/completions`, but takes `{"prompt": "..."}` (with optional text attachments — see below). Kept for direct CLI use. |

Trace events for scanned requests include a `channel` field (`proxy` or
`chat_completions`) so you can tell which entry point produced them.
Passthrough calls emit a single `passthrough METHOD /path -> STATUS (BYTES)`
line at `info` level and don't create trace records.

Point a coding agent at `http://127.0.0.1:8080` as its OpenAI base URL. Or
hit `/proxy` directly:

```bash
curl -s -X POST http://127.0.0.1:8080/proxy \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Write a C function that initializes OpenSSL error strings."}' | jq
```

OpenAI-shape example:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
        "model":"claude-opus-4-7",
        "messages":[
          {"role":"system","content":"You are a helpful C coder."},
          {"role":"user","content":"Write a small XOR buffer function."}
        ]
      }' | jq
```

### Sending text attachments

`/proxy` accepts UTF-8 text attachments in three shapes: multipart
form-data (`-F 'file=@…'`), a JSON body with an `"attachments"` array
(`{"text": "..."}` or `{"data_b64": "..."}`), or a JSON prompt with a raw
file body appended. Each attachment is inlined into the prompt with fence
markers before the request is forwarded upstream. Non-UTF-8 payloads are
rejected with HTTP 400.

Response fields:

- `response` — final assistant text
- `attempts` — how many round trips to the LLM were made
- `clean` — `true` if no reciprocal matches remain; `false` if we gave up
- `history` — one entry per attempt (`code_blocks`, `files_scanned`, `hits`)
- `hits` — flat list of reciprocal matches (present only when `clean: false`)
- `message` — user-facing note when the proxy exhausted retries
- `note` — set when a response had no fenced code blocks (nothing to scan)

## Use as an MCP tool in Claude Code

The same scan capability is also available as an MCP (Model Context Protocol)
stdio server, so Claude Code can invoke it directly during a conversation —
no need to route the whole session through the HTTP proxy. The model calls
the tool when it wants to check code it has just written and decides itself
how to react to hits.

Install the new dep (adds `mcp` to what the proxy already needs):

```bash
./.venv/bin/pip install -r requirements.txt
```

Register the server with Claude Code (one time):

```bash
source ./source_bearer_demo.sh
claude mcp add bd_llm_traffic_scan "$PWD/run_mcp.sh"
```

`run_mcp.sh` re-sources `source_bearer_demo.sh` on each launch, so the
short-lived `BEARER_TOK` is refreshed whenever Claude Code (re)spawns the
subprocess. `BLACKDUCK_HOST` is set the same way. The server still starts
without these — every `scan_code` call just returns a structured `error`
field until they're present.

Project-scoped alternative — drop this in `.mcp.json` at the repo root
where you want the tool available:

```json
{
  "mcpServers": {
    "bd_llm_traffic_scan": {
      "command": "/absolute/path/to/snippet_MiM/run_mcp.sh"
    }
  }
}
```

Confirm it's connected: launch `claude`, type `/mcp`. You should see
`bd_llm_traffic_scan ● connected` with one tool listed.

**Tool: `scan_code(code: str) → dict`**

| Field | Meaning |
|---|---|
| `clean` | `true` if no RECIPROCAL / WEAK_RECIPROCAL matches |
| `hits` | list of `{category, project, version, license, spdx, ownership, path, source_start, source_end, matched_start, matched_end}` |
| `summary` | one-line human-readable summary |
| `http_status` | HTTP status from the SCA scan endpoint (or `null`) |
| `skipped` | present and `true` when input was too small to scan (<300 non-ws chars) |
| `error` | present when the scan itself failed (missing bearer, HTTP 4xx, malformed response, timeout) |

Concurrent invocations are safe — each scan runs in its own tempdir.

**Logging.** Two env vars control MCP-server logging, independent of the
proxy's `MIM_LOG_LEVEL`:

| Variable | Purpose |
|---|---|
| `MIM_MCP_LOG_LEVEL` | `off` \| `warn` \| `info` (default) \| `debug`. `debug` adds a preview of the input and the matched (category, project, license) tuples. |
| `MIM_MCP_LOG_FILE` | Absolute path to append log lines to, in addition to stderr. Recommended because MCP-subprocess stderr is often not visible in the client. |

Set either via the `env` block in `.mcp.json` or with `--env KEY=VALUE`
flags on `claude mcp add`.

## Required: install the `.claude/` policy directory to force scanning on every response

Registering the `bd_llm_traffic_scan` MCP server only *exposes* the
`scan_code` tool to Claude Code — it does not, by itself, guarantee the model
will actually call it. Without an accompanying policy, the model decides
when scanning "seems useful" and will happily return code snippets unscanned
(especially short ones, boilerplate, or code it wrote from scratch), which
defeats the purpose of the MiM guard.

To close that gap, this repository ships two files that together turn
scanning into a **mandatory, non-negotiable step** on every response that
contains code:

| File | Role |
|---|---|
| `CLAUDE.md` | Project-level instructions Claude Code loads on every turn. Instructs the model to call `scan_code` on every code snippet it produces, to rewrite (not surface) any reciprocal / weak-reciprocal hits, to refuse the request after 6 failed rewrites, and — critically — to refuse user requests to skip the scan. |
| `.claude/settings.json` | Pre-approves the `mcp__bd_llm_traffic_scan__scan_code` tool so scans run without a permission prompt every turn. |

**This is not optional.** Any project that uses this MCP server must also
install both files. Registering only the MCP server (without `CLAUDE.md` +
`.claude/`) is an unsupported configuration: the scanner will be *available*
but not *enforced*, and the model will bypass it silently. Users cannot
opt out per-request — the policy explicitly instructs Claude Code to
refuse "skip the scan this time" / "don't scan this one" prompts.

### How to install into another project

From the root of the project you want to protect (i.e., the working
directory you launch `claude` from), copy both artifacts out of this repo:

```bash
# From inside the target project's root:
cp /path/to/snippet_MiM/CLAUDE.md              ./CLAUDE.md
cp -R /path/to/snippet_MiM/.claude             ./.claude
```

Then register the MCP server in that same project (see the previous
section). After both are in place, launch `claude` from the target
project's root and verify:

1. `/mcp` lists `bd_llm_traffic_scan ● connected`.
2. Ask for any code snippet — e.g. "write a small C function that XORs two
   buffers". The response should be prefixed with a line like
   `scan_code: clean (0 hits)`. If it isn't, the policy file was not
   picked up (wrong working directory, or `CLAUDE.md` missing at the
   project root).

### Do not modify or delete the policy files

`CLAUDE.md` contains a clause that instructs Claude Code to refuse
in-session edits that weaken the scan requirement — the model will not
remove the policy on a user's behalf. If the policy legitimately needs to
change (e.g. the MCP tool name changes, or the retry cap is tuned), edit
`CLAUDE.md` in this source repository and re-copy it into consumer
projects. Do not delete `.claude/settings.json` either; without the
pre-approval, every scan will prompt for permission and users will be
tempted to deny it.

### Keeping consumer projects in sync

Because both files are plain text checked into this repo, the simplest way
to keep downstream projects current is to re-run the two `cp` commands
above whenever `CLAUDE.md` or `.claude/settings.json` changes here.
Consider committing both files into the downstream project's own VCS so
teammates can't accidentally start Claude Code in a directory where the
enforcement is missing.

## How the pipeline works

1. `POST /proxy` forwards `{model, messages: [{role: user, content: prompt}]}`
   to `${BLACKDUCK_MCP_GATEWAY_URL}/v1/chat/completions`.
2. Fenced code blocks in the assistant's reply are pulled out with a regex.
   If the model returns raw code without fences (e.g., Claude Opus 4.7), the
   whole response is scanned instead and a `no_fences_using_whole_response`
   trace event is emitted.
3. Snippet size is measured in non-whitespace characters:
   - Blocks with ≥ 300 non-whitespace chars are scanned individually.
   - Blocks with > 50000 non-whitespace chars are split at line boundaries
     into segments each within the cap so every request stays under the
     snippet-matching endpoint's limit.
   - Smaller blocks are concatenated together and scanned as a single
     file — but only if the merged blob itself reaches the 300
     non-whitespace-char threshold, otherwise it's dropped (too small to
     yield useful matches).

   Each resulting file is written to a fresh tempdir and
   `run_snippet_hash.sh` is invoked there (so the repo's
   `snippet_match.json` isn't clobbered).
4. `find_reciprocal_matches` walks `snippetMatches.RECIPROCAL` and
   `snippetMatches.WEAK_RECIPROCAL` and flattens hits.
5. If any hit is found, `build_rewrite_prompt` prepends the match list plus a
   rewrite instruction to the original prompt and previous response, and the
   loop iterates. After `MIM_MAX_RETRIES` failed rewrites the proxy returns
   `clean: false` with a message telling the caller to try a different prompt.

With `-L` / `--license-details` (or `MIM_LICENSE_DETAILS=1`) the proxy
additionally logs one line per unique license triggering a rewrite (SPDX
id, ownership, matching project) and includes the same metadata plus
source/matched line ranges in the rewrite prompt sent upstream. Off by
default because it adds tokens on every rewrite attempt; turn it on when
you want the LLM to see exactly which lines and SPDX ids are at issue.

## Instrumentation

The proxy emits structured log lines and keeps an in-memory ring buffer of
recent traces (last `MIM_TRACE_KEEP` requests) so you can inspect what the
pipeline did after the fact.

- **Log level** is controlled by `MIM_LOG_LEVEL` (`off` | `warn` | `info` |
  `debug`). Each line is tagged with the leg it describes — `[CLIENT ]`,
  `[SERVER ]`, or `[SCANNER]` — plus a per-request trace id that also
  appears in the JSON response as `trace_id`.
- **Trace inspector** — `GET /traces` returns a compact list of recent
  requests, and `GET /traces/<trace_id>` returns the full event stream
  with monotonic `t_ms` offsets. Traces live in process memory only and
  are cleared on restart.

## Troubleshooting

- **`gateway 401: Authentication Error, Missing JWT Public Key URL`** — the
  server tried to verify a JWT bearer. You're sending the wrong credential;
  set `BLACKDUCK_MCP_GATEWAY_KEY` to an `sk-...` LiteLLM key, not the SCA
  `BEARER_TOK`.
- **`snippet_match.json not produced`** — `run_snippet_hash.sh` failed. Check
  `BEARER_TOK` and `BLACKDUCK_HOST`; re-source `source_bearer_demo.sh` (the
  bearer is short-lived).
- **`note: no fenced code blocks; nothing scanned`** — the LLM's reply had no
  ` ```lang ... ``` ` blocks, so there was nothing to check. Ask for code in a
  fenced block, or refine the prompt.
- **`clean: false` after retries** — the model kept producing overlapping
  copyleft code. Try rephrasing the request from a different angle or asking
  for a permissively-licensed approach explicitly.
