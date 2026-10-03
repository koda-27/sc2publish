"""Extract ordered stock catalog entry names from a local StarCraft II install.

The compiler needs the editor's dependency-merged catalog positions to place
locally defined units and upgrades. This script walks every stock mod and
campaign that ships GameData, reads the catalog files in the editor's load
order (standard *Data.xml files first, then the <Catalog> includes listed in
Base.SC2Data/GameData.xml), and writes a small gzipped name table into the
package data directory.

Usage:
    python scripts/build_stock_blob.py --casc-lib <libcasc.so> --install <SC2 dir>
"""
import argparse
import ctypes
import gzip
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path

OUTPUT = Path(__file__).resolve().parents[1] / 'src/sc2publish/data/stock_catalogs.json.gz'
ENTRY = re.compile(r'<C([A-Za-z0-9_]+)\b[^>]*?\bid="([^"]+)"[^>]*?(?:/>|>(.*?)</C\1>)', re.S)
INCLUDE = re.compile(rb'<Catalog\s+path="([^"]+)"')


def open_storage(lib, install):
    lib.CascOpenStorage.restype = ctypes.c_bool
    lib.CascOpenStorage.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    lib.CascOpenFile.restype = ctypes.c_void_p
    lib.CascOpenFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.POINTER(ctypes.c_void_p)]
    lib.CascReadFile.restype = ctypes.c_bool
    lib.CascReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                 ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    lib.CascGetFileSize64.restype = ctypes.c_bool
    lib.CascGetFileSize64.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulonglong)]
    lib.CascCloseFile.restype = ctypes.c_bool
    lib.CascCloseFile.argtypes = [ctypes.c_void_p]
    lib.CascFindFirstFile.restype = ctypes.c_void_p
    lib.CascFindFirstFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_char_p]
    lib.CascFindNextFile.restype = ctypes.c_bool
    lib.CascFindNextFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.CascFindClose.restype = ctypes.c_bool
    lib.CascFindClose.argtypes = [ctypes.c_void_p]
    storage = ctypes.c_void_p()
    if not lib.CascOpenStorage(install, 0, ctypes.byref(storage)):
        raise SystemExit('CASC storage could not be opened.')
    return storage


def find_all(lib, storage):
    buffer = ctypes.create_string_buffer(4096)
    handle = lib.CascFindFirstFile(storage, b'*', buffer, None)
    if handle == ctypes.c_void_p(-1).value:
        return
    try:
        while True:
            yield ctypes.string_at(buffer, 260).split(b'\0')[0].decode('ascii', 'replace')
            if not lib.CascFindNextFile(handle, buffer):
                break
    finally:
        lib.CascFindClose(handle)


def reader(lib, storage):
    def read(path):
        handle = ctypes.c_void_p()
        if not lib.CascOpenFile(storage, path.encode(), 0, 0, ctypes.byref(handle)):
            return None
        size = ctypes.c_ulonglong()
        lib.CascGetFileSize64(handle, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        count = ctypes.c_uint32()
        lib.CascReadFile(handle, buffer, size.value, ctypes.byref(count), None)
        lib.CascCloseFile(handle)
        return buffer.raw[:count.value]
    return read


def packages(lib, storage):
    found = {}
    for path in find_all(lib, storage):
        low = path.lower()
        if not low.endswith('.xml'):
            continue
        marker = low.find('\\base.sc2data\\gamedata')
        if marker < 0:
            continue
        if '\\base.sc2maps\\' in low or '\\base2.sc2maps\\' in low or '\\base3.sc2maps\\' in low:
            continue
        rel = path[marker + len('\\base.sc2data\\'):]
        found.setdefault(low[:marker], []).append(rel)
    return found


def catalog_files(read, key, members, includes_only=False):
    base = key + '\\base.sc2data\\'
    files = []
    if not includes_only:
        files.extend(sorted((rel for rel in members
                             if rel.lower().startswith('gamedata\\')
                             and rel.lower().rsplit('\\', 1)[-1].endswith('data.xml')),
                            key=str.lower))
    manifest = read(base + 'gamedata.xml')
    if manifest:
        files.extend(match.decode('utf-8') for match in INCLUDE.findall(manifest))
    return base, files


def scan_package(read, key, members):
    base, files = catalog_files(read, key, members)
    ordered = OrderedDict()
    for rel in files:
        data = read(base + rel)
        if not data:
            continue
        text = data.decode('utf-8', 'replace')
        for match in ENTRY.finditer(text):
            tag = match.group(1)
            tag = tag[1:] if tag.startswith('C') else tag
            names = ordered.setdefault(tag, [])
            if match.group(2) not in names:
                names.append(match.group(2))
    return ordered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--casc-lib', required=True, help='Path to your CascLib shared library')
    parser.add_argument('--install', required=True, help='Path to your StarCraft II installation')
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    lib = ctypes.CDLL(args.casc_lib)
    storage = open_storage(lib, str(args.install).encode())
    read = reader(lib, storage)
    found = packages(lib, storage)
    blob = OrderedDict()
    total = 0
    for key in sorted(found):
        blob[key] = scan_package(read, key, found[key])
        total += sum(len(names) for names in blob[key].values())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = gzip.compress(json.dumps(blob, separators=(',', ':')).encode(), 9)
    args.output.write_bytes(payload)
    print(f'{len(blob)} packages, {total} entries, {len(payload)} bytes -> {args.output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
