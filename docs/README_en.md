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

**MOMOKA** is a feature-rich Discord bot designed for Discord servers. It integrates various functions including AI chat, music playback, image generation, and notification features into a single bot.

### Features

- 🤖 **Multi-Model AI Chat** - Supports multiple AI providers including OpenAI, Google Gemini, NVIDIA NIM, and KoboldCPP
- 🎵 **Advanced Music Playback** - Play music from YouTube, Spotify, Google Drive, and more
- 🎨 **Image Generation** - Generate images using Stable Diffusion
- 🗣️ **Text-to-Speech** - Convert text to speech using Style-Bert-VITS2
- 📊 **Game Statistics Tracking** - Display stats for Rainbow Six Siege and VALORANT
- 🔔 **Real-time Notifications** - Earthquake alerts and Twitch stream notifications
- 🎲 **Useful Utilities** - Dice rolls, timers, media downloads, and more

---

## Main Features

### 1. AI Chat Feature (LLM)

Mention the bot to chat with AI. Supports multiple AI models and can recognize images.

#### Supported Models

- **OpenAI**: GPT-4o, GPT-4 Turbo
- **Google**: Gemini 2.5 Flash, Gemini 2.5 Pro
- **NVIDIA NIM**: Kimi, Llama, DeepSeek R1, and more
- **KoboldCPP**: Supports local LLM servers

#### Main Features

- Image recognition (for supported models)
- Conversation history management
- User information memory (`/set-user-bio`)
- Global memory feature (`/memory-save`)
- Image generation tool integration
- Web search functionality
- **🔄 Automatic API Key Rotation** - Configure multiple API keys to automatically switch to the next key when rate limit or server errors occur. Ensures stable operation even under high load

### 2. Music Playback Feature

Play music in voice channels. Provides queue management, loop, shuffle, and other features.

#### Supported Sources

- YouTube
- Spotify
- Google Drive
- NicoNico Video
- Other media supported by yt-dlp

#### Main Features

- Queue management (up to 10,000 songs)
- Loop playback (one song/all)
- Shuffle
- Volume adjustment (0-200%)
- Seek (jump to specified time)
- Playlist support

### 3. Image Generation Feature

Generate images using Stable Diffusion. Integrates with WebUI Forge or KoboldCPP.

#### Main Features

- Image generation based on prompts
- Multiple model switching
- Custom parameter settings
- Negative prompt support

### 4. Text-to-Speech Feature (TTS)

Convert text to speech using Style-Bert-VITS2.

#### Main Features

- Multiple model support
- Style settings
- Speech rate and volume adjustment
- Auto announcement for join/leave events

### 5. Game Statistics Tracking

#### Rainbow Six Siege

- Display player statistics
- Rank information
- Match history

#### VALORANT

- Display player statistics
- Rank information
- Match history

### 6. Notification Features

#### Earthquake Alerts

- Real-time notifications of earthquake early warnings from Japan Meteorological Agency
- Fast notifications via WebSocket connection
- Map display of earthquake information
- History display

#### Twitch Notifications

- Notify when specified channels start streaming
- Display stream information

### 7. Utility Features

- Dice rolls (nDn format, range specification)
- Conditional dice rolls
- Timer feature
- Media download
- Server/user information display
- Gacha simulation (Blue Archive style)

---

## Setup

### Requirements

- Python 3.8 or higher
- Discord Bot Token
- Various API keys (depending on features used)

### Installation Steps

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd ProjectMOMOKA
   ```

2. **Create virtual environment (recommended)**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   source .venv/bin/activate  # Linux/Mac
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Prepare configuration file**
   ```bash
   copy config.default.yaml config.yaml  # Windows
   cp config.default.yaml config.yaml  # Linux/Mac
   ```

5. **Edit configuration file**
   - Open `config.yaml` and set the required API keys

6. **Start the bot**
   ```bash
   python main.py
   ```
   Or on Windows:
   ```bash
   startMOMOKA.bat
   ```

---

## Configuration

### Basic Settings

You can configure the following in `config.yaml`:

#### Bot Settings

```yaml
bot_token: YOUR_BOT_TOKEN_HERE
client_id: YOUR_CLIENT_ID_HERE
admin_user_ids:
  - 123456789012345678
