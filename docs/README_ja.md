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

**MOMOKA**は、Discordサーバー向けの多機能ボットです。AI対話、音楽再生、画像生成、通知機能など、様々な機能を1つのボットに統合しています。

### 特徴

- 🤖 **マルチモデルAI対話** - OpenAI、Google Gemini、NVIDIA NIM、KoboldCPPなど複数のAIプロバイダーに対応
- 🎵 **高機能音楽再生** - YouTube、Spotify、Google Driveなどから音楽を再生
- 🎨 **画像生成** - Stable Diffusionを使用した画像生成
- 🗣️ **音声読み上げ** - Style-Bert-VITS2を使用したTTS機能
- 📊 **ゲーム統計追跡** - Rainbow Six Siege、VALORANTの統計を表示
- 🔔 **リアルタイム通知** - 地震速報、Twitch配信通知
- 🎲 **便利なユーティリティ** - ダイスロール、タイマー、メディアダウンロードなど

---

## 主な機能

### 1. AI対話機能 (LLM)

ボットをメンションすると、AIが応答します。複数のAIモデルに対応しており、画像認識も可能です。

#### 対応モデル

- **OpenAI**: GPT-4o, GPT-4 Turbo
- **Google**: Gemini 2.5 Flash, Gemini 2.5 Pro
- **NVIDIA NIM**: Kimi、Llama、DeepSeek R1など
- **KoboldCPP**: ローカルLLMサーバーに対応

#### 主な機能

- 画像認識（対応モデルの場合）
- 会話履歴の管理
- ユーザー情報の記憶（`/set-user-bio`）
- グローバルメモリ機能（`/memory-save`）
- 画像生成ツール連携
- Web検索機能
- **🔄 自動APIキーローテーション** - 複数のAPIキーを設定することで、レートリミットエラーやサーバーエラー時に自動的に次のAPIキーに切り替え。高負荷時でも安定して動作します

### 2. 音楽再生機能

ボイスチャンネルで音楽を再生できます。キュー管理、ループ、シャッフルなどの機能を提供します。

#### 対応ソース

- YouTube
- Spotify
- Google Drive
- ニコニコ動画
- その他yt-dlpが対応するメディア

#### 主な機能

- キュー管理（最大10,000曲）
- ループ再生（1曲/全体）
- シャッフル
- 音量調整（0-200%）
- シーク（指定時刻に移動）
- プレイリスト対応

### 3. 画像生成機能

Stable Diffusionを使用して画像を生成できます。WebUI ForgeまたはKoboldCPPと連携します。

#### 主な機能

- プロンプトに基づく画像生成
- 複数のモデル切り替え
- カスタムパラメータ設定
- ネガティブプロンプト対応

### 4. 音声読み上げ機能 (TTS)

Style-Bert-VITS2を使用してテキストを音声に変換します。

#### 主な機能

- 複数のモデル対応
- スタイル設定
- 話速・音量調整
- 参加/退出通知の自動読み上げ

### 5. ゲーム統計追跡

#### Rainbow Six Siege

- プレイヤー統計の表示
- ランク情報
- マッチ履歴

#### VALORANT

- プレイヤー統計の表示
- ランク情報
- マッチ履歴

### 6. 通知機能

#### 地震速報

- 気象庁の緊急地震速報をリアルタイムで通知
- WebSocket接続による高速通知
- 地震情報の地図表示
- 履歴表示

#### Twitch通知

- 指定チャンネルの配信開始を通知
- 配信情報の表示

### 7. ユーティリティ機能

- ダイスロール（nDn形式、範囲指定）
- 条件判定付きダイスロール
- タイマー機能
- メディアダウンロード
- サーバー/ユーザー情報表示
- ガチャシミュレーション（ブルーアーカイブ風）

---

## セットアップ

### 必要な環境

- Python 3.8以上
- Discord Bot Token
- 各種APIキー（使用する機能に応じて）

### インストール手順

1. **リポジトリのクローン**
   ```bash
   git clone <repository-url>
   cd ProjectMOMOKA
   ```

2. **仮想環境の作成（推奨）**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   source .venv/bin/activate  # Linux/Mac
   ```

3. **依存パッケージのインストール**
   ```bash
   pip install -r requirements.txt
   ```

4. **設定ファイルの準備**
   ```bash
   copy config.default.yaml config.yaml  # Windows
   cp config.default.yaml config.yaml  # Linux/Mac
   ```

5. **設定ファイルの編集**
   - `config.yaml`を開いて、必要なAPIキーを設定してください

6. **ボットの起動**
   ```bash
   python main.py
   ```
   または、Windowsの場合は：
   ```bash
   startMOMOKA.bat
   ```

---

## 設定

### 基本設定

`config.yaml`で以下の設定が可能です：

#### ボット設定

```yaml
bot_token: YOUR_BOT_TOKEN_HERE
client_id: YOUR_CLIENT_ID_HERE
admin_user_ids:
  - 123456789012345678
