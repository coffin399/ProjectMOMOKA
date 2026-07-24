# MOMOKA GUI パッケージ（ログビューア / テーマ / バージョン）

from MOMOKA.GUI.bot_bridge import get_bot_ref, set_bot_ref
from MOMOKA.GUI.logging_bridge import (
    QueueHandler,
    StdoutCapture,
    attach_gui_logging,
    create_log_queue,
)
from MOMOKA.GUI.runner import run_log_viewer_thread
from MOMOKA.GUI.theme import get_theme_colors, is_dark_mode, set_dark_mode
from MOMOKA.GUI.version import APP_NAME, COPYRIGHT, LOG_VIEWER_NAME, VERSION

__all__ = [
    "APP_NAME",
    "COPYRIGHT",
    "LOG_VIEWER_NAME",
    "VERSION",
    "QueueHandler",
    "StdoutCapture",
    "attach_gui_logging",
    "create_log_queue",
    "get_bot_ref",
    "get_theme_colors",
    "is_dark_mode",
    "run_log_viewer_thread",
    "set_bot_ref",
    "set_dark_mode",
]
