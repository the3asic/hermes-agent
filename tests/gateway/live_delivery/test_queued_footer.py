"""Queued delivery through the built-in log transport and a real temporary session store."""

import asyncio
import logging
import os
from pathlib import Path
import time
from uuid import uuid4

import yaml


def test_real_queued_log_delivery_preserves_reply_receipts(tmp_path):
    home = Path(os.environ["HERMES_HOME"]).resolve()
    assert home != (Path.home() / ".hermes").resolve()
    config_path = home / "config.yaml"
    assert not config_path.exists(), "This test requires a fresh isolated Hermes home"
    home.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump({
        "gateway": {"multiplex_profiles": False},
        "display": {"runtime_footer": {"enabled": True, "fields": ["latency"]}},
    }), encoding="utf-8")

    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.run import GatewayRunner, _preserve_queued_followup_history_offset
    from gateway.session_finite import finite_turn_scope
    from gateway.turn_context import TurnContext

    async def deliver():
        adapter = WebhookAdapter(PlatformConfig(enabled=True))
        runner = GatewayRunner(GatewayConfig(multiplex_profiles=False))
        assert runner._session_db_init_error is None
        runner.adapters[Platform.WEBHOOK] = adapter
        source = adapter.build_source(chat_id="webhook:" + str(uuid4()), chat_type="webhook")
        entry = runner.session_store.get_or_create_session(source)
        ctx = TurnContext(source=source, session_id=entry.session_id, session_key=entry.session_key,
                          history=[], _interrupt_depth=0, context_prompt=None)
        ctx.inbound_message_id = ctx.event_message_id = str(uuid4())
        started = time.monotonic()
        body = (Path(__file__).resolve().parents[3] / "README.md").read_text(encoding="utf-8").splitlines()[0]
        ctx.turn_seconds = time.monotonic() - started
        raw = {"final_response": body, "turn_seconds": ctx.turn_seconds, "history_offset": 0}
        finalized = dict(raw)
        log_path = tmp_path / "webhook-delivery.log"
        log = logging.getLogger(WebhookAdapter.__module__)
        old_level = log.level
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        try:
            with finite_turn_scope(False):
                footer = runner._hmwa_runtime_footer_line(finalized, source, ctx.turn_seconds)
                assert footer
                await runner._run_agent_deliver_first_response(ctx, adapter, finalized, raw, None)
                handler.flush()
                delivered = log_path.read_text(encoding="utf-8")
                assert body in delivered and delivered.count(footer) == 1
                for result in (raw, finalized):
                    assert result["already_sent"] is True
                    assert result["media_already_delivered"] is True
                    assert result["runtime_footer_already_delivered"] is True
                    copied = _preserve_queued_followup_history_offset(
                        {"history_offset": 0}, {**result, "history_offset": 1})
                    assert copied["history_offset"] == 0
                    assert runner._hmwa_runtime_footer_line(copied, source, ctx.turn_seconds) == ""
                fresh = {"final_response": body, "turn_seconds": ctx.turn_seconds}
                assert runner._hmwa_runtime_footer_line(fresh, source, ctx.turn_seconds) == footer
            with finite_turn_scope(True):
                assert runner._hmwa_runtime_footer_line(fresh, source, ctx.turn_seconds) == ""
        finally:
            log.removeHandler(handler)
            log.setLevel(old_level)
            handler.close()
            runner.session_store.close_all_db_handles()
            runner.close_all_session_db_handles()

    asyncio.run(deliver())
