# CLAUDE.md — generator-clipper

Contexto técnico do projeto para o Claude Code. Leia antes de qualquer alteração.
Contexto privado (dono, negócio, ambiente local) fica em `CLAUDE.local.md`, fora do git.

## Convenções

- **Respostas em português.** Código, comentários, nomes e mensagens de commit em inglês.
- Código completo e funcional, com tipagem estrita (`mypy --strict src tests` limpo).
- Sempre tratar erros e escrever testes (pytest; HTTP mockado com respx).
- Quando houver mais de uma abordagem válida, citar os trade-offs rapidamente antes de escolher.
- Nunca commitar `.env`, mídia (`*.mp4`), `data/`, `Clips/` nem `CLAUDE.local.md`.

## Arquitetura

Usa os **clips que a comunidade cria na Twitch e na Kick** como detector de momentos bons, em vez de detectar picos por conta própria.

```
Twitch Helix /clips ─┐
Kick (interno) ─────┴─> ranking (views/min + cluster) ─> SQLite ─> yt-dlp ─> faster-whisper ─> ffmpeg 9:16 ─> data/output/AAAA-MM-DD/*.mp4 + .json
```

Stack: **Python 3.12**, httpx, pydantic v2 + pydantic-settings, PyYAML, yt-dlp, faster-whisper (CPU int8), ffmpeg (libass), SQLite (WAL). Roda em Docker (Easypanel).

Escolha da linguagem: Python de ponta a ponta porque o ecossistema necessário (yt-dlp, whisper, streamlink) já é Python. Go ou .NET só fariam sentido para a orquestração, e mesmo assim chamariam esses CLIs.

### Módulos (`src/clipper/`)

