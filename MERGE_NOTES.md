# merge/upstream-20261102 — upstream/main (b7de5ae5a5+) into fork main (c0f75a56db)

Worktree: /home/the3asic/Dev/hermes-merge-20261102 (branch merge/upstream-20261102)
Production worktree: /home/the3asic/.hermes/hermes-agent (keep running; do not touch until deploy)
Base: f97608f178 (upstream v0.21.5, 2026-09-24). Fork ahead 92 commits (74 non-merge), behind 19628.
Conflicts: 61 files = 52 UU + 9 UD. Zero local commits absorbed upstream (git cherry = 0).

Production runtime: Python 3.11 venv at ~/.hermes/hermes-agent/venv. Prod extras set: messaging
(telegram/discord/aiohttp/slack), mcp, cron, acp, homeassistant etc.; NO tts engines (neutts/
kittentts/piper), no matrix, no playwright.

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
  ignores environments), kept fork exclude-newer "14 days" + full exemption table, ADDED
  exemptions: kittentts, misaki, neutts, piper-tts, playwright, urllib3.
- UD modify/delete list (upstream deleted, fork modified): COMPAT_MANIFEST.md,
  compat_manifest.json, tests/tools/test_browser_homebrew_paths.py, test_browser_npx_warmup.py,
  test_browser_suspect_recycle.py, tools/browser_tool_cdp.py, browser_tool_lifecycle.py,
  browser_tool_lightpanda_fallback.py, browser_tool_session.py — need to find where upstream
  moved the logic (browser supervisor refactor family) before deciding keep-vs-port.

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
