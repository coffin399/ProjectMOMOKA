# MOMOKA/link_fix/websites.py
# サイト定義に基づく URL マッチと Fix URL 生成。
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse, urlunparse

from MOMOKA.link_fix.presets import (
    get_site_meta,
    list_site_ids,
    resolve_fix_domain,
    resolve_match_domains,
    resolve_rewrite_mode,
    supports_translation,
)
from MOMOKA.link_fix.url_utils import hostname_of

# youtu.be 短縮ホスト
_YT_SHORT_HOST = "youtu.be"


@dataclass
class MatchedLink:
    """マッチした SNS リンク1件。"""

    site_id: str
    label: str
    original_url: str
    fix_url: str
    fixer_name: str
    fix_domain: str


def _path_matches_patterns(path: str, patterns: List[str]) -> bool:
    """簡易パスパターン（{name} をセグメントワイルドカード）に合うか。"""
    # パスを正規化する
    normalized = path or "/"
    # 末尾スラッシュを落とす（ルート以外）
    if normalized != "/" and normalized.endswith("/"):
        normalized = normalized[:-1]
    # セグメント分割
    segments = [s for s in normalized.split("/") if s != ""]
    # 各パターンを試す
    for pattern in patterns:
        # パターンセグメント
        parts = [p for p in pattern.split("/") if p != ""]
        # /watch 特例（クエリ依存・セグメント1つ）
        if len(parts) == 1 and parts[0] == "watch":
            if segments and segments[0].lower() == "watch":
                return True
            continue
        # 長さ不一致はスキップ
        if len(parts) != len(segments):
            continue
        # 各セグメントを比較する
        ok = True
        for part, seg in zip(parts, segments):
            # {var} なら任意
            if part.startswith("{") and part.endswith("}"):
                continue
            # リテラル不一致
            if part.lower() != seg.lower():
                ok = False
                break
        # 全セグメント一致なら成功
        if ok:
            return True
    # どれにも合わなければ False
    return False


def _is_path_eligible(site_id: str, url: str, meta: Dict[str, Any]) -> bool:
    """サイトごとのパス／クエリ条件を満たすか。"""
    # パースする
    parsed = urlparse(url)
    # パス
    path = parsed.path or "/"
    # パターン一覧
    patterns = [str(p) for p in (meta.get("path_patterns") or [])]
    # パターンが無ければホスト一致だけで許可
    if not patterns:
        return True
    # YouTube 特殊: watch?v= / youtu.be / shorts
    if site_id == "youtube":
        host = hostname_of(url)
        if host == _YT_SHORT_HOST:
            return bool([s for s in path.split("/") if s])
        if "youtube.com" in host:
            if path.rstrip("/").endswith("/watch") or "/watch" in path:
                qs = parse_qs(parsed.query)
                return bool(qs.get("v"))
            if "/shorts/" in path:
                return True
            return False
    # Facebook watch?v=
    if site_id == "facebook" and path.rstrip("/").endswith("/watch"):
        return True
    # 通常パターン照合
    return _path_matches_patterns(path, patterns)


def _build_fixed_url(
    site_id: str,
    original_url: str,
    fix_domain: str,
    rewrite_mode: str,
    lang: Optional[str] = None,
) -> str:
    """元 URL を Fix 先ドメインに差し替えた URL を作る。"""
    # パースする
    parsed = urlparse(original_url)
    # パス＋クエリ＋フラグメントを保持する
    path = parsed.path or ""
    # bilibili の vx プレフィックス方式
    if rewrite_mode == "vx_prefix":
        # 元ホスト
        host = hostname_of(original_url)
        # vx + ホスト（www なし）
        new_host = f"vx{host}" if not host.startswith("vx") else host
        # スキームは https
        return urlunparse(("https", new_host, path, "", parsed.query, parsed.fragment))
    # Tumblr のサブドメインユーザーをパスに載せる簡易対応はパス維持で十分
    # 通常: ドメイン差し替え
    new_netloc = fix_domain
    # Twitter 翻訳サフィックス
    if site_id == "twitter" and lang:
        # 既に末尾が言語コードなら二重付与しない
        parts = [p for p in path.split("/") if p]
        if not (parts and len(parts[-1]) == 2 and parts[-1].isalpha()):
            path = path.rstrip("/") + f"/{lang}"
    # 組み立てる
    return urlunparse(("https", new_netloc, path, "", parsed.query, parsed.fragment))


def _fixer_display_name(fix_domain: str) -> str:
    """Fix 先ドメインから表示名を作る。"""
    # 先頭ラベルをタイトルケースに
    host = (fix_domain or "").split(":")[0]
    # ドットで分割
    labels = host.split(".")
    # 先頭を使う
    if not labels:
        return "Fix"
    # 返す
    return labels[0].capitalize()


