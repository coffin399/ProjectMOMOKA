# MOMOKA/link_fix/url_utils.py
# URL 抽出と公式 embed の健全性判定。
from __future__ import annotations

import re
from typing import List, Optional, Sequence
from urllib.parse import urlparse

import discord

# 生 URL 抽出（<> 囲みは別途除外）
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
# コードブロック／インラインコード
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`]+`")
# Discord のプレビュー抑制 <url>
_NO_PREVIEW_RE = re.compile(r"<https?://[^>]+>", re.IGNORECASE)


def strip_code_spans(content: str) -> str:
    """コードブロックとインラインコードを空白に置換する。"""
    # ブロックを潰す
    text = _CODE_BLOCK_RE.sub(" ", content or "")
    # インラインを潰す
    text = _INLINE_CODE_RE.sub(" ", text)
    # 返す
    return text


def extract_previewable_urls(content: str) -> List[str]:
    """プレビュー対象になり得る URL を抽出する（<> とコード内は除外）。"""
    # 本文が無ければ空
    if not content:
        return []
    # コードを除去した本文
    cleaned = strip_code_spans(content)
    # <> 囲み URL を集合化する（プレビューされない）
    no_preview = {m.group(0)[1:-1] for m in _NO_PREVIEW_RE.finditer(cleaned)}
    # <> 自体も本文から消して誤検知を減らす
    without_brackets = _NO_PREVIEW_RE.sub(" ", cleaned)
    # 結果
    urls: List[str] = []
    # 正規表現で拾う
    for match in _URL_RE.finditer(without_brackets):
        # 末尾の日本語句読点などを落とす
        url = match.group(0).rstrip(")。].,，、》」'")
        # プレビュー抑制済みならスキップ
        if url in no_preview:
            continue
        # 重複を避ける
        if url not in urls:
            urls.append(url)
    # 返す
    return urls


def hostname_of(url: str) -> str:
    """URL のホスト名（www. 除去・小文字）。"""
    # パースする
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return ""
    # 小文字化
    host = host.lower()
    # www. を落とす
    if host.startswith("www."):
        host = host[4:]
    # 返す
    return host


def urls_share_identity(a: str, b: str) -> bool:
    """同一コンテンツを指すかの粗い判定（ホスト＋パス）。"""
    # パースする
    try:
        pa = urlparse(a)
        pb = urlparse(b)
    except Exception:
        return False
    # ホスト比較用
    ha = hostname_of(a)
    hb = hostname_of(b)
    # ホストが違えば不一致
    if not ha or not hb or ha != hb:
        return False
    # パスを正規化する（末尾スラッシュ無視）
    path_a = (pa.path or "").rstrip("/").lower()
    path_b = (pb.path or "").rstrip("/").lower()
    # パス一致で同一とみなす
    return path_a == path_b


def embed_has_media(embed: discord.Embed) -> bool:
    """embed に image / video / thumbnail があるか。"""
    # image
    if embed.image and embed.image.url:
        return True
    # video
    if embed.video and embed.video.url:
        return True
    # thumbnail
    if embed.thumbnail and embed.thumbnail.url:
        return True
    # いずれも無し
    return False


def find_matching_embed(
    embeds: Sequence[discord.Embed],
    url: str,
) -> Optional[discord.Embed]:
    """URL に対応しそうな embed を探す。"""
    # 各 embed を見る
    for embed in embeds:
        # embed.url が一致系なら採用
        if embed.url and urls_share_identity(embed.url, url):
            return embed
        # プロバイダ名だけの場合もあるので URL ホストが本文に含まれるか見る
        # （弱いフォールバックは呼び出し側で不足判定に使う）
    # 見つからなければ None
    return None


def is_embed_broken_or_missing(
    embeds: Sequence[discord.Embed],
    url: str,
) -> bool:
    """公式 embed が無い、またはメディア無しの空プレビューなら True。"""
    # 対応 embed を探す
    matched = find_matching_embed(embeds, url)
    # 無ければ壊れている扱い
    if matched is None:
        # embed が1つも無いなら確実に欠落
        if not embeds:
            return True
        # URL 不一致の embed しか無い場合も欠落とみなす
        return True
    # メディアが無ければ壊れている
    if not embed_has_media(matched):
        return True
    # 健全
    return False
