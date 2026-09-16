"""Public release safeguards must not leak or overwrite local secrets."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_init_generates_independent_private_credentials_without_overwrite(tmp_path):
    initializer = load_script('init-local-env')
    (tmp_path / '.env.example').write_bytes((ROOT / '.env.example').read_bytes())
    initializer.initialize(tmp_path)
    path = tmp_path / '.env'
    before = path.read_bytes()
    values = dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line and not line.startswith('#'))
    secrets = [values[key] for key in initializer.KEYS]
    assert len(set(secrets)) == 8
    assert all(len(value) == 64 for value in secrets)
    assert values['LLM_API_KEY'] == values['RESEND_API_KEY'] == ''
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        initializer.initialize(tmp_path)
    assert path.read_bytes() == before


def test_release_check_detects_deleted_historical_secret_without_printing_it(tmp_path, capsys):
    checker = load_script('check-public-release')
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    path = tmp_path / 'config.txt'
    secret = b'private-test-value-for-history'
    path.write_bytes(secret)
    subprocess.run(['git', '-C', str(tmp_path), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.test', 'commit', '-qm', 'fixture'], check=True)
    path.write_text('clean')
    assert checker.scan(tmp_path, False, [secret]) == 0
    assert checker.scan(tmp_path, True, [secret]) == 1
    output = capsys.readouterr().out
    assert 'local-secret-value' in output
    assert secret.decode() not in output


def test_private_file_rules_allow_templates_and_reject_runtime_artifacts():
    checker = load_script('check-public-release')
    for name in ['.env', '.env.production', 'production.env', 'secrets/x.json',
                 '.deploy/effective.env', 'x-cookies.json', 'backups/data.dump',
                 'worker/identity.json', 'private/report.json', 'repo.bundle']:
        assert checker.private_path(name), name
    for name in ['.env.example', 'deploy/production.env.example', 'config/sources.yaml']:
        assert not checker.private_path(name), name


def test_git_ignores_private_outputs_but_keeps_templates():
    names = ['.deploy/effective.env', 'secrets/x-cookies.json', 'backups/db.dump',
             'private/report.json', 'server.key', 'repo.bundle', 'production.env']
    result = subprocess.run(['git', 'check-ignore', '--no-index', '--stdin'], cwd=ROOT,
                            input='\n'.join(names), text=True, capture_output=True, check=True)
    assert result.stdout.splitlines() == names
    result = subprocess.run(['git', 'check-ignore', '--no-index', '--stdin'], cwd=ROOT,
                            input='.env.example\ndeploy/production.env.example\n',
                            text=True, capture_output=True)
    assert result.returncode == 1
