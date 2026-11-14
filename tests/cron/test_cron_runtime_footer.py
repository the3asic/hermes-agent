"""Cron footer rendering preserves scheduler control responses and unknown usage."""
from __future__ import annotations

import pytest

from cron.scheduler import _cron_failure_marker_error, _is_cron_silence_response
from cron.scheduler_footer import append_runtime_footer


@pytest.mark.parametrize('response', ['', '[SILENT]', 'Details\n[SILENT]', '[CRON_FAILURE]\nbroken'])
def test_control_responses_keep_their_exact_delivery_contract(response):
    config = {'display': {'platforms': {'cron': {'runtime_footer': {
        'enabled': True, 'fields': ['latency'],
    }}}}}
    rendered = append_runtime_footer(response, None, {}, config, seconds=2)
    assert rendered == response
    if response.startswith('[CRON_FAILURE]'):
        assert _cron_failure_marker_error(rendered) == 'broken'
    elif response:
        assert _is_cron_silence_response(rendered)


def test_absent_runtime_keeps_unknown_token_metrics_out_of_the_report():
    config = {'display': {'platforms': {'cron': {'runtime_footer': {
        'enabled': True,
        'fields': ['model_last', 'tokens_turn', 'cache_hit', 'context_window', 'reasoning_effort'],
    }}}}}
    assert append_runtime_footer('Report', None, {}, config, seconds=2) == 'Report'


def test_report_preserves_the_measured_duration_without_token_telemetry():
    config = {'display': {'platforms': {'cron': {'runtime_footer': {
        'enabled': True, 'fields': ['latency'],
    }}}}}
    assert append_runtime_footer('Report', None, {}, config, seconds=2) == 'Report\n\n2s'


def test_disabled_footer_preserves_the_complete_report():
    assert append_runtime_footer('Report', None, {}, {}, seconds=2) == 'Report'
