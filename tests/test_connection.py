"""Synthetic wire exchanges exercise the publisher's real authentication path."""

import base64
from types import SimpleNamespace

import pytest

from sc2publish import auth, connection
from sc2publish.auth import AccountSession
from sc2publish.config import parse_dotenv
from sc2publish.connection import AUTH_TYPE, Front, Native
from sc2publish.errors import PublishError
from sc2publish.login import AUTH_LISTENER, CHALLENGE_LISTENER
from sc2publish.protocol.bits import Writer
from sc2publish.protocol.codec import Codec
from sc2publish.protocol.wire import field, parse, unpack

LOGIN_URL = 'https://eu.account.battle.net/login/?externalChallenge=login&app=s2'


def frame(service=None, method=0, token=0, body=b'', response=False):
    header = field(1, 254 if response else 0, 0) + field(2, method, 0) + field(3, token, 0)
    if service is not None:
        header += field(11, service, 5)
    return len(header).to_bytes(2, 'big') + header + body


class FakeSocket:
    def __init__(self, incoming):
        self.incoming = iter(incoming)
        self.sent = []
        self.closed = False

    def send(self, data, opcode):
        assert opcode == 2
        self.sent.append(data)

    def recv(self):
        return next(self.incoming)

    def close(self):
        self.closed = True


def front_script(challenge=True, reject=False):
    challenge_body = field(2, 'web_auth_url') + field(3, LOGIN_URL)
    frames = [frame(response=True, token=1)]
    if challenge:
        frames.append(frame(CHALLENGE_LISTENER, 3, body=challenge_body))
    if reject:
        frames.append(frame(CHALLENGE_LISTENER, 3, body=challenge_body))
        return frames
    frames.append(frame(AUTH_LISTENER, 5, body=field(1, 0, 0) + field(3, b'synthetic-game-account')
                        + field(9, b'k' * 64)))
    renew_token = 4 if challenge else 3
    frames.append(frame(response=True, token=renew_token, body=field(1, b'EU-RENEWED')))
    attribute = field(1, 'address') + field(2, field(5, 'eu-s2.logon.battle.net'))
    frames.append(frame(response=True, token=renew_token + 1, body=field(1, attribute)))
    return frames


@pytest.mark.parametrize('cached', [None, b'EU-EXPIRED'])
def test_first_login_and_expired_token_use_same_publisher_connection(tmp_path, monkeypatch, cached):
    monkeypatch.setenv('SC2PUBLISH_EMAIL', 'placeholder@example.invalid')
    if cached:
        monkeypatch.setenv('SC2PUBLISH_WEB_CREDENTIAL', base64.b64encode(cached).decode())
    monkeypatch.setattr(auth.keyring, 'get_password', lambda *args: 'not-a-real-password')
    seen = []

    def web_login(email, password, region, http, **kwargs):
        seen.append((email, password, region, kwargs['login_url']))
        return b'EU-NEW'

    monkeypatch.setattr(auth, 'web_login', web_login)
    ws = FakeSocket(front_script())
    monkeypatch.setattr(connection.websocket, 'create_connection', lambda *a, **kw: ws)
    p = SimpleNamespace(base=tmp_path, env_file=None, region='EU', locale='enUS')
    session = AccountSession(p, prompt=False)
    front = Front(p, session)
    assert front.authenticate()['address'] == 'eu-s2.logon.battle.net'
    front.close()
    assert ws.closed
    assert seen == [('placeholder@example.invalid', 'not-a-real-password', 'EU', LOGIN_URL)]
    messages = [unpack(data) for data in ws.sent]
    logon = {n: value for n, _, value in parse(messages[1][1])}
    assert logon.get(12) == cached
    assert messages[2][0][2] == 7
    assert messages[2][1] == field(1, b'EU-NEW')
    values = parse_dotenv(session.env_path.read_text())
    assert base64.b64decode(values['SC2PUBLISH_WEB_CREDENTIAL']) == b'EU-RENEWED'
    assert 'not-a-real-password' not in session.env_path.read_text()


def test_valid_token_does_not_ask_for_password(tmp_path, monkeypatch):
    monkeypatch.setenv('SC2PUBLISH_WEB_CREDENTIAL', base64.b64encode(b'EU-CACHED').decode())
    monkeypatch.setattr(auth, 'web_login', lambda *a, **kw: pytest.fail('unnecessary password login'))
    ws = FakeSocket(front_script(challenge=False))
    monkeypatch.setattr(connection.websocket, 'create_connection', lambda *a, **kw: ws)
    p = SimpleNamespace(base=tmp_path, env_file=None, region='EU', locale='enUS')
    front = Front(p)
    front.authenticate()
    assert not any(unpack(data)[0][2] == 7 for data in ws.sent)


