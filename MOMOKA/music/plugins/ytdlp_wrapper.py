# MOMOKA/music/ytdlp_wrapper.py
from __future__ import annotations

import asyncio
import random
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Union, Optional

import logging
import yt_dlp
# yt-dlpの出力ログをMOMOKAのログシステムに統合するためのロガーを設定する
logger = logging.getLogger("MOMOKA.music.plugins.ytdlp")
from yt_dlp.utils import ExtractorError  # 個別のエラーをキャッチするため


class UnsupportedMediaError(RuntimeError):
    """DRM / 非対応サイトなど、再試行しても取得できないメディア向け例外。"""


# Trackクラス定義
@dataclass
class Track:
    # 音源または動画の元のURL
    url: str
    # 楽曲のタイトル
    title: str
    # 楽曲の長さ（秒単位）
    duration: int
    # サムネイル画像のURL（無い場合はNone）
    thumbnail: Optional[str] = None
    # 再生用の一時ストリームURL（無い場合はNone）
    stream_url: Optional[str] = None
    # リクエストを送信したユーザーのID（無い場合はNone）
    requester_id: Optional[int] = None
    # ユーザーが入力した元の検索クエリ（無い場合はNone）
    original_query: Optional[str] = None
    # アップローダーまたはチャンネル名（無い場合はNone）
    uploader: Optional[str] = None
    # 音声の取得に必要なHTTPヘッダー情報（User-Agentなど、無い場合はNone）
    http_headers: Optional[dict] = None


