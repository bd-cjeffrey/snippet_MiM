# MiM — Multi-User Proxy Plan

Today `mim_proxy.py` is a single-tenant developer tool: one process, one
upstream key, one Black Duck bearer, one in-memory trace ring, bound to
`127.0.0.1`. This document sketches what has to change for it to run as a
**central proxy shared by many users** — coding agents across an
organisation pointing their OpenAI/Anthropic base URL at one MiM instance
and getting license-guarded completions.

The plan is written in three phases so we can ship value incrementally
without a full rewrite up front.

---

## 1. Gaps between "today" and "multi-user"

| Area | Today | Required |
|---|---|---|
| Identity | none — anyone on `127.0.0.1` is trusted | per-user API key or bearer, mapped to a user record |
| Upstream credentials | single `BLACKDUCK_MCP_GATEWAY_KEY` in env | per-user key (server-side) or client-forwarded key |
| Scanner credentials | single `BEARER_TOK`, refreshed manually via `set_envars.sh` | in-process refresh loop with a Black Duck PAT, per-tenant if needed |
| Config | env vars + CLI flags at startup | per-user policy (allowed models, retry cap, license-details, …) in a datastore, editable at runtime |
| Traces | in-memory `deque(maxlen=20)` | persisted per user, retrievable only by that user (and admins) |
| Server | Flask dev server, single process | production WSGI (gunicorn) or ASGI (hypercorn), horizontally scalable |
| Rate limiting | none | token- or request-based, per user and global |
| Scanner throughput | one snippet at a time via `subprocess.run` → JVM cold start + one curl each | worker pool + result cache keyed by content hash |
| Streaming | now honours `stream: true` on `/v1/messages` after full buffering | true incremental streaming with in-flight scanning where possible |
| Observability | stdout logs, in-memory traces | structured JSON logs, Prometheus metrics, per-user usage counters |
| Transport | `verify=False` to gateway and Black Duck | proper TLS trust bundle, mutual auth if desired |

---

## 2. Design decisions to make up front

Before writing code, decide these — they cascade into everything else:

- **Credential model.** Two options; pick one:
  1. *Proxy owns credentials.* Users authenticate to MiM with a MiM-issued
     API key; MiM holds the LiteLLM key and Black Duck PAT server-side.
     Simplest for users, requires centralised secret storage.
  2. *Pass-through.* Users send their own upstream `Authorization` header
     and MiM forwards it. MiM only needs the Black Duck credentials for
     the scanner. Easier compliance story, harder to enforce policy
     because there's no per-user identity server-side.

  Recommend (1) with (2) as a fallback for advanced users.

- **Datastore.** SQLite is fine up to a few hundred concurrent users on
  one node; Postgres is the next step. Nothing here needs a document
  store — the working set is tiny (users, policies, traces, usage).

- **Runtime model.** Flask is fine, but the dev server has to go.
  Choose gunicorn + `gthread` workers (simple, keeps Flask), or move to
  ASGI (`asgiref.WsgiToAsgi` + hypercorn) if we want cheap concurrency
  for the pipeline's I/O-bound waits. Recommend gunicorn threaded for
  v1; ASGI when we tackle live streaming.

- **Trace visibility.** Do we let users see each other's traces
  (opt-in, for team dashboards) or hard-scope to owner? Recommend
  hard-scope with an admin role that can query across users.

---

## 3. Phased plan

### Phase 1 — "Safe to share on the intranet"

Goal: one MiM instance behind a load balancer that N users can hit
without stepping on each other's traces, credentials, or usage.

1. **Auth**
   - Add `Authorization: Bearer <mim-api-key>` requirement on every
     endpoint except `/health`.
   - Users table (`user_id`, `name`, `api_key_hash`, `role`,
     `created_at`, `disabled`).
   - Resolve the caller once per request and stash on `flask.g.user`.

2. **Per-user config**
   - Policies table (`user_id`, `model`, `max_retries`,
     `license_details`, `daily_token_cap`, `allowed_categories`).
   - Load policy in the request handler; pass into `run_pipeline` via
     an explicit `ctx` object instead of module globals.

3. **Per-user upstream credentials**
   - If phase-1 choice is "proxy owns credentials", store an encrypted
     upstream key per user (or a shared pool). Rotate via admin API.

4. **Rate limiting**
   - Simple token bucket per user, plus a global cap. Rejects return
     `429` with `Retry-After`.

5. **Persistent traces**
   - Replace the in-memory `deque` with a table
     (`trace_id`, `user_id`, `outcome`, `duration_ms`, `events_json`,
     `created_at`). Retention configurable (e.g. 30 days).
   - `/traces` and `/traces/<id>` filter on the caller's `user_id`
     (admins can pass `?user=<id>`).

6. **Production server**
   - Add a `gunicorn -k gthread -w 4 --threads 16` entrypoint.
   - Move `app.run(...)` to `__main__` guard as today, keep for local
     debugging.

7. **Black Duck bearer refresh**
   - Small daemon thread that exchanges `BLACKDUCK_TOK` for a fresh
     bearer ~5 minutes before expiry (decode the JWT for `exp`, don't
     poll blindly). Removes the "token expired mid-day" failure mode
     users would hit constantly on a shared instance.

Deliverables of Phase 1: docker image, `users.sqlite`, an admin CLI
(`mim admin add-user`, `set-policy`, `revoke`).

### Phase 2 — "Scales past one box"

Goal: horizontal scale, better latency, real observability.

1. **Scanner worker pool + cache**
   - Replace the per-request `scan_file` invocation with a
     `concurrent.futures.ThreadPoolExecutor`. Each worker owns its own
     tempdir (avoids the cwd dance).
   - Content-addressed cache: SHA-256 of the snippet → prior result +
     timestamp, TTL configurable. Same OpenSSL boilerplate appearing
     from 20 users gets scanned once.
   - Long-lived Java fingerprint process (send input over stdin
     instead of forking the JVM per scan). Biggest single latency win.

