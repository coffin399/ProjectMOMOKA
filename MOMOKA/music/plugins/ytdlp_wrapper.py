# MOMOKA/music/ytdlp_wrapper.py
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from pathlib import Path
from typing import List, Union, Optional

import yt_dlp
from yt_dlp.utils import ExtractorError  # 個別のエラーをキャッチするため


# Trackクラス定義
@dataclass
class Track:
    url: str
    title: str
    duration: int  # 秒
    thumbnail: Optional[str] = None
    stream_url: Optional[str] = None
    requester_id: Optional[int] = None
    original_query: Optional[str] = None


# --- yt-dlp 設定 ---
CACHE_DIR = Path("./cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

NICO_COOKIE_PATH = Path("./nico_cookies.txt")
if not NICO_COOKIE_PATH.exists():
    NICO_COOKIE_PATH.touch(exist_ok=True)  # 存在しない場合のみ作成

COMMON_YTDL_OPTS: dict = {
    "format": "bestaudio[acodec=opus][asr=48000]/bestaudio/best",  # Opusを優先、48kHz
    "noplaylist": False,  # プレイリストも処理対象
    "extract_flat": "in_playlist",  # プレイリスト内の個々のエントリを効率的に取得
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",  # URLでない場合はYouTube検索 (ytsearch5: 検索結果5件など)
    "source_address": "0.0.0.0",  # IPv4 / IPv6 自動選択
    "postprocessors": [{"key": "FFmpegMetadata"}],  # メタデータを埋め込む
    "sleep_interval_requests": 1,
    "sleep_interval": 1,
    "max_sleep_interval": 5,
    "ignoreerrors": True,  # プレイリスト内の個々のエラーを無視
    "skip_download": True,  # 基本はストリーミングなのでダウンロードしない
    "lazy_playlist": True,  # プレイリストの全情報を一度に取得しない
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
    stream_url_val = None
    if is_downloaded_nico:
        stream_url_val = entry.get("local_path")  # ニコニコダウンロード後はローカルパス

    if not stream_url_val:  # ストリーミング再生の場合 (YouTubeなど、またはニコニコのローカルパス取得失敗時)
        # 'url' は format で選択されたオーディオストリームのURL
        # 'webpage_url' は元の動画ページのURL
        stream_url_val = entry.get("url")  # ストリームURL (YouTube等)

    # タイトルがない場合は "タイトルなし" や "id" を使う
    title = entry.get("title", "タイトルなし")
    if title == "タイトルなし" and entry.get("id"):
        title = f"ID: {entry.get('id')}"

    return Track(
        url=entry.get("webpage_url") or entry.get("original_url") or entry.get("url", "不明なURL"),
        title=title,
        duration=int(entry.get("duration") or 0),
        thumbnail=entry.get("thumbnail"),
        stream_url=stream_url_val,
        original_query=entry.get("original_query")  # extractで設定されていれば
    )


async def ensure_stream(track: Track, ytdl_opts_override: Optional[dict] = None) -> Track:
    """
    Trackオブジェクトのstream_urlを検証・更新する (主にYouTubeなどの時間経過で無効になるURL用)。
    ローカルファイルやニコニコのダウンロード済みファイルは対象外。
    """
    if not track.url or track.url.startswith("ytsearch:"):  # 元のURLがないか検索クエリなら解決不可
        return track
    if track.stream_url and Path(track.stream_url).is_file():  # ローカルファイルなら検証不要
        return track
    if _is_nico(track.url) and track.stream_url and Path(track.stream_url).exists():  # ニコニコダウンロード済みもOK
        return track

    loop = asyncio.get_running_loop()
    # ensure_stream 用のオプション (常に単一動画の詳細情報を取得、ダウンロードはしない)
    opts_for_ensure = (ytdl_opts_override or COMMON_YTDL_OPTS).copy()
    opts_for_ensure.update({
        "noplaylist": True,
        "extract_flat": False,  # 詳細情報を得るためにFalse
        "skip_download": True,
    })

    def _run_extract_single_info():
        with yt_dlp.YoutubeDL(opts_for_ensure) as ytdl:
            # extract_info で対象URLの最新情報を取得
            info = ytdl.extract_info(track.url, download=False)
            # プレイリストが返ってくる場合もあるので、最初の要素をチェック
            entry_to_use = info.get("entries")[0] if info.get("_type") == "playlist" and info.get("entries") else info

            # _entry_to_track を使って新しいストリームURLを取得
            temp_track = _entry_to_track(entry_to_use, is_downloaded_nico=False)  # ストリームURLを期待
            return temp_track.stream_url

    try:
        new_stream_url = await loop.run_in_executor(None, _run_extract_single_info)
        if new_stream_url:
            track.stream_url = new_stream_url
        else:
            # ストリームURLが取得できなかった場合 (元のURLが無効になっている可能性など)
            # ここではエラーを発生させるか、stream_urlをNoneのままにする
            print(f"[ytdlp_wrapper Warning] ストリームURLの再取得に失敗: {track.title} (URL: {track.url})")
            # track.stream_url = None # またはそのまま保持
            raise RuntimeError(f"ストリームURLの再取得に失敗: {track.title}")

    except ExtractorError as e:
        print(f"[ytdlp_wrapper Error] ストリーム解決中にyt-dlpエラー: {e} (Track: {track.title})")
        raise RuntimeError(f"ストリーム解決エラー: {e}") from e
    except Exception as e:
        print(f"[ytdlp_wrapper Error] ストリーム解決中に予期せぬエラー: {e} (Track: {track.title})")
        raise RuntimeError(f"ストリーム解決中の予期せぬエラー: {e}") from e
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
        ytdl_final_opts["skip_download"] = True  # ストリーミングなのでダウンロードしない
        ytdl_final_opts["noplaylist"] = False  # プレイリストも処理
        ytdl_final_opts["extract_flat"] = "in_playlist"
        if max_playlist_items and max_playlist_items > 0:
            ytdl_final_opts["playlistend"] = max_playlist_items  # プレイリストの読み込み上限

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
            print(f"[ytdlp_wrapper Error] yt-dlp実行中に予期せぬエラー: {e_gen} (Query: {query})", exc_info=True)
            # extracted_info は None のまま

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