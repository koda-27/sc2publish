"""Minimal protobuf encoding for Battle.net Front RPC."""
import struct


def varint(value):
    if value < 0:
        raise ValueError("Negative protobuf integer")
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value, wire=2):
    key = varint((number << 3) | wire)
    if wire == 0:
        return key + varint(value)
    if wire in (1, 5):
        return key + int(value).to_bytes(8 if wire == 1 else 4, 'little')
    if isinstance(value, str):
        value = value.encode()
    return key + varint(len(value)) + value


def parse(data):
    position = 0
    result = []

    def integer():
        nonlocal position
        value = shift = 0
        while position < len(data) and shift < 70:
            byte = data[position]
            position += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
            shift += 7
        raise ValueError('Malformed protobuf varint')

    while position < len(data):
        tag = integer()
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError('Invalid protobuf field')
        if wire == 0:
            value = integer()
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value = int.from_bytes(data[position:position+size], 'little')
            position += size
        elif wire == 2:
            size = integer()
            value = data[position:position+size]
            position += size
        else:
            raise ValueError('Unsupported protobuf wire type')
        if position > len(data):
            raise ValueError('Truncated protobuf field')
        result.append((number, wire, value))
    return result


def packet(service, method, token, body):
    header = field(1, 0, 0) + field(2, method, 0) + field(3, token, 0) + field(11, service, 5)
    return struct.pack('>H', len(header)) + header + body


def unpack(payload):
    if len(payload) < 2:
        raise ValueError('Truncated RPC packet')
    length = int.from_bytes(payload[:2], 'big')
    if 2 + length > len(payload):
        raise ValueError('Truncated RPC header')
    return {n: v for n, _, v in parse(payload[2:2+length])}, payload[2+length:]