| Arquivo | Responsabilidade |
|---|---|
| `config.py` | `Settings` (env, prefixo `CLIPPER_`) e catálogo `streamers.yaml`. Os layouts `BlurLayout` / `StackLayout` usam `Rect` normalizado (0..1) |
| `models.py` | `Clip` (dataclass frozen) e `ClipStatus` (discovered, rendering, ready, failed, skipped, approved, rejected, posted) |
| `sources/twitch.py` | Helix com app token (client credentials), renovação no 401, backoff no 429/5xx, paginação, resolução de user_id e de game_id. Se o jogo não existir no catálogo, o filtro de jogo é desligado com um warning |
| `sources/kick.py` | Endpoint interno `kick.com/api/v2/channels/{slug}/clips?sort=view&time=day` (validado em 18/11). Paginação por `cursor`, filtro de categoria aceita o sufixo " (GTA)" da Kick. Cloudflare: funciona de IP residencial, pode dar 403 de datacenter. Sem offset de VOD, então não agrupa |
| `ranking.py` | Velocidade = views/min, com piso de 5 min. Clips do mesmo VOD com offset a até 30s formam um *cluster* (o mesmo momento). Score = soma das velocidades × (1 + 0.25 × duplicatas) |
| `store.py` | Fila em SQLite. `upsert_discovered` não reseta o status (só promove `skipped` para `discovered`). `claim_next` é atômico e ignora clips mais antigos que o lookback. Máximo de 3 tentativas. `recover_stuck` recupera clips presos em `rendering` |
| `pipeline.py` | `discover()`: se qualquer clip do cluster já foi tratado, marca o cluster inteiro como skipped, para nunca re-renderizar um momento. `process_queue()`: renderiza o top N. Se a transcrição falhar, renderiza sem legenda em vez de perder o clip |
| `media/download.py` | yt-dlp, idempotente (reaproveita um download que já existe) |
| `media/transcribe.py` | faster-whisper com lazy load, `word_timestamps`, VAD e `condition_on_previous_text=False` |
| `media/captions.py` | ASS no padrão do canal: **uma palavra por vez**, branca, grafia original, sombra suave, a ~80% da altura. `KARAOKE` mantém o estilo antigo (3 palavras, caixa alta, amarelo) |
| `media/render.py` | Filtergraph ffmpeg. `blur` = vídeo sobre fundo desfocado. `stack` = facecam em cima e gameplay embaixo, alturas somando 1920. O ffmpeg roda com `cwd=work_dir` para referenciar `captions.ass` sem escaping. A escrita final é atômica (`.tmp.mp4` e depois `replace`) |
| `hook.py` | Título-gancho via Claude API (`claude-opus-5-5`, effort low, JSON schema, `fallbacks: "default"`). Qualquer falha vira "sem gancho"; sem credencial, desliga no primeiro clip |
| `media/hook_card.py` | PNG do gancho com Pillow: caixa branca arredondada, texto preto bold, até 3 linhas (reduz a fonte se precisar) |
| `media/fonts.py` | Resolve a fonte bold: `assets/fonts/*.ttf` > DejaVu Sans Bold (Docker) > Arial Bold (Windows). Copiada para `work_dir/fonts` e passada ao libass via `fontsdir` |
| `media/facecam.py` | Acha a facecam por clip: amostra 6 frames (OpenCV), roda o detector Haar em cada posição candidata (`facecam` + `facecam_alternates`) e escolhe a que tem rosto em ≥2/6. Sem rosto, o clip sai em `blur`. OpenCV fixado em 4.x (o 5.x tirou os Haar embutidos) |
| `media/audio.py` | Decodifica o áudio com ffmpeg (16 kHz mono) para o Whisper, sem PyAV |
| `panel/` | Painel de revisão: FastAPI (`app.py`) + SPA estática sem build (`static/`). Lista/filtra clips, player 9:16, aprovar/rejeitar/postado (atalhos A/X/P/U), copiar legenda, re-render em background (`jobs.py`, 1 por vez) com gancho novo e corte início/fim. Cada request abre o próprio `Store` |
| `panel/connections.py` | Tela Conexões: `EnvStore` (edita o `.env` preservando comentários, escrita atômica), `ConnectionTester` (Twitch token, Kick canal, Anthropic `models.list`), `CatalogEditor` (ruamel round-trip no `streamers.yaml`, valida pelo schema antes de gravar). Segredos nunca voltam ao navegador (só `••••1234`). O app tem middleware: `Host` local + escrita só com `X-Clipper-Panel: 1` e mesma origem |
| `panel` calibração | Editor visual (`/api/streamers/{p}/{login}/clips·frame·layout·detect`): frame real via `extract_frame` (baixa o clip se preciso), retângulos arrastáveis, prévia 9:16 em canvas com a mesma matemática do `_fill` do ffmpeg, `score_candidates` para testar a detecção, `CatalogEditor.set_layout` grava o layout in-place |
| `cli.py` | `run [--once]`, `panel [--host --port]`, `render <url> [--streamer] [--hook]`, `frame <url>`, `list [--status]`, `set-status <key> <status>`. Tem shutdown gracioso com SIGINT/SIGTERM. O `run` recarrega `.env`/`streamers.yaml` quando o mtime muda (editados pelo painel) |

### Config

- `config/streamers.yaml`: catálogo por plataforma. Hoje: Coringa, Gaules e Alanzoka na Twitch; Davy Jones, Coringa e Gaules na Kick (Davy Jones na Twitch desligado).
  - `min_views` por streamer sobrescreve `CLIPPER_MIN_VIEWS` (clips da Kick têm ~10x menos views).
  - `layout.facecam` + `facecam_alternates`: posições candidatas da câmera; a detecção escolhe por clip.
  - Calibrados: Coringa (Twitch 17/11; Kick 19/11, 3 posições à esquerda no meio da altura), Alanzoka (17/11), Davy Jones (Kick 18/11, cantos superiores esquerdo/direito). Gaules foi calibrado num clip de CS; conferir no GTA. Gaules na Kick segue em `blur`.
- `.env.example`: todas as variáveis `CLIPPER_*`, `TWITCH_CLIENT_ID/SECRET` e `ANTHROPIC_API_KEY`.

### Padrão visual do canal (referência: "Fuga da Polícia com 30K")

- `stack`: facecam em cima (30% da altura), gameplay embaixo (70%), cortado no centro, sem blur.
- Gancho: caixa branca arredondada com texto preto bold em caixa alta, centralizada na emenda facecam/gameplay. Fica 4s e some com fade de 0,4s.
- Legenda: uma palavra por vez, branca, sem destaque, a ~80% da altura.
- `clipper render <url> --hook "TEXTO"` sobrescreve o gancho da IA.

### Saída

