"""Generate and upload a new publication to an existing Battle.net listing."""
import hmac
import socket
import time

from tqdm import tqdm

from .build import build, fingerprint
from .connection import MAP, NATIVE_VERSION, connect, mac
from .errors import PublishError
from .metadata import compile_metadata

CACHE = 'Client::Cache::'
# Names from Battle.net's public BNET/ERRS catalog; report the validation
# stage instead of reducing every rejection to an unexplained numeric code.
VALIDATION_ERRORS = {
    3236: 'validation was cancelled',
    3244: 'localized text file is corrupt',
    3245: 'dependencies could not be validated',
    3246: 'scope information could not be validated',
    3247: 'attribute definitions could not be validated',
    3248: 'score result definitions could not be validated',
    3249: 'default attribute values could not be validated (check lobby dependencies)',
    3250: 'score screen information could not be validated',
    3251: 'variant definitions could not be validated',
    3253: 'Core dependency is missing',
}


class Gateway:
    def __init__(self, codec, info, key, challenge, token):
        self.codec, self.token, self.buffer = codec, token, bytearray()
        self.socket = socket.create_connection((socket.inet_ntoa(info['m_addressPort']['m_address']),
                                               int.from_bytes(info['m_addressPort']['m_port'], 'big')), timeout=40)
        self.socket.settimeout(40)
        try:
            context = challenge + info['m_serverChallenge']
            request = {'Connect': {}, 'm_saveId': info['m_saveId'], 'm_token': token,
                       'm_authProof': mac(key, b'\0' + context),
                       'm_version': {'m_programId': b'Bnet', 'm_component': b'Wn64', 'm_version': NATIVE_VERSION}}
            self.socket.sendall(codec.encode_record(4, 11, CACHE + 'ConnectRequest', request))
            response = self.receive('ConnectResponse', 4)
            if response['m_token'] != token or not hmac.compare_digest(response['m_authProof'], mac(key, b'\1' + context)):
                raise PublishError('Cache gateway authentication proof failed verification.')
        except BaseException:
            self.close()
            raise

    def receive(self, name, command):
        while True:
            try:
                cmd, slot, value, used = self.codec.decode_record(self.buffer, CACHE + name)
                if (slot, cmd) != (11, command):
                    raise PublishError(f'Unexpected gateway route {slot}/{cmd}.')
                del self.buffer[:used]
                return value
            except IndexError:
                data = self.socket.recv(65536)
                if not data:
                    raise PublishError('Upload gateway closed the connection.')
                self.buffer.extend(data)

    def result(self, stage='authorization'):
        response = self.receive('Result', 8)
        if response['m_token'] != self.token:
            raise PublishError('Unexpected upload response token.')
        result = response['m_result']
        if result['choice'] == 0:
            code = result['value']
            detail = VALIDATION_ERRORS.get(code)
            raise PublishError(f'Upload gateway rejected the file during {stage} (error {code})'
                               + (f': {detail}.' if detail else '.'))
        return result['choice']

    def upload(self, request, data, label):
        self.socket.sendall(self.codec.encode_record(5, 11, CACHE + 'RequestWrite', request))
        status = self.result()
        if status == 1:
            return False
        if status != 2:
            raise PublishError('Unexpected upload gateway response.')
        with tqdm(total=len(data), desc=label, unit='B', unit_scale=True, unit_divisor=1024,
                  dynamic_ncols=True, mininterval=0.2, disable=None) as progress:
            pending, payload_bytes = bytearray(), 0
            for offset in range(0, len(data), 1024):
                chunk = data[offset:offset+1024]
                pending.extend(self.codec.encode_record(7, 11, CACHE + 'DataChunk', {'m_data': chunk}))
                payload_bytes += len(chunk)
                if len(pending) >= 128 * 1024:
                    self.socket.sendall(pending)
                    progress.update(payload_bytes)
                    pending.clear()
                    payload_bytes = 0
            if pending:
                self.socket.sendall(pending)
                progress.update(payload_bytes)
            progress.set_postfix_str('awaiting confirmation')
            if self.result('payload validation') != 1:
                raise PublishError('Upload completion was not confirmed.')
            progress.set_postfix_str('confirmed')
        return True

    def close(self):
        self.socket.close()


def write_request(codec, project, file, file_id, map_handle):
    usage = {'s2ma': 3, 's2mv': 1, 's2ml': 2, 's2mh': 0}[file.usage]
    purpose = {} if usage == 2 else {'m_mapHandle': map_handle}
    if usage == 0:
        purpose = codec.make('Cache::Shared::Purpose::MapHeader',
                             m_mapName=project.archive.name, m_mapHandle=map_handle,
                             m_isLocked=bool(project.project.get('locked', True)),
                             m_authorPrefs={'m_showRID': None, 'm_accountLinked': False,
                                            'm_isPrivate': project.project.get('visibility', 'public') == 'private',
                                            'm_transitionId': None},
                             m_categoryMask=int(project.project.get('category_mask', 2)),
                             m_localizedNames=[{'m_locale': project.locale.encode(), 'm_name': project.name}])
    return {'m_info': {'m_bytes': len(file.data), 'm_cacheHandle': file.cache_handle,
                       'm_purpose': {'choice': usage, 'value': purpose}}, 'm_fileId': file_id}


