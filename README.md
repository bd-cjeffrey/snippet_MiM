# MiM — Man-in-the-Middle License Guard

A small HTTP proxy that sits between a coding agent and a LiteLLM-compatible
LLM gateway (`$BLACKDUCK_MCP_GATEWAY_URL`). It is a prototype that sits in a
dev’s desktop but could be extended as a proxy server. It forwards LLM prompts upstream and,
for every fenced code block in the response, runs BlackDuck's snippet-matching
API via `run_snippet_hash.sh`. If any match is classified as `RECIPROCAL` or
`WEAK_RECIPROCAL`, the proxy re-prompts the LLM to rewrite the code without
those matches. It gives up after 6 attempts and asks the caller to try a
different prompt. In practice, we see 1 or 2 reprompts.

Testing was performed with Claude and opus-4-7.

This proxy sends SCA fingerprints instead of plaintext to the api/snippet-matching endpoint, 
for better match results.  A link to download the SCA fingerprint utils is included below.

## Requirements

- Python 3.9+
- `curl` and `jq` on `PATH` (used by `source_bearer_demo.sh` and `run_snippet_hash.sh`)
- Network access to both the BlackDuck SCA host and the MCP/LiteLLM gateway
- A BlackDuck SCA personal access token and a LiteLLM API key
- Access to the SCA fingerprint jar file and java app, found at: https://blackduck.atlassian.net/wiki/spaces/SUCCESS/pages/1979417025/Black+Duck+Generative+AI+Compliance+SDK+Guide   

## Install

```bash
cd /path/to/snippet_MiM
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
chmod +x run_snippet_hash.sh source_bearer_demo.sh
```
Download SCA fingerprint and java app (sca.java) from link. 
Build sca.java app: 
   javac -cp .:sca-fingerprint-client-1.0.0.jar sca.java 

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

**Direct your coding agent** to connect to the proxy, eg:
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080


| Flag | Env var | Default |
|---|---|---|
| `-l`, `--log-level {off,warn,info,debug}` | `MIM_LOG_LEVEL` | `info` |
| `-p`, `--port` | `MIM_PORT` | `8080` |
| `-m`, `--model` | `MIM_MODEL` | `gpt-4o-mini` |
| `-r`, `--max-retries` | `MIM_MAX_RETRIES` | `6` |
| `--trace-keep` | `MIM_TRACE_KEEP` | `20` |
| `-L`, `--license-details` | `MIM_LICENSE_DETAILS` | off |

The proxy listens on `127.0.0.1:$MIM_PORT`. Confirm it's up:

```bash
curl -s http://127.0.0.1:8080/health | jq
# {
#   "gateway_url_set": true,
#   "gateway_key_set": true,
#   "sca_bearer_set": true,
#   "model": "gpt-4o-mini",
#   "script_present": true,
#   "max_retries": 6
# }
```

## Use

The proxy exposes three shapes of endpoint:

| Route | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible entry point. If the request carries a user message, the assistant text is scanned and possibly rewritten before being returned. If it only has system/tool messages, or the last user content is empty, the whole request is forwarded upstream unchanged. |
| `POST /v1/messages` | Anthropic Messages API — used by Claude Code and other Anthropic-native clients. Same scan/rewrite behavior as `/v1/chat/completions`, but the response is wrapped in Anthropic shape (`{id: "msg_...", type: "message", content: [{type:"text", text:...}], stop_reason: "end_turn", ...}`). Tool-result follow-ups (last user message contains only `tool_result` blocks) are forwarded upstream unchanged. |
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

Attachments are treated as UTF-8 text (source files, logs, config). The proxy
inlines each attachment into the prompt with fence markers, then forwards the
combined text to the LLM:

```
<original prompt>

--- attachment: <filename> ---
<contents>
--- end attachment ---
```

