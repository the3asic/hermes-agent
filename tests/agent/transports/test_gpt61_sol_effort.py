"""GPT-6.1 Sol must keep max effort and reject unsupported disable levels."""

import pytest

from agent.transports import get_transport


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "openai/gpt-6.1-sol-pro"])
@pytest.mark.parametrize("requested,expected", [("max", "max"), ("ultra", "max"), ("none", "low")])
def test_sol_effort_reaches_responses_wire(model, requested, expected):
    import agent.transports.codex  # noqa: F401

    transport = get_transport("codex_responses")
    kwargs = transport.build_kwargs(
        model=model,
        messages=[{"role": "user", "content": "Hi"}],
        tools=[],
        base_url="http://127.0.0.1:8317/v1",
        reasoning_config={"enabled": True, "effort": requested},
    )
    assert kwargs["reasoning"]["effort"] == expected
