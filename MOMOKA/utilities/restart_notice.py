# 管理者による再起動・シャットダウン時に、利用中ユーザーへ表示する共有文言

# /shutdown を実行できる Discord ユーザー ID（ハードコード）
SHUTDOWN_USER_ID = 270446628622696449

# LLM 応答生成中メッセージを上書きする本文
RESTART_NOTICE_TEXT = (
    "The bot has been restarted by an administrator for an update.\n"
    "For details, use /updates."
)

# 音楽 Now Playing（LayoutView）の終了表示用テキスト
RESTART_NOTICE_MUSIC = (
    "🔄 **Bot Restarted**\n"
    "The bot has been restarted by an administrator for an update.\n"
    "For details, use `/updates`."
)