# --- yt-dlp 設定 ---
CACHE_DIR = Path("./cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

NICO_COOKIE_PATH = Path("./nico_cookies.txt")
# ニコニコ動画用クッキーファイルが存在しない場合は作成する
if not NICO_COOKIE_PATH.exists():
    # 空ファイルを作成する
    NICO_COOKIE_PATH.touch(exist_ok=True)

# YouTube用クッキーの候補パス（ユーザー指定名を優先し、旧名も互換として残す）
_YOUTUBE_COOKIE_CANDIDATES = (
    Path("./youtube_cookie.txt"),
    Path("./youtube_cookies.txt"),
)
# config から上書きされる任意のクッキーパス（未設定時は None）
_youtube_cookie_override: Optional[Path] = None


def set_youtube_cookie_path(path: Optional[str]) -> None:
    """config の youtube_cookie_file をモジュール設定へ反映する"""
    # モジュール全体で共有する上書きパスを更新するため global を宣言する
    global _youtube_cookie_override
    # 空文字や None の場合は上書きを解除する
    if not path:
        # 上書きパスをクリアする
        _youtube_cookie_override = None
        # 設定解除をログに残す
        logger.info("YouTube cookie path override cleared; using auto-detect.")
        # 処理を終了する
        return
    # 相対/絶対パスを Path オブジェクトへ変換する
    _youtube_cookie_override = Path(path)
    # 設定内容をログへ出力する
    logger.info("YouTube cookie path override set to: %s", _youtube_cookie_override.resolve())


def resolve_youtube_cookie_path() -> Optional[Path]:
    """
    利用可能な YouTube クッキーファイルを解決する。
    優先順: config 上書き → youtube_cookie.txt → youtube_cookies.txt
    空ファイルは無効扱いとする。
    """
    # 探索対象のパス一覧を組み立てる（上書きがあれば先頭へ）
    candidates = []
    # config 上書きが指定されているか判定する
    if _youtube_cookie_override is not None:
        # 上書きパスを最優先候補へ追加する
        candidates.append(_youtube_cookie_override)
    # 既定の候補パスを続けて追加する
    candidates.extend(_YOUTUBE_COOKIE_CANDIDATES)

    # 重複を除きつつ候補を順に検査する
    seen = set()
    # 各候補パスを走査する
    for path in candidates:
        # 解決済みの絶対パス文字列をキーにする
        key = str(path.resolve()) if path.exists() else str(path)
        # 既に検査済みならスキップする
        if key in seen:
            # 次の候補へ進む
            continue
        # 検査済み集合へ登録する
        seen.add(key)
        # ファイルが存在し、かつサイズが 0 より大きいか判定する
        try:
            # 実ファイルかつ非空のみ有効とする
            if path.is_file() and path.stat().st_size > 0:
                # 有効なクッキーファイルを返す
                return path
        # 権限エラー等で stat に失敗した場合のハンドリング
        except OSError as e:
            # 警告を出して次の候補へ進む
            logger.warning("Failed to inspect YouTube cookie file %s: %s", path, e)
    # 有効なファイルが無ければ None を返す
    return None


def _apply_youtube_cookie(opts: dict) -> dict:
    """yt-dlp オプションへ YouTube クッキーを注入する（破壊的更新）"""
    # 有効なクッキーファイルを解決する
    cookie_path = resolve_youtube_cookie_path()
    # クッキーが解決できたか判定する
    if cookie_path is not None:
        # cookiefile オプションへ絶対パスを設定する（cwd 依存を避ける）
        opts["cookiefile"] = str(cookie_path.resolve())
        # 読み込み成功を INFO ログへ残す
        logger.info("Using YouTube cookiefile: %s", cookie_path.resolve())
    # 更新後のオプション辞書を返す
    return opts


# YouTube SABR 対策: format 候補（* はメタデータ不完全な形式も含める）
_FORMAT_TRIES: tuple[str, ...] = (
    "bestaudio*/best*",
    "bestaudio/best",
    "best[acodec!=none]/best*",
    "best*",
)
# player_client 候補（None は yt-dlp デフォルトに任せる）
# tv_embedded は現行 yt-dlp で unsupported（Skipping unsupported client）
# tv/android は DRM・SABR 実験で https 形式が欠けることがあるため android_vr 等を優先
_YOUTUBE_PLAYER_CLIENT_TRIES: tuple[Optional[list[str]], ...] = (
    ["android_vr", "tv", "web_embedded"],
    ["mweb", "ios", "web_creator"],
    ["tv_downgraded", "android"],
    None,
)


def _detect_js_runtimes() -> dict:
    """
    PATH 上の JS ランタイムを検出し、yt-dlp の js_runtimes 辞書を返す。
    deno を優先し、無ければ node を有効化する。
    """
    # 有効化するランタイム設定を格納する
    runtimes: dict = {}
    # Deno が PATH にあれば最優先で有効化する（yt-dlp 推奨）
    if shutil.which("deno"):
        # deno を空設定で有効化する
        runtimes["deno"] = {}
    # Node.js があればフォールバックとして有効化する
    if shutil.which("node"):
        # node を空設定で有効化する
        runtimes["node"] = {}
    # どちらも無い場合は deno をデフォルト指定のまま残す（未インストール警告は別途出す）
    if not runtimes:
        # yt-dlp 既定と同じく deno キーだけ置く
        runtimes["deno"] = {}
    # 検出結果を返す
    return runtimes


def _log_ejs_readiness() -> None:
    """YouTube EJS（JS チャレンジ解決）の準備状況をログへ出す。"""
    # Deno / Node の有無を確認する
    has_deno = bool(shutil.which("deno"))
    has_node = bool(shutil.which("node"))
    # 準備 OK の場合
    if has_deno or has_node:
        # 利用可能なランタイムを INFO で残す
        logger.info(
            "YouTube EJS: JS runtime available (deno=%s, node=%s)",
            has_deno,
            has_node,
        )
        # 処理を終了する
        return
    # ランタイムが無い場合は再生失敗の主因になり得るため強く警告する
    logger.warning(
        "YouTube EJS: No JS runtime found on PATH. "
        "Install Deno (recommended: https://deno.land) or Node.js 22+, "
        "then restart the bot. See https://github.com/yt-dlp/yt-dlp/wiki/EJS"
    )


# モジュール読込時に EJS 準備状況を一度だけログする
_log_ejs_readiness()


def apply_youtube_ejs_opts(opts: dict) -> dict:
    """
    任意の yt-dlp オプションへ YouTube EJS 設定を注入する。
    メディアダウンローダー等、COMMON_YTDL_OPTS を使わない箇所向け。
    """
    # 呼び出し元の辞書を壊さないようコピーする
    merged = opts.copy()
    # JS ランタイムが未指定なら検出結果を入れる
    merged.setdefault("js_runtimes", _detect_js_runtimes())
    # EJS リモート取得が未指定なら GitHub を許可する
    merged.setdefault("remote_components", ["ejs:github"])
    # 注入後のオプションを返す
    return merged


def is_youtube_media_url(url: Optional[str]) -> bool:
    """YouTube / googlevideo の URL かどうかを判定する。"""
    # 空なら False
    if not url:
        return False
    # 小文字化して判定する
    lower = url.lower()
    # YouTube 関連ホストを含むか返す
    return any(
        host in lower
        for host in (
            "youtube.com",
            "youtu.be",
            "googlevideo.com",
            "youtube-nocookie.com",
        )
    )


# yt-dlp KnownDRMIE と同等の主要ホスト（再試行・ログ汚染を事前に防ぐ）
_KNOWN_DRM_HOST_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"open\.spotify\.com",
        r"music\.amazon\.",
        r"deezer\.com",
        r"tv\.apple\.com",
        r"primevideo\.com",
        r"(?:[\w.]+\.)?disneyplus\.com",
        r"hulu\.com",
        r"netflix\.com",
        r"paramountplus\.com",
        r"(?:beta\.)?crunchyroll\.com",
        r"play\.hbomax\.com",
        r"peacocktv\.com",
        r"video\.unext\.jp",
        r"fod\.fujitv\.co\.jp",
        r"tv\.rakuten\.co\.jp",
        r"www\.web\.nhk",
    )
)