```

#### LLM Settings

```yaml
llm:
  model: "google/gemini-2.5-pro"
  available_models:
    - "openai/gpt-4o"
    - "google/gemini-2.5-pro"
  providers:
    openai:
      base_url: https://api.openai.com/v1
      api_key1: YOUR_KEY
      api_key2: YOUR_KEY_2  # Multiple API keys can be configured
      api_key3: YOUR_KEY_3  # Automatically switches to next key on rate limit
    google:
      base_url: https://generativelanguage.googleapis.com/v1beta/
      api_key1: YOUR_KEY
      api_key2: YOUR_KEY_2
      api_key3: YOUR_KEY_3
      api_key4: YOUR_KEY_4
      api_key5: YOUR_KEY_5  # Up to 5 keys can be set (more can be added if needed)
```

**💡 API Key Rotation Feature:**
- You can configure multiple API keys as `api_key1`, `api_key2`, `api_key3`, etc.
- When rate limit or server errors occur, it automatically switches to the next API key
- Automatically retries until all keys are exhausted
- Ensures stable operation even under high load

#### Image Generation Settings

```yaml
llm:
  image_generator:
    forge_url: "http://127.0.0.1:7860"
    model: "sd_xl_base_1.0.safetensors"
    default_size: "1024x1024"
```

#### Music Settings

```yaml
music:
  default_volume: 20
  max_queue_size: 10000
  auto_leave_timeout: 3
```

#### TTS Settings

```yaml
tts:
  api_server_url: "http://127.0.0.1:5000"
  default_model_id: 0
  default_style: "Neutral"
```

For detailed configuration options, refer to `config.default.yaml`.

---

## Command List

### AI Chat Feature (LLM)

| Command | Description |
|---------|------------|
| `@MOMOKA <message>` | Mention the bot to chat with AI |
| `/chat <message>` | Chat with AI without mentioning |
| `/llm_help` | Display LLM feature help |
| `/set-user-bio <info>` | Set user information |
| `/show-user-bio` | Display saved user information |
| `/reset-user-bio` | Delete user information |
| `/memory-save <info>` | Save to global memory |
| `/memory-list` | List global memory |
| `/memory-delete <ID>` | Delete from global memory |
| `/clear_history` | Reset conversation history |
| `/switch-models` | Switch channel-specific model |

### Music Playback Feature

| Command | Description |
|---------|------------|
| `/play <song name or URL>` | Play/add song to queue |
| `/pause` | Pause playback |
| `/resume` | Resume playback |
| `/stop` | Stop playback and clear queue |
| `/skip` | Skip current song |
| `/seek <time>` | Seek to specified time (e.g., `1:30`) |
| `/volume <0-200>` | Change volume |
| `/queue` | Display queue |
| `/shuffle` | Shuffle queue |
| `/clear` | Clear queue |
| `/remove <number>` | Remove song from queue |
| `/nowplaying` | Display currently playing song |
| `/music_help` | Display music feature help |

### Image Generation Feature

| Command | Description |
|---------|------------|
| `@MOMOKA generate an image` | Request image generation from AI |
| `/image-generate` | Direct image generation (if implemented) |

### TTS Feature

| Command | Description |
|---------|------------|
| `/say <text>` | Convert text to speech |
| `/tts-help` | Display TTS feature help |

### Game Statistics Tracking

#### Rainbow Six Siege

| Command | Description |
|---------|------------|
| `/r6s <username>` | Display player statistics |

#### VALORANT

| Command | Description |
|---------|------------|
| `/valorant <username>` | Display player statistics |

### Notification Features

#### Earthquake Alerts

| Command | Description |
|---------|------------|
| `/earthquake_channel <channel>` | Set notification channel |
| `/earthquake_remove <type>` | Remove notification settings |
| `/earthquake_test` | Send test notification |
| `/earthquake_status` | Check system status |
| `/earthquake_history` | Display recent earthquake history |
| `/earthquake_map` | Display earthquakes on map |
| `/earthquake_help` | Display help |

#### Twitch Notifications

| Command | Description |
|---------|------------|
| `/twitch_add <username>` | Add notification target channel |
| `/twitch_remove <username>` | Remove notification target channel |
| `/twitch_list` | Display notification targets |

### Utility Commands

| Command | Description |
|---------|------------|
| `/help` | Display bot help information |
| `/ping` | Check bot latency |
| `/serverinfo` | Display server information |
| `/userinfo [user]` | Display user information |
| `/avatar [user]` | Display avatar image |
| `/invite` | Display bot invite link |
| `/meow` | Display random cat picture |
| `/roll <notation>` | Roll dice in nDn format (e.g., `2d6+3`) |
| `/diceroll <min> <max>` | Roll dice in specified range |
| `/check <notation> [condition] [target]` | Roll dice and check condition |
| `/gacha` | Blue Archive style gacha |
| `/timer <time> <message>` | Set timer |
| `/support` | Display contact information for developer |

---

## Feature Details

### AI Chat Feature Details

#### Mention Method

Mention the bot (`@MOMOKA`) and send a message, and the AI will respond.

#### Image Recognition

For supported models, images attached to messages can be recognized.

#### Conversation History

Conversation history is maintained per channel. Reset with `/clear_history`.

#### User Information

Information set with `/set-user-bio` is referenced in conversations with that user.

#### Global Memory

Information can be saved to server-wide memory. Useful for storing bot settings and common information.

#### API Key Rotation

MOMOKA provides robust handling of rate limits and server errors by configuring multiple API keys.

**How it works:**
1. Configure multiple API keys in the config file (`config.yaml`) as `api_key1`, `api_key2`, `api_key3`, etc.
2. By default, the first API key (`api_key1`) is used
3. When a rate limit error (`RateLimitError`) or server error (`InternalServerError`) occurs, it automatically switches to the next API key
4. Automatically retries until all API keys are exhausted
5. Only returns an error if all keys fail

**Benefits:**
- **High Availability**: Service continues with other keys even if one API key hits rate limits
- **Load Distribution**: Distributes load across multiple keys instead of a single key
- **Automatic Recovery**: Automatically switches without manual intervention when errors occur
- **Flexible Configuration**: Can set different numbers of API keys for each provider

**Example:**
```yaml
providers:
  google:
    api_key1: "key1"
    api_key2: "key2"
    api_key3: "key3"
    api_key4: "key4"
    api_key5: "key5"
