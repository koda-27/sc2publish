"""BSN's MSB-first chunks within LSB-addressed bytes."""


class Bits:
    def __init__(self, data, position=0):
        self.data, self.position = data, position

    def read(self, count):
        value = 0
        while count:
            byte, offset = divmod(self.position, 8)
            width = min(count, 8 - offset)
            value = (value << width) | ((self.data[byte] >> offset) & ((1 << width) - 1))
            self.position += width
            count -= width
        return value

    def bytes(self, count):
        self.position = (self.position + 7) // 8 * 8
        start = self.position // 8
        end = start + count
        if count < 0 or end > len(self.data):
            raise IndexError("Incomplete BSN bytes")
        self.position = end * 8
        return bytes(self.data[start:end])


class Writer:
    def __init__(self):
        self.data = bytearray()
        self.position = 0

    def write(self, value, count):
        if value < 0 or value >= (1 << count):
            raise ValueError("BSN integer is outside its encoded range")
        while count:
            index, offset = divmod(self.position, 8)
            if index == len(self.data):
                self.data.append(0)
            take = min(count, 8 - offset)
            self.data[index] |= ((value >> (count - take)) & ((1 << take) - 1)) << offset
            self.position += take
            count -= take

    def bytes(self, value):
        self.write(0, (-self.position) % 8)
        self.data.extend(value)
        self.position += len(value) * 8
