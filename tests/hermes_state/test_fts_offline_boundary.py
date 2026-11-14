"""Native SQLite corruption stays detached until explicitly offline recovery."""

import sqlite3

from hermes_state import SessionDB
from hermes_state_common import FTS_STALE_KEY, _FTS_TRIGGERS
from hermes_state_registry import acquire, release
from tests.hermes_state.test_fts_runtime_rebuild import _corrupt_fts


def _assert_detached(db):
    assert db._fts_stale and not db._fts_enabled
    assert db.get_meta(FTS_STALE_KEY) == "1"
    placeholders = ",".join("?" for _ in _FTS_TRIGGERS)
    assert not db._conn.execute(
        f"SELECT name FROM sqlite_master WHERE type='trigger' AND name IN ({placeholders})", _FTS_TRIGGERS,
    ).fetchall()


def test_live_open_write_search_and_housekeeping_cannot_rebuild(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("offline-boundary", source="cli")
    db.append_message("offline-boundary", "user", "Ordinary open and live writes must defer full FTS recovery.")
    _corrupt_fts(path)
    db.append_message("offline-boundary", "assistant", "Canonical messages remain writable after FTS detaches.")
    _assert_detached(db)
    assert db.search_messages("Canonical")
    assert db.rebuild_fts() == 0
    assert db.retry_deferred_fts_recovery() is False
    db.close()
    shared = acquire(path)
    try:
        _assert_detached(shared)
        from gateway.run import _housekeeping_deferred_fts_retry
        _housekeeping_deferred_fts_retry()
        _assert_detached(shared)
        shared.append_message("offline-boundary", "user", "A later ordinary open keeps the stale breadcrumb.")
        assert len(shared.get_messages("offline-boundary")) == 3
        assert shared.search_messages("breadcrumb")
    finally:
        release(shared)
    from hermes_state_repair import repair_state_db_schema
    report = repair_state_db_schema(path)
    assert report["repaired"] is True
    assert report["strategy"] == "offline_fts_rebuild"
    offline = SessionDB(db_path=path)
    try:
        assert offline._fts_enabled and not offline._fts_stale
        assert offline.get_meta(FTS_STALE_KEY) is None
        assert len(offline.get_messages("offline-boundary")) == 3
        assert offline.search_messages("breadcrumb")
        offline._conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')")
    finally:
        offline.close()


def test_missing_trigger_on_nonempty_store_defers_until_offline(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("trigger-repair", source="cli")
    db.append_message("trigger-repair", "user", "Repair must not publish synchronization before recovery authority.")
    db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER messages_fts_insert")
    live = SessionDB(db_path=path)
    try:
        _assert_detached(live)
        live.append_message("trigger-repair", "assistant", "This canonical row is written while indexes are detached.")
    finally:
        live.close()
    offline = SessionDB(db_path=path, allow_fts_rebuild=True)
    try:
        assert offline._fts_enabled and not offline._fts_stale
        assert len(offline.get_messages("trigger-repair")) == 2
        assert offline.search_messages("canonical")
    finally:
        offline.close()
