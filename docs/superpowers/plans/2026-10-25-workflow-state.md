# Cairn workflow state implementation plan

**Goal:** Implement the five requested increments with legacy SQLite/protocol compatibility.

**Architecture:** Extend the existing sqlite3/FastAPI models and leased dispatcher flow. Keep deployment middleware and nftables policy independent of the engine. The user's detailed goal specification and requested execution order authorize continuous implementation.

**Decisions:** `current_stage` is a zero-based cursor; `len(stages)` means all stages completed. Each nonempty goal line is a stage. Reason advances the cursor based on evidence; completion is guarded for multi-stage projects. Action descriptions match `next_steps` exactly. Dependencies reference existing same-project intent IDs. Session artifacts carry explicit topic/worker/backend metadata; only matching sessions resume. Full fact descriptions stay in YAML, summaries go into reason prompts. Negative folding uses fact insertion order for recency.

## Work and verification

- [x] Server: additive schema upgrade and standalone SQL; plan lease validation, staged completion guard, dependency guards, artifacts CRUD/expiry/cascade, structured facts. Verify old-schema restart and API behavior.
- [x] Plan dispatcher: validate optional plan, persist before actions, inject current plan, filter dependencies and select ascending priority. Verify three-stage progression and legacy reason payloads.
- [x] Artifacts: validate conclude metadata/artifacts before writes, persist via artifact API, inject active inventory and resume matching sessions in supported drivers. Verify execute and fallback paths, expired/wrong-topic sessions, driver argv.
- [x] Facts: render `id|kind|summary`, legacy fallback, fold negative groups over three entries, retain lazy full YAML access. Verify rendering and escaping.
- [x] Command middleware: concurrent token bucket, proxy injection, request audit, pool checking, image install and local mock HTTP tests.
- [x] Deployment: isolated Docker bridge, atomic nftables policy and systemd lifecycle, strict allowlist parsing, private/host/IPv6 guards, reproducible host verification commands.
- [x] Integration: run all tests, review diff independently, resolve findings, export complete patch.

## Review focus

Malformed output must not cause partial writes before validation. Expiry comparisons must handle timezone offsets. Wrong-topic/backend sessions must not resume. Blocked intents must not dispatch or suppress required planning. nftables must preserve unrelated host rules and cover host INPUT, bridge peer traffic and IPv6.

## Progress

2026-10-25: Clean pre-existing `work` branch verified; independent server, planning, rendering and deployment tasks delegated with disjoint file ownership. No host firewall changes or remote test-target runs are implied by local verification.

Integration decisions: bootstrap may return a fact without completion and yields stage progression to reason. Reopening resets the stage cursor for reevaluation. Explicit blockers permit no-action plans. Validate all action references before writing a plan. Save artifacts before conclusion, then atomically bind their returned IDs with the fact and intent; a failed artifact request keeps the intent retryable. Session recency ties use inventory insertion order. A pre-existing process-cleanup test now recognizes Linux zombies as exited when PID 1 does not reap them; production process behavior is unchanged.

Independent review: core and deployment reviewers identified reference validation, artifact write ordering, request-header injection and proxy/probe documentation gaps. Fixes are covered by regression tests; deployment tests do not substitute for a real Docker-host packet-path check.

Final verification: 291 passed in 45.97s; shell syntax, Python compilation, Compose config and Dockerfile checks passed. Dockerfile check suppresses only the existing constant FROM-platform warning. Full worker image build and real Linux host/remote-target connectivity were not run.

PR verification, 2026-10-25: 306 tests passed with no skips after adding regression coverage for session expiry preservation on both conclusion paths and fail-closed network identity checks during enable/disable, including loss of runtime state. Python/shell syntax, whitespace and Compose configuration checks passed. Live worker-image and host firewall acceptance remain pending the documented environment requirements.
