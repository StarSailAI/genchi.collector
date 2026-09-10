"""Run inside the release image without credentials, network or a database."""

from genchi_product.api import create_app
from genchi_product.emails import render_login_email

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
    assert f'lang="{locale}"' in render_login_email("004281", expires_minutes=15, site_url="https://example.test", locale=locale).html
print("Product image supports four-language catalogue and email rendering.")
