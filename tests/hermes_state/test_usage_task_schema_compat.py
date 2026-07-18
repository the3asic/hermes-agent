"""Regression tests for v22 task-dimension usage schema compatibility.

A database opened by a newer Hermes build may already have ``task`` in the
``session_model_usage`` primary key.  Older recovery branches must use the same
conflict target or the entire token-accounting transaction rolls back.
"""
import sqlite3

from hermes_state import SessionDB


def _rows(db: SessionDB, session_id: str):
    assert db._conn is not None
    with db._lock:
        rows = db._conn.execute(
            "SELECT * FROM session_model_usage WHERE session_id = ? ORDER BY task",
            (session_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def test_main_loop_usage_writes_empty_task_dimension(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("main", source="cron", model="gpt-5.6-sol")

    db.update_token_counts(
        "main",
        input_tokens=120,
        output_tokens=8,
        cache_read_tokens=100,
        model="gpt-5.6-sol",
        billing_provider="custom",
        billing_base_url="http://127.0.0.1:8317/v1",
        api_call_count=1,
    )

    session = db.get_session("main")
    assert session is not None
    assert session["input_tokens"] == 120
    assert session["output_tokens"] == 8
    assert session["cache_read_tokens"] == 100
    assert session["api_call_count"] == 1
    rows = _rows(db, "main")
    assert len(rows) == 1
    assert rows[0]["task"] == ""
    assert rows[0]["input_tokens"] == 120
    db.close()


def test_auxiliary_and_main_usage_can_share_route(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("mixed", source="cli", model="same-model")
    db.update_token_counts(
        "mixed",
        input_tokens=100,
        model="same-model",
        billing_provider="custom",
        api_call_count=1,
    )
    db.record_auxiliary_usage(
        "mixed",
        "compression",
        model="same-model",
        billing_provider="custom",
        input_tokens=25,
    )

    rows = _rows(db, "mixed")
    assert [row["task"] for row in rows] == ["", "compression"]
    mixed_session = db.get_session("mixed")
    assert mixed_session is not None
    assert mixed_session["input_tokens"] == 100
    db.close()


def test_v21_table_rebuild_preserves_existing_usage(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("legacy", source="cli")
    db.update_token_counts(
        "legacy",
        input_tokens=42,
        model="old-model",
        billing_provider="openrouter",
        api_call_count=1,
    )
    db.close()

    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE smu_old AS SELECT session_id, model, billing_provider,
            billing_base_url, billing_mode, api_call_count, input_tokens,
            output_tokens, cache_read_tokens, cache_write_tokens,
            reasoning_tokens, estimated_cost_usd, actual_cost_usd,
            cost_status, cost_source, first_seen, last_seen
            FROM session_model_usage;
        DROP TABLE session_model_usage;
        CREATE TABLE session_model_usage (
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            billing_provider TEXT NOT NULL DEFAULT '',
            billing_base_url TEXT NOT NULL DEFAULT '',
            billing_mode TEXT NOT NULL DEFAULT '',
            api_call_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0,
            actual_cost_usd REAL NOT NULL DEFAULT 0,
            cost_status TEXT,
            cost_source TEXT,
            first_seen REAL,
            last_seen REAL,
            PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode)
        );
        INSERT INTO session_model_usage SELECT * FROM smu_old;
        DROP TABLE smu_old;
        UPDATE schema_version SET version = 21;
    """)
    conn.commit()
    conn.close()

    migrated = SessionDB(path)
    assert migrated._conn is not None
    with migrated._lock:
        pk_columns = [
            row[1]
            for row in migrated._conn.execute(
                "SELECT * FROM pragma_table_info('session_model_usage') WHERE pk > 0"
            ).fetchall()
        ]
        row = migrated._conn.execute(
            "SELECT task, input_tokens FROM session_model_usage WHERE session_id = 'legacy'"
        ).fetchone()
    assert "task" in pk_columns
    assert tuple(row) == ("", 42)
    migrated.update_token_counts(
        "legacy",
        input_tokens=1,
        model="old-model",
        billing_provider="openrouter",
        api_call_count=1,
    )
    legacy_session = migrated.get_session("legacy")
    assert legacy_session is not None
    assert legacy_session["input_tokens"] == 43
    migrated.close()
