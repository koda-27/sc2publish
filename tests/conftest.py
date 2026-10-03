"""All tests run without real account settings, OS credentials or network access."""

import os
import socket

import keyring
import pytest

from sc2publish import config


@pytest.fixture(autouse=True)
def isolate_accounts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in os.environ:
        if name.startswith('SC2PUBLISH_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('SC2PUBLISH_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setattr(config, 'user_state_path', lambda _: tmp_path / 'state')
    monkeypatch.setattr(keyring, 'get_password', lambda *args: None)

    def forbidden(*args, **kwargs):
        pytest.fail('Tests must not use real networks or OS credentials')

    monkeypatch.setattr(keyring, 'set_password', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
