"""Hook title generation: the short uppercase question/statement shown in the first seconds.

The hook is the main "transformation" of a re-upload (YouTube reused-content policy) and
the biggest retention lever in the first 1-2s, so it is written by an LLM from the transcript.
Any failure degrades to "no hook" — a clip must never be lost because of the hook.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import anthropic

from clipper.models import Clip

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
MAX_HOOK_CHARS = 60
MAX_TRANSCRIPT_CHARS = 4000  # ~60s of speech; clips are short, this is just a guard

SYSTEM_PROMPT = """\
Você escreve o título-gancho de cortes verticais (TikTok/Shorts) de lives de GTA em português do Brasil.
O gancho aparece numa caixa branca nos primeiros 4 segundos e precisa fazer a pessoa parar de rolar.

Regras:
- Até 8 palavras e até 45 caracteres.
- Concreto: cite o fato mais inusitado do trecho (números, objetos, situações), não adjetivos vagos.
- Pode ser pergunta ("30 MIL NO PORTA-MALAS, FUGA A PÉ?") ou afirmação curta.
- Linguagem de rua/gamer brasileira, sem hashtags, sem emoji, sem aspas, sem nome do streamer.
- Não invente o que não está na transcrição; se ela for fraca, use o título do clip como pista.
- Nada ofensivo, nada que acuse pessoas reais de crimes reais.
"""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"hook": {"type": "string"}},
    "required": ["hook"],
    "additionalProperties": False,
}


class HookWriter(Protocol):
    def write(self, clip: Clip, transcript: str) -> str | None: ...


def normalize_hook(raw: str) -> str | None:
    """Uppercases and validates; returns None when the text is unusable."""
    text = " ".join(raw.replace("\n", " ").split()).strip().strip("\"'“”")
    if not text:
        return None
    if len(text) > MAX_HOOK_CHARS:
        log.warning("hook too long (%d chars), dropping: %r", len(text), text)
        return None
    return text.upper()


class ClaudeHookWriter:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self._model = model
        # Without an explicit key the SDK resolves ANTHROPIC_API_KEY / `ant auth login` profile.
        self._client = client or anthropic.Anthropic(api_key=api_key or None, timeout=60.0)
        self._disabled = False

    def write(self, clip: Clip, transcript: str) -> str | None:
        if self._disabled:
            return None
        transcript = transcript.strip()[:MAX_TRANSCRIPT_CHARS]
        if not transcript and not clip.title.strip():
            return None
        user = (
            f"Streamer: {clip.broadcaster_name}\n"
            f"Título do clip na Twitch: {clip.title.strip() or '(sem título)'}\n"
            f"Transcrição:\n{transcript or '(sem fala)'}"
        )
        try:
            response = self._client.beta.messages.create(
                model=self._model,
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                },
                # Re-runs on a fallback model if a safety classifier declines (game violence
                # transcripts can trip it); routing is chosen by the API per refusal category.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError:
            log.error("hook: invalid Anthropic credentials; hooks disabled for this run")
            self._disabled = True
            return None
        except TypeError as exc:
            # The SDK raises TypeError when no credential source resolves at all.
            if "authentication" not in str(exc).lower():
                raise
            log.error("hook: no Anthropic credentials (set ANTHROPIC_API_KEY); hooks disabled")
            self._disabled = True
            return None
        except anthropic.RateLimitError:
            log.warning("hook: rate limited (after SDK retries); skipping hook")
            return None
        except anthropic.APIStatusError as exc:
            log.warning("hook: API error %s: %s", exc.status_code, exc.message)
            return None
        except anthropic.APIConnectionError as exc:
            log.warning("hook: connection error: %s", exc)
            return None

        if response.stop_reason == "refusal":
            log.warning("hook: model refused for %s", clip.key)
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            hook = str(json.loads(text)["hook"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("hook: unparseable output %r: %s", text[:200], exc)
            return None
        return normalize_hook(hook)
