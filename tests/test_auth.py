import base64
import os
from types import SimpleNamespace

import keyring
import pytest
from keyring.errors import NoKeyringError

from sc2publish.auth import KEYRING_SERVICE, AccountSession
from sc2publish.config import parse_dotenv
from sc2publish.errors import PublishError


def project(tmp_path, **kwargs):
    return SimpleNamespace(base=tmp_path, region='EU', locale='enUS', env_file=None, **kwargs)


def test_external_credentials_need_no_session_or_character_settings(tmp_path):
    credentials = tmp_path / 'private/password.txt'
    credentials.parent.mkdir()
    credentials.write_text('placeholder@example.invalid\nnot-a-real-password\n')
    session = AccountSession(project(tmp_path, credentials_file=str(credentials)), prompt=False)
    assert session.cached_credential() is None
    assert session.read_password() == ('placeholder@example.invalid', 'not-a-real-password')


def test_password_environment_and_keyring_sources(tmp_path, monkeypatch):
    monkeypatch.setenv('SC2PUBLISH_EMAIL', 'placeholder@example.invalid')
    monkeypatch.setenv('SC2PUBLISH_PASSWORD', 'from-environment')
    assert AccountSession(project(tmp_path), prompt=False).read_password()[1] == 'from-environment'
    monkeypatch.delenv('SC2PUBLISH_PASSWORD')
    calls = []

    def get(service, email):
        calls.append((service, email))
        return 'from-keyring'

    monkeypatch.setattr(keyring, 'get_password', get)
    assert AccountSession(project(tmp_path), prompt=False).read_password()[1] == 'from-keyring'
    assert calls == [(KEYRING_SERVICE, 'placeholder@example.invalid')]


def test_token_cache_excludes_password_and_defaults_outside_project(tmp_path, monkeypatch):
    monkeypatch.setenv('SC2PUBLISH_EMAIL', 'placeholder@example.invalid')
    monkeypatch.setenv('SC2PUBLISH_PASSWORD', 'not-a-real-password')
    session = AccountSession(project(tmp_path), prompt=False)
    session.read_password()
    session.save(b'EU-SYNTHETIC')
    cache = tmp_path / 'config/.env'
    assert session.env_path == cache
    assert not (tmp_path / '.env').exists()
    assert 'not-a-real-password' not in cache.read_text()
    assert 'PASSWORD' not in cache.read_text()
    assert base64.b64decode(parse_dotenv(cache.read_text())['SC2PUBLISH_WEB_CREDENTIAL']) == b'EU-SYNTHETIC'
    if os.name != 'nt':
        assert cache.stat().st_mode & 0o777 == 0o600
    session.clear_password()
    assert session.password is None
    assert 'password' not in session.account


def test_remember_uses_keyring_only_after_success(tmp_path, monkeypatch):
    monkeypatch.setenv('SC2PUBLISH_EMAIL', 'placeholder@example.invalid')
    monkeypatch.setenv('SC2PUBLISH_PASSWORD', 'not-a-real-password')
    saved = []
    monkeypatch.setattr(keyring, 'set_password', lambda *values: saved.append(values))
    session = AccountSession(project(tmp_path), prompt=False, remember=True)
    session.read_password()
    assert not saved
    session.save(b'EU-SYNTHETIC')
    session.finish()
    assert saved == [(KEYRING_SERVICE, 'placeholder@example.invalid', 'not-a-real-password')]


def test_missing_keyring_never_falls_back_to_plaintext_password(tmp_path, monkeypatch):
    monkeypatch.setenv('SC2PUBLISH_EMAIL', 'placeholder@example.invalid')
    monkeypatch.setenv('SC2PUBLISH_PASSWORD', 'not-a-real-password')

    def unavailable(*args):
        raise NoKeyringError('synthetic backend failure')

    monkeypatch.setattr(keyring, 'set_password', unavailable)
    session = AccountSession(project(tmp_path), prompt=False, remember=True)
    session.read_password()
    session.save(b'EU-SYNTHETIC')
    with pytest.raises(PublishError, match='keyring is unavailable'):
        session.finish()
    assert 'not-a-real-password' not in session.env_path.read_text()


def test_new_account_or_region_does_not_reuse_cached_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('SC2PUBLISH_WEB_CREDENTIAL', base64.b64encode(b'EU-OLD').decode())
    assert AccountSession(project(tmp_path)).cached_credential() == b'EU-OLD'
    assert AccountSession(project(tmp_path), email='other@example.invalid').cached_credential() is None
    file = tmp_path / 'password.txt'
    file.write_text('other@example.invalid\nnot-a-real-password\n')
    assert AccountSession(project(tmp_path), credentials=str(file)).cached_credential() is None
    monkeypatch.setenv('SC2PUBLISH_REGION', 'US')
    assert AccountSession(project(tmp_path)).cached_credential() is None


def test_same_credentials_reuse_their_bound_cached_token(tmp_path, monkeypatch):
    monkeypatch.setenv('SC2PUBLISH_EMAIL', 'placeholder@example.invalid')
    monkeypatch.setenv('SC2PUBLISH_PASSWORD', 'not-a-real-password')
    session = AccountSession(project(tmp_path), prompt=False)
    session.read_password()
    session.save(b'EU-CACHED')
    assert AccountSession(project(tmp_path), prompt=False).cached_credential() == b'EU-CACHED'
    monkeypatch.setenv('SC2PUBLISH_EMAIL', 'other@example.invalid')
    assert AccountSession(project(tmp_path), prompt=False).cached_credential() is None


def test_no_credentials_fails_cleanly_in_noninteractive_mode(tmp_path):
    session = AccountSession(project(tmp_path), prompt=False)
    with pytest.raises(PublishError, match='Blizzard credentials are required'):
        session.read_password()
