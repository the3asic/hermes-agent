# merge/upstream-20261102 — upstream/main (b7de5ae5a5+) into fork main (c0f75a56db)

Worktree: /home/the3asic/Dev/hermes-merge-20261102 (branch merge/upstream-20261102)
Production worktree: /home/the3asic/.hermes/hermes-agent (keep running; do not touch until deploy)
Base: f97608f178 (upstream v0.21.5, 2026-09-24). Fork ahead 92 commits (74 non-merge), behind 19628.
Conflicts: 61 files = 52 UU + 9 UD. Zero local commits absorbed upstream (git cherry = 0).

Production runtime: Python 3.11 venv at ~/.hermes/hermes-agent/venv. Prod extras set: messaging
(telegram/discord/aiohttp/slack), mcp, cron, acp, homeassistant etc.; NO tts engines (neutts/
kittentts/piper), no matrix, no playwright.

## Post-merge regression fixes (commit after 6bd7a6fe4a)

Clean merges can silently drop fork or upstream behavior without textual conflicts; the
targeted suite below caught the dropped pieces and each was restored deliberately:

- `gateway/run_turn_prepare.py`: restored `_resolved_fallback_entry` consumption in
  `_resolve_turn_agent_config` → `route["fallback_entry"]` + signature key (feed for
  `_resolve_gateway_reasoning_for_route`).
- `gateway/slash_commands_session.py`: /compress reasoning — non-fallback routes resolve via
  `_resolve_session_reasoning_config` (was `None`; dropped else-branch).
- `gateway/run_background.py`: background tasks route reasoning through
  `_resolve_gateway_reasoning_for_route(fallback_entry=turn_route["fallback_entry"])` +
  `_apply_route_reasoning_policy_to_agent` (upstream file didn't exist on fork side).
- `gateway/run_turn_runner.py`: `usage_baseline` restored to pre-run position (post-run
  baseline zeroed every turn delta → footer `tokens_turn` always hidden).
- `gateway/run_turn.py` + `run_turn_persistence.py`: runtime-footer wiring — `_hmwa_runtime_footer_line`
  calls fork's `_gateway_runtime_footer_line` (upstream `build_footer_line` kwargs signature is
  incompatible and its TypeError was swallowed to ""); queued-followup delivery restores footer
  append + trailing footer send when streamed.
- `agent/turn_context.py`: taken to upstream then fork grafts reapplied — checkpoint hydration
  (`awaiting_real_usage_after_compression` latch on replayable native checkpoints),
  title/session-affinity, reasoning resolver + `reasoning_config` in `_publish_runtime_main`.