# DRM / 非対応サイトの例外メッセージに含まれる判定用キーワード
_NON_RETRYABLE_EXTRACTOR_MARKERS: tuple[str, ...] = (
    "DRM protection",
    "known to use DRM",
    "primarily used for piracy",
    "will not be supported",
    "is not supported and will not be supported",
)


def is_known_drm_url(url_or_query: Optional[str]) -> bool:
    """yt-dlp が DRM 非対応とみなす主要サイト URL かどうか。"""
    # 空入力は対象外とする
    if not url_or_query:
        # DRM URL ではない
        return False
    # ホスト照合のため小文字化する
    lower = url_or_query.lower()
    # 既知 DRM ホストパターンのいずれかに一致するか返す
    return any(pattern.search(lower) for pattern in _KNOWN_DRM_HOST_PATTERNS)


def _is_non_retryable_extractor_error(error: BaseException) -> bool:
    """format / player_client 再試行しても意味がない抽出エラーか判定する。"""
    # 例外メッセージを文字列化する
    message = str(error)
    # DRM / 非対応サイト系の文言を含むか返す
    return any(marker in message for marker in _NON_RETRYABLE_EXTRACTOR_MARKERS)


def _raise_unsupported_media(url_or_query: str, cause: Optional[BaseException] = None) -> None:
    """ユーザー向けの DRM / 非対応メッセージ付き例外を送出する。"""
    # Discord 等に出す短い説明文を組み立てる
    message = (
        "この URL は DRM 保護または非対応サイトのため再生できません。"
        " YouTube / ニコニコ動画などの対応 URL、または曲名検索を使ってください。"
        f" (query: {url_or_query})"
    )
    # 原因例外がある場合はチェーンして送出する
    if cause is not None:
        # 元例外を保持したまま送出する
        raise UnsupportedMediaError(message) from cause
    # 原因が無い場合はそのまま送出する
    raise UnsupportedMediaError(message)


def build_ytdlp_pipe_command(webpage_url: str) -> list[str]:
    """
    FFmpeg へ標準出力パイプするための yt-dlp CLI コマンドを組み立てる。
    直接 googlevideo URL を FFmpeg に渡すと 403 になるため、yt-dlp 経由で取得する。
    """
    # 現在の Python で yt_dlp モジュールを起動する
    cmd: list[str] = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--quiet",
        "--no-warnings",
        "--no-playlist",
        "--no-part",
        "-f",
        "bestaudio*/best*",
        "-o",
        "-",
        "--remote-components",
        "ejs:github",
        "--extractor-args",
        "youtube:player_client=android_vr,tv,web_embedded,mweb",
    ]
    # 有効なクッキーがあれば付与する
    cookie_path = resolve_youtube_cookie_path()
    if cookie_path is not None:
        # --cookies に絶対パスを渡す
        cmd.extend(["--cookies", str(cookie_path.resolve())])
    # 対象の動画ページ URL を末尾に追加する
    cmd.append(webpage_url)
    # 完成したコマンドリストを返す
    return cmd


def _extract_once(opts: dict, url: str, *, download: bool):
    """単発の yt-dlp 抽出。成功時は (info, opts, cookiejar) を返す。"""
    # コンテキストマネージャで yt-dlp を初期化する
    with yt_dlp.YoutubeDL(opts) as ytdl:
        # 指定 URL からメタデータを抽出する
        info = ytdl.extract_info(url, download=download)
        # ニコニコ等でクッキーを保存する必要がある場合に備え cookiejar を退避する
        cookiejar = getattr(ytdl, "cookiejar", None)
    # 抽出結果と実際に使ったオプション、cookiejar を返す
    return info, opts, cookiejar


def _entry_has_playable_url(info: Optional[dict]) -> bool:
    """抽出結果に再生可能な URL があるか判定する。"""
    # 情報が無い場合は再生不可とする
    if not info:
        # 再生不可
        return False
    # プレイリストなら先頭エントリ、単体なら info 自体を対象にする
    entry = info.get("entries")[0] if info.get("_type") == "playlist" and info.get("entries") else info
    # エントリが空なら再生不可
    if not entry:
        # 再生不可
        return False
    # 直接 URL、または結合前の requested_formats があれば再生可能とみなす
    return bool(entry.get("url") or entry.get("requested_formats") or entry.get("formats"))


