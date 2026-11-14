"""Real stored web selections keep rescue decisions within their capability."""

import json

import pytest

from hermes_constants import get_hermes_home
from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider, _use_keyless_ring
from tools.web_tools_rescue import _rescue_eligible


@pytest.mark.parametrize("managed_capability", ["search", "extract"])
def test_mixed_direct_and_managed_firecrawl_rescue_is_capability_scoped(managed_capability):
    direct_capability = "extract" if managed_capability == "search" else "search"
    config = {
        "web": {
            f"{managed_capability}_backend": "nous",
            f"{direct_capability}_backend": "firecrawl",
            "keyless_rescue": True,
            "provider_tier": {"firecrawl": "free"},
        },
    }
    (get_hermes_home() / "config.yaml").write_text(json.dumps(config))
    provider = FirecrawlWebSearchProvider()
    assert _use_keyless_ring(managed_capability) is False
    assert _use_keyless_ring(direct_capability) is True
    assert _rescue_eligible(provider, managed_capability) is True
    assert _rescue_eligible(provider, direct_capability) is False
