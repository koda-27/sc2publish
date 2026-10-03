"""Component compilation, ordered mod merging and portable MPQ packaging."""
import hashlib
import json
import re
import shutil
import struct
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path

from tqdm import tqdm

from . import __version__
from .errors import PublishError
from .mpq import pack


def write_xml(path, root):
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space='    ')
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)


def strings(path):
    result = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                result[key] = value
    return result


def fingerprint(project):
    digest = hashlib.sha256(__version__.encode())
    # Invalidate states from older compilers and include optional publication
    # inputs even when no [publish] table was supplied.
    digest.update(b'publication-format-2')
    digest.update(json.dumps(project.data | {'publish': project.publish}, sort_keys=True).encode())
    if project.publish.get('notes'):
        digest.update(project.resolve(project.publish['notes']).read_bytes())
    for index, layer in enumerate(project.layers):
        digest.update(str(index).encode())
        for path in sorted(layer.rglob('*')):
            relative = path.relative_to(layer)
            if path.is_file() and 'Local' not in relative.parts:
                digest.update(relative.as_posix().encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()


def consolidate(project):
    destination = project.component_dir
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    includes, layouts = ET.Element('Includes'), ET.Element('Desc')
    seen_includes, seen_layouts = set(), set()
    for layer_index, layer in enumerate(project.layers):
        is_map = layer_index == len(project.layers) - 1
        for path in sorted(layer.rglob('*')):
            if not path.is_file():
                continue
            relative = path.relative_to(layer)
            name = relative.as_posix()
            if 'Local' in relative.parts:
                continue
            if len(relative.parts) == 1 and not is_map and path.suffix.lower() != '.galaxy':
                continue
            if name in ('Base.SC2Data/GameData.xml', 'Base.SC2Data/UI/Layout/DescIndex.SC2Layout'):
                root, seen = (includes, seen_includes) if name.endswith('GameData.xml') else (layouts, seen_layouts)
                for entry in ET.parse(path).getroot():
                    marker = ET.tostring(entry)
                    if marker not in seen:
                        root.append(deepcopy(entry))
                        seen.add(marker)
                continue
            if is_map and name.startswith('Base.SC2Data/GameData/') and path.suffix == '.xml' and len(project.layers) > 1:
                relative = Path('Base.SC2Data/GameData/MapOverrides') / relative.relative_to('Base.SC2Data/GameData')
                ET.SubElement(includes, 'Catalog', path=relative.relative_to('Base.SC2Data').as_posix())
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if 'LocalizedData' in relative.parts and path.suffix == '.txt':
                merged = strings(target) | strings(path)
                target.write_text(''.join(f'{key}={value}\n' for key, value in merged.items()))
            elif target.exists() and path.suffix == '.xml' and 'GameData' in relative.parts:
                old, new = ET.parse(target).getroot(), ET.parse(path).getroot()
                if old.tag != 'Catalog' or new.tag != 'Catalog':
                    raise PublishError(f'Cannot merge XML collision: {relative}')
                old.extend(deepcopy(list(new)))
                write_xml(target, old)
            else:
                shutil.copyfile(path, target)
    write_xml(destination / 'Base.SC2Data/GameData.xml', includes)
    write_xml(destination / 'Base.SC2Data/UI/Layout/DescIndex.SC2Layout', layouts)
    info = ET.parse(destination / 'DocumentInfo').getroot()
    if 'dependencies' in project.build:
        dependencies = info.find('Dependencies')
        if dependencies is None:
            dependencies = ET.SubElement(info, 'Dependencies')
        dependencies.clear()
        for value in project.build['dependencies']:
            ET.SubElement(dependencies, 'Value').text = value
        write_xml(destination / 'DocumentInfo', info)
    for relative, content in project.build.get('generated_files', {}).items():
        target = (destination / relative).resolve()
        if destination.resolve() not in target.parents:
            raise PublishError('Generated files must remain inside the map components.')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    normalize_document_header(destination, project)
    bake_overlays(destination)
    for root in (includes, layouts):
        for entry in root:
            if 'path' in entry.attrib and not (destination / 'Base.SC2Data' / entry.attrib['path'].replace('\\', '/')).is_file():
                raise PublishError(f'Missing catalog/layout include: {entry.attrib["path"]}')
    return destination


ARRAY_ELEMENTS = {'LayoutButtons', 'On'}


def _merge_element(target, overlay):
    """Apply one overlay element onto a catalog entry, SC2 overlay style."""
    for attribute, value in overlay.attrib.items():
        # index selects an array slot; it is not data to copy onto the target.
        if attribute == 'index':
            continue
        target.set(attribute, value)
    for child in list(overlay):
        index = child.get('index', None)
        if index is not None:
            indexed = [existing for existing in target if existing.tag == child.tag and
                       existing.get('index') is not None]
            if indexed:
                matches = [existing for existing in indexed if existing.get('index') == index]
                if matches:
                    _merge_element(matches[0], child)
                else:
                    target.append(deepcopy(child))
            else:
                siblings = [existing for existing in target if existing.tag == child.tag]
                position = int(index) if index.lstrip('-').isdigit() else None
                if position is not None and 0 <= position < len(siblings):
                    _merge_element(siblings[position], child)
                else:
                    target.append(deepcopy(child))
            continue
        if child.tag.endswith('Array') or child.tag in ARRAY_ELEMENTS:
            target.append(deepcopy(child))
            continue
        matches = [existing for existing in target if existing.tag == child.tag]
        if len(matches) == 1:
            _merge_element(matches[0], child)
        else:
            target.append(deepcopy(child))


def bake_overlays(destination):
    """Fold map/mod overlays into the files that define their entries.

    Flattening the components into one document removes layer priority, so a
    later overlay file would be ignored at runtime. Baking each overlay field
    into every existing definition keeps it effective regardless of order.
    """
    data = destination / 'Base.SC2Data/GameData'
    if not data.exists():
        return 0
    overlays = [data / 'SB/Classic.xml', *sorted((data / 'MapOverrides').glob('*.xml'))]
    overlays = [path for path in overlays if path.is_file()]
    if not overlays:
        return 0
    targets = []
    for path in sorted(data.rglob('*.xml')):
        if path in overlays:
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        if root.tag == 'Catalog':
            targets.append((path, root))
    index = {}
    for path, root in targets:
        for entry in root:
            if entry.get('id'):
                index.setdefault((entry.tag, entry.get('id')), []).append(entry)
    baked = 0
    for overlay_path in overlays:
        root = ET.parse(overlay_path).getroot()
        kept = []
        for entry in list(root):
            matches = index.get((entry.tag, entry.get('id')), []) if entry.get('id') else []
            if not matches:
                kept.append(entry)
                continue
            for target in matches:
                _merge_element(target, entry)
            baked += 1
        for entry in list(root):
            root.remove(entry)
        for entry in kept:
            root.append(entry)
        write_xml(overlay_path, root)
    for path, root in targets:
        write_xml(path, root)
    return baked


def normalize_document_header(destination, project):
    """Replace the binary dependency list with the project's dependencies.

    The engine resolves the runtime dependency list from DocumentHeader. Stale
    local mod references load the flattened catalogs a second time; naming only
    the configured dependencies (stock Blizzard mods) keeps every effect
    applied exactly once.
    """
    dependencies = project.build.get('dependencies')
    path = destination / 'DocumentHeader'
    if not dependencies or not path.exists():
        return 0
    data = path.read_bytes()
    marker = data.find(b'bnet:')
    if marker < 4:
        return 0
    count = struct.unpack_from('<I', data, marker - 4)[0]
    position = marker
    for _ in range(count):
        position = data.index(b'\x00', position) + 1
    entries = [value.encode('utf-8') for value in dependencies]
    rebuilt = (data[:marker - 4] + struct.pack('<I', len(entries)) +
               b''.join(entry + b'\x00' for entry in entries) + data[position:])
    if rebuilt == data:
        return 0
    path.write_bytes(rebuilt)
    return len(entries)


def check_sources(project, directory):
    """Validate XML and Galaxy include graphs without launching a game runtime."""
    xml_count = 0
    for path in directory.rglob('*.xml'):
        try:
            ET.parse(path)
        except ET.ParseError as exc:
            raise PublishError(f'Invalid XML in {path.relative_to(directory)}: {exc}') from None
        xml_count += 1
    scripts = {p.relative_to(directory).as_posix().casefold(): p for p in directory.rglob('*.galaxy')}
    if 'mapscript.galaxy' not in scripts:
        raise PublishError('MapScript.galaxy is required. Supply Galaxy source or configure build.generated_files.')
    external = set(project.build.get('external_includes', ['TriggerLibs/NativeLib']))
    missing = set()
    edges = {}
    for key, path in scripts.items():
        text = path.read_text(encoding='utf-8-sig')
        text = re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.DOTALL)
        edges[key] = []
        for include in re.findall(r'^\s*include\s+"([^"]+)"', text, flags=re.MULTILINE):
            if include in external or include.startswith('TriggerLibs/'):
                continue
            name = include.replace('\\', '/').removesuffix('.galaxy') + '.galaxy'
            candidates = [name, 'Base.SC2Data/' + name,
                          (path.relative_to(directory).parent / name).as_posix()]
            match = next((x.casefold() for x in candidates if x.casefold() in scripts), None)
            if match:
                edges[key].append(match)
            else:
                missing.add(include)
    if missing:
        raise PublishError('Unresolved Galaxy includes: ' + ', '.join(sorted(missing)))
    visited, visiting = set(), set()

    def visit(key):
        if key in visiting:
            raise PublishError(f'Cyclic Galaxy include: {key}')
        if key in visited:
            return
        visiting.add(key)
        for child in edges[key]:
            visit(child)
        visiting.remove(key)
        visited.add(key)

    visit('mapscript.galaxy')
    return {'xml_files': xml_count, 'galaxy_files': len(scripts), 'reachable_scripts': len(visited)}


def build(project, report=print):
    report(f'Compiling {len(project.layers)} component layer(s)')
    directory = consolidate(project)
    checks = check_sources(project, directory)
    report(f'Validated {checks["xml_files"]} XML files and {checks["galaxy_files"]} Galaxy sources')
    count = sum(1 for p in directory.rglob('*') if p.is_file()) + 1
    with tqdm(total=count, desc='Packing MPQ', unit='file', dynamic_ncols=True, disable=None) as progress:
        result = pack(directory, project.archive, progress.update)
    result.update(checks, source_sha256=fingerprint(project), artifact_sha256=hashlib.sha256(project.archive.read_bytes()).hexdigest())
    report(f'Built {project.archive.name}: {result["bytes"] / 1024**2:.1f} MiB, {result["files"]} files')
    return result
