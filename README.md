<div align="center">

![Moe Counter](http://moecounter.atserver186.jp/@MOMOKAv1?name=MOMOKAv1&theme=booru-lewd&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=0)

# 🎯 MOMOKA

**AIエージェント型DiscordBOT — PLANA + ARONA のデュアルボット構成。AIチャット・音楽・画像生成・討論など。**

[![Invite PLANA](https://img.shields.io/badge/Invite%20PLANA-24/7%20Online-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.com/oauth2/authorize?client_id=1031673203774464160&permissions=6516795221339600&scope=bot)
[![Invite ARONA](https://img.shields.io/badge/Invite%20ARONA-Companion-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.com/oauth2/authorize?client_id=1364917551024308255&permissions=6516795221339600&scope=bot)

</div>

<div align="center">

![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![discord.py](https://img.shields.io/badge/discord.py-2.7+-blue.svg)
![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/coffin399/ProjectMOMOKA)
[![Discord](https://img.shields.io/discord/1305004687921250436?logo=discord&logoColor=white&label=Discord&color=5865F2)](https://discord.gg/H79HKKqx3s)
[![](https://coffin299.net/assets/badge.svg)](https://discord.com/oauth2/authorize?client_id=1031673203774464160&permissions=6516795221339600&scope=bot)

</div>

<div align="center">

[🇯🇵 日本語 (Japanese)](docs/README_ja.md) | [🇺🇸 English](docs/README_en.md)

</div>

---

## ✨ What is MOMOKA?

**MOMOKA** runs **two Discord bots in one process**: **PLANA** (primary) and **ARONA** (companion). Each can run alone for LLM chat and music. Debate / cross-check need both bots in the same guild. TTS, image generation, notifications, trackers, and **Link Fix** are **PLANA only** (ARONA redirects users to PLANA for most of those).

> **Built-ins:**
> - **Built-in image generation** — diffusers pipeline in `MOMOKA/generator/image`
> - **Integrated Style-Bert-VITS2 TTS** — see `MOMOKA/generator/tts` and `NOTICE` (AGPL/LGPL)

### 🚀 Key Features

- 🤖 **AI Chat (LLM)** — Mention `@PLANA` / `@ARONA`. OpenAI, Gemini, NVIDIA NIM, KoboldCPP + API key rotation
- 🗣️ **debate / cross_check** — Multi-round PLANA↔ARONA debate with judge, or a light 3-step cross-check
- 🎵 **Music** — YouTube, Spotify, and more (both bots)
- 🎨 **Image Generation / TTS / Notifications / Trackers** — PLANA only
- 🔗 **Link Fix** — Replace broken social embeds (X/Instagram/TikTok/…) via fixer proxies; `/linkfix` settings (PLANA only)
- 🎲 **Utilities** — `/help` and `/invite` (Components V2), dice, timers, media download (`/download_video` / `/download_audio`, Components V2), and more

### 📋 Quick Start

1. **Clone**
   ```bash
   git clone https://github.com/coffin399/ProjectMOMOKA.git
   cd ProjectMOMOKA
   ```

2. **Requirements**
   - **Python 3.11.x** (required)
   - Two Discord Applications (PLANA + ARONA), both with **Message Content Intent**
   - Optional: Netscape-format `youtube_cookie.txt` in the project root

3. **Configure** (`configs/` — root `config.yaml` is **not** used)
   - First run copies `configs/<category>_config.default.yaml` → `configs/<category>_config.yaml` automatically (or copy manually)
   - Edit `configs/bots_config.yaml`: set `bots.plana.token` and `bots.arona.token`
   - Edit `configs/llm_config.yaml` for API keys
   - Invite both bots:
     - [PLANA](https://discord.com/oauth2/authorize?client_id=1031673203774464160&permissions=6516795221339600&scope=bot)
     - [ARONA](https://discord.com/oauth2/authorize?client_id=1364917551024308255&permissions=6516795221339600&scope=bot)

4. **Run**
   ```bash
   startMOMOKA.bat          # Windows (recommended)
   # or: py -3.11 -m venv .venv && pip install -r requirements.txt && python main.py
   ```

### 📚 Docs

- [🇯🇵 日本語詳細](docs/README_ja.md) · [🇺🇸 English](docs/README_en.md)

### 🔧 Third-Party / License

- Project license: **AGPL-3.0**
- **[Style-Bert-VITS2](https://github.com/litagin02/Style-Bert-VITS2)** integrated under AGPL-3.0 / LGPL-3.0 — see `NOTICE` and `MOMOKA/generator/tts/LICENSE_SBVITS2*`

---

<div align="center">

**Made with ❤️ by the MOMOKA development team**

</div>
