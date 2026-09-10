"""Deployment config must preserve secrets and keep incomplete integrations idle."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("genchi_deploy", REPO / "deploy/manage.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def test_generated_sources_are_not_nested_under_replaceable_repository_mount():
    base = yaml.safe_load((REPO / "docker-compose.yml").read_text())["services"]["control"]
    production = yaml.safe_load((REPO / "deploy/compose.production.yml").read_text())["services"]["control"]
    runtime = production["environment"]["ALLFEEDS_SOURCES"]
    mounted = [volume.split(":")[1] for volume in production["volumes"]]
    assert runtime in mounted
    for volume in base["volumes"]:
        repository_mount = volume.split(":")[1]
        assert not runtime.startswith(repository_mount.rstrip("/") + "/")


def test_deployment_bootstrap_preserves_secrets_and_pauses_unconfigured_integrations(tmp_path):
    config = tmp_path / "genchi.collector/config"
    config.mkdir(parents=True)
    shutil.copy2(REPO / "config/sources.yaml", config / "sources.yaml")
    # Exercise gating even if an operator enabled Natalie in the source file.
    source_path = config / "sources.yaml"
    source_text = source_path.read_text()
    for source_id in ("natalie-comic-news", "natalie-music-news"):
        source_text = source_text.replace(f"  - id: {source_id}\n    enabled: false", f"  - id: {source_id}\n    enabled: true")
    source_path.write_text(source_text)
    deploy.init_env(tmp_path, "http://192.0.2.10")
    env = tmp_path / ".env"
    original = env.read_bytes()
    values = deploy.prepare(tmp_path)
    assert len({values[key] for key in deploy.INTERNAL_KEYS}) == len(deploy.INTERNAL_KEYS)
    assert values["CATALOG_WORKER_MODE"] == "idle"
    assert values["SMTP_HOST"] == "smtp.resend.com"
    assert values["NOTIFIER_WORKER_MODE"] == "idle"
    assert env.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / ".deploy/effective.env").stat().st_mode & 0o777 == 0o600
    sources = (tmp_path / ".deploy/sources.yaml").read_text()
    original_sources = {s["id"]: s for s in yaml.safe_load((config / "sources.yaml").read_text())["sources"]}
    for source in yaml.safe_load(sources)["sources"]:
        if source["fetcher"] == "genchi.x_profile" or source["id"] in {"natalie-comic-news", "natalie-music-news"}:
            assert source["enabled"] is False
        else:
            assert source["enabled"] == original_sources[source["id"]]["enabled"]
    with pytest.raises(ValueError, match="Already exists"):
        deploy.init_env(tmp_path, "http://192.0.2.11")
    assert env.read_bytes() == original
    # Merely checking a disabled integration must not erase a user-supplied cookie file.
    cookies = tmp_path / "secrets/x-cookies.json"
    cookies.write_text('[{"name":"auth_token","value":"test-only"}]')
    deploy.prepare(tmp_path)
    assert "test-only" in cookies.read_text()
    assert cookies.parent.stat().st_mode & 0o777 == 0o700
    assert cookies.stat().st_mode & 0o777 == 0o644


def test_browser_env_migration_preserves_tokens_and_other_integrations(tmp_path):
    path = tmp_path / ".env"
    old = "CLOAKBROWSER_API_TOKEN='literal$secret # value'\nCLOAKBROWSER_CONCURRENCY=1\nCLOAKBROWSER_IMAGE=old/image\nCLOAKBROWSER_LICENSE_KEY=old-license\nRESEND_API_KEY=keep-me\n"
    path.write_text(old)
    deploy.migrate_browser_env(path)
    values = deploy.read_env(path)
    assert values == {
        "BROWSER_API_TOKEN": "literal$secret # value",
        "BROWSER_CONCURRENCY": "1",
        "RESEND_API_KEY": "keep-me",
    }
    assert (tmp_path / ".deploy/env-before-camoufox").read_text() == old
    before = path.read_bytes()
    deploy.migrate_browser_env(path)
    assert path.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o600


def test_deployment_env_literals_validation_and_model_activation(tmp_path):
    config = tmp_path / "genchi.collector/config"
    config.mkdir(parents=True)
    shutil.copy2(REPO / "config/sources.yaml", config / "sources.yaml")
    deploy.init_env(tmp_path, "https://events.example.test")
    env = tmp_path / ".env"
    text = env.read_text().replace("LLM_API_KEY=\n", "LLM_API_KEY='literal$secret # value'\n")
    env.write_text(text)
    with pytest.raises(ValueError, match="together"):
        deploy.prepare(tmp_path)
    text = text.replace("LLM_BASE_URL=\n", "LLM_BASE_URL=https://model.example.test/v1\n")
    text = text.replace("LLM_MODEL=\n", "LLM_MODEL=example\n")
    env.write_text(text)
    assert deploy.prepare(tmp_path)["CATALOG_WORKER_MODE"] == "catalog"
    effective = deploy.read_env(tmp_path / ".deploy/effective.env")
    assert effective["LLM_API_KEY"] == "literal$secret # value"
    assert effective["PUBLIC_SITE_URL"] == "https://events.example.test"
    env.write_text(text.replace("RESEND_API_KEY=\n", "RESEND_API_KEY=re_test_only\n"))
    resend = deploy.prepare(tmp_path)
    assert resend["NOTIFIER_WORKER_MODE"] == "notifications"
    assert (
        resend["SMTP_HOST"],
        resend["SMTP_PORT"],
        resend["SMTP_SECURITY"],
        resend["SMTP_USER"],
    ) == ("smtp.resend.com", "465", "ssl", "resend")
    assert resend["SMTP_PASSWORD"] == "re_test_only"
    env.write_text(
        text.replace("MAIL_PROVIDER=resend", "MAIL_PROVIDER=smtp")
        + "\nSMTP_HOST=smtp.example.test\nSMTP_SECURITY=none\n"
    )
    with pytest.raises(ValueError, match="requires SMTP_SECURITY"):
        deploy.prepare(tmp_path)


def test_x_anonymous_enable_needs_no_cookie_but_cookie_mode_is_explicit(tmp_path):
    config = tmp_path / "genchi.collector/config"
    config.mkdir(parents=True)
    shutil.copy2(REPO / "config/sources.yaml", config / "sources.yaml")
    deploy.init_env(tmp_path, "https://events.example.test")
    env = tmp_path / ".env"
    enabled = env.read_text().replace("ENABLE_X=false", "ENABLE_X=true")
    env.write_text(enabled)
    assert deploy.prepare(tmp_path)["X_AUTH_MODE"] == "anonymous"
    sources = yaml.safe_load((tmp_path / ".deploy/sources.yaml").read_text())["sources"]
    originals = {s["id"]: s for s in yaml.safe_load((REPO / "config/sources.yaml").read_text())["sources"]}
    assert all(s["enabled"] == originals[s["id"]]["enabled"] for s in sources)
    env.write_text(enabled.replace("X_AUTH_MODE=anonymous", "X_AUTH_MODE=cookies"))
    with pytest.raises(ValueError, match="Cookie JSON"):
        deploy.prepare(tmp_path)
    env.write_text(enabled.replace("X_AUTH_MODE=anonymous", "X_AUTH_MODE=unknown"))
    with pytest.raises(ValueError, match="X_AUTH_MODE"):
        deploy.prepare(tmp_path)


def test_visual_agent_requires_model_and_resolves_separate_credential_settings(tmp_path):
    config = tmp_path / 'genchi.collector/config'
    config.mkdir(parents=True)
    shutil.copy2(REPO / 'config/sources.yaml', config / 'sources.yaml')
    deploy.init_env(tmp_path, 'https://events.example.test')
    path = tmp_path / '.env'
    original = path.read_text()
    enabled = original.replace('VERIFICATION_AGENT_ENABLED=false', 'VERIFICATION_AGENT_ENABLED=true')
    path.write_text(enabled)
    with pytest.raises(ValueError, match='vision model'):
        deploy.prepare(tmp_path)
    configured = enabled.replace('\nLLM_API_KEY=\n', '\nLLM_API_KEY=test-private-key\n').replace(
        '\nLLM_BASE_URL=\n', '\nLLM_BASE_URL=https://provider.example.test\n').replace('\nLLM_MODEL=\n', '\nLLM_MODEL=text-only\n').replace(
        '\nVERIFICATION_LLM_MODEL=\n', '\nVERIFICATION_LLM_MODEL=vision-only\n')
    path.write_text(configured)
    values = deploy.prepare(tmp_path)
    assert values['VERIFICATION_LLM_API_KEY'] == 'test-private-key'
    assert values['VERIFICATION_LLM_BASE_URL'] == 'https://provider.example.test'
    assert values['VERIFICATION_LLM_MODEL'] == 'vision-only'
    assert values['LLM_MODEL'] == 'text-only'
    assert path.read_text() == configured
    separate = configured.replace('\nVERIFICATION_LLM_BASE_URL=\n', '\nVERIFICATION_LLM_BASE_URL=https://vision.example.test\n')
    path.write_text(separate)
    with pytest.raises(ValueError, match='own API key'):
        deploy.prepare(tmp_path)
    path.write_text(separate.replace('\nVERIFICATION_LLM_API_KEY=\n', '\nVERIFICATION_LLM_API_KEY=vision-test-key\n'))
    assert deploy.prepare(tmp_path)['VERIFICATION_LLM_API_KEY'] == 'vision-test-key'
    # Model credentials stay in the operator container, never browser children.
    compose = yaml.safe_load((REPO / 'docker-compose.yml').read_text())['services']
    assert 'VERIFICATION_LLM_API_KEY' not in compose['browser']['environment']
    assert 'VERIFICATION_LLM_API_KEY' in compose['verification-agent']['environment']
    assert 'ports' not in compose['verification-agent']
