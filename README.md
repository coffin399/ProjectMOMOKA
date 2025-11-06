<div align="center">

![Moe Counter](http://moecounter.atserver186.jp/@MOMOKAv1?name=MOMOKAv1&theme=booru-lewd&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=0)

# 🎯 MOMOKA

**A powerful, feature-rich Discord bot with AI chat, music playback, image generation, and more!**

[![Invite Bot](https://img.shields.io/badge/Invite%20Sample%20Bot-24/7%20Online-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.com/oauth2/authorize?client_id=1031673203774464160&permissions=551906765824&scope=bot)

</div>

<div align="center">

![Python](https://img.shields.io/badge/python-3.8+-blue.svg)
![discord.py](https://img.shields.io/badge/discord.py-2.0+-blue.svg)
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

**MOMOKA** is your all-in-one Discord companion! 🎮✨ A feature-packed bot that combines the power of AI chat, seamless music playback, local image generation, and essential utility commands—all in one sleek package. Perfect for communities that want everything without the hassle of managing multiple bots!

> **Built-ins:**
> - **Built-in image generation engine** - Fully integrated diffusers-based image generation pipeline (see `MOMOKA/generator/image`). No external services required!
> - **Integrated Style-Bert-VITS2 TTS engine** - The complete [Style-Bert-VITS2](https://github.com/litagin02/Style-Bert-VITS2) source code is integrated into this project. No external API server needed! See `MOMOKA/generator/tts` and `NOTICE` for details.

### 🚀 Key Features

- 🤖 **AI Chat (LLM)** - Simply mention the bot with `@<bot name>` to start chatting! Supports multiple AI models including OpenAI GPT-4, Google Gemini, NVIDIA NIM, and local KoboldCPP with **automatic API key rotation** to handle rate limits seamlessly
- 🎵 **Music Playback** - Play music from YouTube, Spotify, and more in voice channels
- 🎨 **Image Generation (Built-in)** - Fully integrated diffusers-based image generation engine. No external services required! Drop models under `models/image-models/<model_name>/` (single-file weights like `.safetensors`/`.ckpt` supported; optional VAE/LoRA and `model.json`).
- 🗣️ **Text-to-Speech (Built-in)** - **Fully integrated Style-Bert-VITS2 engine** - The complete [Style-Bert-VITS2](https://github.com/litagin02/Style-Bert-VITS2) source code is built into this project. No external API server needed! Put models under `models/tts-models/<model_name>/` with `<model_name>.safetensors` or `G_*.pth` and the matching `config.json`. Optional `pyopenjtalk` dictionary and `style_vectors.npy` are supported. See `NOTICE` for integration details.
- 📊 **Game Tracking** - Track stats for Rainbow Six Siege and VALORANT
- 🔔 **Notifications** - Get notified about earthquakes and Twitch streams
- 🎲 **Utilities** - Dice rolls, timers, media downloads, and more!

### 📋 Quick Start

1. **Clone the repository**
   ```bash
   git clone https://github.com/coffin399/ProjectMOMOKA.git
   cd ProjectMOMOKA
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure the bot**
   - Copy `config.default.yaml` to `config.yaml`
   - Fill in your bot token and API keys
   - (Optional) Place local models:
     - Image: `models/image-models/<model_name>/` → weights (`.safetensors`/`.ckpt`), optional `model.json`, VAE, LoRA
     - TTS: `models/tts-models/<model_name>/` → `<model_name>.safetensors` or `G_*.pth` + `.json`
     - You can also specify `tts.pyopenjtalk_dict_dir` for a custom `pyopenjtalk` dictionary
   - Configure options in `config.yaml` (e.g., default image model, TTS defaults)

4. **Run the bot**
   
   **Windows (Recommended):** Use the all-in-one batch file that handles virtual environment setup and package installation automatically:
   ```bash
   startMOMOKA.bat
   ```
   
   **Manual start (Linux/Mac or if you prefer):**
   ```bash
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