def _extract_with_fallbacks(opts: dict, url: str, *, download: bool, resolve_stream: bool = False):
    """
    yt-dlp で情報抽出する。
    YouTube SABR 等で「Requested format is not available」になる場合に備え、
    format / player_client / cookie の組み合わせで再試行する。
    """
    # DRM 既知サイトは再試行しても無意味なので即時拒否する
    if is_known_drm_url(url):
        # ユーザー向け例外を送出する
        _raise_unsupported_media(url)
    # 最後に発生した例外を保持する
    last_error: Optional[BaseException] = None
    # 元オプションにクッキー指定があるか判定する
    has_cookies = ("cookiefile" in opts) or ("cookiesfrombrowser" in opts)
    # クッキー有り→無しの順で試す（指定が無ければ無しのみ）
    cookie_modes = [True, False] if has_cookies else [False]
    # ストリーム解決時はフォーマット候補を多めに、検索時は軽量に試す
    format_tries = _FORMAT_TRIES if resolve_stream else (_FORMAT_TRIES[0],)
    # まず「現在の opts そのまま」を1回試し、失敗時のみフォールバックする
    primary_attempts: list[tuple[str, Optional[list[str]], bool]] = []
    # プライマリ: 呼び出し元 format + 呼び出し元 client + クッキー有り
    primary_fmt = opts.get("format") or _FORMAT_TRIES[0]
    primary_clients: Optional[list[str]] = None
    # 既存 extractor_args から player_client を取り出す
    existing_args = opts.get("extractor_args") or {}
    existing_yt = existing_args.get("youtube") or {}
    if existing_yt.get("player_client"):
        # 既存指定をプライマリクライアントとする
        primary_clients = list(existing_yt["player_client"])
    # プライマリ試行を先頭に入れる
    primary_attempts.append((primary_fmt, primary_clients, has_cookies))
    # フォールバック候補を追加する（重複は後でスキップ）
    for use_cookies in cookie_modes:
        for clients in _YOUTUBE_PLAYER_CLIENT_TRIES:
            for fmt in format_tries:
                primary_attempts.append((fmt, clients, use_cookies))

    # 試行済みキーを記録して重複実行を避ける
    seen: set[tuple] = set()
    # 各候補を順に試す
    for fmt, clients, use_cookies in primary_attempts:
        # 重複キーを作る
        key = (fmt, tuple(clients) if clients else None, use_cookies)
        # 既に試していればスキップする
        if key in seen:
            # 次へ進む
            continue
        # 試行済みに登録する
        seen.add(key)
        # 試行用にオプションをコピーする
        trial = opts.copy()
        # フォーマット指定を上書きする
        trial["format"] = fmt
        # 再試行を速くするためスリープを無効化する
        trial["sleep_interval"] = 0
        trial["max_sleep_interval"] = 0
        trial["sleep_interval_requests"] = 0
        # ストリーム解決では失敗を握りつぶさず例外で検知する
        if resolve_stream:
            # ignoreerrors を無効化して None 返却を防ぐ
            trial["ignoreerrors"] = False
            # フラット展開を無効化して実 URL を得る
            trial["extract_flat"] = False
        # player_client を指定する場合
        if clients:
            # extractor_args に YouTube クライアント列を設定する
            trial["extractor_args"] = {"youtube": {"player_client": list(clients)}}
        else:
            # デフォルトクライアントに任せるため明示指定を外す
            trial.pop("extractor_args", None)
        # クッキー無しモードなら関連キーを除去する
        if not use_cookies:
            # cookiefile を除去する
            trial.pop("cookiefile", None)
            # cookiesfrombrowser を除去する
            trial.pop("cookiesfrombrowser", None)
        try:
            # 単発抽出を実行する
            info, used_opts, cookiejar = _extract_once(trial, url, download=download)
            # 再生可能な URL が取れたか判定する
            if _entry_has_playable_url(info):
                # 成功した組み合わせを INFO ログへ残す
                logger.info(
                    "yt-dlp extract OK: format=%s clients=%s cookies=%s url=%s",
                    fmt,
                    clients or "default",
                    use_cookies,
                    url,
                )
                # 成功結果を返す
                return info, used_opts, cookiejar
            # URL が無い場合は警告して次候補へ進む
            logger.warning(
                "yt-dlp returned no playable URL (format=%s clients=%s); trying next",
                fmt,
                clients or "default",
            )
        # 抽出失敗を捕捉して次の候補へ進む
        except Exception as e:
            # DRM / 非対応サイトは再試行しても同じ結果なので即時打ち切る
            if _is_non_retryable_extractor_error(e):
                # 1回分だけ警告ログを残す
                logger.warning("yt-dlp non-retryable extract error for %s: %s", url, e)
                # ユーザー向け例外へ変換して送出する
                _raise_unsupported_media(url, e)
            # 例外を保持する
            last_error = e
            # 失敗内容を警告ログへ残す
            logger.warning(
                "yt-dlp extract failed (format=%s clients=%s cookies=%s): %s",
                fmt,
                clients or "default",
                use_cookies,
                e,
            )
            # 次の組み合わせへ進む
            continue
    # 全候補失敗時、最後の例外があれば再送出する
    if last_error is not None:
        # 呼び出し元へ例外を伝播する
        raise last_error
    # 例外も結果も無い場合は None を返す
    return None, opts, None


# 後方互換エイリアス（旧名で呼ばれてもフォールバック抽出へ委譲する）
def _extract_with_cookie_fallback(opts: dict, url: str, *, download: bool):
    """旧 API 名。内部では format/client フォールバック付き抽出を使う。"""
    # 共通フォールバック抽出へ委譲する
    return _extract_with_fallbacks(opts, url, download=download, resolve_stream=False)