```

#### LLM設定

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
      api_key2: YOUR_KEY_2  # 複数のAPIキーを設定可能
      api_key3: YOUR_KEY_3  # レートリミット時に自動的に次のキーに切り替え
    google:
      base_url: https://generativelanguage.googleapis.com/v1beta/
      api_key1: YOUR_KEY
      api_key2: YOUR_KEY_2
      api_key3: YOUR_KEY_3
      api_key4: YOUR_KEY_4
      api_key5: YOUR_KEY_5  # 最大5つまで設定可能（必要に応じて追加可能）
```

**💡 APIキーローテーション機能:**
- `api_key1`, `api_key2`, `api_key3`... のように複数のAPIキーを設定できます
- レートリミットエラーやサーバーエラーが発生した場合、自動的に次のAPIキーに切り替えます
- すべてのキーが失敗するまで、自動的にリトライします
- 高負荷時でも安定した動作を実現します

#### 画像生成設定

```yaml
llm:
  image_generator:
    forge_url: "http://127.0.0.1:7860"
    model: "sd_xl_base_1.0.safetensors"
    default_size: "1024x1024"
```

#### 音楽設定

```yaml
music:
  default_volume: 20
  max_queue_size: 10000
  auto_leave_timeout: 3
```

#### TTS設定

```yaml
tts:
  api_server_url: "http://127.0.0.1:5000"
  default_model_id: 0
  default_style: "Neutral"
```

詳細な設定オプションは`config.default.yaml`を参照してください。

---

## コマンド一覧

### AI対話機能 (LLM)

| コマンド | 説明 |
|---------|------|
| `@MOMOKA <メッセージ>` | ボットをメンションしてAIと対話 |
| `/chat <メッセージ>` | メンションなしでAIと対話 |
| `/llm_help` | LLM機能のヘルプ表示 |
| `/set-user-bio <情報>` | ユーザー情報を設定 |
| `/show-user-bio` | 保存されたユーザー情報を表示 |
| `/reset-user-bio` | ユーザー情報を削除 |
| `/memory-save <情報>` | グローバルメモリに保存 |
| `/memory-list` | グローバルメモリの一覧表示 |
| `/memory-delete <ID>` | グローバルメモリから削除 |
| `/clear_history` | 会話履歴をリセット |
| `/switch-models` | チャンネル専用のモデルを切り替え |

### 音楽再生機能

| コマンド | 説明 |
|---------|------|
| `/play <曲名またはURL>` | 曲を再生/キューに追加 |
| `/pause` | 再生を一時停止 |
| `/resume` | 再生を再開 |
| `/stop` | 再生を停止し、キューをクリア |
| `/skip` | 現在の曲をスキップ |
| `/seek <時刻>` | 指定時刻に移動（例: `1:30`） |
| `/volume <0-200>` | 音量を変更 |
| `/queue` | キューを表示 |
| `/shuffle` | キューをシャッフル |
| `/clear` | キューをクリア |
| `/remove <番号>` | キューから曲を削除 |
| `/nowplaying` | 現在再生中の曲を表示 |
| `/music_help` | 音楽機能のヘルプ表示 |

### 画像生成機能

| コマンド | 説明 |
|---------|------|
| `@MOMOKA 画像を生成して` | AIに画像生成を依頼 |
| `/image-generate` | 直接画像生成（実装されている場合） |

### TTS機能

| コマンド | 説明 |
|---------|------|
| `/say <テキスト>` | テキストを音声で読み上げ |
| `/tts-help` | TTS機能のヘルプ表示 |

### ゲーム統計追跡

#### Rainbow Six Siege

| コマンド | 説明 |
|---------|------|
| `/r6s <ユーザー名>` | プレイヤー統計を表示 |

#### VALORANT

| コマンド | 説明 |
|---------|------|
| `/valorant <ユーザー名>` | プレイヤー統計を表示 |

### 通知機能

#### 地震速報

| コマンド | 説明 |
|---------|------|
| `/earthquake_channel <チャンネル>` | 通知チャンネルを設定 |
| `/earthquake_remove <種類>` | 通知設定を削除 |
| `/earthquake_test` | テスト通知を送信 |
| `/earthquake_status` | システム状態を確認 |
| `/earthquake_history` | 最近の地震履歴を表示 |
| `/earthquake_map` | 地震を地図上に表示 |
| `/earthquake_help` | ヘルプを表示 |

#### Twitch通知

| コマンド | 説明 |
|---------|------|
| `/twitch_add <ユーザー名>` | 通知対象チャンネルを追加 |
| `/twitch_remove <ユーザー名>` | 通知対象チャンネルを削除 |
| `/twitch_list` | 通知対象一覧を表示 |

### ユーティリティコマンド

