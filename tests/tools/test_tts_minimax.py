"""Tests for the MiniMax TTS provider in tools/tts_tool.py."""

import os

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (
        "MINIMAX_API_KEY",
        "MINIMAX_CN_API_KEY",
        "MINIMAX_CN_BASE_URL",
        "MINIMAX_GROUP_ID",
        "HERMES_SESSION_PLATFORM",
    ):
        monkeypatch.delenv(key, raising=False)
    # Keep tests independent from keys stored in the live ~/.hermes/.env.
    monkeypatch.setattr(
        "tools.tts_tool.get_env_value",
        lambda name: os.environ.get(name),
    )


class TestGenerateMiniMaxTts:
    def test_missing_api_keys_raises_value_error(self, tmp_path):
        from tools.tts_tool import _generate_minimax_tts

        with pytest.raises(ValueError, match="MINIMAX_API_KEY / MINIMAX_CN_API_KEY"):
            _generate_minimax_tts("Hello", str(tmp_path / "test.mp3"), {})

    def test_cn_env_key_and_base_url_are_supported(self, tmp_path, monkeypatch):
        from tools.tts_tool import _generate_minimax_tts

        monkeypatch.setenv("MINIMAX_CN_API_KEY", "cn-key")
        monkeypatch.setenv("MINIMAX_CN_BASE_URL", "https://api.minimaxi.com/v1")

        fake_response = MagicMock()
        fake_response.json.return_value = {
            "base_resp": {"status_code": 0},
            "data": {"audio": "0001"},
        }

        output_path = str(tmp_path / "test.mp3")
        config = {"minimax": {"voice_id": "female-yujie"}}
        with patch("requests.post", return_value=fake_response) as mock_post:
            result = _generate_minimax_tts("你好", output_path, config)

        assert result == output_path
        assert (tmp_path / "test.mp3").read_bytes() == b"\x00\x01"
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args.args[0] == "https://api.minimaxi.com/v1/t2a_v2"
        assert call_args.kwargs["headers"]["Authorization"] == "Bearer cn-key"
        assert call_args.kwargs["json"]["voice_setting"]["voice_id"] == "female-yujie"

    def test_config_base_url_wins_and_keeps_explicit_endpoint(self, tmp_path, monkeypatch):
        from tools.tts_tool import _generate_minimax_tts

        monkeypatch.setenv("MINIMAX_CN_API_KEY", "cn-key")
        monkeypatch.setenv("MINIMAX_CN_BASE_URL", "https://api.minimaxi.com/v1")

        fake_response = MagicMock()
        fake_response.json.return_value = {
            "base_resp": {"status_code": 0},
            "data": {"audio": "0001"},
        }

        config = {
            "minimax": {
                "base_url": "https://custom.minimax.example/v1/t2a_v2",
                "voice_id": "female-yujie",
            }
        }
        with patch("requests.post", return_value=fake_response) as mock_post:
            _generate_minimax_tts("你好", str(tmp_path / "test.mp3"), config)

        assert mock_post.call_args.args[0] == "https://custom.minimax.example/v1/t2a_v2"


class TestTtsDispatcherMiniMax:
    def test_dispatcher_routes_to_minimax_with_cn_key(self, tmp_path, monkeypatch):
        import json

        from tools.tts_tool import text_to_speech_tool

        monkeypatch.setenv("MINIMAX_CN_API_KEY", "cn-key")
        monkeypatch.setenv("MINIMAX_CN_BASE_URL", "https://api.minimaxi.com/v1")

        fake_response = MagicMock()
        fake_response.json.return_value = {
            "base_resp": {"status_code": 0},
            "data": {"audio": "0001"},
        }

        with patch("requests.post", return_value=fake_response), patch(
            "tools.tts_tool._load_tts_config",
            return_value={
                "provider": "minimax",
                "minimax": {
                    "voice_id": "female-yujie",
                    "base_url": "https://api.minimaxi.com/v1/t2a_v2",
                },
            },
        ):
            result = json.loads(
                text_to_speech_tool("你好", output_path=str(tmp_path / "out.mp3"))
            )

        assert result["success"] is True
        assert result["provider"] == "minimax"


class TestCheckTtsRequirementsMiniMax:
    def test_cn_key_counts_as_available_provider(self, monkeypatch):
        from tools import tts_tool

        monkeypatch.setenv("MINIMAX_CN_API_KEY", "cn-key")
        monkeypatch.setattr(
            tts_tool,
            "_load_tts_config",
            lambda: {"provider": "minimax", "minimax": {}},
        )

        assert tts_tool.check_tts_requirements() is True