COMMON_YTDL_OPTS: dict = {
    # 音声優先。SABR で bestaudio 単体が空になることがあるため * 付き + best へフォールバック
    "format": "bestaudio*/best*",
    # プレイリストURLが指定された場合も中身を展開して処理する
    "noplaylist": False,
    # プレイリストの高速なインデックス取得を行うためのフラット展開設定
    "extract_flat": "in_playlist",
    # 詳細なログを取得するために出力を抑制しない設定にする
    "quiet": False,
    # 警告情報を出力させる設定にする
    "no_warnings": False,
    # 詳細なデバッグログ（[debug]で始まる行など）を出力させる設定にする
    "verbose": True,
    # ログの出力先を定義した logger オブジェクトに設定する
    "logger": logger,
    # URLでない文字列が入力された場合はYouTube検索を実行する
    "default_search": "ytsearch",
    # IPv4およびIPv6を自動で選択してバインドするIPアドレス
    "source_address": "0.0.0.0",
    # 抽出後にオーディオメタデータを自動でファイルへ埋め込むポストプロセッサ設定
    "postprocessors": [{"key": "FFmpegMetadata"}],
    # リクエスト間のスリープ時間を設定し、YouTube側からの接続制限を回避する
    "sleep_interval_requests": 1,
    # 基本スリープ時間（秒）
    "sleep_interval": 1,
    # 最大スリープ時間（秒）
    "max_sleep_interval": 5,
    # プレイリストの一部にアクセスエラーがあっても処理を中断しない
    "ignoreerrors": True,
    # 音声ファイル全体のダウンロードはスキップし、ストリームURLのみを取得する
    "skip_download": True,
    # プレイリスト展開を必要時にオンデマンドで読み込む設定
    "lazy_playlist": True,
    # YouTube SABR / DRM 実験対策:
    # tv_embedded は unsupported。android_vr / tv / web_embedded を優先する
    "extractor_args": {
        "youtube": {
            "player_client": ["android_vr", "tv", "web_embedded", "mweb"],
        }
    },
    # YouTube JS チャレンジ解決用ランタイム（deno 優先、無ければ node）
    "js_runtimes": _detect_js_runtimes(),
    # pip の yt-dlp-ejs が無い/古い場合に GitHub から EJS スクリプト取得を許可する
    "remote_components": ["ejs:github"],
}

# --- ヘルパー関数 ---
def _is_nico(url_or_query: str) -> bool:
    """ニコニコ動画のURLか判定する"""
    return ("nicovideo.jp" in url_or_query) or ("nico.ms" in url_or_query)


def _build_nico_opts(login: bool, nico_email: Optional[str] = None, nico_password: Optional[str] = None) -> dict:
    """ニコニコ動画用のyt-dlpオプションを構築する"""
    opts = COMMON_YTDL_OPTS.copy()
    opts.update({
        "paths": {"home": str(CACHE_DIR)},  # ダウンロードキャッシュの場所
        "outtmpl": {"default": "%(id)s.%(ext)s"},  # ダウンロード時のファイル名テンプレート
        "cookiefile": str(NICO_COOKIE_PATH),
        "extract_flat": False,  # ニコニコ動画の場合は詳細情報を取得したい
        "noplaylist": True,  # ニコニコ動画のプレイリストは特殊なので、ここでは単体として扱うことが多い
        "skip_download": False,  # ニコニコ動画はダウンロードを基本とする (ストリームURLが不安定な場合があるため)
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "opus",  # DiscordはOpus推奨
                "preferredquality": "0",  # 最高品質 (libopusではビットレート指定になることが多い)
                # "audioquality": "0", # FFmpegExtractAudio の場合
            },
            {"key": "FFmpegMetadata"},
        ],
    })
    if login and nico_email and nico_password:
        opts.update({
            "username": nico_email,
            "password": nico_password,
        })
    return opts


def _inject_local_path_nico(entry: dict, ytdl: Optional[yt_dlp.YoutubeDL] = None):
    """ニコニコ動画ダウンロード後のローカルパスをentryに注入する"""
    if not entry: return
    # yt-dlpがダウンロード後に設定するキーは 'filepath'
    if entry.get('filepath'):
        entry['local_path'] = entry['filepath']
    elif entry.get("requested_downloads"):  # requested_downloads はダウンロード前の情報
        # 実際にダウンロードされたファイルパスを取得する必要がある
        # ここでは、ダウンロードが成功したと仮定してファイル名を構築するが、確実ではない
        # より確実なのは、yt-dlpのダウンロード後の情報を使うこと
        try:
            # yt-dlp.prepare_filename(entry) は entry['ext'] などが必要
            # ダウンロード後のファイルパスは ytdl.prepare_filename よりも
            # 実際に生成されたファイルパスを特定する方が良い
            # ここでは 'id' と 'ext' (通常 'opus') から推測する
            if 'id' in entry and 'acodec' in entry:  # 'opus' など
                entry['local_path'] = str(CACHE_DIR / f"{entry['id']}.{entry['acodec']}")
            elif 'id' in entry and 'ext' in entry:
                entry['local_path'] = str(CACHE_DIR / f"{entry['id']}.{entry['ext']}")

        except Exception as e:
            print(f"[ytdlp_wrapper Warning] ニコニコ動画のローカルパス注入に失敗: {e} (Entry: {entry.get('id')})")
            pass  # 失敗しても処理は続ける


