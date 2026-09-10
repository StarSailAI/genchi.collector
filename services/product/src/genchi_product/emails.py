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


def render_login_email(
    code: str, *, expires_minutes: int, site_url: str, locale: str = "zh-Hans"
) -> RenderedEmail:
    """Render the explicitly selected language, with a readable plain-text fallback."""
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("Expected a six-digit email code")
    if type(expires_minutes) is not int or not 1 <= expires_minutes <= 60:
        raise ValueError("Invalid email code lifetime")
    origin = site_url.rstrip("/")
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
