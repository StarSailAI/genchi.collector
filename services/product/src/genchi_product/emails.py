"""Pure transactional email rendering; no delivery, persistence or logging."""

import re
from dataclasses import dataclass, field
from html import escape
from importlib.resources import files
from string import Template
from urllib.parse import urlsplit

from .localization import locale_of, translate


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str = field(repr=False)
    html: str = field(repr=False)


def _public_origin(value: str) -> str:
    origin = value.rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", parsed.netloc)
        or any(character.isspace() for character in origin)
    ):
        raise ValueError("Email links require a configured public origin")
    return origin


def _safe_link(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(character in value for character in "\r\n\0")
    ):
        raise ValueError("Invalid email link")
    return value


def _line(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def render_login_email(
    code: str, *, expires_minutes: int, site_url: str, locale: str = "zh-Hans"
) -> RenderedEmail:
    """Render the explicitly selected language, with a readable plain-text fallback."""
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("Expected a six-digit email code")
    if type(expires_minutes) is not int or not 1 <= expires_minutes <= 60:
        raise ValueError("Invalid email code lifetime")
    origin = _public_origin(site_url)
    locale = locale_of(locale)

    def t(key, **values):
        return translate(key, locale, **values)

    subject = t("genchi.news 登录验证码")
    text = (
        "\n\n".join(
            [
                "genchi.news 現地情報",
                t("你的登录验证码"),
                code,
                t("{minutes} 分钟内有效，仅可使用一次。", minutes=expires_minutes),
                t("请回到刚才的页面输入验证码，")
                + (" " if locale == "en" else "")
                + t("完成登录或注册。")
                + " "
                + t("首次验证成功后将自动创建账户。"),
                t("请勿将验证码分享给他人。") + " " + t("如果这不是你的操作，请忽略此邮件。"),
                t("下一次心动，现场见。"),
                origin + "/" + locale,
            ]
        )
        + "\n"
    )
    template = Template(
        files("genchi_product").joinpath("templates/login_code.html").read_text(encoding="utf-8")
    )
    html = template.substitute(
        code=escape(code, quote=True),
        expires_minutes=escape(str(expires_minutes), quote=True),
        site_url=escape(origin + "/" + locale, quote=True),
        locale=locale,
        **{
            key: escape(value, quote=True)
            for key, value in {
                "subject": subject,
                "preheader": t(
                    "你的登录验证码已准备好，请在 {minutes} 分钟内使用。", minutes=expires_minutes
                ),
                "heading": t("你的登录验证码"),
                "instructions1": t("请回到刚才的页面输入验证码，"),
                "instructions2": t("完成登录或注册。"),
                "expiry": t("{minutes} 分钟内有效 · 仅可使用一次", minutes=expires_minutes),
                "signup": t("首次验证成功后将自动创建账户。"),
                "security1": t("请勿将验证码分享给他人。"),
                "security2": t("如果这不是你的操作，请忽略此邮件。"),
                "tagline1": t("下一次心动，"),
                "tagline2": t("现场见。"),
                "footnote": t("为喜欢的事，留一个位置。"),
            }.items()
        },
    )
    return RenderedEmail(subject, text, html)


def render_notification_email(
    subject: str,
    *,
    locale: str,
    site_url: str,
    eyebrow: str,
    heading: str,
    intro: str = "",
    items: list[tuple[str, str | None]] | None = None,
    facts: list[tuple[str, str]] | None = None,
    cta_label: str,
    cta_url: str,
    official_url: str | None = None,
    unsubscribe_url: str,
) -> RenderedEmail:
    """Render a compact branded notification with a matching plain-text fallback."""
    locale = locale_of(locale)
    origin = _public_origin(site_url)
    home_url = origin + "/" + locale
    dashboard_url = home_url + "/dashboard"
    cta_url = _safe_link(cta_url)
    unsubscribe_url = _safe_link(unsubscribe_url)
    official_url = _safe_link(official_url) if official_url else None
    subject, eyebrow, heading, intro, cta_label = map(
        _line, (subject, eyebrow, heading, intro, cta_label)
    )
    items = [(_line(label), _safe_link(url) if url else None) for label, url in (items or [])]
    facts = [(_line(label), _line(value)) for label, value in (facts or []) if value]

    def t(key: str) -> str:
        return translate(key, locale)

    text_parts = ["genchi.news 現地情報", eyebrow, heading]
    if intro:
        text_parts.append(intro)
    if items:
        text_parts.append(
            "\n".join(f"• {label}" + (f"\n  {url}" if url else "") for label, url in items)
        )
    if facts:
        text_parts.append("\n".join(f"{label}：{value}" for label, value in facts))
    text_parts.append(f"{cta_label}：{cta_url}")
    if official_url:
        text_parts.append(f"{t('官方入口')}：{official_url}")
    text_parts.extend(
        [
            f"{t('管理关注')}：{dashboard_url}",
            f"{t('退订所有提醒')}：{unsubscribe_url}",
            t("下一次心动，现场见。"),
        ]
    )
    text = "\n\n".join(text_parts) + "\n"

    def linked(label: str, url: str | None) -> str:
        safe_label = escape(label)
        return (
            f'<a href="{escape(url, quote=True)}" style="color:#252535;text-decoration:none;">'
            f"{safe_label}</a>"
            if url
            else safe_label
        )

    items_block = ""
    if items:
        rows = "".join(
            '<tr><td valign="top" style="width:18px;padding:7px 0;color:#e83e60;font-size:16px;">•</td>'
            f'<td style="padding:7px 0;color:#252535;font-size:14px;line-height:1.7;">{linked(label, url)}</td></tr>'
            for label, url in items
        )
        items_block = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:20px 0 0;border-collapse:collapse;">{rows}</table>'
        )
    facts_block = ""
    if facts:
        rows = "".join(
            "<tr>"
            f'<td style="padding:6px 12px 6px 0;color:#858593;font-size:12px;line-height:1.6;white-space:nowrap;">{escape(label)}</td>'
            f'<td style="padding:6px 0;color:#252535;font-size:13px;line-height:1.6;">{escape(value)}</td>'
            "</tr>"
            for label, value in facts
        )
        facts_block = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'bgcolor="#fafafe" style="margin:22px 0 0;padding:12px 16px;border:1px solid #eeeef4;border-radius:12px;background-color:#fafafe;">{rows}</table>'
        )
    official_block = (
        f'<p style="margin:18px 0 0;text-align:center;"><a href="{escape(official_url, quote=True)}" '
        f'style="color:#4361c8;font-size:12px;text-decoration:none;">{escape(t("官方入口"))} ↗</a></p>'
        if official_url
        else ""
    )
    template = Template(
        files("genchi_product").joinpath("templates/notification.html").read_text(encoding="utf-8")
    )
    html = template.substitute(
        locale=locale,
        subject=escape(subject),
        preheader=escape(intro or heading),
        site_url=escape(home_url, quote=True),
        eyebrow=escape(eyebrow),
        heading=escape(heading),
        intro_block=(
            f'<p style="margin:14px 0 0;color:#70717f;font-size:14px;line-height:1.9;">{escape(intro)}</p>'
            if intro
            else ""
        ),
        items_block=items_block,
        facts_block=facts_block,
        cta_url=escape(cta_url, quote=True),
        cta_label=escape(cta_label),
        official_block=official_block,
        dashboard_url=escape(dashboard_url, quote=True),
        manage_label=escape(t("管理关注")),
        unsubscribe_url=escape(unsubscribe_url, quote=True),
        unsubscribe_label=escape(t("退订所有提醒")),
        tagline1=escape(t("下一次心动，")),
        tagline2=escape(t("现场见。")),
    )
    return RenderedEmail(subject, text, html)
