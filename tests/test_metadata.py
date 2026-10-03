import struct
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from sc2publish.config import Project
from sc2publish.errors import PublishError
from sc2publish.mapinfo import MapInfo
from sc2publish.metadata import compile_metadata, serialize_locale
from sc2publish.protocol.tagged import Reader


def mapinfo():
    def u32(n):
        return struct.pack('<I', n)
    data = b'IpaM' + u32(25) + bytes(8) + u32(128) + u32(128)
    data += bytes(12) + b'\0TestTileset\0'
    data += b''.join(u32(n) for n in (10, 10, 118, 118))
    data += bytes(8) + b'\0' + bytes(2) + bytes(32) + bytes(8) + u32(2)
    for i in (1, 2):
        data += bytes([i]) + u32(1) + u32(0xffffffff) + b'Terr\0' + bytes(12) + b'\0'
    return data + bytes(40)


def test_geometry_and_players():
    info = MapInfo(mapinfo())
    assert (info.right - info.left, info.top - info.bottom) == (108, 108)
    assert [p['id'] for p in info.players] == [1, 2]
    assert info.players[1]['race'] == 'Terr'


def source_project(tmp_path):
    root = tmp_path / 'components'
    root.mkdir()
    (root / 'MapInfo').write_bytes(mapinfo())
    (root / 'DocumentInfo').write_text('<DocInfo/>')
    (root / 'MapScript.galaxy').write_text('void InitMap() {}\n')
    manifest = tmp_path / 'project.toml'
    manifest.write_text('[project]\nname="Example"\n[build]\nlayers=["components"]\n')
    return Project(str(manifest))


def test_locale_wire_format_preserves_xml_text():
    data = serialize_locale('enUS', [(1, '"Quoted" & <tag>'), (2, "L'été"), (3, '')])
    assert data == (b'<?xml version="1.0" encoding="utf-8"?>\r\n<Locale region="enUS">\r\n'
                    b'    <e id="1">&quot;Quoted&quot; &amp; &lt;tag&gt;</e>\r\n'
                    b'    <e id="2">L&apos;\xc3\xa9t\xc3\xa9</e>\r\n'
                    b'    <e id="3"></e>\r\n</Locale>')
    assert [entry.text for entry in ET.fromstring(data)] == ['"Quoted" & <tag>', "L'été", None]


def test_tutorial_identity_survives_variant_removal_and_teams_dependency_is_inferred(tmp_path):
    from sc2publish.build import build
    p = source_project(tmp_path)
    (p.layers[0] / 'DocumentInfo').write_text('''<DocInfo><Tutorial>
      <Type><Value>Self</Value></Type><VariantIndex><Value>3</Value></VariantIndex>
      <VariantCategory><Value>1</Value></VariantCategory><VariantMode><Value>2</Value></VariantMode>
      </Tutorial></DocInfo>''')
    (p.layers[0] / 'Attributes').write_text('''<Attributes><Variant>
      <CategoryId Value="1"/><ModeId Value="2"/>
      <Attribute Namespace="999" Id="2018"><Default><Slot Id="0"/><Value Id="21553"/></Default></Attribute>
      </Variant></Attributes>''')
    build(p, lambda _: None)
    bnet = compile_metadata(p, 65537).metadata['m_bnet']
    assert bnet['m_arcadeData']['m_tutorialLink']['m_variantIndex'] == 0
    assert bnet['m_extraDependencies'] == [{'m_id': 17, 'm_version': 0}]
    p.project['extra_dependencies'] = [17, 123, 17]
    assert compile_metadata(p, 65537).metadata['m_bnet']['m_extraDependencies'] == [
        {'m_id': 17, 'm_version': 0}, {'m_id': 123, 'm_version': 0}]
    (p.component_dir / 'Attributes').write_text('<Attributes/>')
    with pytest.raises(PublishError, match='missing lobby variant'):
        compile_metadata(p, 65537)


def test_result_definitions_follow_source_for_every_map_id(tmp_path):
    from sc2publish.build import build
    p = source_project(tmp_path)
    data = p.layers[0] / 'Base.SC2Data/GameData'
    data.mkdir(parents=True)
    source = data / 'UnitData.xml'
    source.write_text('<Catalog><CUnit id="ObserverSpawnedUnit"><ScoreResult value="BuildOrder"/></CUnit></Catalog>')
    build(p, lambda _: None)
    for map_id in (42, 268504):
        p.map_id = map_id
        results = compile_metadata(p, 65537).metadata['m_bnet']['m_resultDefinitions']
        assert len(results) == 1
        assert results[0]['m_handle']['m_id'] == 1 << 24
    source.write_text('<Catalog><CUnit id="NewUnit"><ScoreResult value="BuildOrder"/></CUnit></Catalog>')
    build(p, lambda _: None)
    assert [r['m_name'] for r in compile_metadata(p, 65538).metadata['m_bnet']['m_resultDefinitions']] == ['NewUnit']


