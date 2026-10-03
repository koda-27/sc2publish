import os

import mpyq

from sc2publish.mpq import pack


def test_archive_is_readable_by_independent_mpq_reader(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'Base.SC2Data').mkdir()
    contents = {'MapScript.galaxy': b'void InitMap() {}\n' * 100,
                'Base.SC2Data/noise.bin': os.urandom(8193), 'empty.txt': b''}
    for name, data in contents.items():
        (source / name).write_bytes(data)
    target = tmp_path / 'map.SC2Map'
    pack(source, target)
    first = target.read_bytes()
    with target.open('rb') as stream:
        archive = mpyq.MPQArchive(stream)
        for name, data in contents.items():
            filename = name.replace('/', '\\')
            assert filename.encode() in archive.files
            # mpyq represents a present zero-length block as None.
            assert (archive.read_file(filename) or b'') == data
        assert b'MapScript.galaxy' in archive.read_file('(listfile)')
    pack(source, target)
    assert target.read_bytes() == first