- `agent/auxiliary_client.py`: `set_runtime_main(..., reasoning_config=)` parameter +
  ContextVar (merged signature dropped it; every publish call TypeError'd under suppress).
- `agent/auxiliary_codex_response.py`: `input_tokens_details → prompt_tokens_details`,
  `output_tokens_details → completion_tokens_details` (Codex usage detail fields the upstream
  extraction dropped → cache-hit tokens invisible to pricing).
- `agent/usage_pricing.py`: restored `_MODELS_DEV_DIRECT_HOSTS` + `_models_dev_pricing_entry()`
  (models.dev pricing for first-party direct routes) and `raw_usage` propagation in
  `CanonicalUsage` (fast-mode `speed`/`service_tier` detection).
- `agent/auxiliary fallback ladder`: transient 5xx/408 exhaustion now enters the configured
  fallback chain (fork `_is_recoverable_aux_fallback_error` semantics grafted onto upstream's
  `_FALLBACK_REASONS`/`_ladder_provider_fallback`; per-endpoint `base_url` isolation keys).
- `tools/browser_supervisor.py`: fork registry restored onto upstream rewrite — `target_id`
  pinning, `CDPProtocolError`, structured `_runtime_failure`, generation fencing, in-flight
  starter tracking, displaced-instance stop outside the lock; upstream `capture()`/`stop_home()`
  retained.
- `tools/web_tools_extract.py`: redirect reconciliation accepts exactly-one-requested-identity
  rows (final URL in `url` + requested in `metadata.sourceURL` is valid, not ambiguous);
  `metadata["url"]` carries the final URL to the cache; `async_is_safe_url` late-bound so the
  `tools.url_safety` patch seam works; `_extract_safe_urls(facts=None)` backward compat.
- `tools/process_registry*.py`: `ProcessObservabilityMixin` wired into `ProcessRegistry`
  (dead on fork main since the fa91c06c7c refactor — mixin never entered the MRO): session
  fields `ended_at`/`last_output_at`/`output_history_available`, `_init_observability()` in
  `__init__`, snapshots on spawn/output/exit/recover, `HERMES_PROCESS_ID` env marker on
  PTY/pipe/remote spawns. `spawn_via_env` keeps the FORK contract (retained `failed_start`
  session) — upstream cd818a4d05's raise-reporting UX re-implemented one layer up in
  `terminal_tool_background.py`; `test_process_registry.py` and
  `test_terminal_background_spawn_failure.py` assertions updated to match.
- `hermes_cli/doctor.py` tests: `PROJECT_ROOT` pointed at an existing temp dir (subprocess
  cwd), utf-8 `.env` fallback.
- `run_agent.py`: `import copy` restored (`_current_main_runtime` deepcopy).
- `agent/auxiliary_client.py`: `get_scoped_runtime_main` accessor (dead lazy import surfaced
  by a new upstream test).
- `hermes_cli/_early_recovery.py`: pytest-owned-checkout check hoisted above the update-pull
  marker stat (home_io_guard fired on `~/.hermes`-resident worktrees).

Test isolation note: `test_process_registry_observability.py` was red on fork main already
(mixin never wired). Verified-clean suite set: see PR body. Remaining suite failures were
diffed per-test against upstream baseline fbf6c6c91d — every merged failure is red upstream
too (banner, multiplex_status, hold_stream_edges residuals, fal_sdk import = missing dep).

## Deploy procedure (later)
1. In production worktree: `git checkout -- gateway/run_agent_cache.py gateway/run_turn.py gateway/session_state.py`
2. `rm tests/gateway/test_internal_event_pin_wiring.py`
3. `git stash drop` — stash@{0} "preserve preexisting gateway work for reviewed 20261017 release"
   holds the SAME pin diff; must be dropped or it lurks.
4. Fast-forward to merged origin/main, restart gateway, verify channels + pin smoke.
Pin fix note: dirty files = upstream commit 9c18b383de verbatim; upstream main now has superset
(durable _rehydrate_prompt_pins + inherit_channel_pin). After upgrade verify rehydrate works
with fork's session persistence layer.

## Resolution decisions per file
- pyproject.toml DONE: upstream versions (PyJWT 2.15.1, urllib3>=2.8,<3 unconditional for 3.11,
  edge-tts 7.3.1, mcp 2.3.0/httpx2 2.15.1/starlette 1.9.0, aiohttp 3.14.5, teams 2.1.1, all new
  extras neutts/kittentts/piper/google-meet), kept fork homeassistant extra at aiohttp 3.14.5,
  kept constraint-dependencies + upstream default-groups=[] + environments 3.14-only (only
  setting that lets `uv lock` resolve all-extras; prod installs via `uv pip install -e` which
  ignores environments). Fork's 14-day exclude-newer + exemption table superseded by upstream's
  exclude-newer "3 days" — upstream pm resolves the same bricking via _without_release_age and
  a test asserts P3D, so the fork table was retired. uv.lock regenerated via `python -m pm.lock`.
- UD modify/delete list (upstream deleted, fork modified): all 9 deleted per upstream —
  COMPAT_MANIFEST.md, compat_manifest.json, tests/tools/test_browser_homebrew_paths.py,
  test_browser_npx_warmup.py, test_browser_suspect_recycle.py, tools/browser_tool_cdp.py,
  browser_tool_lifecycle.py, browser_tool_lightpanda_fallback.py, browser_tool_session.py.
  Upstream's supervisor refactor relocated their logic; the fork's boundary guard was ported
  into the new cleanup_browser path in agent/turn_facade.py (marked after cleanup completes so
  a failing cleanup keeps the guard armed for retry).

## Local commits to preserve (74, grouped)
- Compression family: stall fallback bounds, output limits, route capability filter, in-place
  compaction LCM session, preflight guards (3bdff3d3f8, e19795ae20, 4b985e7518, 5512c388a8,
  57934fd13c, ee4ddd5989, 5fc1046645, 4611b20932, aab4145655, eb77d0ac01)
- Cron: runtime footer honesty + explicit cron-only fallback chain (5d7c6fbb1e, 931f8806ea)
- Models: GPT-6.1 Sol max reasoning effort (2eb8dea263)
- Browser/CDP family: pinned CDP ownership, supervisor bind, tab-pin sessions, lifecycle fence,
  NUL guards, endpoint rediscovery, idle-wait retention (61c81f6bce, 9764a3ed34, 519b5b32ed,
  eae29dfc6f, 6bcb7ba3fe, e5916ac4a9, 4a99d748ba, 3ced7c248a, 9af83f3715, a9c8c1b461,
  018a0e897c, d958ff051f)
- Web/Firecrawl: redirect policy, rescue, provenance, fail-closed selection, capability
  discovery (054c11f91c, 2b4c74b3eb, ab523ffe64, 2d77fe7419, 8b4411591c, 97f8361013,
  561a836849, 25568ea2e7, 786fc4518a, 891dd93cc4)
- State/FTS: fail-closed FTS repair, defer corrupt rebuilds, durable row identity, transcript
  replay metadata (b0a0ac031e, a200bc53b6, c69db2d53b, 098b69405e, 0f00185016, ea6870ea2a,
  6c5ec5632d)
- Auxiliary: fallback after 5xx, Responses usage/effort, cache diagnostic, unstructured 5xx
  (3a1dc5002d, 824e69828f, eedf3041a4, 9bcefe62e5, 1bc1f3c03d)
- Runtime/gateway: fallback provider resolution, usage reporting, checkpoint serialization,
  queued turns, context telemetry, timestamp replay, display metadata, redirected processes,
  expired clarify prompts (40c1bdf3e4, 8bb00e12ee, 838fd4c81b, db926050fd, 4858b31007,
  526aac1d20, ea59829d86, 0fdccde8c5, fdec8f7e68)
- Misc: telegram skill menu priority, discord backfill/attachment modes, systemd intentional
  stops, process snapshots, CI guards, release policy doc (d315a39892, c946966362,
  388648766c, fdec8f7e68, 8e97eb10a1, beda0b0f1a, 17214f4250, b7d82ea792, e8e1febd75,
  4bff2e81f6, c86019a6cc, fdb1d25cc4, 453e93538d, 3e821e888c, dada95e26b, 31150a3195,
  f4f0cfd319, 86b472d39a, beda0b0f1a)
