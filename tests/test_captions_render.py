from __future__ import annotations

import pytest

from clipper.config import BlurLayout, Rect, StackLayout
from clipper.media.captions import KARAOKE, CaptionStyle, build_ass, chunk_words
from clipper.media.render import HookOverlay, build_filtergraph, hook_center_y
from clipper.media.transcribe import Word


def _words(*spec: tuple[str, float, float]) -> list[Word]:
    return [Word(t, s, e) for t, s, e in spec]


def test_chunking_respects_max_words_and_gaps() -> None:
    words = _words(("a", 0, 0.2), ("b", 0.2, 0.4), ("c", 0.4, 0.6), ("d", 0.6, 0.8), ("e", 2.0, 2.2))
    chunks = chunk_words(words, CaptionStyle(max_words=3))
    assert [[w.text for w in c] for c in chunks] == [["a", "b", "c"], ["d"], ["e"]]


def test_karaoke_highlights_one_word_per_event_and_escapes() -> None:
    words = _words(("olha{isso}", 0.0, 0.3), ("mano", 0.3, 0.6))
    ass = build_ass(words, KARAOKE)
    events = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert len(events) == 2
    assert "{isso}" not in ass and "OLHA(ISSO)" in ass
    assert events[0].count("\\c&H0000E5FF") == 1
    assert events[0].startswith("Dialogue: 0,0:00:00.00,0:00:00.30")


def test_last_word_hold_never_overlaps_next_chunk() -> None:
    words = _words(("a", 0, 0.2), ("b", 0.5, 0.7))
    ass = build_ass(words, CaptionStyle(max_words=1, hold_s=1.0))
    first = next(ln for ln in ass.splitlines() if ln.startswith("Dialogue:"))
    assert ",0:00:00.00,0:00:00.50," in first


def test_empty_transcript_produces_valid_header() -> None:
    assert "[Events]" in build_ass([])


@pytest.mark.parametrize("captions", [True, False])
def test_filtergraph_blur(captions: bool) -> None:
    g = build_filtergraph(BlurLayout(), captions)
    assert g.endswith("[v]") and "gblur" in g and ("ass=" in g) is captions


def test_filtergraph_stack_heights_sum_to_1920() -> None:
    layout = StackLayout(mode="stack", facecam=Rect(x=0.75, y=0.7, w=0.25, h=0.3), facecam_ratio=0.33)
    g = build_filtergraph(layout, True)
    assert "crop=1080:634" in g and "crop=1080:1286" in g and "vstack" in g


def test_default_style_is_one_plain_word_in_natural_case() -> None:
    words = _words(("Eu", 0.0, 0.3), ("tenho", 0.3, 0.6), ("carro", 0.6, 0.9))
    events = [ln for ln in build_ass(words).splitlines() if ln.startswith("Dialogue:")]
    assert [e.rsplit(",", 1)[1] for e in events] == ["Eu", "tenho", "carro"]
    assert "&H0000E5FF" not in "".join(events)


def test_filtergraph_overlays_hook_after_captions_with_fade() -> None:
    g = build_filtergraph(BlurLayout(), True, hook=HookOverlay(top_y=500), with_fontsdir=True)
    assert "ass=captions.ass:fontsdir=fonts[capt]" in g
    assert "[1:v]format=rgba,fade=t=out:st=3.60:d=0.40:alpha=1[hk]" in g
    assert "[capt][hk]overlay=x=(W-w)/2:y=500:eof_action=pass" in g
    assert g.endswith("[hooked]null[v]")


def test_hook_is_centered_on_the_facecam_seam() -> None:
    stack = StackLayout(mode="stack", facecam=Rect(x=0, y=0, w=0.3, h=0.3), facecam_ratio=0.3)
    assert hook_center_y(stack) == 576
    assert hook_center_y(BlurLayout()) == (1920 - 607) // 2
