# 📚 MOMOKA 詳細ドキュメント（日本語）

## 目次

- [概要](#概要)
- [主な機能](#主な機能)
- [セットアップ](#セットアップ)
- [設定](#設定)
- [コマンド一覧](#コマンド一覧)
- [機能詳細](#機能詳細)
- [トラブルシューティング](#トラブルシューティング)

---

## 概要

**MOMOKA** は、**PLANA** と **ARONA** の2つの Discord ボットを1プロセスで動かす多機能ボットです。AI対話・音楽は各ボット単体でも利用できます。討論（debate）やクロスチェック（cross_check）は、同じギルドに両方いる必要があります。

### デュアルボット

| Bot | 役割 | 招待 |
|-----|------|------|
| **PLANA** | プライマリ。LLM・音楽・TTS・画像・通知・tracker・ユーティリティ | [招待リンク](https://discord.com/oauth2/authorize?client_id=1031673203774464160&permissions=551906765824&scope=bot) |
| **ARONA** | コンパニオン。LLM・音楽・ユーティリティ。TTS/画像/通知/tracker は PLANA へ誘導 | [招待リンク](https://discord.com/oauth2/authorize?client_id=1364917551024308255&permissions=551906765824&scope=bot) |

- Discord Developer Portal で **2つの Application** を用意し、両方に **Message Content Intent** を有効化してください
- 旧ルートの `config.yaml` / `config.default.yaml` は **使用しません**（互換なし）

### 特徴

- 🤖 **マルチモデルAI対話** - OpenAI、Google Gemini、NVIDIA NIM、KoboldCPP など
- 🗣️ **debate / cross_check** - PLANA↔ARONA の多ラウンド討論＋評定、または軽量3ステップ検証
- 🎵 **音楽再生** - YouTube、Spotify、Google Drive など（両ボット）
- 🎨 **画像生成 / TTS / 通知 / tracker** - **PLANA 専用**
- 🎲 **ユーティリティ** - `/help`・`/invite`（Components V2）、ダイス、タイマーなど

---

## 主な機能

### 1. AI対話機能 (LLM)

`@PLANA` / `@ARONA` でメンションすると AI が応答します。

#### 対応モデル

- **OpenAI**: GPT-4o, GPT-4 Turbo
- **Google**: Gemini 系
- **NVIDIA NIM**: Kimi、Llama、DeepSeek R1 など
- **KoboldCPP**: ローカル LLM サーバー

#### 主な機能

- 画像認識（対応モデルの場合）
- 会話履歴
- Web 検索
- **debate** — 多ラウンドの PLANA↔ARONA 討論のあと評定ターン
- **cross_check** — PLANA案 → ARONA検証 → PLANA結論の軽量3ステップ（パネルなし）
- **自動APIキーローテーション** — 複数キーでレートリミット時に切り替え

### 2. 音楽再生機能

両ボットでボイスチャンネル再生が可能です（キュー・ループ・シャッフルなど）。

#### 対応ソース

- YouTube / Spotify / Google Drive / ニコニコ動画 / その他 yt-dlp 対応メディア

### 3. 画像生成機能（PLANA 専用）

**内製 diffusers エンジン**（外部サービス不要）。モデルは `models/image-models/` 配下。オプションで Stable Diffusion WebUI Forge API も利用可。

ARONA に画像生成を頼むと、PLANA への誘導メッセージが返ります。

### 4. 音声読み上げ (TTS)（PLANA 専用）

統合 [Style-Bert-VITS2](https://github.com/litagin02/Style-Bert-VITS2) エンジン。モデルは `models/tts-models/`。詳細は `NOTICE` を参照。

### 5. ゲーム統計追跡（PLANA 専用）

Rainbow Six Siege / VALORANT の統計表示。

### 6. 通知機能（PLANA 専用）

地震速報・Twitch 配信通知。

### 7. ユーティリティ

ダイス、タイマー、メディアダウンロード、サーバー/ユーザー情報、ガチャなど。`/help` と `/invite` は Components V2 で、両ボットの招待を案内します。

---

## セットアップ

### 必要な環境

- **Python 3.11.x**（必須。3.10 / 3.12 以降は非対応）
- Discord Bot Token（PLANA / ARONA 各1つ）
- 両方の Application で **Message Content Intent** を有効化
- 各種 API キー（利用機能に応じて）
- （任意）`youtube_cookie.txt`（Netscape 形式、プロジェクト直下）

### インストール手順

1. **リポジトリのクローン**
   ```bash
   git clone https://github.com/coffin399/ProjectMOMOKA.git
   cd ProjectMOMOKA
   ```

2. **設定ファイル**
   - 初回起動時、`configs/<category>_config.yaml` が無いカテゴリだけ `*_config.default.yaml` から自動コピーされます
   - 手動でコピーする場合の例:
     ```bash
     copy configs\bots_config.default.yaml configs\bots_config.yaml   # Windows
     cp configs/bots_config.default.yaml configs/bots_config.yaml     # Linux/Mac
     ```
   - **旧ルート `config.yaml` は読みません**

3. **必須の編集**
   - `configs/bots_config.yaml` — `bots.plana.token` / `bots.arona.token`
   - `configs/llm_config.yaml` — API キー

4. **ボットの招待**
   - [PLANA](https://discord.com/oauth2/authorize?client_id=1031673203774464160&permissions=551906765824&scope=bot)
   - [ARONA](https://discord.com/oauth2/authorize?client_id=1364917551024308255&permissions=551906765824&scope=bot)
   - debate / cross_check を使うサーバーには **両方** を入れてください

5. **起動**

   **Windows (推奨):**
   ```bash
   startMOMOKA.bat
   ```

   **手動:**
   ```bash
   py -3.11 -m venv .venv
   .venv\Scripts\activate  # Windows / source .venv/bin/activate  # Linux/Mac
   pip install -r requirements.txt
   python main.py
   ```

---

## 設定

設定は `configs/` 配下のカテゴリ別 YAML です。詳細なキーは各 `*_config.default.yaml` を参照してください。

| ファイル | 内容 |
|---------|------|
| `bots_config.yaml` | PLANA/ARONA の token・invite・role |
| `llm_config.yaml` | モデル・プロバイダ API キー・persona |
| `music_config.yaml` | 音量・キュー・Cookie パスなど |
| `tts_config.yaml` | TTS モデル（PLANA） |
| `images_config.yaml` | 画像生成（PLANA） |
| `notifications_config.yaml` | 地震・Twitch（PLANA） |
| `tracker_config.yaml` | ゲーム統計（PLANA） |
| `debate_config.yaml` | debate / cross_check |
| `utilities_config.yaml` | ユーティリティ |
| `core_config.yaml` | コア共通設定 |

#### ボット設定例（`bots_config.yaml`）

```yaml
bots:
  plana:
    token: YOUR_PLANA_BOT_TOKEN
  arona:
    token: YOUR_ARONA_BOT_TOKEN
```

#### LLM 設定例（`llm_config.yaml`）

```yaml
llm:
  model: "google/gemini-2.5-pro"
  providers:
    google:
      api_key1: YOUR_KEY
      api_key2: YOUR_KEY_2  # レートリミット時に自動切替
```

#### 画像生成（`images_config.yaml` / LLM 連携設定）

```yaml
# provider: "local" （内製）または "forge"
# モデル配置: models/image-models/<name>/<name>.safetensors
```

#### TTS（`tts_config.yaml`）

```yaml
tts:
  model_root: "models/tts-models"
  model_name: "your-model-name"
```

#### 音楽（`music_config.yaml`）

```yaml
music:
  default_volume: 20
  max_queue_size: 10000
  auto_leave_timeout: 3
```

---

## コマンド一覧

### AI対話 (LLM)

| コマンド | 説明 |
|---------|------|
| `@PLANA` / `@ARONA` `<メッセージ>` | メンションで AI 対話 |
| `/chat <メッセージ>` | メンションなしで対話 |
| `/clear_history` | 会話履歴リセット |
| `/switch-models` | チャンネル専用モデル切替 |

※ debate / cross_check は LLM ツールとして呼び出されます（多ラウンド討論＋評定 / 軽量3ステップ検証）。

### 音楽

| コマンド | 説明 |
|---------|------|
| `/play` `/pause` `/resume` `/stop` `/skip` | 再生制御 |
| `/seek` `/volume` `/queue` `/shuffle` `/clear` `/remove` `/nowplaying` | キュー・音量など |

### 画像生成（PLANA）

| コマンド | 説明 |
|---------|------|
| `@PLANA` で画像生成を依頼 | AI 経由で生成 |

### TTS（PLANA）

| コマンド | 説明 |
|---------|------|
| `/say <テキスト>` | 読み上げ |
| `/tts-help` | TTS ヘルプ |

### ゲーム統計（PLANA）

| コマンド | 説明 |
|---------|------|
| `/r6s` / `/valorant` | プレイヤー統計 |

### 通知（PLANA）

| コマンド | 説明 |
|---------|------|
| `/earthquake_*` | 地震速報設定・履歴など |
| `/twitch_add` `/twitch_remove` `/twitch_list` | Twitch 通知 |

### ユーティリティ

| コマンド | 説明 |
|---------|------|
| `/help` | ヘルプ（Components V2） |
| `/invite` | PLANA / ARONA 招待（Components V2） |
| `/ping` `/serverinfo` `/userinfo` `/avatar` | 情報系 |
| `/roll` `/diceroll` `/check` `/gacha` `/timer` `/meow` `/support` | その他 |

---

## 機能詳細

### debate / cross_check

- **debate**: PLANA と ARONA が交互に発言し、最後に評定ターンで要点と推奨を出します。同じギルドに両ボットが必要です
- **cross_check**: PLANA の案 → ARONA の検証 → PLANA の結論。3ステップすべてチャンネルに投稿。debate より気軽で停止パネルなし
- 各発言の文頭に相手ボットへのメンションが付きます

### APIキーローテーション

`llm_config.yaml` の各プロバイダに `api_key1`, `api_key2`, … を並べると、レートリミット／サーバーエラー時に次のキーへ自動切替します。

### 音楽

キュー最大 10,000 曲、ループ（OFF/ONE/ALL）、音量 0–200%、VC 空室時の自動退出に対応。

### 画像生成（PLANA）

`models/image-models/<name>/<name>.safetensors` を配置し、設定で `provider: "local"`（デフォルト）を使用。Forge 利用時は `--api` 付きで Forge を起動し `provider: "forge"` を設定。

### 地震速報（PLANA）

気象庁 WebSocket による緊急地震速報・地震情報・津波予報の通知。

---

## トラブルシューティング

### ボットが起動しない

1. `configs/bots_config.yaml` の `bots.plana.token` / `bots.arona.token` を確認
2. Python 3.11.x か確認
3. 依存パッケージがインストールされているか確認
4. ルートに旧 `config.yaml` だけ置いても読み込まれません — `configs/` を使ってください

### AIが応答しない

1. `configs/llm_config.yaml` の API キーを確認
2. Developer Portal で **Message Content Intent** が両ボット有効か確認
3. 使用モデルが利用可能か確認

### debate / cross_check が動かない

1. 同じサーバーに PLANA と ARONA の両方がいるか確認
2. `/invite` から不足しているボットを追加

### 音楽が再生されない

1. ボイスチャンネル接続・FFmpeg・`youtube_cookie.txt` を確認
2. **YouTube EJS**: Deno（推奨）または Node.js 22+ を PATH に入れ、`pip install -U "yt-dlp[default]"` を実行

### 画像生成ができない

1. PLANA 側で実行しているか（ARONA は誘導のみ）
2. モデル配置と `images` / LLM 画像設定を確認

### 地震速報が届かない

1. `/earthquake_status` で状態確認（PLANA）
2. 通知チャンネル設定・WebSocket 接続を確認

---

## サポート

- Discord: [https://discord.com/invite/H79HKKqx3s](https://discord.com/invite/H79HKKqx3s)
- `/support` コマンド

### ライセンス

- 本プロジェクト: **AGPL-3.0**
- Style-Bert-VITS2 統合部分: AGPL-3.0 / LGPL-3.0（`NOTICE` 参照）

---

**Made with ❤️ by the MOMOKA development team**
