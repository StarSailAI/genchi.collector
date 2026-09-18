"""The small, curated popularity pool used by the homepage countdowns.

This is deliberately a preference pool rather than a hard ranking.  Activity
records still need a confirmed milestone and verified evidence before they can
reach the homepage; the pool only prevents an arbitrary long-tail activity
from displacing a project or artist that Genchi users are likely to follow.
"""

from __future__ import annotations

import re
import unicodedata

# Keep aliases in the same file as the policy so a new source can be added
# without changing SQL or the presentation layer.  The names mirror the
# initial 25 anime projects and 50 Japanese music artists selected for Genchi.
HOME_CANDIDATES: dict[str, dict[str, tuple[str, ...]]] = {
    "anime": {
        "love-live": ("ラブライブ", "love live"),
        "bang-dream": ("バンドリ", "bang dream"),
        "gakumas": ("学園アイドルマスター", "学マス", "gakumas"),
        "idolmaster": ("アイドルマスター", "idolmaster", "偶像大师"),
        "million-live": ("million live", "ミリオンライブ", "百万现场"),
        "shiny-colors": ("シャイニーカラーズ", "shiny colors", "闪耀色彩"),
        "project-sekai": ("プロジェクトセカイ", "project sekai", "世界计划"),
        "ensemble-stars": ("あんさんぶるスターズ", "ensemble stars", "偶像梦幻祭"),
        "idolish7": ("アイドリッシュセブン", "idolish7", "idolish"),
        "hypnosis-mic": ("ヒプノシスマイク", "hypnosis mic", "催眠麦克风"),
        "d4dj": ("d4dj",),
        "revue-starlight": ("レヴュースタァライト", "revue starlight", "少女歌剧"),
        "tokyo-7th-sisters": ("tokyo 7th", "ナナシス"),
        "uta-no-prince-sama": ("うたの☆プリンスさまっ", "uta no prince", "歌之王子殿下"),
        "uma-musume": ("ウマ娘", "uma musume", "赛马娘"),
        "pokemon": ("ポケモン", "pokemon", "宝可梦"),
        "gundam": ("ガンダム", "gundam"),
        "demon-slayer": ("鬼滅の刃", "鬼滅", "demon slayer"),
        "jujutsu-kaisen": ("呪術廻戦", "咒术回战", "jujutsu kaisen"),
        "one-piece": ("ワンピース", "one piece", "海贼王"),
        "oshi-no-ko": ("推しの子", "oshi no ko", "我推的孩子"),
        "frieren": ("葬送のフリーレン", "frieren", "芙莉莲"),
        "apothecary-diaries": ("薬屋のひとりごと", "apothecary diaries", "药屋少女"),
        "dandadan": ("ダンダダン", "dandadan", "胆大党"),
        "bocchi-the-rock": ("ぼっち・ざ・ろっく", "bocchi the rock", "孤独摇滚"),
        "girls-band-cry": ("ガールズバンドクライ", "girls band cry"),
    },
    "music": {
        "mrs-green-apple": ("mrs. green apple", "mrs.green apple"),
        "back-number": ("back number",),
        "kenshi-yonezu": ("米津玄師", "kenshi yonezu"),
        "hana": ("hana",),
        "m-lk": ("m!lk",),
        "official-hige-dandism": ("official髭男dism", "髭男"),
        "vaundy": ("vaundy", "バウンディ"),
        "king-gnu": ("king gnu",),
        "fujii-kaze": ("藤井風", "fujii kaze"),
        "snow-man": ("snow man", "スノーマン"),
        "sixtones": ("sixtones", "ストーンズ"),
        "aimyon": ("あいみょん", "aimyon"),
        "masaharu-fukuyama": ("福山雅治", "masaharu fukuyama"),
        "equal-love": ("=love", "イコラブ"),
        "ebidan": ("ebidan",),
        "radwimps": ("radwimps",),
        "sakanaction": ("サカナクション", "sakanaction"),
        "yoasobi": ("yoasobi",),
        "ado": ("ado",),
        "lisa": ("lisa",),
        "eve": ("eve",),
        "yorushika": ("ヨルシカ", "yorushika"),
        "zutomayo": ("ずっと真夜中でいいのに", "zutomayo"),
        "creepy-nuts": ("creepy nuts",),
        "utada-hikaru": ("宇多田ヒカル", "utada hikaru"),
        # Expansion pool: artists with strong current activity, touring density
        # or a reliable Japanese ticket/event footprint.  These names broaden
        # discovery without changing the homepage's popularity ranking.
        "aimer": ("aimer",),
        "milet": ("milet",),
        "natori": ("natori", "なとり"),
        "saucy-dog": ("saucy dog",),
        "yuri": ("優里", "yuri"),
        "macaroni-enpitsu": ("マカロニえんぴつ", "macaroni enpitsu"),
        "novelbright": ("novelbright",),
        "one-ok-rock": ("one ok rock",),
        "bump-of-chicken": ("bump of chicken",),
        "mr-children": ("mr.children", "mr children"),
        "spitz": ("スピッツ", "spitz"),
        "uverworld": ("uverworld",),
        "jo1": ("jo1",),
        "be-first": ("be:first", "be first"),
        "number-i": ("number_i", "number i"),
        "king-prince": ("king & prince", "king and prince"),
        "nizi-u": ("niziu", "nizi u"),
        "akb48": ("akb48",),
        "nogizaka46": ("乃木坂46", "nogizaka46"),
        "hinatazaka46": ("日向坂46", "hinatazaka46"),
        "sakurazaka46": ("櫻坂46", "sakurazaka46"),
        "fruits-zipper": ("fruits zipper",),
        "candy-tune": ("candy tune",),
        "cho-tokkyu": ("超特急", "cho tokkyu"),
        "ini": ("ini",),
    },
}


def _normalise(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).lower()


def _contains(text: str, alias: str) -> bool:
    """Match Latin aliases as words so ``Eve`` does not match ``FEVER``."""

    alias = _normalise(alias)
    if not alias:
        return False
    if re.search(r"[a-z0-9]", alias):
        return re.search(
            rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text
        ) is not None
    return alias in text


def candidate_type(row: dict) -> str | None:
    """Return ``anime``/``music`` when a row belongs to the candidate pool."""

    parts = [row.get("title"), row.get("title_zh"), row.get("subject_slug")]
    # The query intentionally contains only verified/current subject metadata;
    # aliases in activity titles cover artists that have not got a subject row.
    text = _normalise(" ".join(str(part) for part in parts if part))
    for kind, candidates in HOME_CANDIDATES.items():
        for alias in candidates.values():
            if any(_contains(text, value) for value in alias):
                return kind
    return None


def candidate_count() -> int:
    # ``idolmaster`` is retained as a subject alias so existing generic
    # catalogue relations can match the Million Live!/Shiny Colors branches;
    # it is not an additional item in the 25-project editorial list.
    return sum(len(items) for items in HOME_CANDIDATES.values()) - 1
