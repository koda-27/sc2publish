"""Read component MapInfo geometry, previews and player slots."""
import struct

from .errors import PublishError


class MapInfo:
    def __init__(self, data):
        self.data, self.pos = data, 0
        if self.take(4) not in (b'MapI', b'IpaM'):
            raise PublishError('MapInfo has an invalid magic value.')
        version = self.u32()
        if not 24 <= version <= 39:
            raise PublishError(f'Unsupported MapInfo version {version}; expected 24–39.')
        self.take(8)
        self.width, self.height = self.u32(), self.u32()
        self.small_preview_type = self.u32()
        self.small_preview = self.string() if self.small_preview_type == 2 else 'Minimap.tga'
        self.large_preview_type = self.u32()
        self.large_preview = self.string() if self.large_preview_type == 2 else 'Minimap.tga'
        if version >= 31:
            self.string()
            self.u32()
        self.u32()
        self.string()
        if version >= 39:
            # Newer component versions store an extra scene field (for example
            # "Dark") between the fog type and the tileset name.
            self.string()
        self.tileset = self.string()
        self.left, self.bottom, self.right, self.top = [self.u32() for _ in range(4)]
        if not (0 < self.right - self.left <= 256 and 0 < self.top - self.bottom <= 256):
            raise PublishError('MapInfo camera bounds are outside supported limits.')
        if version >= 34:
            self.players = self.modern_players()
            return
        self.u32()
        self.u32()
        self.string()
        self.take(self.u16())
        self.take(7 * 4)
        self.u32()
        if version >= 25:
            self.take(8)
        if version >= 31:
            self.take(9)
        if version >= 32:
            self.take(4)
        count = self.u32()
        if count > 16:
            raise PublishError(f'Invalid MapInfo player count: {count}')
        self.players = []
        for _ in range(count):
            player = {'id': self.take(1)[0], 'control': self.u32(), 'color': self.u32(), 'race': self.string()}
            self.take(12)
            self.string()
            self.players.append(player)

    def modern_players(self):
        # Newer component versions add variable loading-screen UI fields before
        # the counted player table. Recognize the table structurally rather than
        # treating version-specific UI bytes as player settings.
        candidates = []
        for start in range(self.pos, len(self.data) - 30):
            self.pos = start
            count = self.u32()
            if not 1 <= count <= 16:
                continue
            players = []
            try:
                for _ in range(count):
                    player = {'id': self.take(1)[0], 'control': self.u32(), 'color': self.u32(), 'race': self.string()}
                    if not 1 <= player['id'] <= 16 or player['control'] > 4:
                        break
                    if player['color'] != 0xffffffff and player['color'] > 31:
                        break
                    if len(player['race']) > 64 or any(ord(c) < 32 for c in player['race']):
                        break
                    self.take(12)
                    decal = self.string()
                    if len(decal) > 1024 or any(ord(c) < 32 for c in decal):
                        break
                    players.append(player)
                if len(players) == count and len({p['id'] for p in players}) == count:
                    candidates.append(players)
            except (PublishError, UnicodeError):
                continue
        if not candidates:
            raise PublishError('Cannot locate the modern MapInfo player table.')
        maximum = max(map(len, candidates))
        candidates = [p for p in candidates if len(p) == maximum]
        if len(candidates) != 1:
            raise PublishError('Modern MapInfo contains ambiguous player tables.')
        return candidates[0]

    def take(self, count):
        end = self.pos + count
        if count < 0 or end > len(self.data):
            raise PublishError('MapInfo is truncated.')
        value = self.data[self.pos:end]
        self.pos = end
        return value

    def u32(self):
        return struct.unpack('<I', self.take(4))[0]

    def u16(self):
        return struct.unpack('<H', self.take(2))[0]

    def string(self):
        end = self.data.find(b'\0', self.pos)
        if end < 0:
            raise PublishError('Unterminated MapInfo string.')
        value = self.take(end - self.pos).decode('utf-8')
        self.take(1)
        return value
