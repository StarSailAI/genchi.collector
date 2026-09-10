"""Subscription labels and literal matching shared by agendas and mail delivery."""

import unicodedata

KIND_LABELS = {
    "LIVE": "演唱会",
    "FESTIVAL": "音乐节",
    "POPUP": "快闪店",
    "CAFE": "联动咖啡",
    "EXHIBITION": "展览",
    "MEETUP": "见面会",
    "GOODS": "商品贩售",
    "OTHER": "其他活动",
}

# SQL aliases a/f are shared by both read models. strpos treats %, _ and \ literally.
DIRECT_FOLLOW_MATCH = """(f.target_type='ACTIVITY' AND f.target_id=a.id)
 OR (f.target_type='TAG' AND f.target_id=a.kind)
 OR (f.target_type='KEYWORD' AND strpos(lower(normalize(concat_ws(' ',a.title,a.title_zh,a.summary,
   (SELECT string_agg(concat_ws(' ',s.name,s.name_zh),' ') FROM catalog_activity_subjects l
    JOIN catalog_subjects s ON s.slug=l.subject_slug WHERE l.activity_id=a.id)),NFKC)),f.target_id)>0)"""


def normalize_keyword(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().lower()
    if not 2 <= len(value) <= 80 or not value.isprintable():
        raise ValueError("关键词需要 2 至 80 个可见字符")
    return value
