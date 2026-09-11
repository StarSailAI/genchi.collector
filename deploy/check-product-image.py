"""Run inside the release image without credentials, network or a database."""

from importlib.resources import files

from genchi_product.api import create_app
from genchi_product.emails import render_login_email, render_notification_email
from genchi_product.notifications import grouped_changes, localized_change

# Route registration must not query the database. A truthy sentinel avoids
# constructing the production Catalog or requiring DATABASE_URL.
schema = create_app(object()).openapi()
verify = schema["paths"]["/auth/verify"]["post"]["requestBody"]["content"]["application/json"]
model = verify["schema"]["$ref"].rsplit("/", 1)[-1]
required = set(schema["components"]["schemas"][model].get("required", []))
if not {"email", "code"}.issubset(required) or "token" in required:
    raise SystemExit("Product image uses legacy authentication; email-code login is required.")
if "/auth/logout-all" not in schema["paths"]:
    raise SystemExit("Product image is missing account session management.")
print("Product image supports email-code login and session management.")

agent_paths = {
    "/me/api-keys",
    "/agent/v1/updates",
    "/agent/v1/activities",
    "/agent/v1/subscriptions",
    "/agent/v1/agenda",
    "/mcp",
}
missing_agent_paths = agent_paths - set(schema["paths"])
if missing_agent_paths:
    raise SystemExit(f"Product image is missing Agent access: {sorted(missing_agent_paths)}")
print("Product image supports API keys, Agent REST and MCP.")

properties = schema["components"]["schemas"][model]["properties"]
if set(properties.get("locale", {}).get("enum", [])) != {"zh-Hans", "zh-Hant", "en", "ja"}:
    raise SystemExit("Product image is missing four-language authentication.")

for locale in ("zh-Hans", "zh-Hant", "en", "ja"):
    assert (
        f'lang="{locale}"'
        in render_login_email(
            "004281", expires_minutes=15, site_url="https://example.test", locale=locale
        ).html
    )
print("Product image supports four-language catalogue and email rendering.")

notification_template = files("genchi_product").joinpath("templates/notification.html")
if not notification_template.is_file():
    raise SystemExit("Product image is missing the activity notification template.")
changes = [
    *({"summary": "更新：活动开始"} for _ in range(14)),
    *({"summary": "更新：开放入场"} for _ in range(14)),
]
groups = grouped_changes(changes)
if groups != [("更新：活动开始", 14), ("更新：开放入场", 14)]:
    raise SystemExit("Product image does not aggregate repeated activity changes.")
report_rows = [(localized_change(summary, 1, "zh-Hans"), count) for summary, count in groups]
notification = render_notification_email(
    "活动信息更新 · 示例活动",
    locale="zh-Hans",
    site_url="https://example.test",
    eyebrow="活动信息更新",
    heading="示例活动",
    intro="这次共更新 28 项记录，已为你合并相同内容。",
    report_rows=report_rows,
    cta_label="活动详情与官方依据",
    cta_url="https://example.test/zh-Hans/activities/example",
    unsubscribe_url="https://example.test/zh-Hans/unsubscribe?token=test-only",
)
if "变更内容\t记录数" not in notification.text or 'role="table"' not in notification.html:
    raise SystemExit("Product image notification is missing the readable report table.")
for expected in ("更新：活动开始", "更新：开放入场"):
    if notification.text.count(expected) != 1 or notification.html.count(expected) != 1:
        raise SystemExit("Product image notification content is not aggregated consistently.")
if notification.text.count("\t14") != 2 or notification.html.count(">14</td>") != 2:
    raise SystemExit("Product image notification report does not show grouped counts.")
print("Product image includes branded, aggregated activity notifications.")
