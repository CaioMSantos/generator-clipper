<div align="center">

# 🎬 generator-clipper

**Cortes verticais automáticos de lives — da Twitch e da Kick direto para o formato TikTok / Shorts.**

Monitora os clips que a comunidade cria, escolhe os melhores momentos, enquadra a facecam sozinho,
legenda palavra por palavra, escreve um título-gancho com IA e entrega tudo num painel de revisão.

<br/>

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-panel-009688?logo=fastapi&logoColor=white)
![ffmpeg](https://img.shields.io/badge/ffmpeg-libass-007808?logo=ffmpeg&logoColor=white)
![Whisper](https://img.shields.io/badge/faster--whisper-CPU%20int8-412991)
![OpenCV](https://img.shields.io/badge/OpenCV-facecam-5C3EE8?logo=opencv&logoColor=white)
![mypy](https://img.shields.io/badge/mypy-strict-2A6DB2)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)

</div>

---

## ✨ O que ele faz

| | |
|---|---|
| 🔎 **Descobre** | Lê os clips da **Twitch** (Helix API) e da **Kick** a cada 2 min, filtrados pelo jogo |
| 🏆 **Ranqueia** | Views por minuto + *clusters*: 5 pessoas clipando o mesmo lance = momento forte |
| 🎯 **Enquadra** | Detecta a **facecam por clip** (OpenCV), mesmo quando o streamer muda a câmera de lugar |
| 📐 **Reformata** | 1080×1920: facecam em cima (30%), gameplay embaixo (70%) — ou fundo desfocado como fallback |
| 💬 **Legenda** | Transcrição local com **faster-whisper** e legenda queimada **uma palavra por vez** |
| 🪝 **Gancho** | Título curto gerado por **Claude** numa caixa branca nos primeiros 4s (opcional) |
| 🖥️ **Revisa** | **Painel web** com player, aprovar/rejeitar por atalho, copiar legenda e re-render com novo corte |

```
                    ┌──────────────┐
 Twitch Helix ───┐  │   ranking    │   ┌────────┐   ┌────────┐   ┌──────────┐   ┌──────────────┐
                 ├─▶│ views/min +  │──▶│ SQLite │──▶│ yt-dlp │──▶│ Whisper  │──▶│ ffmpeg 9:16  │──▶ Clips/AAAA-MM-DD/*.mp4 + .json
 Kick (interno)──┘  │   clusters   │   │  fila  │   └────────┘   └──────────┘   │ facecam·gancho│
                    └──────────────┘   └───┬────┘                               └──────────────┘
                                           │
                                           └──────────▶  painel de revisão (FastAPI)  ◀── você
```

---

## 🚀 Começando

### Pré-requisitos

- **Python 3.12** e **ffmpeg** no `PATH` (com libass — os builds do [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) no Windows já vêm com ele)
  ```bash
  winget install --id Gyan.FFmpeg -e      # Windows
  sudo apt install ffmpeg                  # Debian/Ubuntu
  brew install ffmpeg                      # macOS
  ```
- Um app na **Twitch** em <https://dev.twitch.tv/console/apps> (tipo *Confidential*, redirect `http://localhost`). A Twitch exige **2FA** na conta para criar apps.
- *(Opcional)* uma chave da **Anthropic** para o título-gancho por IA.

### Instalação

```bash
git clone git@github.com:CaioMSantos/generator-clipper.git
cd generator-clipper

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env               # preencha TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET
```

### Rodando

```bash
clipper -v run --once     # um ciclo, com log detalhado (bom pro primeiro teste)
clipper run               # loop contínuo
clipper panel             # painel em http://127.0.0.1:8000
```

> O `-v` vem **antes** do subcomando.

### Docker

```bash
docker compose up -d --build        # sobe o loop + o painel (http://localhost:8000)
docker compose logs -f clipper
```

O modelo do Whisper é baixado na build, então o primeiro clip não espera download.

---

## 🖥️ Painel de revisão

`clipper panel` abre uma SPA leve (sem build, sem Node) servida pelo FastAPI, lendo a mesma fila do loop.

- **Grid 9:16** com preview ao passar o mouse, status, plataforma, views e duração
- **Menu lateral recolhível** (trilho de ícones ↔ expandido), com bolinhas de pendência por status e por streamer; vira menu deslizante no celular
- **Gaveta de detalhes**: player grande, legenda sugerida pronta pra copiar, transcrição, download do MP4 e link do clip original
- **Re-render em background** com gancho novo (texto / IA / sem gancho) e **corte de início e fim** marcado no próprio player

| Atalho | Ação | | Atalho | Ação |
|:-:|---|---|:-:|---|
| <kbd>A</kbd> | Aprovar e ir pro próximo | | <kbd>C</kbd> | Copiar legenda |
| <kbd>X</kbd> | Rejeitar e ir pro próximo | | <kbd>I</kbd> / <kbd>O</kbd> | Marcar início / fim do corte |
| <kbd>P</kbd> | Marcar como postado | | <kbd>←</kbd> <kbd>→</kbd> | Corte anterior / próximo |
| <kbd>U</kbd> | Voltar pra revisão | | <kbd>/</kbd> | Buscar |
| <kbd>Espaço</kbd> | Play / pause | | <kbd>[</kbd> | Recolher / expandir o menu |

> ⚠️ O painel **não tem login**. Por padrão escuta só em `127.0.0.1`. Não exponha na internet sem um proxy com autenticação.

---

## ⚙️ Configuração

### Streamers — `config/streamers.yaml`

```yaml
streamers:
  - name: Davy Jones
    platform: kick            # twitch | kick
    login: davyjones          # slug da URL do canal
    min_views: 5              # opcional: sobrescreve CLIPPER_MIN_VIEWS (Kick tem bem menos views)
    layout:
      mode: stack             # stack (facecam em cima) | blur (vídeo sobre fundo desfocado)
      facecam: { x: 0.015, y: 0.022, w: 0.215, h: 0.213 }      # posição principal (0..1)
      facecam_alternates:                                       # outros lugares onde a câmera aparece
        - { x: 0.784, y: 0.098, w: 0.213, h: 0.211 }
      facecam_ratio: 0.30     # fração da altura 9:16 ocupada pela facecam
```

**Como calibrar:** `clipper frame <url-de-um-clip>` salva um PNG; meça a câmera em frações da tela e confira com
`clipper render <url> --streamer <login>`. Se o streamer troca a câmera de canto, liste as posições em
`facecam_alternates` — a cada clip vence a que tem um rosto bem enquadrado; sem rosto, o corte sai em `blur`.

### Variáveis de ambiente — `.env`

| Variável | Padrão | Descrição |
|---|---|---|
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | — | Credenciais do app da Twitch |
| `CLIPPER_GAME_NAMES` | *(vazio)* | Filtra por jogo, nome exato da Twitch (ex.: `Grand Theft Auto VI`). A Kick aceita o mesmo nome |
| `CLIPPER_POLL_INTERVAL_SECONDS` | `120` | Intervalo entre ciclos |
| `CLIPPER_LOOKBACK_HOURS` | `6` | Janela de busca; momentos mais velhos saem da fila |
| `CLIPPER_MIN_VIEWS` | `30` | Mínimo de views por momento (soma do cluster) |
| `CLIPPER_MAX_CLIP_SECONDS` | `90` | Descarta clips mais longos (`0` = sem limite) |
| `CLIPPER_MAX_RENDERS_PER_CYCLE` | `4` | Renders por ciclo |
| `CLIPPER_MAX_RENDERS_PER_STREAMER` | `2` | Máximo por streamer em cada ciclo, em rodízio (`0` = sem limite) |
| `CLIPPER_WHISPER_MODEL` | `small` | `tiny` · `base` · `small` · `medium` · `large-v3` |
| `CLIPPER_WHISPER_LANGUAGE` | `pt` | Idioma da transcrição |
| `CLIPPER_FACECAM_DETECT` | `true` | Detecção da facecam por clip |
| `ANTHROPIC_API_KEY` | — | Liga o título-gancho por IA (sem chave, sai sem gancho) |
| `CLIPPER_HOOK_ENABLED` | `true` | Liga/desliga o gancho |
| `CLIPPER_HOOK_MODEL` | `claude-opus-5-5` | Modelo usado no gancho |
| `CLIPPER_DATA_DIR` | `data` | Fila SQLite, downloads e arquivos de trabalho |
| `CLIPPER_OUTPUT_DIR` | `data/output` | Onde os vídeos prontos são salvos |
| `CLIPPER_FONTS_DIR` | `assets/fonts` | Fonte bold do gancho e das legendas (ex.: Montserrat, OFL) |
| `CLIPPER_STREAMERS_FILE` | `config/streamers.yaml` | Catálogo de streamers |

---

## 🧠 Como funciona por dentro

<details>
<summary><b>Ranking e clusters</b></summary>

- **Velocidade** = views ÷ minutos desde a criação (piso de 5 min, pra um clip de 2 min com 40 views não atropelar tudo).
- Clips do **mesmo VOD** com offset a até **30s** são o mesmo momento. Só o melhor é renderizado; os outros viram `skipped`, e cada duplicata dá **+25%** no score.
- Um momento já tratado **nunca volta** pra fila. A fila é atômica em SQLite (WAL), com até 3 tentativas e recuperação de clips presos em `rendering`.
- **Rodízio por streamer**: o melhor de cada um sai antes do segundo de qualquer um — um canal com 300 clips/h não monopoliza o ciclo.
</details>

<details>
<summary><b>Detecção da facecam</b></summary>

Para cada clip, 6 frames são amostrados e um detector Haar (frontal + perfil, dos dois lados) roda dentro de cada posição candidata.
Ganha a posição com rosto em ≥ 2/6 frames; entre posições sobrepostas, a que deixa o rosto mais bem enquadrado.
Sem rosto (cena de chat, cutscene sem câmera), o corte cai no layout `blur` em vez de sair com um topo vazio.
OpenCV fica em 4.x porque o 5.x deixou de embutir os modelos Haar.
</details>

<details>
<summary><b>Legenda e gancho</b></summary>

- O áudio é decodificado pelo próprio ffmpeg (16 kHz mono) e transcrito com **faster-whisper** em CPU int8, com `word_timestamps` e VAD.
- A legenda é um `.ass` queimado pelo libass: uma palavra por vez, branca, sombra suave, a ~80% da altura.
- O gancho é gerado por **Claude** com saída em JSON schema e renderizado como PNG (Pillow) sobre a emenda facecam/gameplay, com fade-out aos 4s. Qualquer falha da API resulta em "sem gancho" — o clip nunca é perdido por causa dele.
</details>

<details>
<summary><b>Kick</b></summary>

A API pública da Kick ainda não expõe clips, então o código usa o endpoint interno do site
(`kick.com/api/v2/channels/{slug}/clips?sort=view&time=day`). Ele fica atrás do Cloudflare: funciona de IP residencial e
pode responder **403 a partir de datacenter**. Clips da Kick não têm offset de VOD, então não formam clusters.
</details>

---

## 📦 Saída

Para cada corte, em `CLIPPER_OUTPUT_DIR/AAAA-MM-DD/`:

```
davyjones_kick_clip_01M7Q5NYKA03B2N9VBKCSRMDSC.mp4   ← 1080×1920, H.264 + AAC, faststart
davyjones_kick_clip_01M7Q5NYKA03B2N9VBKCSRMDSC.json  ← metadados
```

O `.json` traz `hook`, `suggested_caption` (título + créditos ao canal + hashtags, pronto pra colar), `transcript`,
`score`, `views_at_render`, `trim` e `source_url`.

---

## 🛠️ Desenvolvimento

```bash
pytest -q                 # 60+ testes: ranking, fila, Twitch/Kick (respx), render, facecam, gancho, painel
mypy --strict src tests   # tipagem estrita
```

```
src/clipper/
├── sources/        twitch.py · kick.py
├── media/          download · audio · transcribe · captions · facecam · hook_card · fonts · render
├── panel/          app.py (FastAPI) · jobs.py (re-render) · static/ (SPA sem build)
├── ranking.py · store.py · pipeline.py · hook.py · config.py · cli.py
```

| Comando | O que faz |
|---|---|
| `clipper run [--once]` | Loop de descoberta e render |
| `clipper panel [--host --port]` | Painel de revisão |
| `clipper render <url> [--streamer <login>] [--hook "TEXTO"]` | Renderiza um clip específico |
| `clipper frame <url> [--at 5]` | PNG de um frame, pra calibrar a facecam |
| `clipper list [--status ready]` | Lista a fila |
| `clipper set-status <key> <status>` | Muda o status (`approved`, `rejected`, `posted`…) |

### Requisitos de máquina

4 vCPU e 8 GB rodam o Whisper `small` em CPU — cerca de 1 min por clip de 30s, incluindo download e render.
Pra mais volume, use `CLIPPER_WHISPER_MODEL=base` ou uma máquina com GPU.

---

## 🗺️ Roadmap

- [x] Descoberta na Twitch e na Kick, ranking com clusters e fila em rodízio
- [x] Layout com facecam detectada por clip, legenda palavra por palavra e gancho por IA
- [x] Painel de revisão com re-render e corte
- [ ] Detector próprio: buffer da live (`streamlink`) + pico de chat (IRC / websocket) — elimina o atraso dos clips da comunidade
- [ ] Upload automático no YouTube (Data API v3) e no TikTok (Content Posting API)
- [ ] A/B de ganchos e métricas de retenção
- [ ] Limpeza automática de downloads antigos

---

## ⚖️ Uso responsável

Este projeto automatiza **edição**, não autoriza o uso do conteúdo de ninguém.

- Os vídeos pertencem aos **streamers** e os jogos às suas **publishers**. Antes de publicar cortes, tenha **autorização** de quem
  produziu a live (muitos streamers têm programas de clipadores oficiais) e respeite os termos de uso de cada plataforma.
- O conector da Kick usa um endpoint **não oficial** do site; ele pode mudar ou ser bloqueado a qualquer momento.
- Dê sempre o crédito ao canal de origem — a legenda sugerida já inclui o link.

## 🤝 Contribuindo

Issues e PRs são bem-vindos. Antes de abrir um PR, rode `pytest -q` e `mypy --strict src tests`.
Pra adicionar uma plataforma, implemente o protocolo `ClipSource` em `src/clipper/sources/`.
