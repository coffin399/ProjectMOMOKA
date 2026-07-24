# ダーク / ライトテーマ検出と色パレット、Windows ダークモード適用

import ctypes
import os
import platform


# ダークモードの色設定
DARK_BG = "#1e1e1e"
DARK_FG = "#e0e0e0"
DARK_SELECTION_BG = "#264f78"
DARK_SELECTION_FG = "#ffffff"
DARK_INSERT_BG = "#3c3c3c"
DARK_INSERT_FG = "#ffffff"
DARK_SCROLLBAR_BG = "#2d2d2d"
DARK_SCROLLBAR_TROUGH = "#1e1e1e"

# ライトモードの色設定
LIGHT_BG = "#f0f0f0"
LIGHT_FG = "#000000"
LIGHT_SELECTION_BG = "#cce8ff"
LIGHT_SELECTION_FG = "#000000"
LIGHT_INSERT_BG = "#ffffff"
LIGHT_INSERT_FG = "#000000"
LIGHT_SCROLLBAR_BG = "#e0e0e0"
LIGHT_SCROLLBAR_TROUGH = "#f0f0f0"


def is_dark_mode() -> bool:
    """OS のダークモード設定を検出する。"""
    try:
        # Windows のみ darkdetect で判定する
        if platform.system() == "Windows":
            # 遅延 import（未インストール環境でも起動可能にする）
            import darkdetect
            # OS がダークなら True
            return darkdetect.isDark()
        # 非 Windows はライト扱い
        return False
    except Exception as e:
        # 検出失敗時はライトにフォールバックする
        print(f"ダークモード検出エラー: {e}")
        # False = ライトテーマ
        return False


# 起動時に一度だけテーマを確定する（ウィンドウ生成前の参照用）
DARK_THEME = is_dark_mode()


def get_theme_colors() -> dict:
    """現在のテーマに応じた色辞書を返す。"""
    # ダークテーマ用パレットを返す
    if DARK_THEME:
        return {
            "bg": DARK_BG,
            "fg": DARK_FG,
            "select_bg": DARK_SELECTION_BG,
            "select_fg": DARK_SELECTION_FG,
            "insert_bg": DARK_INSERT_BG,
            "insert_fg": DARK_INSERT_FG,
            "scrollbar_bg": DARK_SCROLLBAR_BG,
            "scrollbar_trough": DARK_SCROLLBAR_TROUGH,
            "button_bg": "#2d2d2d",
            "button_fg": DARK_FG,
            "button_active_bg": "#3c3c3c",
            "button_active_fg": DARK_FG,
            "frame_bg": DARK_BG,
            "label_bg": DARK_BG,
            "label_fg": DARK_FG,
            "entry_bg": DARK_INSERT_BG,
            "entry_fg": DARK_INSERT_FG,
            "text_bg": DARK_INSERT_BG,
            "text_fg": DARK_INSERT_FG,
            "border": "#3c3c3c",
            "error": "#ff6b6b",
            "warning": "#ffd93d",
            "info": "#4dabf7",
            "debug": "#adb5bd",
        }
    # ライトテーマ用パレットを返す
    return {
        "bg": LIGHT_BG,
        "fg": LIGHT_FG,
        "select_bg": LIGHT_SELECTION_BG,
        "select_fg": LIGHT_SELECTION_FG,
        "insert_bg": LIGHT_INSERT_BG,
        "insert_fg": LIGHT_INSERT_FG,
        "scrollbar_bg": LIGHT_SCROLLBAR_BG,
        "scrollbar_trough": LIGHT_SCROLLBAR_TROUGH,
        "button_bg": "#e0e0e0",
        "button_fg": LIGHT_FG,
        "button_active_bg": "#d0d0d0",
        "button_active_fg": LIGHT_FG,
        "frame_bg": LIGHT_BG,
        "label_bg": LIGHT_BG,
        "label_fg": LIGHT_FG,
        "entry_bg": LIGHT_INSERT_BG,
        "entry_fg": LIGHT_INSERT_FG,
        "text_bg": LIGHT_INSERT_BG,
        "text_fg": LIGHT_INSERT_FG,
        "border": "#c0c0c0",
        "error": "#dc3545",
        "warning": "#ffc107",
        "info": "#0d6efd",
        "debug": "#6c757d",
    }


def set_dark_mode() -> None:
    """Windows のプロセス DPI / ダークモードを有効化する。"""
    try:
        # Windows 以外は何もしない
        if os.name != "nt":
            return
        # DPI 認識を有効化する（ぼやけ防止）
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        try:
            # OS テーマ検出ライブラリを読む
            import darkdetect
            # ダークでなければタイトルバー変更は不要
            if not darkdetect.isDark():
                return
            # DWM 属性定数（イマーシブダークモード）
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            # 前面ウィンドウの HWND を取得する
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            # ダークモード ON の値
            value = 1
            # タイトルバーをダークにする
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(ctypes.c_int(value)),
                ctypes.sizeof(ctypes.c_int(value)),
            )
        except ImportError:
            # darkdetect 未導入ならスキップする
            pass
    except Exception as e:
        # GUI 起動を止めないためログのみ出す
        print(f"ダークモードの設定中にエラーが発生しました: {e}")


def apply_windows_dark_mode_to_foreground() -> None:
    """前面ウィンドウへ Windows ダークモード属性を適用する。"""
    # ダークテーマでない、または非 Windows なら何もしない
    if not DARK_THEME or platform.system() != "Windows":
        return
    try:
        # DWM 属性定数
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        # 前面ウィンドウを対象にする
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        # ダーク ON
        value = 1
        # 属性を書き込む
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(ctypes.c_int(value)),
            ctypes.sizeof(ctypes.c_int(value)),
        )
    except Exception as e:
        # 失敗してもビューアは継続する
        print(f"ダークモードの適用中にエラーが発生しました: {e}")
