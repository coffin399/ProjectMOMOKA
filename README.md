<div align="center">

![Moe Counter](http://moecounter.atserver186.jp/@MOMOKAv1?name=MOMOKAv1&theme=booru-lewd&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=0)

# 🎯 MOMOKA

**AIエージェント型DiscordBOT - An intelligent AI agent Discord bot with autonomous decision-making, AI chat, music playback, image generation, and more!**

[![Invite Bot](https://img.shields.io/badge/Invite%20Sample%20Bot-24/7%20Online-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.com/oauth2/authorize?client_id=1031673203774464160&permissions=551906765824&scope=bot)

</div>

<div align="center">

![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![discord.py](https://img.shields.io/badge/discord.py-2.7+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/coffin399/ProjectMOMOKA)
[![Discord](https://img.shields.io/discord/1305004687921250436?logo=discord&logoColor=white&label=Discord&color=5865F2)](https://discord.gg/H79HKKqx3s)
[![](https://coffin399.github.io/coffin299page/assets/badge.svg)](https://discord.com/oauth2/authorize?client_id=1031673203774464160&permissions=551906765824&scope=bot)

</div>
</div>

<div align="center">

[🇯🇵 日本語 (Japanese)](docs/README_ja.md) | [🇺🇸 English](docs/README_en.md)

</div>

---

## ✨ What is MOMOKA?

**MOMOKA** is an **AI Agent-type Discord Bot** that acts as your intelligent Discord companion! 🤖✨ Unlike traditional bots that simply respond to commands, MOMOKA operates as an autonomous AI agent capable of making decisions, understanding context, and proactively assisting your community. It combines the power of advanced AI chat with autonomous capabilities, seamless music playback, local image generation, and essential utility commands—all in one sleek package. Perfect for communities that want an intelligent, self-aware bot that can think and act independently!

> **Built-ins:**
> - **Built-in image generation engine** - Fully integrated diffusers-based image generation pipeline (see `MOMOKA/generator/image`). No external services required!
> - **Integrated Style-Bert-VITS2 TTS engine** - The complete [Style-Bert-VITS2](https://github.com/litagin02/Style-Bert-VITS2) source code is integrated into this project. No external API server needed! See `MOMOKA/generator/tts` and `NOTICE` for details.

### 🚀 Key Features

- 🤖 **AI Chat (LLM)** - Simply mention the bot with `@<bot name>` to start chatting! Supports multiple AI models including OpenAI GPT-4, Google Gemini, NVIDIA NIM, and local KoboldCPP with **automatic API key rotation** to handle rate limits seamlessly
- 🎵 **Music Playback** - Play music from YouTube, Spotify, and more in voice channels
- 🎨 **Image Generation (Built-in)** - Fully integrated diffusers-based image generation engine. No external services required! Place models at `models/image-models/<image model名>/<image model名>.safetensors` (optional VAE/LoRA and `model.json`).
- 🗣️ **Text-to-Speech (Built-in)** - **Fully integrated Style-Bert-VITS2 engine** - The complete [Style-Bert-VITS2](https://github.com/litagin02/Style-Bert-VITS2) source code is built into this project. No external API server needed! Place models at `models/tts-models/<tts model名>/<tts model名>.safetensors` (or `G_*.pth` with matching `config.json`). Optional `pyopenjtalk` dictionary and `style_vectors.npy` are supported. See `NOTICE` for integration details.
- 📊 **Game Tracking** - Track stats for Rainbow Six Siege and VALORANT
- 🔔 **Notifications** - Get notified about earthquakes and Twitch streams
- 🎲 **Utilities** - Dice rolls, timers, media downloads, and more!

### 📋 Quick Start

1. **Clone the repository**
   ```bash
   git clone https://github.com/coffin399/ProjectMOMOKA.git
   cd ProjectMOMOKA
   ```

2. **Requirements**
   - **Python 3.11.x** (required — 3.10 / 3.12+ / 3.14 are not supported)
   - On Windows, install so that `py -3.11` works
   - Optional for music: place Netscape-format `youtube_cookie.txt` in the project root

3. **Configure the bot**
   - Copy `config.default.yaml` to `config.yaml`
   - Fill in your bot token and API keys
  - (Optional) Place local models:
    
    **Directory Structure:**
    ```
    models/
    ├── image-models/
    │   └── <image model名>/
    │       └── <image model名>.safetensors
    │       └── (optional) VAE, LoRA, model.json
    └── tts-models/
        └── <tts model名>/
            └── <tts model名>.safetensors
            └── (optional) config.json, style_vectors.npy
    ```
    
    **Examples:**
    - **Image models**: `models/image-models/my-model/my-model.safetensors`
      - Optional: VAE, LoRA, and `model.json` in the same directory
    - **TTS models**: `models/tts-models/my-voice/my-voice.safetensors`
      - Alternative: `G_*.pth` with matching `config.json`
      - Optional: `pyopenjtalk` dictionary and `style_vectors.npy`
   - Configure options in `config.yaml` (e.g., default image model, TTS defaults)

4. **Run the bot**
   
   **Windows (Recommended):** Creates a Python 3.11 venv, installs deps, and starts the bot:
   ```bash
   startMOMOKA.bat
   ```
   If an old `.venv` (e.g. 3.10) exists, the script recreates it automatically.
   
   **Manual start (Linux/Mac or if you prefer):**
   ```bash
   py -3.11 -m venv .venv   # or: python3.11 -m venv .venv
   # Windows: .venv\Scripts\activate
   # Linux/Mac: source .venv/bin/activate
   pip install -r requirements.txt
   python main.py
   ```

### 📚 Documentation Highlights

For detailed documentation, please check the language-specific README files:

- [🇯🇵 日本語詳細ドキュメント (Japanese Detailed Documentation)](docs/README_ja.md)
- [🇺🇸 English Detailed Documentation](docs/README_en.md)

Key guides inside the docs include:
- Configuring built-in image generation and model management
- Tips for Stable Diffusion prompt crafting

### 🔧 Third-Party Integrations

This project integrates source code from the following open-source projects:

- **[Style-Bert-VITS2](https://github.com/litagin02/Style-Bert-VITS2)** - Text-to-speech engine integrated into `MOMOKA/generator/tts`. The Style-Bert-VITS2 source code is built into this project under AGPL-3.0 and LGPL-3.0 licenses. See `NOTICE` and `MOMOKA/generator/tts/LICENSE_SBVITS2*` for details.

---

<div align="center">

**Made with ❤️ by the MOMOKA development team**

</div>