def test_notes_are_unbounded_by_a_previous_publication_and_affect_fingerprint(tmp_path):
    from sc2publish.build import build, fingerprint
    p = source_project(tmp_path)
    p.publish['notes'] = 'notes.txt'
    notes = tmp_path / 'notes.txt'
    notes.write_text('# comment\n' + '\n'.join(f'Gameplay change {i}' for i in range(40)))
    build(p, lambda _: None)
    before = fingerprint(p)
    bundle = compile_metadata(p, 65578)
    section = bundle.metadata['m_bnet']['m_arcadeData']['m_patchNoteSections']
    texts = {int(e.get('id')): e.text for e in ET.fromstring(next(f.data for f in bundle.files if f.usage == 's2ml'))}
    assert texts[section['m_headers'][0]['m_title']['m_id']] == 'Version 1.42'
    assert len(section['m_sectionData']) == 40
    notes.write_text('Changed gameplay\n')
    assert fingerprint(p) != before
    notes.write_text('x' * 201)
    with pytest.raises(PublishError, match='200 characters'):
        compile_metadata(p, 65578)


def test_metadata_is_compiled_from_changed_sources(tmp_path):
    root = tmp_path / 'components'
    root.mkdir()
    (root / 'MapInfo').write_bytes(mapinfo())
    (root / 'DocumentInfo').write_text('''<DocInfo><PatchNote>
      <Version><Value>1.0</Value></Version><Date><Value>9/20/2026</Value></Date>
      <Notes><Value>1</Value></Notes></PatchNote></DocInfo>''')
    (root / 'Attributes').write_text('''<Attributes><Attribute>
      <Id Value="1"/><Type Value="Global"/><Name Value="Setting/Name"/>
      <Value><Id Value="1"/><Name Value="Setting/On"/></Value>
      <Value><Id Value="2"/><Name Value="Setting/Off"/></Value>
      <Default><Slot Id="Global"/><Value Id="2"/></Default>
    </Attribute></Attributes>''')
    locale = root / 'enUS.SC2Data/LocalizedData'
    locale.mkdir(parents=True)
    text = locale / 'GameStrings.txt'
    text.write_text('Setting/Name=Example setting\nSetting/On=On\nSetting/Off=Off\nDocInfo/PatchNote001=\n')
    archive = tmp_path / 'Example.SC2Map'
    archive.write_bytes(b'example archive content')
    p = SimpleNamespace(component_dir=root, archive=archive, name='Example', locale='enUS',
                        region='EU', map_id=42, namespace=81, output=tmp_path / 'out', project={})
    one = compile_metadata(p, 65537)
    race = next(d for d in one.metadata['m_bnet']['m_definitions'] if d['m_handle']['m_id'] == 3001)
    race_instance = next(d for d in one.metadata['m_bnet']['m_workingSet']['m_instances'] if d['m_definitionHandle']['m_id'] == 3001)
    assert not any(v['m_baked'] for v in race['m_defaults']['value'])
    assert race_instance['m_data']['value'][0]['m_baked']
    definition = next(d for d in one.metadata['m_bnet']['m_definitions'] if d['m_handle']['m_nameSpace'] == 81)
    assert definition['m_defaults']['value']['m_index'] == 1
    assert [entry['m_code'] for entry in definition['m_table']] == [b'0001', b'0002']
    text.write_text(text.read_text().replace('Example setting', 'Changed setting'))
    two = compile_metadata(p, 65538)
    assert one.files[-1].cache_handle != two.files[-1].cache_handle
    localized = next(f for f in two.files if f.usage == 's2ml')
    assert 'Changed setting' in [entry.text for entry in ET.fromstring(localized.data)]
    empty_ref = two.metadata['m_bnet']['m_arcadeData']['m_patchNoteSections']['m_sectionData'][0]
    assert empty_ref['m_id'] > 0
    entries = {int(e.get('id')): e.text for e in ET.fromstring(localized.data)}
    assert entries[empty_ref['m_id']] in (None, '')
    parsed = Reader(two.files[-1].data).decode()
    assert parsed[0][0] == {0: 42, 1: 65538}
    assert parsed[0][2] == two.files[0].cache_handle
