# Agent Runs ProcessRegistry Snapshot

`ProcessRegistry` atomically publishes a bounded, read-only JSON snapshot at:

```text
<HERMES_HOME>/runtime/agent-runs-observability.json
```

This is the supported file contract for external Agent Runs experiments.
`<HERMES_HOME>/processes.json` remains a private crash-recovery checkpoint and
must not be scraped.

The v1 snapshot contains only orchestration lifecycle data:

- `schema_version`: `hermes.agent_runs_observability.v1`
- `observation_scope`: always `orchestration`
- `orchestration_host_alias`: a validated local host alias, or `unknown`
- `generated_at` and `freshness.observed_at`: snapshot wall-clock time
- `freshness.max_age_seconds`: `null`; the reader owns its stale threshold
- `runs`: at most 64 currently or recently retained ProcessRegistry entries

Each run contains only `proc_id`, bounded orchestration state and completion
reason, start/end timestamps, exit code, and tri-state output availability
metadata. `null` means unknown; it must not be converted to zero or false.
After crash recovery, `last_output_at`, `last_output_available`, and
`output_history_available` are all `null` because the original in-memory
output history is gone.

The file intentionally excludes local PID, command, cwd, task/session/channel
metadata, output text, URLs, credentials, and resource counters. In particular,
a local PID may belong to an SSH wrapper while Codex executes remotely, so it
must never be interpreted as remote runner identity or remote resource use.

## Remote execution join

Hermes does not currently receive a trustworthy remote runner identity from
the ProcessRegistry spawn API. The remote launcher/observer must receive the
opaque `proc_id` explicitly and publish its own bounded execution observation,
then join externally on `proc_id`. Do not derive remote identity by parsing the
command string. The remote observation producer is outside this contract and
is the remaining integration dependency for Hyperbole Max resource data.

The snapshot is written with mode `0600`; its `runtime` parent is mode `0700`.
Readers require only filesystem read access and never need access to secrets.