def _entry_to_track(entry: dict, *, is_downloaded_nico: bool = False) -> Track:
    """yt-dlpのentry辞書をTrackオブジェクトに変換する"""
    # 一時ストリームURLの変数を初期化する
    stream_url_val = None
    # ニコニコ動画が既にローカルにダウンロードされているか判定する
    if is_downloaded_nico:
        # ダウンロードされたファイルのローカルパスをストリームURLとして使用する
        stream_url_val = entry.get("local_path")

    # ストリーミング再生（YouTubeなど、またはローカルパス取得失敗時）であるか判定する
    if not stream_url_val:
        # エントリ情報からストリームURL（YouTubeなどのCDNパス）を取得する
        stream_url_val = entry.get("url")

    # タイトルキーが存在しない場合のデフォルト値を設定する
    title = entry.get("title", "タイトルなし")
    # タイトルがデフォルト値のままであり、かつIDキーが存在するか判定する
    if title == "タイトルなし" and entry.get("id"):
        # IDをタイトル名として代替設定する
        title = f"ID: {entry.get('id')}"

    # Trackデータクラスのインスタンスを生成して返す
    return Track(
        # 元の動画ページURLを取得して設定する
        url=entry.get("webpage_url") or entry.get("original_url") or entry.get("url", "不明なURL"),
        # フォーマットしたタイトル文字列を設定する
        title=title,
        # 再生時間を整数型にキャストして設定する（無い場合は0）
        duration=int(entry.get("duration") or 0),
        # サムネイル画像のURLを設定する
        thumbnail=entry.get("thumbnail"),
        # 再生用のストリームURL（ローカルパスまたはCDNパス）を設定する
        stream_url=stream_url_val,
        # 検索元のクエリを設定する
        original_query=entry.get("original_query"),
        # アップローダー、チャンネル、またはアップローダーIDを設定する
        uploader=entry.get("uploader") or entry.get("channel") or entry.get("uploader_id"),
        # 接続に必要なHTTPヘッダー情報を設定する
        http_headers=entry.get("http_headers")
    )


