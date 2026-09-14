# State database and FTS recovery

state.db has two different kinds of data:

- sessions and messages are the canonical session and transcript records.
- messages_fts, the CJK/trigram FTS tables, and their sync triggers are
  derived search indexes.

The derived indexes are allowed to be temporarily unavailable. They must never
make an inbound message wait for a whole-database rebuild.

## Online behavior when FTS is corrupt

If an FTS write or search reports a corruption error, SessionDB:

1. records the durable fts_stale marker in state_meta;
2. removes the FTS sync triggers;
3. retries the write against the canonical tables;
4. serves search through a bounded LIKE fallback.

Normal gateway and CLI opens see the marker and keep FTS detached. They do not
run FTS5 rebuild during startup or a user request. This is intentional:
rebuilding scans every message while holding a SQLite write transaction, and a
multi-gigabyte database can otherwise turn one bad index into a gateway-wide
outage. The transcript remains durable; only indexed search is degraded.

## Live behavior when the file itself is corrupt

If a live write reports bare `SQLITE_CORRUPT` / `SQLITE_NOTADB` (`database
disk image is malformed`, `file is not a database`) with no FTS provenance,
the damage is in a canonical B-tree, the schema, or the freelist. `SessionDB`
then quarantines that handle (`StateDbCorruptError`):

1. the failing write propagates the typed error and nothing is retried;
2. later writes on the handle fail immediately without touching the file;
3. the handle never reopens its connection after `close()`; and
4. `close()` skips its explicit WAL checkpoint.

Stopping the writes is the protection. In the field, a handle that kept
writing for ~50 minutes after the first structural error checkpointed 15
pages under the wrong page numbers on shutdown (page 1 received a
`messages_fts_trigram_data` leaf) and turned a damaged-but-readable file into
one that no longer opened at all. Skipping the explicit checkpoint is the
second line of defence; on Python 3.12+ the quarantine also disables
SQLite's own last-connection checkpoint (`SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`),
so the `-wal` sidecar survives `close()` for forensics. On Python 3.11 that
switch is unavailable and SQLite may still checkpoint once on close, so copy
`state.db`, `state.db-wal` and `state.db-shm` together before restarting
anything.

The gateway and the agent flush path treat the quarantine like a replaced
file: pending transcripts go to `sessions/<id>.jsonl` and the gateway
`pending_messages/` spool instead of the retry queue, and the FTS one-shot
rebuild never runs on the damaged file. The quarantine is per process — the
shared handle stays poisoned for every holder until the process restarts on a
repaired or restored file. Do not run `hermes doctor --fix` while the gateway
is still up. Next steps:

```bash
hermes gateway stop
HERMES_HOME="$HOME/.hermes" hermes sessions recover --source "$HOME/.hermes/state.db" --inspect-only
# if recoverable:
HERMES_HOME="$HOME/.hermes" hermes sessions recover --source "$HOME/.hermes/state.db" --output "$HOME/recovered-state.db"
```

or restore the newest snapshot from `state-snapshots/`.

## Offline repair

Stop every process that can open the profile's database before repairing it,
especially hermes-gateway.service. Keep the gateway stopped for the complete
repair and verification window.

    systemctl --user stop hermes-gateway.service
    HERMES_HOME="$HOME/.hermes" hermes sessions repair --check-only
    HERMES_HOME="$HOME/.hermes" hermes sessions repair

sessions repair creates a SQLite backup by default, then opts into the
unbounded FTS rebuild. It only reports success after the database opens cleanly
again. Do not copy state.db, state.db-wal, or state.db-shm with cp as a backup;
use the command's SQLite backup or the repository backup helper so the snapshot
is consistent.

After a successful repair, verify all of the following before starting the
gateway:

    HERMES_HOME="$HOME/.hermes" hermes sessions repair --check-only
    sqlite3 "$HOME/.hermes/state.db" \
      "SELECT key, value FROM state_meta WHERE key = 'fts_stale';"
    sqlite3 "$HOME/.hermes/state.db" \
      "SELECT type, name FROM sqlite_master WHERE name IN
       ('messages_fts_insert','messages_fts_update','messages_fts_delete')
       ORDER BY name;"
    sqlite3 "$HOME/.hermes/state.db" \
      "SELECT 'sessions', COUNT(*) FROM sessions
       UNION ALL SELECT 'messages', COUNT(*) FROM messages;"

The marker query should return no row, all expected FTS triggers should be
present, and the canonical row counts must not decrease. Then start the
gateway and perform a real platform message smoke test; a healthy systemd unit
alone is not sufficient.

If in-place repair fails, keep both the live database and the reported backup.
Use the non-destructive recovery flow printed by hermes sessions repair:

    HERMES_HOME="$HOME/.hermes" hermes sessions recover \
      --source /path/to/backup/state.db --inspect-only

Only after inspection confirms the source is readable should recovery write a
new database with --output. Never delete canonical transcript rows merely to
make an FTS error disappear.
