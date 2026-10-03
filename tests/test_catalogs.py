import xml.etree.ElementTree as ET

from sc2publish.catalogs import catalog_files, definitions, merge, package_key, stock


def component(tmp_path, manifests, standard=None):
    root = tmp_path / 'components'
    data = root / 'Base.SC2Data'
    (data / 'GameData').mkdir(parents=True)
    for name, body in (standard or {}).items():
        (data / 'GameData' / name).write_text(body)
    includes = ET.Element('Includes')
    for name in manifests:
        ET.SubElement(includes, 'Catalog', path=name)
    (data / 'GameData.xml').write_text(ET.tostring(includes, encoding='unicode'))
    return root


def unit(name, body=''):
    return f'<Catalog><CUnit id="{name}">{body}</CUnit></Catalog>'


def test_blob_covers_stock_packages():
    entries = stock()
    assert 'mods\\liberty.sc2mod' in entries
    assert 'mods\\swarm.sc2mod' in entries
    assert 'campaigns\\liberty.sc2campaign' in entries
    assert 'MISSILE' in entries['mods\\core.sc2mod']['Unit']
    total = sum(len(names) for package in entries.values() for names in package.values())
    assert total > 100000


def test_package_key_normalizes_dependency_values():
    assert package_key('bnet:Liberty (Mod)/0.0/999,file:Mods/Liberty.SC2Mod') == 'mods\\liberty.sc2mod'
    assert package_key('file:Mods/SBC/Data.SC2Mod') == 'mods\\sbc\\data.sc2mod'


def test_catalog_files_load_standard_before_includes(tmp_path):
    root = component(tmp_path, ['GameData/Custom.xml'], {'UpgradeData.xml': '<Catalog/>'})
    names = [path.name for path in catalog_files(root)]
    assert names == ['UpgradeData.xml', 'Custom.xml']


def test_merge_keeps_stock_positions_and_appends_local_entries(tmp_path):
    root = component(tmp_path, ['GameData/Custom.xml'], {'UnitData.xml': unit('MISSILE')})
    (root / 'Base.SC2Data/GameData/Custom.xml').write_text(unit('LocalHero', '<ScoreResult value="BuildOrder"/>'))
    catalog = merge(['file:Mods/Core.SC2Mod'], root)
    assert catalog.positions['Unit']['MISSILE'] == 7
    assert catalog.is_stock('Unit', 'MISSILE')
    assert catalog.positions['Unit']['LocalHero'] == catalog.totals['Unit'] - 1
    assert not catalog.is_stock('Unit', 'LocalHero')


def test_definitions_resolve_inherited_score_results(tmp_path):
    root = component(tmp_path, ['GameData/Custom.xml'])
    (root / 'Base.SC2Data/GameData/Custom.xml').write_text(
        '<Catalog>'
        '<CUnit id="Parent"><ScoreResult value="BuildOrder"/></CUnit>'
        '<CUnit id="Child" parent="Parent"/>'
        '<CUnit id="Lonely"/>'
        '</Catalog>')
    found = definitions(root)['Unit']
    assert found['Parent']['score']
    assert found['Child']['score']
    assert not found['Lonely']['score']


def test_definitions_resolve_inherited_icons(tmp_path):
    root = component(tmp_path, ['GameData/Custom.xml'])
    (root / 'Base.SC2Data/GameData/Custom.xml').write_text(
        '<Catalog>'
        '<CUpgrade id="Parent"><Icon value="Assets\\Textures\\icon.dds"/></CUpgrade>'
        '<CUpgrade id="Child" parent="Parent"/>'
        '<CUpgrade id="Lonely"/>'
        '</Catalog>')
    found = definitions(root)['Upgrade']
    assert found['Parent']['icon'] == 'Assets\\Textures\\icon.dds'
    assert found['Child']['icon'] == 'Assets\\Textures\\icon.dds'
    assert found['Lonely']['icon'] is None


def test_explicitly_cleared_score_does_not_reappear_from_parent(tmp_path):
    root = component(tmp_path, ['GameData/Custom.xml'])
    (root / 'Base.SC2Data/GameData/Custom.xml').write_text(
        '<Catalog>'
        '<CUpgrade id="Parent"><ScoreResult value="BuildOrder"/><Icon value="parent.dds"/></CUpgrade>'
        '<CUpgrade id="Removed" parent="Parent"><ScoreResult value=""/><Icon value=""/></CUpgrade>'
        '<CUpgrade id="Grandchild" parent="Removed"/>'
        '<CUpgrade id="Inherited" parent="Parent"/>'
        '</Catalog>')
    catalog = merge([], root)
    assert [entry[0] for entry in catalog.result_entries(root)] == ['Parent', 'Inherited']
    assert definitions(root)['Upgrade']['Grandchild']['icon'] == ''


def test_result_entries_skip_stock_overrides_and_empty_scores(tmp_path):
    root = component(tmp_path, ['GameData/Custom.xml'])
    (root / 'Base.SC2Data/GameData/Custom.xml').write_text(
        '<Catalog>'
        '<CUpgrade id="LocalOne"><ScoreResult value="BuildOrder"/></CUpgrade>'
        '<CUnit id="MISSILE"><ScoreResult value="BuildOrder"/></CUnit>'
        '<CUpgrade id="LocalTwo"><ScoreResult value=""/></CUpgrade>'
        '<CUpgrade id="LocalThree"><ScoreResult value="BuildOrder"/></CUpgrade>'
        '</Catalog>')
    catalog = merge(['file:Mods/Core.SC2Mod'], root)
    selected = catalog.result_entries(root)
    assert [name for name, _, _, _, _ in selected] == ['LocalOne', 'LocalThree']
    assert all(tag == 'Upgrade' for _, tag, _, _, _ in selected)
    assert catalog.is_stock('Unit', 'MISSILE')


def test_result_entries_order_types_then_catalog_position(tmp_path):
    root = component(tmp_path, ['GameData/Custom.xml'])
    (root / 'Base.SC2Data/GameData/Custom.xml').write_text(
        '<Catalog>'
        '<CUpgrade id="Second"><ScoreResult value="BuildOrder"/></CUpgrade>'
        '<CUnit id="Hero"><ScoreResult value="BuildOrder"/></CUnit>'
        '<CUpgrade id="First"><ScoreResult value="BuildOrder"/></CUpgrade>'
        '</Catalog>')
    catalog = merge([], root)
    assert [(n, t, i) for n, t, i, _, _ in catalog.result_entries(root)] == [('Hero', 'Unit', 0), ('Second', 'Upgrade', 0), ('First', 'Upgrade', 1)]