async def ensure_stream(track: Track, ytdl_opts_override: Optional[dict] = None) -> Track:
    """
    Trackオブジェクトのstream_urlを検証・更新する (主にYouTubeなどの時間経過で無効になるURL用)。
    ローカルファイルやニコニコのダウンロード済みファイルは対象外。
    """
    # トラックURLが空であるか、または検索クエリのプレフィックスから始まるか判定する
    if not track.url or track.url.startswith("ytsearch:"):
        # 解決不可能であるため、受け取ったオブジェクトをそのまま返す
        return track
    # ストリームURLが有効であり、かつローカルに存在する実ファイルか判定する
    if track.stream_url and Path(track.stream_url).is_file():
        # 再検証は不要であるため、そのままオブジェクトを返す
        return track
    # ニコニコ動画のURLであり、かつローカルファイルが存在するか判定する
    if _is_nico(track.url) and track.stream_url and Path(track.stream_url).exists():
        # 再取得は不要であるため、そのままオブジェクトを返す
        return track

    # 現在稼働中の非同期イベントループを取得する
    loop = asyncio.get_running_loop()
    # 呼び出し元が指定したオーバーライド設定または共通のyt-dlpオプションのコピーを作成する
    opts_for_ensure = (ytdl_opts_override or COMMON_YTDL_OPTS).copy()
    # YouTube クッキーを解決してオプションへ注入する
    _apply_youtube_cookie(opts_for_ensure)
    # 単一の動画情報のみを正確に取得するためのパラメータで辞書を更新する
    opts_for_ensure.update({
        # プレイリスト展開を無効化する
        "noplaylist": True,
        # 詳細なストリームURLおよびヘッダーを得るためにフラット展開を無効化する
        "extract_flat": False,
        # 動画のダウンロードは行わない
        "skip_download": True,
    })

    # yt-dlp をスレッド上で同期実行してストリーム情報とヘッダーを取得するローカル関数を定義する
    def _run_extract_single_info():
        # format / player_client / cookie フォールバック付きでメタデータを抽出する
        info, _used_opts, _cookiejar = _extract_with_fallbacks(
            opts_for_ensure, track.url, download=False, resolve_stream=True
        )

        # 抽出した情報（info）が None であるか判定する
        # ignoreerrors=True の設定により、情報抽出失敗時に例外ではなく None が返されるため、
        # 後続の get メソッド呼び出しで AttributeError が発生するのを防ぐ目的
        if info is None:
            # 取得失敗を示す RuntimeError を明示的に送出する
            raise RuntimeError(f"ストリーム情報の抽出に失敗しました（動画が非公開、または無効なフォーマットです）。URL: {track.url}")
        # 抽出結果がプレイリスト形式か判定し、プレイリストの場合は最初のエントリ、それ以外はinfo自体を選択する
        # _type が playlist であり、かつ entries リストが存在する場合のみ最初のエントリを対象とする
        entry_to_use = info.get("entries")[0] if info.get("_type") == "playlist" and info.get("entries") else info
        # 選択したエントリ（entry_to_use）が None であるか判定する
        # プレイリストが空の場合などに、後続の Track オブジェクト変換でエラーになるのを防ぐ目的
        if entry_to_use is None:
            # エントリが取得できないことを示す RuntimeError を明示的に送出する
            raise RuntimeError(f"プレイリスト内の有効な動画エントリが見つかりませんでした。URL: {track.url}")

        # 抽出したメタデータから一時的なTrackオブジェクトを構築する
        temp_track = _entry_to_track(entry_to_use, is_downloaded_nico=False)
        # 再生に必要なストリームURL、HTTPヘッダー、サムネイル、アップローダー情報をタプルで返す
        return temp_track.stream_url, temp_track.http_headers, temp_track.thumbnail, temp_track.uploader

    try:
        # 同期実行のyt-dlp抽出処理を非同期イベントループのバックグラウンドスレッドプールで実行する
        new_stream_url, new_http_headers, new_thumbnail, new_uploader = await loop.run_in_executor(None, _run_extract_single_info)
        # 取得した新しいストリームURLが有効か判定する
        if new_stream_url:
            # トラックオブジェクトのストリームURLを最新のものに更新する
            track.stream_url = new_stream_url
            # トラックオブジェクトのHTTPヘッダー情報を最新のものに更新する
            track.http_headers = new_http_headers
            # トラックオブジェクトのサムネイル画像を更新する
            track.thumbnail = new_thumbnail
            # トラックオブジェクトのアップローダー情報を更新する
            track.uploader = new_uploader
        else:
            # 取得失敗時は警告メッセージをコンソールに出力する
            print(f"[ytdlp_wrapper Warning] ストリームURLの再取得に失敗: {track.title} (URL: {track.url})")
            # 実行時エラーを送出して呼び出し元に通知する
            raise RuntimeError(f"ストリームURLの再取得に失敗: {track.title}")

    # yt-dlpモジュール内の例外を捕捉した場合のハンドリング
    except UnsupportedMediaError:
        # DRM / 非対応は呼び出し元で専用メッセージを出すため再送出する
        raise
    except ExtractorError as e:
        # DRM 文言が含まれる場合は再試行不要の例外へ変換する
        if _is_non_retryable_extractor_error(e):
            # ユーザー向け例外へ変換する
            _raise_unsupported_media(track.url, e)
        # エラーの旨を標準出力に記録する
        print(f"[ytdlp_wrapper Error] ストリーム解決中にyt-dlpエラー: {e} (Track: {track.title})")
        # 例外をラップしてRuntimeErrorを送出する
        raise RuntimeError(f"ストリーム解決エラー: {e}") from e
    # その他予期せぬ一般例外を捕捉した場合のハンドリング
    except Exception as e:
        # UnsupportedMediaError は上位で処理するため再送出する
        if isinstance(e, UnsupportedMediaError):
            # 呼び出し元へそのまま伝播する
            raise
        # 予期せぬエラーの旨を標準出力に記録する
        print(f"[ytdlp_wrapper Error] ストリーム解決中に予期せぬエラー: {e} (Track: {track.title})")
        # 例外をラップしてRuntimeErrorを送出する
        raise RuntimeError(f"ストリーム解決中の予期せぬエラー: {e}") from e
    # 更新完了したトラックオブジェクトを返す
    return track