| コマンド | 説明 |
|---------|------|
| `/help` | ボットのヘルプ情報を表示 |
| `/ping` | ボットの応答速度を確認 |
| `/serverinfo` | サーバー情報を表示 |
| `/userinfo [ユーザー]` | ユーザー情報を表示 |
| `/avatar [ユーザー]` | アバター画像を表示 |
| `/invite` | ボットの招待リンクを表示 |
| `/meow` | ランダムな猫の画像を表示 |
| `/roll <表記>` | nDn形式でダイスロール（例: `2d6+3`） |
| `/diceroll <最小値> <最大値>` | 指定範囲でダイスロール |
| `/check <表記> [条件] [目標値]` | ダイスロールと条件判定 |
| `/gacha` | ブルーアーカイブ風ガチャ |
| `/timer <時間> <メッセージ>` | タイマーを設定 |
| `/support` | 開発者への連絡方法を表示 |

---

## 機能詳細

### AI対話機能の詳細

#### メンション方式

ボットをメンション（`@MOMOKA`）してからメッセージを送信すると、AIが応答します。

#### 画像認識

対応モデルを使用している場合、メッセージに添付された画像を認識できます。

#### 会話履歴

各チャンネルごとに会話履歴が保持されます。`/clear_history`でリセットできます。

#### ユーザー情報

`/set-user-bio`で設定した情報は、そのユーザーとの会話で参照されます。

#### グローバルメモリ

全サーバー共通のメモリに情報を保存できます。ボットの設定や共通情報を保存するのに便利です。

#### APIキーローテーション

MOMOKAは、複数のAPIキーを設定することで、レートリミットやサーバーエラーに対する堅牢な対応を実現しています。

**動作メカニズム:**
1. 設定ファイル（`config.yaml`）で`api_key1`, `api_key2`, `api_key3`...のように複数のAPIキーを設定できます
2. デフォルトでは最初のAPIキー（`api_key1`）が使用されます
3. レートリミットエラー（`RateLimitError`）やサーバーエラー（`InternalServerError`）が発生した場合、自動的に次のAPIキーに切り替えます
4. すべてのAPIキーが試されるまで、自動的にリトライします
5. すべてのキーが失敗した場合のみ、エラーを返します

**メリット:**
- **高可用性**: 1つのAPIキーがレートリミットに達しても、他のキーでサービスを継続
- **負荷分散**: 複数のキーを使用することで、単一キーの負荷を分散
- **自動復旧**: エラー発生時に手動介入なしで自動的に切り替え
- **設定の柔軟性**: 各プロバイダーごとに異なる数のAPIキーを設定可能

**使用例:**
```yaml
providers:
  google:
    api_key1: "key1"
    api_key2: "key2"
    api_key3: "key3"
    api_key4: "key4"
    api_key5: "key5"
```

この設定により、Google Gemini APIを使用する際に、5つのAPIキーが自動的にローテーションされます。

### 音楽再生機能の詳細

#### キュー管理

最大10,000曲までキューに追加できます。

#### ループモード

- **OFF**: ループなし
- **ONE**: 現在の曲をループ
- **ALL**: キュー全体をループ

#### 音量調整

0-200%の範囲で音量を調整できます。デフォルトは20%です。

#### 自動退出

ボイスチャンネルが空になった場合、設定された時間（デフォルト3分）後に自動的に退出します。

### 画像生成機能の詳細

#### Stable Diffusion WebUI Forge連携

Forge WebUIを起動している場合、ボットから画像生成を依頼できます。

#### プロンプト

AIとの対話で「画像を生成して」などと依頼すると、AIがプロンプトを生成して画像を生成します。

### 地震速報機能の詳細

#### WebSocket接続

気象庁のWebSocketサーバーに接続し、リアルタイムで地震情報を取得します。

#### 通知タイプ

- **緊急地震速報**: 最大震度5弱以上が予想される場合
- **地震情報**: 震度1以上の地震発生時
- **津波予報**: 津波が予想される場合

---

## トラブルシューティング

### ボットが起動しない

1. `config.yaml`に`bot_token`が正しく設定されているか確認
2. Pythonのバージョンが3.8以上か確認
3. 必要なパッケージがインストールされているか確認

### AIが応答しない

1. `config.yaml`のLLM設定を確認
2. APIキーが正しく設定されているか確認
3. 使用しているモデルが利用可能か確認

### 音楽が再生されない

1. ボットがボイスチャンネルに接続されているか確認
2. 音声ファイルの権限を確認
3. FFmpegがインストールされているか確認（yt-dlpが使用する場合）

### 画像生成ができない

1. Stable Diffusion WebUI Forgeが起動しているか確認
2. `config.yaml`の`forge_url`が正しいか確認
3. モデルが正しくロードされているか確認

### 地震速報が届かない

1. `/earthquake_status`でシステム状態を確認
2. 通知チャンネルが正しく設定されているか確認
3. WebSocket接続が確立されているか確認

---

## サポート

問題が発生した場合、以下の方法でサポートを受けられます：

- Discordサポートサーバー: [リンク]
- 開発者への連絡: `/support`コマンドで表示

---

**Made with ❤️ by the MOMOKA development team**

