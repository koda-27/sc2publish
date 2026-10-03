"""Credential sources and private session caching shared by all online commands."""

import base64
import sys
from pathlib import Path

import click
import keyring
import requests
from keyring.errors import KeyringError

from .config import config_home, load_account, update_env_file
from .errors import PublishError
from .login import LoginError, read_credentials, web_login

KEYRING_SERVICE = 'sc2publish.blizzard'


class AccountSession:
    """One login attempt; passwords never enter the token cache or log output."""

    def __init__(self, project, *, credentials=None, email=None, remember=False,
                 prompt=None, force=False):
        self.project = project
        self.account, self.env_path = load_account(project, getattr(project, 'env_file', None))
        self.credentials_file = (credentials or getattr(project, 'credentials_file', None)
                                 or self.account.get('credentials_file'))
        if not self.credentials_file and (config_home() / 'credentials.txt').is_file():
            self.credentials_file = config_home() / 'credentials.txt'
        self.email = email or self.account.get('email')
        self.password = None
        self.remember = remember
        self.prompt = sys.stdin.isatty() if prompt is None else prompt
        self.credential = None
        # Explicit new credentials must not silently reuse another account's token.
        self.force = force
        self.explicit_identity = bool(self.credentials_file or email or self.account.get('password'))
        if self.account.get('region', project.region).upper() != project.region:
            self.force = True

    def cached_credential(self):
        if self.force:
            return None
        if self.credentials_file:
            self.read_password()
        bound_email = self.account.get('credential_email')
        if self.explicit_identity and not bound_email:
            return None
        if bound_email and self.email and bound_email.casefold() != self.email.casefold():
            return None
        try:
            value = base64.b64decode(self.account.get('web_credential', ''), validate=True)
            return value or None
        except (ValueError, TypeError):
            return None

    def read_password(self):
        if self.password is not None:
            return self.email, self.password
        source = self.credentials_file
        if source:
            try:
                path = '-' if str(source) == '-' else Path(source).expanduser()
                self.email, self.password = read_credentials(path)
            except (OSError, ValueError, LoginError):
                raise PublishError('Could not read Blizzard email/password from the credentials file.') from None
        else:
            if self.email:
                self.password = self.account.get('password')
                if not self.password:
                    try:
                        self.password = keyring.get_password(KEYRING_SERVICE, self.email)
                    except KeyringError:
                        pass
            if not self.email or not self.password:
                if not self.prompt:
                    raise PublishError('Blizzard credentials are required. Run sc2publish auth, supply '
                                       '--credentials FILE, or set SC2PUBLISH_EMAIL and SC2PUBLISH_PASSWORD.')
                self.email = self.email or click.prompt('Blizzard email')
                self.password = click.prompt('Blizzard password', hide_input=True)
        if not self.email or not self.password:
            raise PublishError('Blizzard email and password must both be non-empty.')
        return self.email, self.password

    def mfa_code(self, kind):
        if not self.prompt:
            raise LoginError(f'Battle.net requires {kind} verification. Run sc2publish auth interactively.')
        return click.prompt(f'Blizzard {kind} code', hide_input=True)

    def answer_challenge(self, url):
        email, password = self.read_password()
        try:
            with requests.Session() as http:
                return web_login(email, password, self.project.region, http,
                                 locale=self.project.locale, login_url=url, mfa_provider=self.mfa_code)
        except LoginError as exc:
            raise PublishError(str(exc)) from None

    def save(self, credential):
        """Called only after the server has accepted and renewed a credential."""
        self.credential = credential
        updates = {'web_credential': base64.b64encode(credential).decode(),
                   'region': self.project.region}
        if self.email:
            updates['email'] = self.email
            updates['credential_email'] = self.email
        update_env_file(self.env_path, updates)
        self.account.update(updates)

    def finish(self):
        """Optionally remember the password after the full SC2 session succeeds."""
        if self.remember and self.password:
            try:
                keyring.set_password(KEYRING_SERVICE, self.email, self.password)
            except KeyringError:
                raise PublishError('Login succeeded and its token was cached, but the OS keyring is '
                                   'unavailable. Use an external credentials file for unattended renewal.') from None

    def clear_password(self):
        self.password = None
        self.account.pop('password', None)