def verify_identity(project, response, discover=False):
    prior = response['m_priorRevision']
    if prior is None:
        raise PublishError('No existing listing found. This release updates existing maps only.')
    if discover:
        # PrePublish resolves the authenticated author's listing by name.
        # Explicit IDs remain assertions, never silently replaced.
        if not project.map_id:
            project.map_id = prior['m_mapId']
        if not project.namespace:
            project.namespace = response['m_attributeNs']
    if prior['m_mapId'] != project.map_id or response['m_attributeNs'] != project.namespace:
        raise PublishError('The live map identity or namespace differs from the project configuration.')
    return prior


def publish(project, force=False, dry_run=False, report=print):
    import os
    if project.project.get('visibility', 'public') not in ('public', 'private'):
        raise PublishError('Project visibility must be public or private.')
    source_hash = fingerprint(project)
    state = project.read_state()
    if not force and not dry_run and state.get('source_sha256') == source_hash:
        return {'status': 'unchanged', 'name': project.name, 'version': state.get('version')}
    built = build(project, report)
    report('Compiling fresh publication metadata')
    bundle = compile_metadata(project, 65536)
    from .protocol.codec import Codec
    codec = Codec()
    for i, file in enumerate(bundle.files):
        codec.encode_record(5, 11, CACHE + 'RequestWrite',
                            write_request(codec, project, file, i, {'m_id': project.map_id, 'm_version': 65536}))
    if dry_run:
        return {'status': 'ready', 'files': len(bundle.files), 'map_bytes': built['bytes'],
                'metadata': 'compiled', 'network_used': False}
    report(f'Authenticating with Battle.net {project.region}')
    native = connect(project)
    try:
        prior = verify_identity(project, native.prepublish(), discover=True)
        token = native.next_token()
        native.send_record(2, 13, MAP + 'PublishRequest',
                           {'Publish': {}, 'm_mapNames': [{'m_locale': project.locale.encode(), 'm_name': project.name}],
                            'm_attributeNs': project.namespace, 'm_isMajorRevision': False, 'm_commandId': token})
        result = native.correlated(MAP + 'PublishResponse', 13, 2, 'm_commandId', token)['m_result']
        if result['choice'] != 0:
            raise PublishError(f'Publication reservation rejected (error {result["value"].get("m_reason")}).')
        map_handle = result['value']['m_mapHandle']
        version = map_handle['m_version']
        if map_handle['m_id'] != project.map_id or version <= prior['m_version']:
            raise PublishError('Unexpected reserved map identity or revision.')
        version_text = f'{version >> 16}.{version & 65535}'
        report(f'Reserved {project.name} {version_text}')
        bundle = compile_metadata(project, version)
        token, challenge = native.next_token(), os.urandom(16)
        declaration = {'PublishList': {}, 'm_token': token,
                       'm_type': {'choice': 1, 'value': {'m_mapHandle': map_handle}},
                       'm_challenge': challenge,
                       'm_files': [{'m_id': i, 'm_usage': f.usage.encode(), 'm_cacheHandle': f.cache_handle,
                                    'm_skipUpload': False, 'm_bytes': len(f.data)} for i, f in enumerate(bundle.files)]}
        native.send_record(7, 11, CACHE + 'PublishListDeclare', declaration)
        result = native.correlated(CACHE + 'PublishListResponse', 11, 7, 'm_token', token)['m_result']
        if result['choice'] != 0:
            raise PublishError(f'Upload declaration rejected (error {result["value"].get("m_error")}).')
        gateways = result['value']['m_gateways']
        if len(gateways) != len(bundle.files):
            raise PublishError('Upload gateway count differs from the declaration.')
        transferred = 0
        for i, (info, file) in enumerate(zip(gateways, bundle.files)):
            gateway = Gateway(codec, info, native.key, challenge, i + 1)
            try:
                request = write_request(codec, project, file, i, map_handle)
                sent = gateway.upload(request, file.data, f'{i+1}/{len(bundle.files)} {file.label}')
            finally:
                gateway.close()
            if sent:
                transferred += len(file.data)
            report(f'{i+1}/{len(bundle.files)} {file.label}: {"uploaded" if sent else "already stored"}')
        report('Verifying the published revision')
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            verified = verify_identity(project, native.prepublish())
            if verified['m_version'] == version:
                visibility = 'private' if verified['m_isPrivate'] else 'public'
                if visibility != project.project.get('visibility', 'public'):
                    raise PublishError('The published visibility differs from the requested setting.')
                result = {'name': project.name, 'region': project.region, 'map_id': project.map_id,
                          'namespace': project.namespace, 'version': version_text, 'visibility': visibility,
                          'source_sha256': built['source_sha256'], 'artifact_sha256': built['artifact_sha256'],
                          'header_sha256': bundle.files[-1].cache_handle[8:].hex(), 'transferred_bytes': transferred,
                          'metadata': 'compiled'}
                project.save_state(result)
                return result
            time.sleep(2)
        raise PublishError('Uploads completed, but the new revision was not confirmed by Battle.net.')
    finally:
        native.close()