def match_url(
    url: str,
    bot_config: Dict[str, Any],
    guild_sites: Optional[Dict[str, Any]] = None,
    *,
    translate_lang: Optional[str] = None,
) -> Optional[MatchedLink]:
    """1 URL をサイト定義に照合し、有効なら MatchedLink を返す。"""
    # ホスト
    host = hostname_of(url)
    # ホスト無ければ対象外
    if not host:
        return None
    # ギルドサイト設定
    guild_sites = guild_sites or {}
    # 各サイトを走査する
    for site_id in list_site_ids(bot_config):
        # ギルド個別
        guild_site = guild_sites.get(site_id)
        if isinstance(guild_site, dict) and guild_site.get("enabled") is False:
            # サイト無効
            continue
        # YAML メタ
        meta = get_site_meta(bot_config, site_id)
        # YAML で無効ならスキップ（ギルドで明示 enabled:true があれば後で上書き可）
        if meta.get("enabled") is False and not (
            isinstance(guild_site, dict) and guild_site.get("enabled") is True
        ):
            continue
        # マッチドメイン
        match_domains = resolve_match_domains(bot_config, site_id, guild_site if isinstance(guild_site, dict) else None)
        # ホスト一致（末尾一致も許可: foo.bilibili.com）
        if not any(host == d or host.endswith("." + d) for d in match_domains):
            continue
        # パス条件
        if not _is_path_eligible(site_id, url, meta):
            continue
        # Fix 先
        fix_domain = resolve_fix_domain(
            bot_config, site_id, guild_site if isinstance(guild_site, dict) else None
        )
        # 書き換えモード
        mode = resolve_rewrite_mode(bot_config, site_id)
        # Twitter かつ翻訳対応ドメインのときだけ lang を付与する
        lang = None
        if (
            site_id == "twitter"
            and translate_lang
            and supports_translation(bot_config, site_id, fix_domain)
        ):
            lang = translate_lang
        # Fix URL
        fix_url = _build_fixed_url(site_id, url, fix_domain, mode, lang=lang)
        # ラベル
        label = str(meta.get("label") or site_id.title())
        # 結果を返す（最初の一致）
        return MatchedLink(
            site_id=site_id,
            label=label,
            original_url=url,
            fix_url=fix_url,
            fixer_name=_fixer_display_name(fix_domain),
            fix_domain=fix_domain,
        )
    # 不一致
    return None


def match_urls(
    urls: List[str],
    bot_config: Dict[str, Any],
    guild_sites: Optional[Dict[str, Any]] = None,
    *,
    translate_lang: Optional[str] = None,
) -> List[MatchedLink]:
    """複数 URL をマッチする（同一 URL の重複は除外）。"""
    # 結果
    matched: List[MatchedLink] = []
    # 既出 URL
    seen = set()
    # 走査する
    for url in urls:
        # 重複スキップ
        if url in seen:
            continue
        # マッチ試行
        link = match_url(
            url,
            bot_config,
            guild_sites,
            translate_lang=translate_lang,
        )
        # ヒットしたら追加
        if link is not None:
            matched.append(link)
            seen.add(url)
    # 返す
    return matched


def format_reply_line(link: MatchedLink) -> str:
    """返信1行（元URLはプレビュー抑制、Fix URL はプレビュー対象）。"""
    # ラベル付きリンクを組み立てる
    return (
        f"[{link.label}](<{link.original_url}>) • "
        f"[{link.fixer_name}]({link.fix_url})"
    )


def apply_twitter_lang(fix_url: str, lang: Optional[str]) -> str:
    """既存 Fix URL に対し翻訳 lang を付与／除去する。"""
    # パースする
    parsed = urlparse(fix_url)
    # パス分割
    parts = [p for p in (parsed.path or "").split("/") if p]
    # 末尾が2文字言語コードなら除去する
    if parts and len(parts[-1]) == 2 and parts[-1].isalpha():
        parts = parts[:-1]
    # lang があれば付与する
    if lang:
        parts.append(lang.lower())
    # 新パス
    new_path = "/" + "/".join(parts)
    # 組み立てて返す
    return urlunparse((parsed.scheme or "https", parsed.netloc, new_path, "", parsed.query, parsed.fragment))


def rebuild_fix_url_for_domain(original_url: str, fix_domain: str, site_id: str, bot_config: Dict[str, Any], lang: Optional[str] = None) -> str:
    """ドメイン変更後の Fix URL を再生成する。"""
    # モード解決
    mode = resolve_rewrite_mode(bot_config, site_id)
    # 構築して返す
    return _build_fixed_url(site_id, original_url, fix_domain, mode, lang=lang)