```

With this configuration, 5 API keys will automatically rotate when using the Google Gemini API.

### Music Playback Feature Details

#### Queue Management

Up to 10,000 songs can be added to the queue.

#### Loop Mode

- **OFF**: No loop
- **ONE**: Loop current song
- **ALL**: Loop entire queue

#### Volume Adjustment

Volume can be adjusted from 0-200%. Default is 20%.

#### Auto Leave

When the voice channel becomes empty, the bot automatically leaves after the configured time (default 3 minutes).

### Image Generation Feature Details

#### Stable Diffusion WebUI Forge Integration

If Forge WebUI is running, you can request image generation from the bot.

#### Prompts

When you request image generation in AI chat (e.g., "generate an image"), the AI generates a prompt and creates the image.

### Earthquake Alert Feature Details

#### WebSocket Connection

Connects to Japan Meteorological Agency's WebSocket server to receive earthquake information in real-time.

#### Notification Types

- **Earthquake Early Warning**: When maximum seismic intensity of 5 Lower or higher is expected
- **Earthquake Information**: When earthquakes of intensity 1 or higher occur
- **Tsunami Forecast**: When tsunamis are expected

---

## Troubleshooting

### Bot won't start

1. Check if `bot_token` is correctly set in `config.yaml`
2. Check if Python version is 3.8 or higher
3. Check if required packages are installed

### AI doesn't respond

1. Check LLM settings in `config.yaml`
2. Check if API keys are correctly set
3. Check if the model being used is available

### Music won't play

1. Check if the bot is connected to a voice channel
2. Check audio file permissions
3. Check if FFmpeg is installed (if yt-dlp uses it)

### Image generation doesn't work

1. Check if Stable Diffusion WebUI Forge is running
2. Check if `forge_url` in `config.yaml` is correct
3. Check if the model is correctly loaded

### Earthquake alerts don't arrive

1. Check system status with `/earthquake_status`
2. Check if notification channel is correctly set
3. Check if WebSocket connection is established

---

## Support

If you encounter issues, you can get support through:

- Discord Support Server: [Link]
- Contact Developer: Displayed with `/support` command

---

**Made with ❤️ by the MOMOKA development team**

