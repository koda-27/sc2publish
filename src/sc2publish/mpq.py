"""Portable deterministic MPQ v0 writer (zlib-compressed single-unit files)."""
import struct
import zlib
from pathlib import Path

MASK = 0xffffffff


def crypt_table():
    seed = 0x100001
    table = [0] * 1280
    for low in range(256):
        for high in range(5):
            seed = (seed * 125 + 3) % 0x2aaaab
            first = (seed & 0xffff) << 16
            seed = (seed * 125 + 3) % 0x2aaaab
            table[low + high * 256] = first | (seed & 0xffff)
    return table


CRYPT = crypt_table()


def hash_name(name, kind):
    a, b = 0x7fed7fed, 0xeeeeeeee
    for byte in name.replace('/', '\\').upper().encode('utf-8'):
        a = (CRYPT[(kind << 8) + byte] ^ (a + b)) & MASK
        b = (byte + a + b + (b << 5) + 3) & MASK
    return a


def encrypt_table(data, key):
    seed = 0xeeeeeeee
    output = bytearray()
    for (word,) in struct.iter_unpack('<I', data):
        seed = (seed + CRYPT[0x400 + (key & 255)]) & MASK
        output.extend(struct.pack('<I', word ^ ((key + seed) & MASK)))
        key = (((~key << 21) + 0x11111111) | (key >> 11)) & MASK
        seed = (word + seed + (seed << 5) + 3) & MASK
    return output


def pack(source, destination, progress=None):
    source, destination = Path(source), Path(destination)
    paths = sorted(p for p in source.rglob('*') if p.is_file() and 'Local' not in p.relative_to(source).parts)
    names = [p.relative_to(source).as_posix().replace('/', '\\') for p in paths]
    if len({n.upper() for n in names}) != len(names):
        raise ValueError('MPQ filenames collide when compared case-insensitively')
    if '(listfile)' in {n.lower() for n in names}:
        raise ValueError('The MPQ listfile is generated automatically')
    count = len(names) + 1
    slots = 1
    while slots < count * 2:
        slots *= 2
    table = [[MASK, MASK, 0xffff, 0xffff, MASK] for _ in range(slots)]
    blocks = []
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + '.tmp')
    try:
        with temporary.open('wb') as out:
            out.write(bytes(32))
            for index, name in enumerate([*names, '(listfile)']):
                raw = paths[index].read_bytes() if index < len(paths) else ('\r\n'.join([*names, '(listfile)']) + '\r\n').encode()
                compressed = b'\x02' + zlib.compress(raw, 9)
                encoded = compressed if len(compressed) < len(raw) else raw
                flags = 0x81000200  # EXISTS | SINGLE_UNIT | COMPRESS
                blocks.append((out.tell(), len(encoded), len(raw), flags))
                out.write(encoded)
                slot = hash_name(name, 0) & (slots - 1)
                while table[slot][4] != MASK:
                    slot = (slot + 1) & (slots - 1)
                table[slot] = [hash_name(name, 1), hash_name(name, 2), 0, 0, index]
                if progress:
                    progress(1)
            hash_offset = out.tell()
            out.write(encrypt_table(b''.join(struct.pack('<IIHHI', *entry) for entry in table), hash_name('(hash table)', 3)))
            block_offset = out.tell()
            out.write(encrypt_table(b''.join(struct.pack('<4I', *entry) for entry in blocks), hash_name('(block table)', 3)))
            size = out.tell()
            if size > MASK:
                raise ValueError('MPQ v0 archives must be smaller than 4 GiB')
            out.seek(0)
            out.write(struct.pack('<4sIIHH4I', b'MPQ\x1a', 32, size, 0, 3,
                                  hash_offset, block_offset, slots, count))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {'files': len(paths), 'bytes': size, 'path': str(destination)}
