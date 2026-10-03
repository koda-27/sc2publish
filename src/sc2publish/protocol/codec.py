"""Schema-driven serialization; no captured packets or opaque request templates."""
import json
from importlib.resources import files

from .bits import Bits, Writer

LAYOUTS = {
    "Client::S2Map::PrePublish": [],
    "Client::S2Map::Publish": [],
    "Client::S2Map::PrePublishRequest": [(0, 0), (2, 20), (1, 0)],
    "S2Map::LocalizedName": [(0, 0), (1, 0)],
    "Client::S2Map::PublishRequest": [(0, 0), (4, 0), (2, 29), (1, 0), (3, 0)],
    "Client::S2Map::PrePublishResponse": [(0, 0), (2, 0), (1, 16)],
    "S2Map::PriorRevisionInfo": [(8, 0), (4, 0), (3, 0), (5, 0), (2, 7), (6, 0), (7, 0), (0, 0), (1, 2)],
    "Client::S2Map::PublishResponse": [(0, 0), (1, 0), (2, 14)],
    "Client::Cache::PublishList": [],
    "Client::Cache::PublishFileDescription": [(0, 0), (1, 0), (2, 0), (4, 0), (3, 0)],
    "Client::Cache::PublishListDeclare": [(0, 0), (1, 0), (2, 19), (4, 0), (3, 0)],
    "Client::Cache::GatewayConnectInfo": [(1, 0), (2, 2), (0, 0)],
    "Client::Cache::PublishListResponse": [(0, 0), (1, 16), (2, 0)],
    "Cache::Shared::SaveInfo": [(2, 0), (1, 0), (0, 0)],
    "Client::Cache::RequestWrite": [(0, 0), (1, 0)],
    "Client::Cache::Result": [(1, 0), (0, 0)],
    "Client::Cache::DataChunk": [(0, 0)],
    "Toon::Handle": [(1, 0), (0, 0), (2, 0), (3, 0)],
    "Client::Toon::ToonSelect": [(0, 0), (1, 10)],
    "Client::Toon::ToonList": [(0, 0)],
    "Toon::Display": [(0, 0), (3, 0), (4, 3), (2, 0), (1, 0)],
    "Client::Toon::ToonSelected": [(3, 0), (1, 0), (2, 0), (4, 0), (0, 0)],
    "S2Map::AuthorPrefs": [(0, 0), (1, 0), (2, 0), (3, 0)],
    "Client::Connection::ConnectionClosing": [(4, 0), (0, 0), (3, 0), (1, 0), (2, 0)],
    "PacketInfo": [(1, 0), (0, 0), (4, 0), (2, 0), (3, 0)],
}