`$CLIPPER_OUTPUT_DIR/<data>/<login>_<plataforma>_<id>.mp4` (padrão `data/output`; `Clips/` é ignorada no git) e um `.json` com `suggested_caption` (título, créditos e hashtags), `transcript`, `score` e `source_url`.

## Estado da verificação

- ✅ 64 testes (`pytest -q`): ranking, fila (rodízio, limite de duração), captions, filtergraph, config, Twitch e Kick com respx, gancho (Claude fake, card PNG), facecam, pipeline, painel (API, Range, re-render) e áudio.
- ✅ `mypy --strict src tests` limpo.
- ✅ **Testado com a Twitch real (17/11/2026, Windows 11):** token, resolução de logins e `/clips` funcionando. O `render <url>` busca título, views e created_at reais via Helix.
- ✅ **yt-dlp real:** download de clips da Twitch ok.
- ✅ **Whisper real:** `small` em CPU int8, transcrição PT-BR boa. ~75s por clip de 30s, incluindo o load do modelo.
- ✅ **Render real:** 1080x1920@60, legenda libass ok (ffmpeg 9.0.2 gyan.dev no Windows).
- ⚠️ O áudio é decodificado pelo ffmpeg (`media/audio.py`), não pelo PyAV: o PyAV 19 removeu `metadata_errors`, que o faster-whisper 1.2.1 ainda passa.
- ⚠️ O `view_count` dos clips na Helix sobe devagar (clips de 1–3h tinham 1–3 views). Com `CLIPPER_MIN_VIEWS=30`, nada entra na fila fora de pico. Calibrar no lançamento; o tamanho do cluster talvez seja um sinal melhor que views/min.
- ⚠️ O Whisper às vezes repete frases em áudio barulhento ("tá chato" ×7). Avaliar `no_repeat_ngram_size` / `repetition_penalty`.
- ✅ Gancho + layout `stack` renderizados de verdade (Coringa e Alanzoka) com `--hook` manual.
- ❌ **Gancho por IA ainda não testado de verdade:** falta `ANTHROPIC_API_KEY` no `.env`.
- ✅ **Kick real (18/11):** endpoint interno responde de IP residencial; ordenado por views (`sort=view`), paginação por `cursor`, categoria "Grand Theft Auto VI (GTA)". yt-dlp baixa `kick.com/<canal>/clips/<id>`. Davy Jones transmite GTA VI pela Kick.
- ✅ Detecção da facecam validada em clips reais: Davy Jones (51 com câmera, 2 cenas de chat → blur) e Coringa na Kick (7/8; 1 cena IRL → blur). Frontal + perfil; entre posições sobrepostas vence a que enquadra melhor o rosto.
- ✅ Fila em rodízio por streamer (`CLIPPER_MAX_RENDERS_PER_STREAMER`, padrão 2).
- ✅ Painel testado no navegador (desktop e mobile): aprovar/desfazer, re-render real com gancho e corte.

**Atenção:** o `-v` vem antes do subcomando: `clipper -v run --once`.

## Comandos

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                             # requer ffmpeg no PATH
pytest -q && mypy src tests
clipper -v run --once
clipper panel                                       # painel em http://127.0.0.1:8000
docker compose up -d --build
```

## Roadmap

- [x] Painel de revisão (`clipper panel`, http://127.0.0.1:8000). Postagem ainda manual a partir do painel.
- [x] Kick validada com a API real.
- [ ] **Fase 2: detector próprio.**
  - Gravar a live num buffer rotativo (`streamlink` + `ffmpeg -f segment`).
  - Monitorar o chat via IRC da Twitch e websocket da Kick.
  - Calcular o z-score de mensagens/s, somar palavras-chave (KEKW, kkkk, "clipa") e o pico de volume do áudio.
  - Cortar do buffer. Isso elimina o atraso dos clips da comunidade.
- [ ] Upload automático no YouTube (Data API v3; cota padrão de 10k/dia, ~6 uploads).
- [ ] TikTok via Content Posting API. Sem auditoria do app, os posts sobem como privados, então é preciso submeter o app.
- [ ] Hook nos primeiros 1–2s (texto de título no topo) e A/B de títulos.
- [ ] Limpeza de storage (apagar downloads com mais de 7 dias). Talvez S3 com lifecycle.