Non-text content (binary bytes that don't decode as UTF-8) is rejected with
HTTP 400.

**Multipart form-data**:

```bash
curl -s -X POST http://127.0.0.1:8080/proxy \
  -F 'prompt=Find the bug in these files.' \
  -F 'file=@./main.c' \
  -F 'file=@./build.log'
```

Any number of file parts is accepted; the field name is ignored.

**JSON**:

```json
{
  "prompt": "Fix this:",
  "attachments": [
    {"filename": "app.py", "text": "def hi(): return nam\n"}
  ]
}
```

Either `"text": "..."` (raw string) or `"data_b64": "..."` (base64 that
decodes to UTF-8) is accepted per attachment.

**JSON prompt + raw file body** (convenient one-liner from a shell):

```bash
curl -s -X POST http://127.0.0.1:8080/proxy \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"finish the free_ssl routine in the attached file"}' \
  --data-binary @./prompt.txt
```

curl concatenates `-d` and `--data-binary` with `&`, sending
`{"prompt":"..."}&<file contents>`. The proxy parses the JSON prefix and
treats the trailing bytes as a UTF-8 text attachment named `attached`. Any
`"attachments"` array inside the JSON is honored too — they simply add to
whatever comes in the trailing body.

Attachment metadata (`filename`, `chars`) appears in the `request` trace
event; the full inlined prompt is only visible at `--log-level debug`.

Response fields:

- `response` — final assistant text
- `attempts` — how many round trips to the LLM were made
- `clean` — `true` if no reciprocal matches remain; `false` if we gave up
- `history` — one entry per attempt (`code_blocks`, `files_scanned`, `hits`)
- `hits` — flat list of reciprocal matches (present only when `clean: false`)
- `message` — user-facing note when the proxy exhausted retries
- `note` — set when a response had no fenced code blocks (nothing to scan)

## How the pipeline works

1. `POST /proxy` forwards `{model, messages: [{role: user, content: prompt}]}`
   to `${BLACKDUCK_MCP_GATEWAY_URL}/v1/chat/completions`.
2. Fenced code blocks in the assistant's reply are pulled out with a regex.
   If the model returns raw code without fences (e.g., Claude Opus 4.7), the
   whole response is scanned instead and a `no_fences_using_whole_response`
   trace event is emitted.
3. Blocks < 300 chars are concatenated into a single file; larger blocks are
   scanned individually. Each file is written to a fresh tempdir and
   `run_snippet_hash.sh` is invoked there (so the repo's `snippet_match.json`
   isn't clobbered).
4. `find_reciprocal_matches` walks `snippetMatches.RECIPROCAL` and
   `snippetMatches.WEAK_RECIPROCAL` and flattens hits.
5. If any hit is found, `build_rewrite_prompt` prepends a
   rewrite instruction to the original prompt and previous response, and the
   loop iterates. After `MIM_MAX_RETRIES` failed rewrites the proxy returns
   `clean: false` with a message telling the caller to try a different prompt.

## Instrumentation

The proxy emits structured log lines and keeps an in-memory ring buffer of
recent traces so you can watch — and later inspect — what the pipeline is
doing.

**Log level.** Set `MIM_LOG_LEVEL` before starting:

- `off` — silence everything, including werkzeug access lines
- `warn` — only errors
- `info` (default) — one line per pipeline event
- `debug` — adds the underlying HTTP request logs from `urllib3`

Every request gets a short hex trace id used as a prefix on log lines and
returned in the JSON response as `trace_id`, so you can correlate output
across streams.

Example info-level output for a clean request:

```
11:01:23 INFO  [28465c0e] request prompt_chars=45
11:01:23 INFO  [28465c0e] attempt_start n=1 prompt_chars=45
11:01:30 INFO  [28465c0e] upstream_ok ms=6905 resp_chars=1963
11:01:30 INFO  [28465c0e] code_blocks found=1 files=1 sizes=[1058]
11:01:31 INFO  [28465c0e] scan_ok idx=0 bytes=1058 ms=1610
11:01:31 INFO  [28465c0e] clean
11:01:31 INFO  [28465c0e] done outcome=clean
```

Emitted event kinds:

| Kind | When | Useful fields |
|---|---|---|
| `request` | request received | `prompt_chars` |
| `attempt_start` | before each round trip to the LLM | `n`, `prompt_chars` |
| `upstream_ok` | LLM responded | `ms`, `resp_chars` |
| `gateway_error` | LLM call failed | `err` |
| `no_fences_using_whole_response` | assistant returned raw code without fences; whole response is scanned | `chars` |
| `empty_response` | assistant returned an empty message | — |
| `code_blocks` | after extraction/grouping | `found`, `files`, `sizes` |
| `scan_ok` | one file scanned by `run_snippet_hash.sh` | `idx`, `bytes`, `ms` |
| `scan_error` | scan failed | `err` |
| `hits` | reciprocal matches found | `count`, `unique`, `sample` |
| `clean` | no matches | — |
| `rewrite_prompt` | re-prompt is about to be sent | — |
| `done` | terminal state for the request | `outcome` |

**Recent-request inspector.** Two endpoints let you pull traces back after the
fact — handy when a coding agent, not you, is driving the proxy:

- `GET /traces` — compact list of the last `MIM_TRACE_KEEP` requests
  (newest first), one line per request with `trace_id`, `outcome`,
  `duration_ms`, `events` count, and prompt preview.
- `GET /traces/<trace_id>` — the full event stream for one request, with
  monotonic `t_ms` offsets so you can see where the time went.

```bash
curl -s http://127.0.0.1:8080/traces | jq
curl -s http://127.0.0.1:8080/traces/28465c0e | jq
```

Traces live in process memory only — restarting the proxy clears them.

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
