from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from PIL import Image

from clipper.hook import ClaudeHookWriter, normalize_hook
from clipper.media.fonts import resolve_font
from clipper.media.hook_card import render_hook_card
from tests.conftest import make_clip


class FakeMessages:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _writer(result: Any) -> tuple[ClaudeHookWriter, FakeMessages]:
    messages = FakeMessages(result)
    client: Any = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return ClaudeHookWriter(client=client), messages


def _response(text: str, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


def test_normalize_hook() -> None:
    assert normalize_hook('  "30 mil no porta-malas,\nfuga a pé?" ') == "30 MIL NO PORTA-MALAS, FUGA A PÉ?"
    assert normalize_hook("   ") is None
    assert normalize_hook("x" * 61) is None


def test_writer_sends_transcript_and_parses_json() -> None:
    writer, messages = _writer(_response(json.dumps({"hook": "fuga a pé com 30 mil?"})))
    assert writer.write(make_clip(), "eu tenho 30 mil no porta-malas") == "FUGA A PÉ COM 30 MIL?"
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert "30 mil no porta-malas" in call["messages"][0]["content"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default"


@pytest.mark.parametrize(
    "result",
    [
        _response("", stop_reason="refusal"),
        _response("not json"),
        _response(json.dumps({"other": 1})),
        anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")),
    ],
)
def test_writer_degrades_to_none(result: Any) -> None:
    writer, _ = _writer(result)
    assert writer.write(make_clip(), "fala qualquer") is None


def test_writer_skips_call_without_any_text() -> None:
    writer, messages = _writer(_response("{}"))
    assert writer.write(dataclasses.replace(make_clip(), title=""), "  ") is None
    assert messages.calls == []


def test_hook_card_wraps_and_fits(tmp_path: Path) -> None:
    font = resolve_font(tmp_path / "no-bundled-fonts")
    png = render_hook_card("30 MIL NO PORTA-MALAS, FUGA A PÉ?", font.path, tmp_path / "hook.png")
    with Image.open(png) as img:
        assert img.mode == "RGBA"
        assert 620 <= img.width <= 860
        assert img.getpixel((img.width // 2, 2)) == (255, 255, 255, 255)  # box fill
        corner = img.getpixel((0, 0))
        assert isinstance(corner, tuple) and corner[3] == 0  # rounded corner is transparent


def test_writer_disables_itself_without_credentials() -> None:
    writer, messages = _writer(TypeError("Could not resolve authentication method. Expected ..."))
    assert writer.write(make_clip(), "fala") is None
    assert writer.write(make_clip(), "fala") is None
    assert len(messages.calls) == 1  # no retry storm on every clip
