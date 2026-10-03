"""Authenticated Front and native connections, generated from account settings."""
import hmac
import os
import socket
import time
from importlib.resources import files
from pathlib import Path

import websocket
from Crypto.Cipher import ARC4
from Crypto.Hash import SHA512
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

from .auth import AccountSession
from .config import private_write
from .errors import PublishError
from .login import AUTH_LISTENER, CHALLENGE_LISTENER, LoginError, challenge_from_body, logon_request
from .protocol.bits import Bits
from .protocol.codec import Codec
from .protocol.wire import field, packet, parse, unpack

AUTH = 0x0DECFC01
CONNECTION = 0x65446991
GAME_UTILITIES = 0x3FC1274D
AUTH_TYPE = 'Client::Authentication::'
MAP = 'Client::S2Map::'
IN_LABEL = bytes.fromhex('68e0c72eddd6d2f31e5ab155b18b631e')
OUT_LABEL = bytes.fromhex('dea965ae543a1e939e690caa68de7839')
NATIVE_VERSION = 661159
VERSIONS = [(b'\0\0S2', b'NGD1', 0x5bc8dcc1), (b'\0\0S2', b'NGD2', 0xfade3a32),
            (b'\0\0S2', b'NGD3', 0x0c129365), (b'\0\0S2', b'NGD4', 0x86b7c0ed),
            (b'Bnet', b'Wn64', NATIVE_VERSION)]


def mac(key, message):
    return hmac.digest(key, message, 'sha256')


class Front:
    def __init__(self, project, session=None):
        self.project = project
        self.session = session or AccountSession(project)
        self.account, self.env_path = self.session.account, self.session.env_path
        self.ws = websocket.create_connection(f'wss://{project.region.lower()}.actual.battle.net:1119/',
                                              timeout=25, subprotocols=['v1.rpc.battle.net'],
                                              suppress_origin=True)
        self.token = 0
        self.responses = {}

    def send(self, service, method, body):
        self.token += 1
        self.ws.send(packet(service, method, self.token, body), opcode=websocket.ABNF.OPCODE_BINARY)
        return self.token

    def receive(self):
        header, body = unpack(self.ws.recv())
        if header.get(6, 0):
            raise PublishError(f'Battle.net RPC error {header[6]}.')
        if header.get(1) == 254:
            self.responses[header.get(3)] = body
        return header, body

    def call(self, service, method, body):
        token = self.send(service, method, body)
        deadline = time.monotonic() + 45
        while token not in self.responses:
            if time.monotonic() > deadline:
                raise PublishError('Battle.net RPC timed out.')
            self.receive()
        return self.responses.pop(token)

    def authenticate(self):
        credential = self.session.cached_credential()
        self.call(CONNECTION, 1, field(3, 1, 0))
        logon = logon_request(self.project.locale, credential=credential,
                              sdk_version=self.account.get('sdk_version'), device_id=self.account.get('device_id'))
        self.send(AUTH, 1, logon)
        challenged = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            header, body = self.receive()
            if (header.get(11), header.get(2)) == (CHALLENGE_LISTENER, 3):
                if challenged:
                    raise PublishError('Battle.net rejected the new login credential; no automatic retry was made.')
                challenged = True
                try:
                    url = challenge_from_body(body)
                except LoginError as exc:
                    raise PublishError(str(exc)) from None
                credential = self.session.answer_challenge(url)
                self.send(AUTH, 7, field(1, credential))
                # Interactive MFA time is not part of the Front response timeout.
                deadline = time.monotonic() + 60
            if (header.get(11), header.get(2)) == (AUTH_LISTENER, 5):
                values = parse(body)
                status = next((v for n, w, v in values if n == 1 and w == 0), 0)
                if status:
                    raise PublishError(f'Battle.net authentication failed (status {status}).')
                renewed = self.call(AUTH, 8, field(1, 0x5332, 5))
                try:
                    credential = next(v for n, w, v in parse(renewed) if n == 1 and w == 2 and v)
                    seed = next(v for n, w, v in values if n == 9 and w == 2)
                    account = next(v for n, w, v in values if n == 3 and w == 2)
                except StopIteration:
                    raise PublishError('Battle.net did not return a usable StarCraft II account and session.') from None
                self.session.save(credential)

                def attr(name, value, variant):
                    return field(1, field(1, name) + field(2, field(variant, value)))

                request = (attr('LogonTokenRequest', '0.0.1', 5)
                           + attr('environment', self.project.region, 5)
                           + attr('session_key', seed, 6) + attr('locale', self.project.locale, 5)
                           + field(4, account))
                response = self.call(GAME_UTILITIES, 1, request)
                result = {}
                for n, w, value in parse(response):
                    if n == 1 and w == 2:
                        entry = {nn: vv for nn, _, vv in parse(value)}
                        kind, _, data = parse(entry[2])[0]
                        result[entry[1].decode()] = data.decode() if kind == 5 else data
                return result
        raise PublishError('Battle.net authentication timed out.')

    def close(self):
        self.ws.close()


