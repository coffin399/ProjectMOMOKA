# 📚 MOMOKA Detailed Documentation (English)

## Table of Contents

- [Overview](#overview)
- [Main Features](#main-features)
- [Setup](#setup)
- [Configuration](#configuration)
- [Command List](#command-list)
- [Feature Details](#feature-details)
- [Troubleshooting](#troubleshooting)

---

## Overview

**MOMOKA** runs **two Discord bots in one process**: **PLANA** (primary) and **ARONA** (companion). Each bot works standalone for LLM chat and music. Debate and cross-check require both bots in the same guild.

### Dual Bot

| Bot | Role | Invite |
|-----|------|--------|
| **PLANA** | Primary — LLM, music, TTS, images, notifications, trackers, Link Fix, utilities | [Invite](https://discord.com/oauth2/authorize?client_id=1031673203774464160&permissions=6516795221339600&scope=bot) |
| **ARONA** | Companion — LLM, music, utilities. TTS/images/notifications/trackers/Link Fix redirect to PLANA | [Invite](https://discord.com/oauth2/authorize?client_id=1364917551024308255&permissions=6516795221339600&scope=bot) |

- Create **two Discord Applications** and enable **Message Content Intent** on both
- The old root `config.yaml` / `config.default.yaml` are **not used** (no compatibility)

### Features

- 🤖 **Multi-model AI chat** — OpenAI, Google Gemini, NVIDIA NIM, KoboldCPP, and more
- 🗣️ **debate / cross_check** — Multi-round PLANA↔ARONA debate with a judge turn, or a light 3-step verification
- 🎵 **Music playback** — YouTube, Spotify, Google Drive, and more (both bots)
- 🎨 **Image generation / TTS / notifications / trackers** — **PLANA only**
- 🔗 **Link Fix** — Replace broken social embeds via fixer proxies (`/linkfix`, **PLANA only**)
- 🎲 **Utilities** — `/help` and `/invite` (Components V2), dice, timers, media download (`/download_video` / `/download_audio`, Components V2), and more

---

## Main Features

### 1. AI Chat (LLM)

Mention `@PLANA` or `@ARONA` to chat.

#### Supported Models

- **OpenAI**: GPT-4o, GPT-4 Turbo
- **Google**: Gemini family
- **NVIDIA NIM**: Kimi, Llama, DeepSeek R1, and more
- **KoboldCPP**: Local LLM servers

#### Highlights

- Image recognition (supported models)
- Conversation history
- Web search
- **debate** — Multi-round PLANA↔ARONA discussion, then a judge turn
- **cross_check** — PLANA draft → ARONA review → PLANA conclusion (3 posts, no stop panel)
- **Automatic API key rotation** on rate limits / server errors

### 2. Music Playback

Both bots can play in voice channels (queue, loop, shuffle, etc.).

#### Sources

- YouTube / Spotify / Google Drive / NicoNico / other yt-dlp media

### 3. Image Generation (PLANA only)

Built-in **diffusers** engine (no external service required). Models under `models/image-models/`. Optional Stable Diffusion WebUI Forge API.

Requests on ARONA are redirected to PLANA.

### 4. Text-to-Speech (PLANA only)

Integrated [Style-Bert-VITS2](https://github.com/litagin02/Style-Bert-VITS2). Models under `models/tts-models/`. See `NOTICE`.

### 5. Game Trackers (PLANA only)

Rainbow Six Siege / VALORANT stats.

### 6. Notifications (PLANA only)

Earthquake alerts and Twitch stream notifications.

### 7. Link Fix (PLANA only)

When Discord’s official embed is missing or broken, PLANA silently replies with a fixer-proxy URL so media previews work again.

- Examples: X (Twitter), Instagram, TikTok, Reddit, Threads, Bluesky, Facebook, Pixiv, YouTube, and more
- Skip with `fxignore` in the message, or wrap the URL in `<>`
- For X posts, if the guild `preferred_locale` is known, `🌐` / flag buttons switch original vs translated embeds (FxEmbed-compatible fixers only)
- `/linkfix` (Manage Server): per-guild master/site toggles and fixer source/destination domains (Components V2)
- Config: `configs/link_fix_config.yaml`; guild overrides in `data/link_fix_settings.json`

### 8. Utilities

Dice, timers, server/user info, gacha, etc. `/help` and `/invite` use Components V2 and cover both bot invites.

#### Media download (Components V2)

Fetches media with yt-dlp and shares it via Google Drive (links expire after a delay).

| Command | Description |
|---------|-------------|
| `/download_video <query>` | Download from a URL or search query. Shows a Components V2 format picker, then auto-merges best audio and shares |
| `/download_audio <query> <format>` | Extract audio only (mp3 / m4a / opus / flac / wav) and share |

- Format labels do **not** show “video only” notes (audio is merged after selection)
- Requires Google Drive API `client_secrets.json` / `token.json` and the folder ID configured in the cog
- For YouTube, Deno (recommended) or Node.js 22+ plus `yt-dlp[default]` is advised (same EJS guidance as music)

---

## Setup

### Requirements

- **Python 3.11.x** (required; 3.10 / 3.12+ not supported)
- Discord bot tokens for PLANA and ARONA
- **Message Content Intent** enabled on both applications
- API keys as needed
- (Optional) Netscape-format `youtube_cookie.txt` in the project root

### Installation

1. **Clone**
   ```bash
   git clone https://github.com/coffin399/ProjectMOMOKA.git
   cd ProjectMOMOKA
   ```

2. **Configuration**
   - On first run, each missing `configs/<category>_config.yaml` is copied from the matching `*_config.default.yaml`
   - Manual copy example:
     ```bash
     copy configs\bots_config.default.yaml configs\bots_config.yaml   # Windows
     cp configs/bots_config.default.yaml configs/bots_config.yaml     # Linux/Mac
     ```
   - **Root `config.yaml` is not read**

3. **Required edits**
   - `configs/bots_config.yaml` — `bots.plana.token` and `bots.arona.token`
   - `configs/llm_config.yaml` — API keys

4. **Invite bots**
   - [PLANA](https://discord.com/oauth2/authorize?client_id=1031673203774464160&permissions=6516795221339600&scope=bot)
   - [ARONA](https://discord.com/oauth2/authorize?client_id=1364917551024308255&permissions=6516795221339600&scope=bot)
   - Invite **both** to any guild where you want debate / cross_check

5. **Start**

   **Windows (recommended):**
   ```bash
   startMOMOKA.bat
   ```

   **Manual:**
   ```bash
   py -3.11 -m venv .venv
   .venv\Scripts\activate  # Windows / source .venv/bin/activate  # Linux/Mac
   pip install -r requirements.txt
   python main.py
   ```

---

## Configuration

Settings live under `configs/` as category YAML files. See each `*_config.default.yaml` for full keys.

| File | Purpose |
|------|---------|
| `bots_config.yaml` | PLANA/ARONA tokens, invites, roles |
| `llm_config.yaml` | Models, provider API keys, personas |
| `music_config.yaml` | Volume, queue, cookie path, etc. |
| `tts_config.yaml` | TTS models (PLANA) |
| `images_config.yaml` | Image generation (PLANA) |
| `notifications_config.yaml` | Earthquake / Twitch (PLANA) |
| `tracker_config.yaml` | Game stats (PLANA) |
| `debate_config.yaml` | debate / cross_check |
| `link_fix_config.yaml` | Link Fix (broken social embeds, PLANA) |
| `utilities_config.yaml` | Utilities |
| `core_config.yaml` | Shared core settings |

#### Bot tokens (`bots_config.yaml`)

```yaml
bots:
  plana:
    token: YOUR_PLANA_BOT_TOKEN
  arona:
    token: YOUR_ARONA_BOT_TOKEN
```

#### LLM (`llm_config.yaml`)

```yaml
llm:
  model: "google/gemini-2.5-pro"
  providers:
    google:
      api_key1: YOUR_KEY
      api_key2: YOUR_KEY_2  # rotates on rate limit
```

#### Images / TTS / Music

- Images: place models at `models/image-models/<name>/<name>.safetensors`; configure via images/LLM image settings (`local` or `forge`)
- TTS: `models/tts-models/` + `tts_config.yaml`
- Music: `music_config.yaml` (`default_volume`, `max_queue_size`, cookie path, etc.)

---

## Command List

### AI Chat (LLM)

| Command | Description |
|---------|-------------|
| `@PLANA` / `@ARONA` `<message>` | Chat via mention |
| `/chat <message>` | Chat without mention |
| `/clear_history` | Reset history |
| `/switch-models` | Per-channel model |

\* `debate` / `cross_check` are LLM tools (multi-round debate + judge / light 3-step check).

### Music

| Command | Description |
|---------|-------------|
| `/play` `/pause` `/resume` `/stop` `/skip` | Playback |
| `/seek` `/volume` `/queue` `/shuffle` `/clear` `/remove` `/nowplaying` | Queue & volume |

### Image Generation (PLANA)

| Command | Description |
|---------|-------------|
| Ask `@PLANA` to generate an image | Via AI tools |

### TTS (PLANA)

| Command | Description |
|---------|-------------|
| `/say <text>` | Speak text |
| `/tts-help` | TTS help |

### Game Trackers (PLANA)

| Command | Description |
|---------|-------------|
| `/r6s` / `/valorant` | Player stats |

### Notifications (PLANA)

| Command | Description |
|---------|-------------|
| `/earthquake_*` | Earthquake alerts |
| `/twitch_add` `/twitch_remove` `/twitch_list` | Twitch alerts |

### Link Fix (PLANA)

| Command / action | Description |
|------------------|-------------|
| Post an SNS URL | Silent quote-reply with a fixer URL when the official embed is missing/broken |
| `/linkfix` | Master/site toggles and fixer source/destination (Components V2, Manage Server) |
| Include `fxignore` | Skip Link Fix for that message |

### Utilities

| Command | Description |
|---------|-------------|
| `/help` | Help (Components V2) |
| `/invite` | PLANA / ARONA invites (Components V2) |
| `/download_video` `/download_audio` | Media download (Components V2, Google Drive share) |
| `/ping` `/serverinfo` `/userinfo` `/avatar` | Info |
| `/roll` `/diceroll` `/check` `/gacha` `/timer` `/meow` `/support` | Misc |

---

## Feature Details

### debate / cross_check

- **debate**: Alternating PLANA↔ARONA turns, then a judge turn with summary and recommendation. Both bots must be in the guild
- **cross_check**: PLANA draft → ARONA review → PLANA conclusion; all three steps are posted. Lighter than debate; no stop panel
- Each message is prefixed with a mention of the partner bot

### API key rotation

Set `api_key1`, `api_key2`, … per provider in `llm_config.yaml`. On rate limit / server error, the next key is tried automatically.

### Music

Up to 10,000 queued tracks, loop modes, volume 0–200%, auto-leave when the VC is empty.

### Image generation (PLANA)

Place models under `models/image-models/`, use `provider: "local"` (default). For Forge, start with `--api` and set `provider: "forge"`.

### Earthquake alerts (PLANA)

Real-time JMA WebSocket alerts (early warning, quake info, tsunami forecast).

### Link Fix (PLANA)

- Waits briefly for Discord’s official embed; replies with a fixer URL **only when media is missing/mismatched** (not always-on)
- Replies are `@silent`; original embeds are suppressed when Manage Messages is available
- If the fixer reply gets no embed, it is deleted
- `/linkfix` lets each guild pick fixer denominations (e.g. `fxtwitter.com` / `vxtwitter.com`) and match domains

---

## Troubleshooting

### Bot won't start

1. Check `bots.plana.token` / `bots.arona.token` in `configs/bots_config.yaml`
2. Confirm Python 3.11.x
3. Confirm dependencies are installed
4. A root `config.yaml` alone will not work — use `configs/`

### AI doesn't respond

1. Check API keys in `configs/llm_config.yaml`
2. Enable **Message Content Intent** on both applications
3. Confirm the selected model is available

### debate / cross_check fail

1. Ensure both PLANA and ARONA are in the same server
2. Use `/invite` to add the missing bot

### Music won't play

1. Check VC connection, FFmpeg, and `youtube_cookie.txt`
2. **YouTube EJS**: install Deno (recommended) or Node.js 22+ on PATH; run `pip install -U "yt-dlp[default]"`

### Image generation fails

1. Use PLANA (ARONA only redirects)
2. Check model paths and images/LLM image settings

### Earthquake alerts missing

1. `/earthquake_status` on PLANA
2. Check notification channel and WebSocket connection

---

## Support

- Discord: [https://discord.com/invite/H79HKKqx3s](https://discord.com/invite/H79HKKqx3s)
- `/support` command

### License

- This project: **AGPL-3.0**
- Style-Bert-VITS2 integration: AGPL-3.0 / LGPL-3.0 (see `NOTICE`)

---

**Made with ❤️ by the MOMOKA development team**
