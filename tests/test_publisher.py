from types import SimpleNamespace

import pytest
from test_metadata import source_project

from sc2publish import publisher
from sc2publish.errors import PublishError
from sc2publish.protocol.tagged import Reader


def test_dry_run_builds_without_credentials_network_or_publication_state(tmp_path, monkeypatch):
    p = source_project(tmp_path)
    monkeypatch.setattr(publisher, 'connect', lambda _: pytest.fail('offline command connected'))
    p.save_state = lambda _: pytest.fail('dry-run saved a publication')
    result = publisher.publish(p, dry_run=True, report=lambda _: None)
    assert result['metadata'] == 'compiled'
    assert result['network_used'] is False
    assert result['map_bytes'] > 0
    assert (p.output / 'publication/manifest.json').is_file()
    assert (p.map_id, p.namespace) == (0, 0)


def test_publish_discovers_identity_uploads_generated_header_and_confirms_before_saving(tmp_path, monkeypatch):
    p = source_project(tmp_path)
    reserved = {'m_id': 42, 'm_version': 65543}
    declarations, uploaded, saved, closed = [], [], [], []
    calls = []

    def prepublish():
        calls.append('lookup')
        return {'m_attributeNs': 81, 'm_priorRevision': {
            'm_mapId': 42, 'm_version': 65543 if uploaded else 65542, 'm_isPrivate': False}}

    def send(command, slot, name, value):
        if name.endswith('PublishListDeclare'):
            declarations.append(value)
        else:
            assert value['m_attributeNs'] == 81

    def correlated(name, slot, command, key, token):
        if name.endswith('PublishResponse'):
            return {'m_result': {'choice': 0, 'value': {'m_mapHandle': reserved}}}
        return {'m_result': {'choice': 0, 'value': {'m_gateways': [{}] * len(declarations[-1]['m_files'])}}}

    native = SimpleNamespace(prepublish=prepublish, next_token=lambda: 1, send_record=send,
                             correlated=correlated, key=b'synthetic', close=lambda: closed.append(True))
    monkeypatch.setattr(publisher, 'connect', lambda _: native)

    class Gateway:
        def __init__(self, codec, *args):
            self.codec = codec

        def upload(self, request, data, label):
            if request['m_info']['m_purpose']['choice'] == 0:
                header = Reader(data).typed(self.codec, 'S2Map::Header')
                assert header['m_bnet']['m_handle'] == reserved
                assert header['m_bnet']['m_mapNamespace'] == 81
            uploaded.append(request)
            return True

        def close(self):
            pass

    monkeypatch.setattr(publisher, 'Gateway', Gateway)
    p.read_state = lambda: {}
    p.save_state = saved.append
    result = publisher.publish(p, report=lambda _: None)
    assert calls == ['lookup', 'lookup']
    assert result['version'] == '1.7'
    assert result['metadata'] == 'compiled'
    assert saved == [result]
    assert closed == [True]
    assert len(uploaded) == len(declarations[0]['m_files'])
    assert (p.map_id, p.namespace) == (42, 81)


def test_explicit_destination_mismatch_is_not_overwritten():
    p = SimpleNamespace(map_id=99, namespace=81)
    with pytest.raises(PublishError, match='differs'):
        publisher.verify_identity(p, {'m_attributeNs': 81, 'm_priorRevision': {'m_mapId': 42}}, discover=True)
    assert p.map_id == 99


def test_gateway_explains_locale_and_dependency_errors():
    gateway = publisher.Gateway.__new__(publisher.Gateway)
    gateway.token = 1
    gateway.receive = lambda *a: {'m_token': 1, 'm_result': {'choice': 0, 'value': 3249}}
    with pytest.raises(PublishError, match='lobby dependencies'):
        gateway.result('payload validation')
