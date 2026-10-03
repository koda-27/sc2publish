import json
from types import SimpleNamespace

from click.testing import CliRunner

from sc2publish.cli import cli
from sc2publish.config import load_account, parse_dotenv


def test_help_works_without_credentials():
    result = CliRunner().invoke(cli, ['--help'])
    assert result.exit_code == 0
    for name in ('build', 'check', 'push', 'status', 'auth', 'login', 'doctor'):
        assert name in result.output
    assert 'baseline' not in cli.commands
    assert '--metadata' not in CliRunner().invoke(cli, ['push', '--help']).output


def test_auth_only_prompts_for_email_and_hidden_password(tmp_path, monkeypatch):
    from sc2publish import connection

    seen = {}

    def connect(project, *, session):
        seen['credentials'] = session.read_password()
        session.save(b'EU-SYNTHETIC')
        session.finish()
        session.clear_password()
        return SimpleNamespace(identity={'toon_name': 'Discovered#123'}, close=lambda: None)

    monkeypatch.setattr(connection, 'connect', connect)
    result = CliRunner().invoke(cli, ['auth'], input='placeholder@example.invalid\nnot-a-real-password\n')
    assert result.exit_code == 0, result.output
    assert seen['credentials'] == ('placeholder@example.invalid', 'not-a-real-password')
    assert 'Blizzard email' in result.output
    assert 'Blizzard password' in result.output
    assert 'not-a-real-password' not in result.output
    assert 'EU-SYNTHETIC' not in result.output
    assert 'Discovered#123' in result.output
    config = tmp_path / 'config/.env'
    assert config.is_file()
    assert not (tmp_path / '.env').exists()
    assert 'PASSWORD' not in config.read_text()


def test_auth_dry_run_never_connects_or_writes(tmp_path, monkeypatch):
    from sc2publish import connection

    monkeypatch.setattr(connection, 'connect', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('network')))
    result = CliRunner().invoke(cli, ['--json', 'auth', '--credentials', '-', '--dry-run'],
                                input='placeholder@example.invalid\nnot-a-real-password\n')
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {'status': 'credentials valid', 'network_used': False, 'files_written': False}
    assert not (tmp_path / 'config').exists()
    assert 'not-a-real-password' not in result.output


def test_auth_uses_project_region_and_explicit_new_env(tmp_path, monkeypatch):
    from sc2publish import connection

    (tmp_path / 'components').mkdir()
    manifest = tmp_path / 'project.toml'
    manifest.write_text('[project]\nname="Example"\nregion="US"\nlocale="enUS"\n'
                        '[build]\nlayers=["components"]\noutput="build"\n')
    old = tmp_path / '.env'
    old.write_text('SC2PUBLISH_TOON_NAME=Other#123\n')
    chosen = tmp_path / 'private/.env'

    def connect(project, *, session):
        assert project.region == 'US'
        assert session.env_path == chosen
        assert 'toon_name' not in session.account
        session.save(b'US-SYNTHETIC')
        return SimpleNamespace(identity={'toon_name': 'Discovered#123'}, close=lambda: None)

    monkeypatch.setattr(connection, 'connect', connect)
    result = CliRunner().invoke(cli, ['-p', str(manifest), '--env', str(chosen), 'auth', '--credentials', '-'],
                                input='placeholder@example.invalid\nnot-a-real-password\n')
    assert result.exit_code == 0, result.output
    assert parse_dotenv(chosen.read_text())['SC2PUBLISH_REGION'] == 'US'
    assert old.read_text() == 'SC2PUBLISH_TOON_NAME=Other#123\n'


def test_global_credentials_reach_normal_online_commands(tmp_path, monkeypatch):
    from sc2publish import connection

    (tmp_path / 'components').mkdir()
    manifest = tmp_path / 'project.toml'
    manifest.write_text('[project]\nname="Example"\n[build]\nlayers=["components"]\n')
    credentials = tmp_path / 'credentials.txt'
    credentials.write_text('placeholder@example.invalid\nnot-a-real-password\n')

    def connect(project):
        assert project.credentials_file == str(credentials)
        return SimpleNamespace(prepublish=lambda: {'m_priorRevision': None}, close=lambda: None)

    monkeypatch.setattr(connection, 'connect', connect)
    result = CliRunner().invoke(cli, ['--credentials', str(credentials), '-p', str(manifest), 'status', '--remote'])
    assert result.exit_code == 0, result.output


def test_auth_invalid_project_is_not_silently_ignored(tmp_path):
    result = CliRunner().invoke(cli, ['-p', str(tmp_path / 'missing.toml'), 'auth', '--dry-run'])
    assert result.exit_code != 0


def test_account_settings_load_from_env_and_process(tmp_path, monkeypatch):
    env = tmp_path / '.env'
    env.write_text('SC2PUBLISH_EMAIL=placeholder@example.invalid\n')
    monkeypatch.setenv('SC2PUBLISH_PASSWORD', 'not-a-real-password')
    project = SimpleNamespace(base=tmp_path, region='EU')
    values, path = load_account(project, env)
    assert values['email'] == 'placeholder@example.invalid'
    assert values['password'] == 'not-a-real-password'
    assert 'web_credential' not in values
    assert path == env.resolve()


def test_login_is_the_same_authentication_command():
    assert cli.commands['login'] is cli.commands['auth']
