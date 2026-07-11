# MOMOKA/music/ytdlp_wrapper.py
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from pathlib import Path
from typing import List, Union, Optional

import logging
import yt_dlp
# yt-dlpの出力ログをMOMOKAのログシステムに統合するためのロガーを設定する
logger = logging.getLogger("MOMOKA.music.plugins.ytdlp")
from yt_dlp.utils import ExtractorError  # 個別のエラーをキャッチするため


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

# YouTube用クッキーファイルの保存パスを定義する
YOUTUBE_COOKIE_PATH = Path("./youtube_cookies.txt")

COMMON_YTDL_OPTS: dict = {
    # 音声フォーマットの品質指定
    # YouTubeの一部動画で「Requested format is not available」エラーが発生するのを防ぐため、
    # 特定のacodecやasrに制限せず、利用可能な中で最良のオーディオストリームをフォールバック付きで取得する
    "format": "bestaudio/best",
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
    # YouTubeの年齢制限やクッキー適用時のフォーマット取得エラーを回避するため、
    # defaultのクライアント構成に加え、クッキー認証と相性の良いweb_creatorクライアントをフォールバックに指定する
    "extractor_args": {
        "youtube": {
            "player_client": ["default", "web_creator"]
        }
    }
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


def _inject_local_path_nico(entry: dict, ytdl: yt_dlp.YoutubeDL):
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
    # YouTube用のクッキーファイルが存在するか判定する
    if YOUTUBE_COOKIE_PATH.exists():
        # クッキーファイルをオプションに設定して年齢制限等の認証を通過させる
        opts_for_ensure["cookiefile"] = str(YOUTUBE_COOKIE_PATH)
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
        # クッキーインポート設定を含めた一時オプションで処理を試みる
        try:
            # コンテキストマネージャで yt-dlp インスタンスを初期化する
            with yt_dlp.YoutubeDL(opts_for_ensure) as ytdl:
                # 指定されたURLから最新の動画メタデータを抽出する（ダウンロードはスキップ）
                info = ytdl.extract_info(track.url, download=False)
        except Exception as e:
            # クッキーの読み込み失敗などが発生した場合に備える
            # cookiesfrombrowser 設定が存在する場合のみフォールバックを実行する
            if "cookiesfrombrowser" in opts_for_ensure:
                # クッキー読み込みエラーを検知してコンソールに警告を表示する
                print(f"[ytdlp_wrapper Warning] クッキー自動インポートに失敗しました。クッキーなしで再試行します: {e}")
                # オプションのコピーを作成してクッキー設定を削除する
                fallback_opts = opts_for_ensure.copy()
                # クッキーインポートのオプションを安全に削除する
                fallback_opts.pop("cookiesfrombrowser", None)
                # 再度 yt-dlp をクッキーなしの状態で初期化する
                with yt_dlp.YoutubeDL(fallback_opts) as ytdl:
                    # 指定されたURLから最新の動画メタデータを抽出する（ダウンロードはスキップ）
                    info = ytdl.extract_info(track.url, download=False)
            else:
                # クッキー設定がないか再試行でも解決しなかった場合は例外をそのまま送出する
                raise

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
    except ExtractorError as e:
        # エラーの旨を標準出力に記録する
        print(f"[ytdlp_wrapper Error] ストリーム解決中にyt-dlpエラー: {e} (Track: {track.title})")
        # 例外をラップしてRuntimeErrorを送出する
        raise RuntimeError(f"ストリーム解決エラー: {e}") from e
    # その他予期せぬ一般例外を捕捉した場合のハンドリング
    except Exception as e:
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
        # YouTube用のクッキーファイルが存在するか判定する
        if YOUTUBE_COOKIE_PATH.exists():
            # クッキーファイルをオプションに設定して年齢制限等の認証を通過させる
            ytdl_final_opts["cookiefile"] = str(YOUTUBE_COOKIE_PATH)
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
            with yt_dlp.YoutubeDL(ytdl_final_opts) as ytdl:
                # extract_info を実行
                info_result = ytdl.extract_info(query, download=perform_download_for_nico)

                if perform_download_for_nico and info_result:  # ニコニコ動画ダウンロード後処理
                    if info_result.get("entries"):  # プレイリストの場合
                        for entry in info_result["entries"]:
                            if entry: _inject_local_path_nico(entry, ytdl)
                    else:  # 単一動画の場合
                        _inject_local_path_nico(info_result, ytdl)

                    # ニコニコ動画のクッキー保存 (ログイン成功時など)
                    try:
                        ytdl.cookiejar.save(str(NICO_COOKIE_PATH), ignore_discard=True, ignore_expires=True)
                    except Exception as e_cookie:
                        print(f"[ytdlp_wrapper Warning] ニコニコ動画のクッキー保存に失敗: {e_cookie}")

                extracted_info = info_result  # 抽出結果を保存
        except ExtractorError as e_ext:  # yt-dlpが処理できないURLや検索結果なしなど
            print(f"[ytdlp_wrapper Info] 情報抽出失敗 (ExtractorError): {e_ext} (Query: {query})")
            # extracted_info は None のまま
        except Exception as e_gen:  # その他の予期せぬyt-dlpエラー
            # クッキーの読み込み失敗などが発生した場合に備える
            # cookiesfrombrowser 設定が存在する場合のみフォールバックを実行する
            if "cookiesfrombrowser" in ytdl_final_opts:
                # クッキー読み込みエラーを検知してコンソールに警告を表示する
                print(f"[ytdlp_wrapper Warning] クッキー自動インポートに失敗しました。クッキーなしで再試行します: {e_gen}")
                # オプションのコピーを作成してクッキー設定を削除する
                fallback_opts = ytdl_final_opts.copy()
                # クッキーインポートのオプションを安全に削除する
                fallback_opts.pop("cookiesfrombrowser", None)
                try:
                    # 再度 yt-dlp をクッキーなしの状態で初期化する
                    with yt_dlp.YoutubeDL(fallback_opts) as ytdl_fallback:
                        # extract_info を実行して結果を保存する
                        info_result_fallback = ytdl_fallback.extract_info(query, download=perform_download_for_nico)
                        # 抽出成功した結果がある場合のみ後処理を実行する
                        if info_result_fallback:
                            if perform_download_for_nico:
                                if info_result_fallback.get("entries"):
                                    for entry in info_result_fallback["entries"]:
                                        if entry: _inject_local_path_nico(entry, ytdl_fallback)
                                else:
                                    _inject_local_path_nico(info_result_fallback, ytdl_fallback)
                                try:
                                    ytdl_fallback.cookiejar.save(str(NICO_COOKIE_PATH), ignore_discard=True, ignore_expires=True)
                                except Exception as e_cookie:
                                    print(f"[ytdlp_wrapper Warning] ニコニコ動画のクッキー保存に失敗: {e_cookie}")
                            # 抽出結果をクロージャ変数に保存する
                            extracted_info = info_result_fallback
                except Exception as e_fallback:
                    # フォールバック後も失敗した場合はエラーログを記録する
                    logger.error(f"[ytdlp_wrapper Error] クッキーなしの再試行中にエラーが発生しました: {e_fallback}", exc_info=True)
            else:
                # クッキー設定がないか再試行でも解決しなかった場合はエラーログを記録する
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