class Native:
    def __init__(self, handoff, account, project):
        expected = f'{project.region.lower()}-s2.logon.battle.net'
        if handoff.get('address') != expected:
            raise PublishError('Unexpected native endpoint in the authentication handoff.')
        self.project = project
        self.codec = Codec()
        game_account_region = self.game_account_region(handoff)
        self.socket = socket.create_connection((expected, 1119), timeout=25)
        self.socket.settimeout(25)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buffer = bytearray()
        self.in_cipher = self.out_cipher = None
        self.token = int.from_bytes(os.urandom(3), 'big')
        try:
            common = {'m_program': b'\0\0S2', 'm_platform': b'Wn64', 'm_locale': project.locale.encode(),
                      'm_versions': [{'m_programId': p, 'm_component': c, 'm_version': v} for p, c, v in VERSIONS]}
            resume = {'RequestCommon': common, 'm_account': handoff['account_mail'].encode(),
                      'm_gameAccountRegion': game_account_region,
                      'm_gameAccountName': handoff['game_account_name'].encode()}
            self.send_record(1, 0, AUTH_TYPE + 'ResumeRequest', resume)
            self.receive_record(AUTH_TYPE + 'Configuration', (0, 18))
            modules = self.receive_record(AUTH_TYPE + 'ProofRequest', (0, 2))['m_request']
            signature = next(x['m_data'] for x in modules if len(x['m_data']) == 512)
            public_key = RSA.import_key(files('sc2publish.protocol').joinpath('server-public.pem').read_bytes())
            context = b'\0' * 10 + b'\xff\xff' + socket.inet_aton(self.socket.getpeername()[0])
            try:
                pkcs1_15.new(public_key).verify(SHA512.new(context + b'Thumbprint.IPv6'), signature[::-1])
            except ValueError:
                raise PublishError('Native server signature verification failed.') from None
            server = next(x['m_data'][1:] for x in modules if len(x['m_data']) == 17)
            client = os.urandom(16)
            seed = handoff['session_key']
            self.key = mac(seed, b'\0' + client + server) + mac(seed, b'\1' + server + client)
            proof = b'\1' + client + mac(self.key, b'\0' + client + server)
            self.send_record(2, 0, AUTH_TYPE + 'ProofResponse', {'Proof': {}, 'm_response': [{'m_data': proof}]})
            while len(self.buffer) < 2:
                self.read_more()
            bits = Bits(self.buffer)
            command = bits.read(6)
            slot = bits.read(4) if bits.read(1) else None
            if (slot, command) == (1, 11):
                self.receive_record('Client::Connection::RegulatorUpdate', (1, 11))
            result = self.receive_record(AUTH_TYPE + 'ResumeResponse', (0, 1))['m_result']
            if result['choice'] != 0:
                raise PublishError('Native authentication was rejected.')
            server_proof = result['value']['ResponseSuccessCommon']['m_finalRequest'][0]['m_data'][1:]
            if not hmac.compare_digest(mac(self.key, b'\1' + server + client), server_proof):
                raise PublishError('Native server proof verification failed.')
            self.send(bytes.fromhex('4501'))
            self.in_cipher = ARC4.new(mac(self.key, IN_LABEL))
            self.out_cipher = ARC4.new(mac(self.key, OUT_LABEL))
            self.buffer = bytearray(self.in_cipher.decrypt(self.buffer))
            self.select_character(account)
            self.identity['game_account_region'] = game_account_region
        except BaseException:
            self.close()
            raise

    def game_account_region(self, handoff):
        """Read the SC2 account region from Front's native logon response."""
        try:
            reader = Bits(handoff['logon_response'])
            wrapper = self.codec.decode(reader, AUTH_TYPE + 'LogonResponse3')
            result = wrapper['LogonResponse']['m_result']
            if result['choice'] != 0:
                raise PublishError('Battle.net rejected the StarCraft II account login.')
            success = result['value']
            if (success['m_accountRegion'] != handoff['account_region']
                    or success['m_gameAccountName'] != handoff['game_account_name'].encode()):
                raise PublishError('Battle.net returned inconsistent SC2 account details.')
            remaining = len(reader.data) * 8 - reader.position
            if remaining > 7 or (remaining and reader.read(remaining)):
                raise PublishError('Battle.net returned malformed SC2 account details.')
            return success['m_gameAccountRegion']
        except (IndexError, KeyError, TypeError, ValueError, StopIteration):
            raise PublishError('Could not decode the StarCraft II account returned by Battle.net.') from None

    def select_character(self, account):
        listing = self.wait_message('Client::Toon::ToonList', 15, 0)
        displays = listing['m_toonDisplays']
        if not displays:
            raise PublishError('This Blizzard account has no StarCraft II character in the project region.')
        # Preserve a previously selected character when it is still advertised.
        # Fresh accounts follow the server's order, like the native client.
        selected = next((entry for entry in displays
                         if entry['m_name'] == account.get('toon_name')
                         and entry['m_realm'] == account.get('realm')), displays[0])
        self.send_record(5, 15, 'Client::Toon::ToonSelect',
                         {'m_toonName': selected['m_name'], 'm_realm': selected['m_realm']})
        confirmed = self.wait_message('Client::Toon::ToonSelected', 15, 6)
        if (confirmed['m_toonName'], confirmed['m_realm']) != (selected['m_name'], selected['m_realm']):
            raise PublishError('Battle.net confirmed a different StarCraft II character.')
        self.identity = {'toon_name': confirmed['m_toonName'], 'realm': confirmed['m_realm']}

    def next_token(self):
        self.token = (self.token + 1) & 0xffffffff
        return self.token

    def closing_notice(self):
        for start in range(len(self.buffer) - 1):
            if self.buffer[start] & 127 != 30 | 64 or self.buffer[start + 1] & 7 != 3 & 7:
                continue
            try:
                _, _, value, _ = self.codec.decode_record(self.buffer[start:],
                                                          'Client::Connection::ConnectionClosing')
                reason = value.get('m_closingReason')
                packets = len(value.get('m_packets') or [])
                return f'closing reason {reason}, {packets} recent packets'
            except (IndexError, ValueError, KeyError, StopIteration):
                continue
        return None

    def dump_buffer(self):
        from platformdirs import user_state_path
        path = Path(user_state_path('sc2publish')) / 'native-last.bin'
        private_write(path, bytes(self.buffer))
        return path

    def read_more(self):
        data = self.socket.recv(65536)
        if not data:
            notice = self.closing_notice()
            detail = f' ({notice})' if notice else ''
            try:
                dump = self.dump_buffer()
                detail += f'; raw buffer saved to {dump}'
            except OSError:
                pass
            raise PublishError('Battle.net closed the native connection' + detail + '.')
        if len(self.buffer) + len(data) > 8 * 1024 * 1024:
            raise PublishError('Native response exceeds the receive limit.')
        self.buffer.extend(self.in_cipher.decrypt(data) if self.in_cipher else data)

    def receive_record(self, name, route):
        while True:
            try:
                cmd, slot, value, used = self.codec.decode_record(self.buffer, name)
                if (slot, cmd) != route:
                    raise PublishError(f'Unexpected native authentication route {slot}/{cmd}.')
                del self.buffer[:used]
                return value
            except IndexError:
                self.read_more()

    def wait_message(self, name, slot, command):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            for offset in range(len(self.buffer) - 1):
                bits = Bits(self.buffer, offset * 8)
                cmd = bits.read(6)
                found_slot = bits.read(4) if bits.read(1) else None
                if (found_slot, cmd) == (slot, command):
                    try:
                        _, _, value, used = self.codec.decode_record(self.buffer[offset:], name)
                    except (IndexError, ValueError, KeyError, StopIteration):
                        continue
                    del self.buffer[:offset + used]
                    return value
            self.read_more()
        raise PublishError('Native character selection timed out.')

    def send(self, value):
        self.socket.sendall(self.out_cipher.encrypt(value) if self.out_cipher else value)

    def send_record(self, command, slot, name, value):
        self.send(self.codec.encode_record(command, slot, name, value))

    def correlated(self, name, slot, command, token_field, token):
        deadline = time.monotonic() + 40
        last_error = None
        while time.monotonic() < deadline:
            for start in range(len(self.buffer) - 1):
                if (self.buffer[start] & 127) != (command | 64) or (self.buffer[start+1] & 7) != (slot & 7):
                    continue
                try:
                    cmd, found, value, used = self.codec.decode_record(self.buffer[start:], name)
                    if (found, cmd) == (slot, command) and value[token_field] == token:
                        del self.buffer[:start+used]
                        return value
                except (IndexError, ValueError, StopIteration, KeyError) as exc:
                    last_error = f'{type(exc).__name__}: {exc}'
            self.read_more()
        short = name.rsplit('::', 1)[-1]
        detail = f' last decode error: {last_error}.' if last_error else ''
        raise PublishError(
            f'No correlated {short} response ({len(self.buffer)} buffered bytes;'
            f' connection closed by Battle.net?).{detail}')

    def prepublish(self):
        token = self.next_token()
        self.send_record(3, 13, MAP + 'PrePublishRequest',
                         {'PrePublish': {}, 'm_commandId': token, 'm_mapNames': [self.project.name]})
        result = self.correlated(MAP + 'PrePublishResponse', 13, 3, 'm_commandId', token)['m_result']
        if result['choice'] != 0:
            raise PublishError('Battle.net rejected the map lookup.')
        return result['value']

    def close(self):
        self.socket.close()


def connect(project, *, session=None):
    session = session or AccountSession(project)
    front = None
    native = None
    try:
        front = Front(project, session)
        handoff = front.authenticate()
        native = Native(handoff, front.account, project)
        session.finish()
        return native
    except BaseException:
        if native is not None:
            native.close()
        raise
    finally:
        if front is not None:
            front.close()
        session.clear_password()
