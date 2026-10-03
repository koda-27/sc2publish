"""Compile lobby, localization and Arcade metadata directly from map components.

No prior publication, serialized header or editor traffic is an input.
"""
import hashlib
import io
import json
import xml.etree.ElementTree as ET
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from xml.sax.saxutils import escape

from PIL import Image

from .build import strings
from .errors import PublishError
from .mapinfo import MapInfo
from .protocol.codec import Codec
from .protocol.tagged import Reader, encode

# Source IDs and entry indices for built-in lobby attributes. These tables are
# protocol constants, not publication metadata. Custom definitions use their
# own source-defined order.
CORE_INDICES = {
    1001: {28271: 0},
    2000: {29746: 0, 29747: 1, 29748: 2, 29749: 3, 29750: 4, 29751: 5,
           29752: 6, 29753: 7, 1131770740: 12},
    500: {1131180900: 0, 1332766062: 1, 1215655278: 2, 1131375984: 3},
    3006: {12592: 3, 12597: 4},
    3007: {1348563572: 0, 1466004579: 1},
}
TEAM_STRIDES = {2012: 3, 2014: 2, 2018: 15}
# The 15-team attribute is supplied by Blizzard's Teams 15 (Mod), rather
# than Core. Referencing it requires the publication dependency as well.
LOBBY_DEPENDENCIES = {2018: 17}
# Stock tileset display names; extend as needed for other tilesets.
TILESET_NAMES = {'AvernusEx1': 'Avernus (Space Platform)'}


def value(node, child, default=''):
    child = node.find(child) if node is not None else None
    return child.get('Value', child.text or default) if child is not None else default


def custom_code(number):
    """Editor custom value IDs are four printable characters or padded decimal IDs."""
    raw = number.to_bytes(4, 'big')
    return raw if all(32 <= byte < 127 for byte in raw) else f'{number:04d}'[:4].encode()


def handle(namespace, ident):
    return {'m_nameSpace': namespace, 'm_id': ident}


def near(index=0):
    return {'m_index': index, 'm_baked': False}


@dataclass
class PublicationFile:
    usage: str
    data: bytes
    region: str
    label: str

    @property
    def cache_handle(self):
        return self.usage.encode() + b'\0\0' + self.region.encode() + hashlib.sha256(self.data).digest()


@dataclass
class Bundle:
    files: list
    metadata: dict


def serialize_locale(locale, entries):
    """Battle.net's locale reader requires the editor's escaped, CRLF format.

    General XML serializers can produce valid XML that is nevertheless rejected
    as a corrupt localization file (3244) during header validation.
    """
    lines = ['<?xml version="1.0" encoding="utf-8"?>', f'<Locale region="{locale}">']
    for ident, text in entries:
        escaped = escape(text, {'"': '&quot;', "'": '&apos;'})
        lines.append(f'    <e id="{ident}">{escaped}</e>')
    lines.append('</Locale>')
    return '\r\n'.join(lines).encode('utf-8')


