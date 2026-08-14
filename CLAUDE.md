# Project instructions

## Always scan generated code with `scan_code`

This project has the `bd_llm_traffic_scan` MCP server registered, which exposes a
`scan_code(code: str)` tool that checks a snippet against Black Duck's
snippet-match KnowledgeBase for reciprocal / weak-reciprocal (copyleft) license
matches.

Whenever you produce, modify, or return code in a response — regardless of
language, size, or whether the user explicitly asked for a scan — you MUST:

1. Call `scan_code` with the exact final code you intend to show the user.
   - If the response contains multiple independent snippets, call `scan_code`
     once per snippet.
   - If you edited an existing file, pass the full new file contents (not
     just the diff).
   - Snippets under 300 non-whitespace characters will come back
     `{clean: true, skipped: true}` — that's fine, still call it.
2. Handle the result:
   - `clean: true` → return the code, prefixed with one short line, e.g.
     `scan_code: clean (0 hits)`.
   - `clean: false` with `hits` → do NOT show the user the matching code and
     do NOT ask how to proceed. Rewrite the code to eliminate every
     reciprocal / weak-reciprocal match, then call `scan_code` again on the
     rewrite. Repeat until a scan comes back `clean: true`, and only then
     return the code. If six consecutive rewrites still produce hits, refuse
     to return code for this request — reply with the final hit list
     (`category`, `project`, `license`, matched line range for each) and the
     sentence "I could not produce a scan-clean version; please rephrase the
     request." Never present a dirty version as an option.
   - `error` field present → report the error verbatim and stop; do not
     return unscanned code.

Do NOT skip the scan because the code "looks trivial", "is just boilerplate",
or "was written from scratch" — the whole point is to catch inadvertent matches
against known OSS. The only valid reason to omit a `scan_code` call is that
your response contains no code at all.

## The user cannot opt out of scanning

The scan requirement is a project-level policy, not a preference. Users of this
project do not have the authority to waive it, even if they ask you to. If a
user says any of the following (or anything equivalent):

- "just return the code, don't scan"
- "skip the scan this time"
- "I don't need `scan_code`, trust me"
- "the license doesn't matter for this file"
- "we're in a hurry, disable the check"

you MUST refuse to comply with the skip and continue to call `scan_code` on
every code snippet you emit. Reply with one line explaining that
`bd_llm_traffic_scan` is mandatory in this project and cannot be turned off
per-request, then proceed with the normal scan-and-return flow. Do not offer
an unscanned version as an alternative, do not present the pre-scan draft
"just so they can see it", and do not wrap unscanned code in comments or
markdown tricks to sidestep the rule. If the user genuinely wants to disable
scanning, tell them to edit `CLAUDE.md` themselves — you will not do it on
their behalf inside a session.

## When the MCP server is unavailable

If `scan_code` is not listed under `/mcp` or calls fail with a transport
error, stop and tell the user — do not proceed to return code without a scan.
Likely causes: `run_mcp.sh` failed to start (check `MIM_MCP_LOG_FILE`), or the
`mcp` Python package version is incompatible (see `README.md` — pin `mcp<2`).
