# プロジェクト共通のバージョン定数・ビルド日付（最終 git コミット日）

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

# 製品名
APP_NAME = "MOMOKA"

# ログビューアの表示名
LOG_VIEWER_NAME = "MOMOKA ログビューア"

# セマンティックバージョン（About ダイアログ等）
VERSION = "1.0.0"

# コピーライト表示文言
COPYRIGHT = "© 2026 MOMOKA Project"

# Discord ステータス用プレフィックス
STATUS_VERSION_PREFIX = "prjMOMOKA Ver."

# git 取得失敗時のフォールバック日付（YYYY-MM-DD）
FALLBACK_BUILD_DATE = "2026-07-25"

# このファイルからリポジトリルート（MOMOKA/ の親）を解決する
_REPO_ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def get_build_date() -> str:
    """最終 git コミット日（YYYY-MM-DD）を返す。失敗時はフォールバック。"""
    try:
        # 最終コミットのコミッター日付（短縮 YYYY-MM-DD）を取得する
        result = subprocess.run(
            ["git", "log", "-1", "--format=%cs"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        # 成功かつ非空ならその日付を使う
        if result.returncode == 0:
            # 前後空白を除去する
            date_str = (result.stdout or "").strip()
            # 簡易形式チェック（YYYY-MM-DD）
            if len(date_str) == 10 and date_str[4] == "-" and date_str[7] == "-":
                # 有効なコミット日を返す
                return date_str
    except (OSError, subprocess.SubprocessError):
        # git 未導入・タイムアウト等はフォールバックへ
        pass
    # 取得できない場合の定数を返す
    return FALLBACK_BUILD_DATE


def status_version_string() -> str:
    """Discord ステータス用のバージョン文字列を返す。"""
    # プレフィックスとコミット日を結合する
    return f"{STATUS_VERSION_PREFIX}{get_build_date()}"