class Compiler:
    def __init__(self, project, version):
        self.project, self.version = project, version
        self.root = project.component_dir
        self.codec = Codec()
        self.map_handle = {'m_id': project.map_id, 'm_version': version}
        self.info = ET.parse(self.root / 'DocumentInfo').getroot()
        self.attrs = ET.parse(self.root / 'Attributes').getroot() if (self.root / 'Attributes').exists() else ET.Element('Attributes')
        self.mapinfo = MapInfo((self.root / 'MapInfo').read_bytes())
        self.text = {}
        for path in sorted((self.root / f'{project.locale}.SC2Data/LocalizedData').glob('*.txt')):
            self.text.update(strings(path))
        self.text['DocInfo/Name'] = project.name
        self.localized = OrderedDict()
        self.localize('DocInfo/Name')
        self.localize(self.text.get('DocInfo/DescLong', self.text.get('DocInfo/DescShort', '')))
        self.visuals = []
        self.images = {}
        self.pending = []
        self.atlases = {}
        self.image_order = 0
        self.custom = {}
        self.stock = OrderedDict()
        self.files = [PublicationFile('s2ma', project.archive.read_bytes(), project.region, project.archive.name)]

    def localize(self, key):
        text = self.text.get(key, key)
        if not key:
            return {'m_colorHint': None, 'm_fileIndex': 0, 'm_id': 0}
        # Empty source strings still need real entries when referenced by a
        # section or attribute. ID zero means "unset", not an empty string.
        # Key-based interning also keeps distinct translation contexts intact.
        if key not in self.localized:
            self.localized[key] = (len(self.localized) + 1, text)
        return {'m_colorHint': None, 'm_fileIndex': 0, 'm_id': self.localized[key][0]}

    def image_path(self, name):
        if not name:
            return None
        relative = name.replace('\\', '/')
        options = [self.root / relative, self.root / 'Base.SC2Assets' / relative]
        return next((p for p in options if p.is_file()), None)

    def image(self, name, size=None, required=False, image_format=None, role=None):
        if not name:
            return None
        key = (name.replace('\\', '/').casefold(), size, image_format)
        if key in self.images:
            return self.images[key]
        path = self.image_path(name)
        if path is None:
            if required:
                raise PublishError(f'Missing publication image: {name}')
            return None
        with Image.open(path) as image:
            if size:
                image = image.convert('RGBA').resize(size, Image.Resampling.LANCZOS)
                out = io.BytesIO()
                if image_format == 'DDS':
                    image.save(out, format='DDS', pixel_format='DXT5')
                else:
                    image.save(out, format='TGA')
                data = out.getvalue()
            else:
                data = path.read_bytes()
            width, height = image.size
        if len(self.pending) + len(self.atlases) >= 32:
            raise PublishError('Publication uses more than 32 distinct preview/icon images.')
        result = {'m_visualFileIndex': 0, 'm_top': 0, 'm_left': 0,
                  'm_height': height, 'm_width': width}
        self.pending.append({'role': role, 'order': self.image_order, 'data': data,
                             'label': path.name, 'art': result})
        self.image_order += 1
        self.images[key] = result
        return result

    def atlas_cell(self, key, cell, icon):
        """Register one slot of a lazily composed icon atlas.

        The editor packs preview/icon images column-major into fixed 8-row
        columns with a one-pixel transparent gutter (attribute icons 16x16,
        result icons 64x64). Cells keep their registration order, which
        follows the source definitions and result-catalog order.
        """
        atlas = self.atlases.setdefault(key, {'cell': cell, 'icons': []})
        index = len(atlas['icons'])
        art = {'m_visualFileIndex': 0, 'm_top': (index % 8) * (cell + 1),
               'm_left': (index // 8) * (cell + 1), 'm_height': cell, 'm_width': cell}
        atlas['icons'].append({'icon': icon, 'art': art})
        return art

    def compose_atlas(self, key):
        atlas = self.atlases[key]
        cell = atlas['cell']
        columns = max(1, (len(atlas['icons']) + 7) // 8)
        width, height = columns * cell + columns - 1, 8 * cell + 7
        sheet = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        for item in atlas['icons']:
            path = self.image_path(item['icon'])
            if path is None:
                tile = Image.new('RGBA', (cell, cell), (0, 0, 0, 0))
            else:
                with Image.open(path) as source:
                    tile = source.convert('RGBA').resize((cell, cell), Image.Resampling.BOX)
            sheet.paste(tile, (item['art']['m_left'], item['art']['m_top']))
        out = io.BytesIO()
        sheet.save(out, format='TGA')
        return out.getvalue(), width, height

    def finalize_visuals(self):
        """Assign visual file indices in the editor's publication order.

        The editor ships the big map, the attribute icon atlas, the result
        icon atlas, the game-info screenshots, then the map icon.
        """
        order = {'bigmap': 0, 'attribute': 1, 'result': 2, 'screenshot': 3, 'map-icon': 8}
        entries = list(self.pending)
        for key, role in (('attribute', 'attribute'), ('result', 'result')):
            atlas = self.atlases.get(key)
            if not atlas or not atlas['icons']:
                continue
            data, width, height = self.compose_atlas(key)
            entries.append({'role': role, 'order': self.image_order, 'data': data,
                            'label': f'{key} icon atlas', 'art': {'m_top': 0, 'm_left': 0,
                                                                  'm_height': height, 'm_width': width},
                            'cells': [item['art'] for item in atlas['icons']]})
            self.image_order += 1
        entries.sort(key=lambda entry: (order.get(entry['role'], 9), entry['order']))
        for index, entry in enumerate(entries):
            entry['art']['m_visualFileIndex'] = index
            for art in entry.get('cells', ()):
                art['m_visualFileIndex'] = index
            item = PublicationFile('s2mv', entry['data'], self.project.region, entry['label'])
            self.files.append(item)
            self.visuals.append(item.cache_handle)
        self.pending = []

    def visual(self, node):
        name, tip, icon = value(node, 'Name'), value(node, 'Description'), value(node, 'Icon')
        return {'m_text': self.localize(name) if name else None,
                'm_tip': self.localize(tip) if tip else None,
                'm_art': self.atlas_cell('attribute', 16, icon) if icon else None}

    def namespace(self, node):
        namespace = int(node.get('Namespace', '0'))
        return namespace or self.project.namespace

    def stock_values(self):
        """Collect source choices; core definitions are inherited from SC2."""
        for node in self.attrs.findall('Variant/Attribute'):
            if self.namespace(node) != 999:
                continue
            ident = int(node.get('Id'))
            entry = self.stock.setdefault(ident, {'values': [], 'player': False})
            for default in node.findall('Default'):
                number = int(default.find('Value').get('Id'))
                if number not in entry['values']:
                    entry['values'].append(number)
                if default.find('Slot').get('Id') != 'Global':
                    entry['player'] = True
        self.stock[500] = {'values': [int.from_bytes(x, 'big') for x in (b'Clsd', b'Open', b'Humn', b'Comp')], 'player': True}
        races = list(dict.fromkeys(p['race'] for p in self.mapinfo.players if p['race']))
        if races:
            self.stock[3001] = {'values': [int.from_bytes(r.encode()[:4].ljust(4, b'\0'), 'big') for r in races]
                               + [int.from_bytes(b'RAND', 'big')], 'player': True}

    def indices(self, namespace, ident):
        if namespace == 999 and ident in self.stock:
            return self.stock[ident]['values']
        if namespace == self.project.namespace and ident in self.custom:
            return [int(value(v, 'Id')) for v in self.custom[ident].findall('Value')]
        raise PublishError(f'No source definition for attribute {namespace}/{ident}.')

    def defaults(self, node, namespace, ident, inherited=None):
        choices = self.indices(namespace, ident)
        defaults = node.findall('Default')
        is_player = (value(node, 'Type') == 'Player' or
                     any(d.find('Slot').get('Id') != 'Global' for d in defaults) or
                     (inherited is not None and inherited['choice'] == 1))
        result = deepcopy(inherited) if inherited is not None else {
            'choice': 1 if is_player else 0,
            'value': [near() for _ in range(16)] if is_player else near()}
        team_counts = {}
        for default in defaults:
            slot = default.find('Slot').get('Id')
            code = int(default.find('Value').get('Id'))
            if code not in choices:
                raise PublishError(f'Unknown default value {code} for attribute {namespace}/{ident}.')
            index = choices.index(code)
            if namespace == 999 and ident != 3001:
                if ident in TEAM_STRIDES:
                    team = (code & 255) - ord('1')
                    if not 0 <= team < TEAM_STRIDES[ident]:
                        raise PublishError(f'Unsupported team code {code} for attribute {ident}.')
                    index = team + team_counts.get(team, 0) * TEAM_STRIDES[ident]
                    team_counts[team] = team_counts.get(team, 0) + 1
                elif code in CORE_INDICES.get(ident, {}):
                    index = CORE_INDICES[ident][code]
                else:
                    raise PublishError(f'Unsupported built-in attribute value {ident}/{code}.')
            selected = near(index)
            if slot == 'Global':
                if result['choice'] == 1:
                    result['value'] = [deepcopy(selected) for _ in range(16)]
                else:
                    result = {'choice': 0, 'value': selected}
            else:
                result['value'][int(slot)] = selected
        return result

    def requirements(self, node):
        result = []
        for requirement in node.findall('Requirement'):
            attribute = requirement.find('Attribute')
            namespace = self.namespace(attribute)
            ident = int(attribute.get('Id'))
            source = self.custom.get(ident) if namespace == self.project.namespace else None
            scope = 21 if source is not None and value(source, 'Type') == 'Player' else 20
            codes = [int(v.get('Id')) for v in requirement.findall('Value')]
            result.append({'m_address': {'m_scope': scope, 'm_definitionHandle': handle(namespace, ident)},
                           'm_set': [custom_code(n) if namespace != 999 else n.to_bytes(4, 'big') for n in codes]})
        return result

    def attributes(self):
        self.custom = {int(value(a, 'Id')): a for a in self.attrs.findall('Attribute')}
        self.stock_values()
        definitions, instances = [], []
        base_defaults = {}
        for ident, stock in self.stock.items():
            defaults = {'choice': 1 if stock['player'] else 0,
                        'value': [near() for _ in range(16)] if stock['player'] else near()}
            if ident == 500:
                # Editor default: every slot starts Open. A variant that sets
                # controller slots starts from the map's player layout instead.
                defaults['value'] = [near(1) for _ in range(16)]
                self.player_controllers = [near() for _ in range(16)]
                for player in self.mapinfo.players:
                    if 1 <= player['id'] <= 16:
                        self.player_controllers[player['id'] - 1] = near(
                            {1: 1, 2: 3}.get(player['control'], 0))
            elif ident == 3001:
                defaults['value'] = [near(len(stock['values']) - 1) for _ in range(16)]
            base_defaults[(999, ident)] = defaults
            if ident in (500, 3001):
                instances.append({'m_definitionHandle': handle(999, ident), 'm_data': deepcopy(defaults)})
            if ident != 3001:
                continue
            labels = {500: 'Controller', 1001: 'Game mode', 2000: 'Teams', 3001: 'Race', 3006: 'Game speed', 3007: 'Participant role'}
            table = []
            for number in stock['values']:
                raw = number.to_bytes(4, 'big')
                label = raw.strip(b'\0').decode('ascii', errors='replace')
                label = self.text.get('Race/Name/' + label, 'Random' if label == 'RAND' else label)
                table.append({'m_code': raw, 'm_visual': {'m_text': self.localize(label),
                                                        'm_tip': None, 'm_art': None}, 'm_requirements': []})
            definitions.append(self.codec.make('Attribute::Definition', m_handle=handle(999, ident),
                                               m_table=table, m_visual={'m_text': self.localize(labels.get(ident, f'Attribute {ident}')),
                                                                      'm_tip': None, 'm_art': None},
                                               m_parents=[{'m_address': {'m_scope': 21, 'm_definitionHandle': handle(999, 500)},
                                                           'm_set': [b'Humn', b'Comp']},
                                                          {'m_address': {'m_scope': 21, 'm_definitionHandle': handle(999, 3007)},
                                                           'm_set': [b'Part']}],
                                               m_visibility=3, m_access=1, m_defaults=defaults))
        roles, colors = [near(1) for _ in range(16)], [near() for _ in range(16)]
        race_instance = next((i for i in instances if i['m_definitionHandle']['m_id'] == 3001), None)
        if race_instance:
            race_instance['m_data']['value'] = [near() for _ in range(16)]
        for player in self.mapinfo.players:
            slot = player['id'] - 1
            if 0 <= slot < 16:
                roles[slot] = near(0)
                colors[slot] = near(slot if player['color'] == 0xffffffff else player['color'])
                if race_instance and player['race']:
                    code = int.from_bytes(player['race'].encode()[:4].ljust(4, b'\0'), 'big')
                    race_instance['m_data']['value'][slot] = {'m_index': self.stock[3001]['values'].index(code), 'm_baked': True}
        instances.extend([{'m_definitionHandle': handle(999, 3007), 'm_data': {'choice': 1, 'value': roles}},
                          {'m_definitionHandle': handle(999, 3002), 'm_data': {'choice': 1, 'value': colors}}])
        core_order = {500: 0, 3007: 1, 3001: 2, 3002: 3}
        instances.sort(key=lambda entry: core_order[entry['m_definitionHandle']['m_id']])
        for order, (ident, node) in enumerate(self.custom.items(), 32):
            table = [{'m_code': custom_code(int(value(v, 'Id'))), 'm_visual': self.visual(v),
                      'm_requirements': self.requirements(v)} for v in node.findall('Value')]
            if not table:
                raise PublishError(f'Custom attribute {ident} has no values.')
            defaults = self.defaults(node, self.project.namespace, ident)
            flags = (2 if node.find('PublicLocked') is not None else 0) | (4 if node.find('Hidden') is not None else 0)
            parents = self.requirements(node)
            parents.append({'m_address': {'m_scope': 21, 'm_definitionHandle': handle(999, 500)},
                            'm_set': [b'Humn', b'Comp']})
            definitions.append(self.codec.make('Attribute::Definition', m_handle=handle(self.project.namespace, ident),
                                               m_table=table, m_visual=self.visual(node), m_parents=parents,
                                               m_arbitration=int(value(node, 'Availability') == 'First'),
                                               m_visibility={'None': 0, 'Self': 1, 'Host': 2, 'All': 3}.get(value(node, 'Visibility'), 3),
                                               m_access={'None': 0, 'Self': 1, 'Host': 2, 'All': 3}.get(value(node, 'Access'), 2),
                                               m_flags=flags, m_defaults=defaults, m_sortOrder=order))
            base_defaults[(self.project.namespace, ident)] = defaults
            instances.append({'m_definitionHandle': handle(self.project.namespace, ident), 'm_data': deepcopy(defaults)})
        variants = []
        for node in self.attrs.findall('Variant'):
            variant_instances, locks, restrictions = [], [], []
            for attr in node.findall('Attribute'):
                namespace, ident = self.namespace(attr), int(attr.get('Id'))
                inherited = base_defaults.get((namespace, ident))
                if namespace == 999 and ident == 500 and attr.findall('Default'):
                    inherited = {'choice': 1, 'value': deepcopy(self.player_controllers)}
                defaults = self.defaults(attr, namespace, ident, inherited)
                variant_instances.append({'m_definitionHandle': handle(namespace, ident), 'm_data': defaults})
                if any(d.find('Locked') is not None for d in attr.findall('Default')):
                    scopes = [0] * 16
                    for d in attr.findall('Default'):
                        if d.find('Locked') is not None:
                            slot = d.find('Slot').get('Id')
                            if slot != 'Global':
                                scopes[int(slot)] = 1
                    locks.append({'m_handle': handle(namespace, ident), 'm_lockedScopes': scopes})
                # <ValueHidden> entries restrict individual value choices per
                # slot. The editor stores one bitfield per slot, in slot order,
                # trimmed after the last restricted slot.
                hidden_values = attr.findall('ValueHidden')
                if hidden_values:
                    choices = self.indices(namespace, ident)
                    slots = {}
                    for entry in hidden_values:
                        code = int(entry.get('Id'))
                        if code not in choices:
                            raise PublishError(f'Unknown hidden value {code} for attribute {namespace}/{ident}.')
                        bit = choices.index(code)
                        scope = entry.get('Slot')
                        for slot in range(16) if scope == 'Global' else [int(scope)]:
                            slots.setdefault(slot, set()).add(bit)
                    masks = [[1 if bit in slots.get(slot, ()) else 0 for bit in range(len(choices))]
                             for slot in range(max(slots) + 1)]
                    restrictions.append({'m_handle': handle(namespace, ident),
                                         'm_data': {'choice': 1, 'value': {'m_bySlot': masks}}})
            hidden = [{'m_handle': handle(self.namespace(a), int(a.get('Id'))), 'm_hidden': True}
                      for a in node.findall('AttributeHidden')]
            controllers = next((i['m_data'] for i in variant_instances if i['m_definitionHandle'] == handle(999, 500)), None)
            if controllers is None:
                controllers = deepcopy(base_defaults[(999, 500)])
                # The editor appends a controller instance when the source
                # variant does not define one; existing ones keep source order.
                variant_instances.append({'m_definitionHandle': handle(999, 500), 'm_data': controllers})
            participation = next((i['m_data']['value'] for i in variant_instances
                                  if i['m_definitionHandle'] == handle(999, 3007)), roles)
            # MaxPlayers fields are range-checked by Battle.net (1..16); a
            # variant whose slots are all closed still needs a valid minimum.
            maximum = max(1, sum(v['m_index'] == 0 for v in participation))
            open_slots = max(1, sum(v['m_index'] in (1, 2) for v in controllers['value']))
            variants.append(self.codec.make('S2Map::VariantInfo',
                m_variantId={'m_category': int(value(node, 'CategoryId', '1')), 'm_mode': int(value(node, 'ModeId', '1'))},
                m_categoryName=self.localize(value(node, 'CategoryName', 'Other')),
                m_modeName=self.localize(value(node, 'ModeName', 'Tutorial' if value(node, 'ModeId') == '2' else 'Custom')),
                m_categoryDescription=self.localize(value(node, 'CategoryDesc', 'Unclassified game type.')),
                m_modeDescription=self.localize(value(node, 'ModeDesc', 'Interactive tutorial to learn how to play the map.' if value(node, 'ModeId') == '2' else '')),
                m_instances=variant_instances, m_lockedAttributes=locks, m_visibilityOverrides=hidden,
                m_instanceRestrictions=restrictions,
                m_maxTeamSize=int(value(node, 'MaxTeamSize', str(maximum))),
                m_maxHumanPlayers=maximum, m_maxOpenSlots=open_slots,
                m_teamNames=[self.localize(a.get('Value')) for a in node.findall('TeamName')]))
        if not variants:
            variants.append(self.codec.make('S2Map::VariantInfo', m_variantId={'m_category': 1, 'm_mode': 1},
                                           m_categoryName=self.localize('DocInfo/Name'), m_modeName=self.localize('Standard'),
                                           m_maxTeamSize=16, m_maxHumanPlayers=16, m_maxOpenSlots=16))
        return definitions, instances, variants

    def arcade(self):
        arcade = self.codec.default('S2Map::ArcadeGameInfo')
        icon = self.info.findtext('Icon/Value', '')
        arcade['m_mapIcon'] = self.image(icon, (225, 150), required=bool(icon), role='map-icon')
        screenshots = self.info.findall('Screenshot/File/Value')
        captions = self.info.findall('Screenshot/CaptionId/Value')
        for index, node in enumerate(screenshots):
            picture = self.image(node.text or '', required=True, role='screenshot')
            caption = captions[index].text if index < len(captions) else str(index + 1)
            arcade['m_gameInfoScreenshots'].append({'m_picture': picture,
                                                   'm_caption': self.localize(f'DocInfo/Screenshot{int(caption):02d}')})
        unset = {'m_colorHint': None, 'm_fileIndex': 0, 'm_id': 0}
        website = self.text.get('DocInfo/Website', '')
        arcade['m_website'] = self.localize('DocInfo/Website') if website else unset
        for group, title in [('Basic', 'Basic Instructions'), ('Winning', 'How To Win'),
                             ('Advanced', 'Advanced Instructions')]:
            keys = sorted(k for k in self.text if k.startswith('DocInfo/HowToPlay' + group) and self.text[k])
            if keys:
                section = arcade['m_howToPlaySections']
                section['m_headers'].append({'m_title': self.localize(title),
                                             'm_startOffset': len(section['m_sectionData']),
                                             'm_listType': 2 if group == 'Winning' else 0,
                                             'm_subtitle': unset})
                section['m_sectionData'].extend(self.localize(k) for k in keys)
        patch = self.info.find('PatchNote')
        if patch is not None:
            for version, date, notes in zip(patch.findall('Version/Value'), patch.findall('Date/Value'), patch.findall('Notes/Value')):
                section = arcade['m_patchNoteSections']
                try:
                    parsed = datetime.strptime(date.text or '', '%m/%d/%Y')
                    formatted = f'{parsed:%B} {parsed.day}, {parsed.year}'
                except ValueError:
                    formatted = date.text or ''
                section['m_headers'].append({'m_title': self.localize('') if not version.text
                                             else {'m_colorHint': None, 'm_fileIndex': 0,
                                                   'm_id': self._intern(f'Version {version.text}')[0]},
                                             'm_startOffset': len(section['m_sectionData']), 'm_listType': 2,
                                             'm_subtitle': {'m_colorHint': None, 'm_fileIndex': 0,
                                                            'm_id': self._intern(formatted)[0]} if formatted else unset})
                for ident in (notes.text or '').split(','):
                    key = f'DocInfo/PatchNote{int(ident):03d}' if ident.isdecimal() else ident
                    section['m_sectionData'].append(self.localize(key))
        notes_file = getattr(self.project, 'publish', {}).get('notes')
        if notes_file:
            notes = [line.strip() for line in self.project.resolve(notes_file).read_text(encoding='utf-8-sig').splitlines()
                     if line.strip() and not line.lstrip().startswith('#')]
            if len(notes) > 100 or any(len(note) > 200 for note in notes):
                raise PublishError('Publication notes allow at most 100 bullets of 200 characters each.')
            section = arcade['m_patchNoteSections']
            section['m_headers'] = []
            section['m_sectionData'] = []
            if notes:
                now = datetime.now()
                title = f'Version {self.version >> 16}.{self.version & 65535}'
                subtitle = f'{now:%B} {now.day}, {now.year}'
                def literal(text):
                    return {'m_colorHint': None, 'm_fileIndex': 0, 'm_id': self._intern(text)[0]}
                section['m_headers'].append({'m_title': literal(title), 'm_subtitle': literal(subtitle),
                                             'm_startOffset': 0, 'm_listType': 2})
                section['m_sectionData'] = [literal(note) for note in notes]
        if self.info.findtext('Tutorial/Type/Value') == 'Self':
            speeds = {'Slower': b'Slor', 'Slow': b'Slow', 'Normal': b'Norm', 'Fast': b'Fast', 'Faster': b'Fasr'}
            category = self.info.findtext('Tutorial/VariantCategory/Value')
            mode = self.info.findtext('Tutorial/VariantMode/Value')
            variants = self.attrs.findall('Variant')
            index = int(self.info.findtext('Tutorial/VariantIndex/Value', '0'))
            if category is not None and mode is not None:
                index = next((i for i, variant in enumerate(variants)
                              if value(variant, 'CategoryId', '1') == category
                              and value(variant, 'ModeId', '1') == mode), -1)
            if not 0 <= index < max(1, len(variants)):
                raise PublishError('The tutorial refers to a missing lobby variant.')
            arcade['m_tutorialLink'] = {'m_variantIndex': index,
                                       'm_speed': speeds.get(self.info.findtext('Tutorial/GameSpeed/Value'), b'Fast'),
                                       'm_entries': [{'m_handle': self.map_handle, 'm_type': 0}]}
        return arcade

    def _intern(self, text):
        key = f'\0literal:{text}'
        if key not in self.localized:
            self.localized[key] = (len(self.localized) + 1, text)
        return self.localized[key]

    def catalogs(self):
        from .catalogs import merge
        dependencies = self.info.find('Dependencies')
        references = ([node.text or '' for node in dependencies.findall('Value')]
                      if dependencies is not None else [])
        return merge(references, self.root, getattr(self.project, 'layers', ()) or ())

    def result_definitions(self):
        """Locally defined units and upgrades with a score result.

        Battle.net shows these in the score screen event list. The editor
        selects mod-only definitions whose merged catalog carries a
        ScoreResult field and indexes them at their merged catalog position;
        their 64x64 icons are packed into one shared atlas.
        """
        from .catalogs import RESULT_TAGS
        items, icons = [], {}
        for name, tag, index, chain, icon in self.catalogs().result_entries(self.root):
            key = next((f'{tag}/Name/{candidate}' for candidate in chain
                        if f'{tag}/Name/{candidate}' in self.text), f'{tag}/Name/{name}')
            text = self.text.get(key, '')
            items.append({
                'm_name': name[:31],
                'm_handle': handle(self.project.namespace, (RESULT_TAGS[tag] << 24) | index),
                'm_visual': {'m_text': self.localize(key) if text else None, 'm_tip': None, 'm_art': None},
                'm_containers': [{'m_handle': handle(self.project.namespace, 0x04000006), 'm_orderHint': 0}],
                'm_form': 4, 'm_flags': 0, 'm_achievement': None,
                'm_performanceMeasure': None, 'm_specificHero': None, 'm_uniqueTag': None})
            icons[name[:31]] = icon
        # Atlas cells follow the final emission order so m_top/m_left match
        # the editor's persisted layout.
        for item in items:
            item['m_visual']['m_art'] = self.atlas_cell('result', 64, icons.get(item['m_name']))
        return items

    def compile(self):
        definitions, instances, variants = self.attributes()
        dependencies = list(dict.fromkeys(int(ident) for ident in self.project.project.get('extra_dependencies', [])))
        for ident in self.stock:
            product = LOBBY_DEPENDENCIES.get(ident)
            if product is not None and product not in dependencies:
                dependencies.append(product)
        working = self.codec.default('Attribute::WorkingSet')
        working.update(m_name=self.localize('DocInfo/Name'), m_description=self.localize(self.text.get('DocInfo/DescLong', self.text.get('DocInfo/DescShort', ''))),
                       m_maxPlayers=max(1, sum(p['control'] == 1 for p in self.mapinfo.players)),
                       m_specialScope=22, m_instances=instances)
        if self.mapinfo.large_preview_type or self.mapinfo.small_preview_type:
            preview = self.mapinfo.large_preview if self.mapinfo.large_preview_type else self.mapinfo.small_preview
            working['m_bigMap'] = self.image(preview, (24, 24), required=True, image_format='DDS', role='bigmap')
        arcade = self.arcade()
        header = self.codec.default('S2Map::Header')
        header['m_bnet'].update(m_handle=self.map_handle, m_name=self.project.archive.name,
                               m_archiveCacheHandle=self.files[0].cache_handle,
                               m_mapNamespace=self.project.namespace, m_workingSet=working,
                               m_definitions=definitions, m_variants=variants,
                               m_size={'m_horizontal': self.mapinfo.right - self.mapinfo.left,
                                       'm_vertical': self.mapinfo.top - self.mapinfo.bottom},
                               m_tileset=self.localize(TILESET_NAMES.get(self.mapinfo.tileset, self.mapinfo.tileset)),
                               m_extraDependencies=[{'m_id': ident, 'm_version': 0} for ident in dependencies],
                               m_addDefaultPermissions=True, m_arcadeData=arcade,
                               m_resultDefinitions=self.result_definitions())
        localized = serialize_locale(self.project.locale, self.localized.values())
        locale_file = PublicationFile('s2ml', localized, self.project.region, self.project.locale)
        locales = [{'m_locale': self.project.locale.encode(), 'm_stringFileTable': [locale_file.cache_handle]}]
        self.finalize_visuals()
        self.files.append(locale_file)
        # The map definition table and lobby working set are separate locale
        # owners in the publication declaration, even when their bytes match.
        self.files.append(PublicationFile('s2ml', localized, self.project.region,
                                          self.project.locale + ' (map definitions)'))
        working['m_visualFiles'] = list(self.visuals)
        working['m_locales'] = locales
        header['m_bnet']['m_locales'] = locales
        data = encode(self.codec, 'S2Map::Header', header)
        typed_reader = Reader(data)
        if typed_reader.typed(self.codec, 'S2Map::Header') != header or typed_reader.position != len(data):
            raise PublishError('Generated metadata does not match its typed representation.')
        reader = Reader(data)
        decoded = reader.decode()
        if reader.position != len(data) or decoded[0][0] != {0: self.project.map_id, 1: self.version}:
            raise PublishError('Generated map metadata failed validation.')
        self.files.append(PublicationFile('s2mh', data, self.project.region, 'publication header'))
        output = self.project.output / 'publication'
        output.mkdir(parents=True, exist_ok=True)
        manifest = []
        for i, item in enumerate(self.files):
            path = output / f'{i:02d}.{item.usage}'
            path.write_bytes(item.data)
            manifest.append({'id': i, 'usage': item.usage, 'bytes': len(item.data),
                             'sha256': item.cache_handle[8:].hex(), 'label': item.label})
        (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        return Bundle(self.files, header)


def compile_metadata(project, version):
    return Compiler(project, version).compile()
