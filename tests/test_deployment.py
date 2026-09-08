"""Deployment config must preserve secrets and keep incomplete integrations idle."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("genchi_deploy", REPO / "deploy/manage.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def test_deployment_bootstrap_preserves_secrets_and_pauses_unconfigured_integrations(tmp_path):
    config = tmp_path / "genchi.collector/config"
    config.mkdir(parents=True)
    shutil.copy2(REPO / "config/sources.yaml", config / "sources.yaml")
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
    assert sources.splitlines().count("    enabled: false") == 4
    assert sources.splitlines().count("    enabled: true") == 8
    with pytest.raises(ValueError, match="Already exists"):
        deploy.init_env(tmp_path, "http://192.0.2.11")
    assert env.read_bytes() == original
    # Merely checking a disabled integration must not erase a user-supplied cookie file.
    cookies = tmp_path / "secrets/x-cookies.json"
    cookies.write_text('[{"name":"auth_token","value":"test-only"}]')
    deploy.prepare(tmp_path)
    assert "test-only" in cookies.read_text()


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