def test_rejected_password_login_is_not_retried_or_saved(tmp_path, monkeypatch):
    ws = FakeSocket(front_script(reject=True))
    monkeypatch.setattr(connection.websocket, 'create_connection', lambda *a, **kw: ws)
    p = SimpleNamespace(base=tmp_path, env_file=None, region='EU', locale='enUS')
    session = AccountSession(p, prompt=False)
    calls = []
    monkeypatch.setattr(session, 'answer_challenge', lambda url: calls.append(url) or b'EU-REJECTED')
    with pytest.raises(PublishError, match='no automatic retry'):
        connection.connect(p, session=session)
    assert calls == [LOGIN_URL]
    assert not session.env_path.exists()
    assert ws.closed


def test_native_failure_closes_front_and_clears_password(tmp_path, monkeypatch):
    p = SimpleNamespace(base=tmp_path, env_file=None, region='EU', locale='enUS')
    session = AccountSession(p)
    session.password = 'synthetic-password'
    closed = []
    fake = SimpleNamespace(authenticate=lambda: {}, account={}, close=lambda: closed.append(True))
    monkeypatch.setattr(connection, 'Front', lambda *args: fake)

    def fail(*args):
        raise PublishError('synthetic native failure')

    monkeypatch.setattr(connection, 'Native', fail)
    with pytest.raises(PublishError):
        connection.connect(p, session=session)
    assert closed == [True]
    assert session.password is None


def encoded_handoff(game_region=3):
    codec = Codec()
    wrapper = codec.default(AUTH_TYPE + 'LogonResponse3')
    success = wrapper['LogonResponse']['m_result']['value']
    success.update(m_accountRegion=1, m_gameAccountRegion=game_region, m_gameAccountName=b'SC2-Synthetic')
    writer = Writer()
    codec.encode(writer, AUTH_TYPE + 'LogonResponse3', wrapper)
    return {'account_region': 1, 'game_account_name': 'SC2-Synthetic', 'logon_response': bytes(writer.data)}


def test_game_region_is_decoded_from_server_not_assumed_from_endpoint():
    native = Native.__new__(Native)
    native.codec = Codec()
    assert native.game_account_region(encoded_handoff(3)) == 3
    bad = encoded_handoff(3)
    bad['game_account_name'] = 'Different-Synthetic'
    with pytest.raises(PublishError, match='inconsistent'):
        native.game_account_region(bad)


def independent_toon_list():
    # Reference wire order from the upstream native protocol, not Codec.encode.
    writer = Writer()
    writer.write(0, 6)
    writer.write(1, 1)
    writer.write(15, 4)
    writer.write(1, 6)
    name = b'Synthetic#123'
    writer.write(len(name) - 2, 7)
    writer.bytes(name)
    writer.write(123 ^ 0x80000000, 32)  # signed last-online timestamp
    writer.write(0, 3)  # layout selector
    writer.write(0, 32)  # flags
    writer.write(7, 32)  # profile label
    writer.write(42, 64)  # profile id
    writer.write(2, 32)  # realm
    return bytes(writer.data)


def test_character_discovery_consumes_full_fragmented_wire_records():
    native = Native.__new__(Native)
    native.codec = Codec()
    listing = independent_toon_list()
    selected = native.codec.make('Client::Toon::ToonSelected', m_toonName='Synthetic#123', m_realm=2)
    response = native.codec.encode_record(6, 15, 'Client::Toon::ToonSelected', selected)
    native.buffer = bytearray(listing[:7])
    fragments = iter([listing[7:] + response[:4], response[4:]])
    native.read_more = lambda: native.buffer.extend(next(fragments))
    sent = []
    native.send_record = lambda *args: sent.append(args)
    native.select_character({})
    assert native.identity == {'toon_name': 'Synthetic#123', 'realm': 2}
    assert sent == [(5, 15, 'Client::Toon::ToonSelect', {'m_toonName': 'Synthetic#123', 'm_realm': 2})]
    assert not native.buffer


def test_account_without_sc2_character_is_reported_cleanly():
    native = Native.__new__(Native)
    native.wait_message = lambda *args: {'m_toonDisplays': []}
    with pytest.raises(PublishError, match='no StarCraft II character'):
        native.select_character({})