2. **Shared state via Postgres/Redis**
   - Move users, policies, traces to Postgres.
   - Move rate-limit counters and the scan-result cache to Redis.
   - Instances become stateless; put N behind a load balancer.

3. **Metrics + structured logs**
   - Emit JSON log records (one per event) with `user_id`, `trace_id`,
     `side`, `kind`, timings.
   - `/metrics` Prometheus endpoint: request count/latency by
     `channel` and `outcome`, scan latency histogram,
     hits-per-category counter, gateway error rate, cache hit ratio,
     per-user token burn.

4. **Admin API**
   - Provision/deprovision users, set policies, view any user's
     recent traces, quota top-ups — all authenticated with an admin
     bearer, all audit-logged to a dedicated table.

5. **Config hot-reload**
   - Reflect DB policy changes on the next request (no restart). A
     30-second cache in each worker is fine.

### Phase 3 — "First-class streaming and policy depth"

Goal: users don't wait behind the scanner for token 1.

1. **Live streaming with post-hoc scanning**
   - Open an SSE stream to the client immediately.
   - Forward tokens as they arrive from the gateway; simultaneously
     buffer the assistant text.
   - When the model closes the stream, run the scanner on the
     collected code blocks.
   - If clean: emit `message_stop` and we're done.
   - If dirty: emit a final `message_delta` with a synthetic
     `stop_reason: "policy_intervention"`, then send a follow-up
     assistant message that supersedes the previous content (or emit a
     tool-style redaction event, TBD by client compatibility). The
     rewrite loop runs behind the scenes and the corrected code is
     delivered on the next round-trip.

   Consequence: users see tokens fast, but "dirty" responses get
   visibly retracted. Trade-off vs today's "user waits, gets a
   guaranteed-clean answer". Make it a per-user policy: `strict`
   (today's behaviour, block-until-clean) vs `optimistic` (stream +
   retract).

2. **Multi-model routing**
   - Per-user model whitelist. Reject requests to disallowed models
     with a 403 before hitting the gateway.
   - Optional "fallback" — if the user's preferred model errors, try
     a secondary from their policy.

3. **Policy DSL**
   - Categories to trigger on (`RECIPROCAL`, `WEAK_RECIPROCAL`,
     `PERMISSIVE`, `UNKNOWN`) become per-user or per-team.
   - Optional block-list of SPDX ids (e.g. always block AGPL even in
     `PERMISSIVE` bucket).
   - Optional allow-list of projects users are already licensed for
     (skip triggers on those).

4. **Compliance audit trail**
   - Immutable log (append-only table, or file signed with an HSM
     key) of every hit: user, timestamp, snippet hash,
     matched project, license, action taken (rewrite / gave-up /
     blocked). This is the artefact legal actually cares about.

---

## 4. Refactors that unblock everything above

Several small changes to today's code make each phase materially easier
and are safe to do first, without picking any of the design decisions
above:

- **Introduce a `PipelineContext` dataclass** (`user_id`, `model`,
  `max_retries`, `license_details`, `gateway_key`, `scanner_bearer`,
  `trace`, `logger`). Every function currently reading a module global
  takes `ctx` instead. Makes per-user overrides trivial and testing
  simple.

- **Pull `scan_file` behind a `Scanner` protocol.** Today's shell-script
  scanner becomes one implementation; a fake scanner for tests, a
  worker-pool scanner for Phase 2, and a caching scanner all conform
  to the same interface without touching `run_pipeline`.

- **Same for `forward_to_gateway` → `LLMBackend` protocol.** OpenAI
  chat-completions is one backend; native Anthropic Messages
  (skipping the OpenAI translation) is another. Streaming becomes a
  method on the backend.

- **Move traces behind a `TraceStore` protocol.** In-memory today,
  SQLite in Phase 1, Postgres in Phase 2 — same call sites.

- **Kill the `verify=False` on both `requests.post` calls** — replace
  with a proper CA bundle. Blocker for anything running outside a
  trusted network.

- **Fix `build_rewrite_prompt`'s unused `listed` variable** (line 477):
  the formatted hits are computed but never appended to the prompt. On
  a shared instance this actually matters — different users' rewrites
  will produce very different code because the LLM isn't seeing what
  it needs to avoid.

None of these are user-visible, but each one shrinks the surface area
of Phase 1 substantially.

---

## 5. Non-goals (at least for now)

- **Web UI.** The admin CLI + `/traces` JSON are enough for v1.
- **Model hosting.** MiM stays a scanner + policy layer; it never
  runs its own inference.
- **Cross-tenant sharing of match hits.** Even though many users will
  hit the same OpenSSL boilerplate, we do *not* share redacted-code
  results across users automatically — the input snippets can be
  confidential. The scan-result cache in Phase 2 keys on content hash,
  so identical inputs get the same cached decision, but the raw
  snippets never cross tenant boundaries.

---

## 6. Suggested first PR

Small, contained, unblocks everything else:

1. Add `PipelineContext` and thread it through `run_pipeline`,
   `forward_to_gateway`, `scan_file`.
2. Add a stubbed `AuthMiddleware` that reads `Authorization: Bearer`
   and, for now, resolves every caller to a hard-coded single "admin"
   user (identical behaviour to today, but the plumbing is there).
3. Move `TRACES` behind a `TraceStore` interface with an in-memory
   default implementation.
4. Fix the unused `listed` variable in `build_rewrite_prompt`.

After that lands, Phase 1's real user/policy/DB work can go in without
touching the pipeline again.