class Codec:
    def __init__(self):
        entries = json.loads(files("sc2publish.protocol").joinpath("schema.json").read_text())
        entries += json.loads(files("sc2publish.protocol").joinpath("auth-schema.json").read_text())
        self.types = {x['id']: x for x in entries}
        self.names = {x['name']: x['id'] for x in entries if x.get('name')}

    def find(self, name):
        return self.names[name if name.startswith('Battlenet::') else 'Battlenet::' + name]

    def shape(self, number):
        item = self.types[self.find(number) if isinstance(number, str) else number]
        while item['kind'] == 'alias':
            item = self.types[item['element']]
        return item

    def default(self, number):
        item = self.shape(number)
        kind = item['kind']
        if kind == 'struct':
            return {m['name']: self.default(m['type']) for m in item['members']}
        if kind in ('optional', 'void'):
            return None
        if kind in ('array', 'bits'):
            return []
        if kind == 'bool':
            return False
        if kind == 'string':
            return ''
        if kind == 'fourcc':
            return b'\0' * 4
        if kind in ('bytestring', 'blob'):
            return bytes(item['range']['min'])
        if kind == 'choice':
            member = item['members'][0]
            return {'choice': member['index'], 'value': self.default(member['type'])}
        if kind in ('integer', 'enum'):
            return max(0, item['range']['min'])
        raise ValueError(f'Unsupported default kind: {kind}')

    def make(self, name, **values):
        result = self.default(name)
        unknown = values.keys() - result.keys()
        if unknown:
            raise ValueError(f'Unknown fields for {name}: {sorted(unknown)}')
        result.update(values)
        return result

    def bound(self, io, item, value=None):
        bound = item['range']
        width = bound['bits'] or 0
        maximum = bound['max']
        if item['kind'] == 'string':
            width += 2
            maximum *= 4
        if isinstance(io, Bits):
            result = bound['min'] + io.read(width)
            if result > maximum:
                raise ValueError('BSN range overflow')
            return result
        if not bound['min'] <= value <= maximum:
            raise ValueError(f'Value outside range for {item.get("name")}')
        io.write(value - bound['min'], width)

    def layout(self, item):
        name = (item.get('name') or '').removeprefix('Battlenet::')
        if name in LAYOUTS:
            return LAYOUTS[name]
        if item['obfuscated']:
            raise ValueError(f'Unsupported native layout: {name}')
        return [(i, 0) for i in range(len(item['members']))]

    def decode(self, reader, number):
        item = self.shape(number)
        kind = item['kind']
        if kind == 'void':
            return None
        if kind == 'bool':
            return bool(reader.read(1))
        if kind in ('integer', 'enum'):
            return self.bound(reader, item)
        if kind == 'fourcc':
            return reader.read(32).to_bytes(4, 'big')
        if kind in ('bytestring', 'blob', 'string'):
            data = reader.bytes(self.bound(reader, item))
            return data.decode() if kind == 'string' else data
        if kind in ('array', 'bits'):
            count = self.bound(reader, item)
            if count > 65536:
                raise ValueError('BSN collection exceeds message limit')
            return [reader.read(1) if kind == 'bits' else self.decode(reader, item['element']) for _ in range(count)]
        if kind == 'optional':
            return self.decode(reader, item['element']) if reader.read(1) else None
        if kind == 'choice':
            index = self.bound(reader, item)
            member = next(m for m in item['members'] if m['index'] == index)
            return {'choice': index, 'value': self.decode(reader, member['type'])}
        if kind == 'struct':
            result = {}
            for index, padding in self.layout(item):
                reader.read(padding)
                member = item['members'][index]
                result[member['name']] = self.decode(reader, member['type'])
            return result
        raise ValueError(f'Unsupported BSN kind {kind}')

    def encode(self, writer, number, value):
        item = self.shape(number)
        kind = item['kind']
        if kind == 'void':
            return
        if kind == 'bool':
            writer.write(int(value), 1)
        elif kind in ('integer', 'enum'):
            self.bound(writer, item, value)
        elif kind == 'fourcc':
            writer.write(int.from_bytes(value, 'big') if isinstance(value, bytes) else value, 32)
        elif kind in ('bytestring', 'blob', 'string'):
            value = value.encode() if isinstance(value, str) else value
            self.bound(writer, item, len(value))
            writer.bytes(value)
        elif kind in ('array', 'bits'):
            self.bound(writer, item, len(value))
            for child in value:
                if kind == 'bits':
                    writer.write(child, 1)
                else:
                    self.encode(writer, item['element'], child)
        elif kind == 'optional':
            writer.write(int(value is not None), 1)
            if value is not None:
                self.encode(writer, item['element'], value)
        elif kind == 'choice':
            self.bound(writer, item, value['choice'])
            member = next(m for m in item['members'] if m['index'] == value['choice'])
            self.encode(writer, member['type'], value['value'])
        elif kind == 'struct':
            for index, padding in self.layout(item):
                writer.write(0, padding)
                member = item['members'][index]
                self.encode(writer, member['type'], value[member['name']])
        else:
            raise ValueError(f'Unsupported BSN kind {kind}')

    def decode_record(self, data, name):
        reader = Bits(data)
        command = reader.read(6)
        slot = reader.read(4) if reader.read(1) else None
        result = self.decode(reader, name)
        return command, slot, result, (reader.position + 7) // 8

    def encode_record(self, command, slot, name, value):
        writer = Writer()
        writer.write(command, 6)
        writer.write(1, 1)
        writer.write(slot, 4)
        self.encode(writer, name, value)
        return bytes(writer.data)
