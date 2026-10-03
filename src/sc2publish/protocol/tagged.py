"""Compile typed metadata into Blizzard's self-describing versioned format."""
import struct


def vint(value):
    value = abs(value) * 2 + (value < 0)
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def encode(codec, number, value):
    item = codec.shape(number)
    kind = item['kind']
    if kind == 'struct':
        members = item['members']
        return b'\x05' + vint(len(members)) + b''.join(
            vint(m['index']) + encode(codec, m['type'], value[m['name']]) for m in members)
    if kind == 'array':
        return b'\x00' + vint(len(value)) + b''.join(encode(codec, item['element'], x) for x in value)
    if kind == 'optional':
        return b'\x04' + (b'\0' if value is None else b'\1' + encode(codec, item['element'], value))
    if kind == 'choice':
        member = next(m for m in item['members'] if m['index'] == value['choice'])
        return b'\x03' + vint(value['choice']) + encode(codec, member['type'], value['value'])
    if kind in ('string', 'bytestring', 'blob'):
        value = value.encode() if isinstance(value, str) else value
        return b'\x02' + vint(len(value)) + value
    if kind in ('integer', 'enum'):
        return b'\x09' + vint(value)
    if kind == 'fourcc':
        return b'\x07' + (value if isinstance(value, bytes) else value.to_bytes(4, 'little'))
    if kind == 'bool':
        return b'\x06' + bytes([bool(value)])
    if kind == 'bits':
        data = bytearray((len(value) + 7) // 8)
        for i, bit in enumerate(value):
            data[i//8] |= int(bit) << (i % 8)
        return b'\x01' + vint(len(value)) + data
    if kind in ('float32', 'float64'):
        return (b'\x07' + struct.pack('<f', value)) if kind == 'float32' else (b'\x08' + struct.pack('<d', value))
    if kind == 'void':
        return b'\x05\0'
    raise ValueError(f'Unsupported metadata kind {kind}')


class Reader:
    def __init__(self, data):
        self.data, self.position = data, 0

    def take(self, count):
        end = self.position + count
        if count < 0 or end > len(self.data):
            raise ValueError('Truncated tagged metadata')
        result = self.data[self.position:end]
        self.position = end
        return result

    def integer(self):
        byte = self.take(1)[0]
        negative, value, shift = byte & 1, (byte & 127) >> 1, 6
        while byte & 128:
            if shift > 70:
                raise ValueError('Oversized tagged integer')
            byte = self.take(1)[0]
            value |= (byte & 127) << shift
            shift += 7
        return -value if negative else value

    def decode(self):
        kind = self.take(1)[0]
        if kind == 0:
            count = self.integer()
            if not 0 <= count <= 65536:
                raise ValueError('Invalid tagged array count')
            return [self.decode() for _ in range(count)]
        if kind == 1:
            return self.take((self.integer() + 7) // 8)
        if kind == 2:
            return self.take(self.integer())
        if kind == 3:
            return self.integer(), self.decode()
        if kind == 4:
            return self.decode() if self.take(1)[0] else None
        if kind == 5:
            result = {}
            count = self.integer()
            if not 0 <= count <= 65536:
                raise ValueError('Invalid tagged struct count')
            for _ in range(count):
                key = self.integer()
                result[key] = self.decode()
            return result
        if kind in (6, 7, 8):
            return int.from_bytes(self.take({6: 1, 7: 4, 8: 8}[kind]), 'little')
        if kind == 9:
            return self.integer()
        raise ValueError(f'Unknown metadata tag {kind}')

    def typed(self, codec, number):
        """Decode and range-check a named metadata value against its schema."""
        item = codec.shape(number)
        kind = item['kind']
        expected = {'struct': 5, 'array': 0, 'optional': 4, 'choice': 3,
                    'string': 2, 'bytestring': 2, 'blob': 2, 'integer': 9,
                    'enum': 9, 'fourcc': 7, 'bool': 6, 'bits': 1,
                    'float32': 7, 'float64': 8, 'void': 5}[kind]
        actual = self.take(1)[0]
        if actual != expected:
            raise ValueError(f'Invalid tag for {item.get("name")}: {actual}, expected {expected}')

        def bound(value, string=False):
            limits = item['range']
            maximum = limits['max'] * (4 if string else 1)
            if not limits['min'] <= value <= maximum:
                raise ValueError(f'Metadata range violation for {item.get("name")}: {value}')
            return value

        if kind == 'struct':
            count = self.integer()
            if not 0 <= count <= 65536:
                raise ValueError('Invalid metadata field count')
            members = {m['index']: m for m in item['members']}
            result = {}
            for _ in range(count):
                index = self.integer()
                if index in members:
                    member = members[index]
                    if member['name'] in result:
                        raise ValueError('Duplicate metadata field')
                    result[member['name']] = self.typed(codec, member['type'])
                else:
                    self.decode()
            return result
        if kind == 'array':
            count = bound(self.integer())
            if count > 65536:
                raise ValueError('Metadata array exceeds decode limit')
            return [self.typed(codec, item['element']) for _ in range(count)]
        if kind == 'optional':
            return self.typed(codec, item['element']) if self.take(1)[0] else None
        if kind == 'choice':
            index = bound(self.integer())
            member = next(m for m in item['members'] if m['index'] == index)
            return {'choice': index, 'value': self.typed(codec, member['type'])}
        if kind in ('string', 'blob', 'bytestring'):
            value = self.take(bound(self.integer(), string=kind == 'string'))
            return value.decode('utf-8') if kind == 'string' else value
        if kind in ('integer', 'enum'):
            return bound(self.integer())
        if kind == 'fourcc':
            return self.take(4)
        if kind == 'bool':
            value = self.take(1)[0]
            if value not in (0, 1):
                raise ValueError('Invalid metadata boolean')
            return bool(value)
        if kind == 'bits':
            count = bound(self.integer())
            data = self.take((count + 7) // 8)
            return [(data[i // 8] >> (i % 8)) & 1 for i in range(count)]
        if kind in ('float32', 'float64'):
            return struct.unpack('<f' if kind == 'float32' else '<d', self.take(4 if kind == 'float32' else 8))[0]
        if kind == 'void':
            if self.integer() != 0:
                raise ValueError('Nonempty metadata void')
            return None
