# gta6-clipper

Pipeline de cortes automáticos: monitora os clips que a comunidade cria nas lives, escolhe os melhores momentos, baixa, converte para 9:16 com legenda estilo TikTok e deixa pronto para revisão e postagem.

```
Twitch Helix /clips ─┐
Kick (experimental) ─┴─> ranking (views/min + cluster) ─> SQLite ─> yt-dlp ─> Whisper ─> ffmpeg 9:16 ─> data/output/AAAA-MM-DD/*.mp4 + .json
```

## Como funciona o ranking

- **Velocidade:** views por minuto desde a criação do clip (com piso de 5 min).
- **Cluster:** clips do mesmo VOD com offset a até 30s um do outro são o *mesmo momento*. Só a melhor versão é renderizada e as outras ficam como `skipped`. Cada clip a mais do mesmo momento dá +25% no score, porque 5 pessoas clipando a mesma coisa é sinal forte.
- Um momento que já foi renderizado nunca volta para a fila, mesmo que outra versão passe a ter mais views.

## Setup

1. Crie um app em <https://dev.twitch.tv/console/apps> (tipo *Confidential*, redirect `http://localhost`).
2. Configure o ambiente:
   ```bash
   cp .env.example .env    # preencha TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET
   ```
3. Revise `config/streamers.yaml`.

### Docker (recomendado; serve para o Easypanel também)

```bash
docker compose up -d --build
docker compose logs -f
```

### Local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"     # precisa de ffmpeg no PATH
clipper run --once -v
```

## Comandos

| Comando | O que faz |
|---|---|
| `clipper run` | Loop: descobre, ranqueia e renderiza a cada `CLIPPER_POLL_INTERVAL_SECONDS` |
| `clipper run --once` | Um ciclo só |
| `clipper list --status ready` | Fila pronta para revisão |
| `clipper set-status twitch:<id> posted` | Marca como aprovado, rejeitado ou postado |
| `clipper render <url> --streamer alanzoka` | Renderiza qualquer clip manualmente |
| `clipper frame <url>` | Salva um PNG do clip para calibrar o layout |

No Docker: `docker compose run --rm clipper list --status ready`.

## Calibrando o layout `stack` (facecam em cima)

O default é `blur` (vídeo inteiro sobre fundo desfocado), que funciona com qualquer overlay. Para usar o `stack`, que costuma ter mais retenção:

1. `clipper frame <url-de-um-clip-do-streamer>` → abre o PNG.
2. Meça a facecam em proporção da tela. Por exemplo: começa em 75% da largura e 70% da altura, e ocupa 25% × 30%.
3. No YAML:
   ```yaml
   layout:
     mode: stack
     facecam: { x: 0.75, y: 0.70, w: 0.25, h: 0.30 }
     gameplay: { x: 0.12, y: 0.0, w: 0.76, h: 1.0 }
   ```
4. `clipper render <url> --streamer <login>` para conferir.

## Saída

Para cada clip, `data/output/<data>/<streamer>_<plataforma>_<id>.mp4` e um `.json` com:

- `suggested_caption`: título, créditos (`twitch.tv/<login>`) e hashtags, prontos para colar.
- `transcript`: a fala transcrita, útil para escrever um título melhor.
- `source_url`, `views_at_render` e `score`.

## Requisitos de máquina

- 4 vCPU e 8 GB aguentam o Whisper `small` em CPU (~1x tempo real; um clip de 30s leva cerca de 30–40s).
- Para mais volume: `CLIPPER_WHISPER_MODEL=base` (mais rápido e menos preciso) ou uma máquina com GPU.

## Limitações conhecidas

- **Kick:** a API pública oficial ainda não expõe clips. Por isso o código usa o endpoint interno do site, que fica atrás do Cloudflare e pode dar 403 a partir de IPs de datacenter.
- **Filtro de jogo:** se "Grand Theft Auto VI" ainda não existir no catálogo da Twitch, o filtro é desligado automaticamente com um warning, e os clips de outros jogos entram também.
- **Clips da Twitch** aparecem na API com alguns minutos de atraso. A fase 2 (detecção própria por pico de chat) resolve isso.

## Testes

```bash
pytest -q && mypy src
```

## Próximos passos

- [ ] Dia 2: painel de revisão (aprovar, ajustar início e fim, rejeitar) e fluxo de postagem.
- [ ] Detector próprio: chat (IRC/websocket) com z-score de mensagens/s e buffer da live via streamlink.
- [ ] Upload automático no YouTube (Data API v3) e no TikTok (Content Posting API, após auditoria).