async def extract(
        query: str,
        *,
        shuffle_playlist: bool = False,
        nico_email: Optional[str] = None,
        nico_password: Optional[str] = None,
        max_playlist_items: Optional[int] = 50
) -> Union[Track, List[Track], None]:
    """
    与えられたクエリ (URLまたは検索語) から音楽情報を抽出する。
    ニコニコ動画の場合はダウンロードを試み、それ以外はストリームURLを取得する。
    """
    loop = asyncio.get_running_loop()
    is_nico_query = _is_nico(query)

    ytdl_final_opts: dict
    perform_download_for_nico = False

    if is_nico_query:
        # ニコニコ動画の場合: ダウンロードを試みる
        ytdl_final_opts = _build_nico_opts(
            login=bool(not NICO_COOKIE_PATH.stat().st_size or (nico_email and nico_password)),
            nico_email=nico_email,
            nico_password=nico_password
        )
        perform_download_for_nico = True
        # ニコニコ動画のプレイリストは特殊なので、extract_flat=False, noplaylist=True で1件ずつ処理する想定
        # もしニコニコのプレイリストURLが渡された場合、yt-dlpは個々の動画情報を取得する
        # ytdl_final_opts["noplaylist"] = False # プレイリストも展開させる
        # ytdl_final_opts["extract_flat"] = "in_playlist" # ただしフラットに
    else:
        # YouTubeやその他のサイト: ストリーミング用情報を取得
        ytdl_final_opts = COMMON_YTDL_OPTS.copy()
        # YouTube クッキーを解決してオプションへ注入する
        _apply_youtube_cookie(ytdl_final_opts)
        # ストリーミング再生のため、直接のファイル全体のダウンロード処理はスキップする
        ytdl_final_opts["skip_download"] = True
        # プレイリストURLが指定された場合も中身を展開して処理する
        ytdl_final_opts["noplaylist"] = False
        # プレイリストの高速なインデックス取得を行うためのフラット展開設定
        ytdl_final_opts["extract_flat"] = "in_playlist"
        # プレイリストの最大読み込み数が指定されているか判定する
        if max_playlist_items and max_playlist_items > 0:
            # プレイリスト展開の読み込み上限数を設定する
            ytdl_final_opts["playlistend"] = max_playlist_items

    extracted_info: Optional[dict] = None

    def _run_yt_dlp_extraction():
        nonlocal extracted_info  # クロージャ内の変数を更新するため
        try:
            # format / client / cookie フォールバック付きで抽出する
            info_result, _used_opts, cookiejar = _extract_with_fallbacks(
                ytdl_final_opts, query, download=perform_download_for_nico, resolve_stream=False
            )

            if perform_download_for_nico and info_result:  # ニコニコ動画ダウンロード後処理
                # パス注入用にダミーの ytdl 参照は不要（関数内で未使用）のため None を渡す
                if info_result.get("entries"):  # プレイリストの場合
                    for entry in info_result["entries"]:
                        if entry: _inject_local_path_nico(entry, None)
                else:  # 単一動画の場合
                    _inject_local_path_nico(info_result, None)

                # ニコニコ動画のクッキー保存 (ログイン成功時など)
                if cookiejar is not None:
                    try:
                        cookiejar.save(str(NICO_COOKIE_PATH), ignore_discard=True, ignore_expires=True)
                    except Exception as e_cookie:
                        print(f"[ytdlp_wrapper Warning] ニコニコ動画のクッキー保存に失敗: {e_cookie}")

            extracted_info = info_result  # 抽出結果を保存
        except UnsupportedMediaError:
            # DRM / 非対応は呼び出し元でユーザー向けメッセージを出すため再送出する
            raise
        except ExtractorError as e_ext:  # yt-dlpが処理できないURLや検索結果なしなど
            # DRM 文言が含まれる場合は再試行不要の例外へ変換する
            if _is_non_retryable_extractor_error(e_ext):
                # ユーザー向け例外へ変換する
                _raise_unsupported_media(query, e_ext)
            print(f"[ytdlp_wrapper Info] 情報抽出失敗 (ExtractorError): {e_ext} (Query: {query})")
            # extracted_info は None のまま
        except Exception as e_gen:  # その他の予期せぬyt-dlpエラー
            # DRM 系は既に UnsupportedMediaError へ変換済みの可能性がある
            if isinstance(e_gen, UnsupportedMediaError):
                # 呼び出し元へそのまま伝播する
                raise
            # フォールバック後も失敗した場合はエラーログを記録する
            logger.error(f"[ytdlp_wrapper Error] yt-dlp実行中に予期せぬエラー: {e_gen} (Query: {query})", exc_info=True)

    await loop.run_in_executor(None, _run_yt_dlp_extraction)

    if not extracted_info:  # 情報抽出に失敗した場合
        return None

    # 結果をTrackオブジェクトに変換
    tracks: List[Track] = []
    if "entries" in extracted_info and extracted_info["entries"]:  # プレイリストの場合
        valid_entries = [entry for entry in extracted_info["entries"] if entry]  # Noneエントリを除外
        for entry_data in valid_entries:
            entry_data["original_query"] = query  # 元のクエリ情報を付加
            tracks.append(_entry_to_track(entry_data, is_downloaded_nico=perform_download_for_nico))

        if shuffle_playlist and tracks:
            random.shuffle(tracks)
        return tracks if tracks else None  # 空のプレイリストならNone
    elif extracted_info:  # 単一の動画/曲の場合
        extracted_info["original_query"] = query
        single_track = _entry_to_track(extracted_info, is_downloaded_nico=perform_download_for_nico)
        return single_track

    return None  # 何も見つからなかった場合