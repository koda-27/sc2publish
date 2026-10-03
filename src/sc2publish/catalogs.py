"""Stock catalog data, dependency merging and result-definition selection.

The shipped blob (``data/stock_catalogs.json.gz``) holds ordered entry names
per catalog type for every stock mod and campaign that ships GameData. The
metadata compiler uses it to reproduce the editor's dependency-merged catalog
positions, which decide whether a local unit or upgrade is new to a map and
which handle index a result definition receives.
"""
from __future__ import annotations

import gzip
import json
import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

BLOB = Path(__file__).resolve().parent / 'data/stock_catalogs.json.gz'
ENTRY = re.compile(r'<C([A-Za-z0-9_]+)\b[^>]*?\bid="([^"]+)"[^>]*?(?:/>|>(.*?)</C\1>)', re.S)
PARENT = re.compile(r'\bparent="([^"]+)"')
SCORE = re.compile(r'<ScoreResult\b[^>]*\bvalue="([^"]*)"')
ICON = re.compile(r'<Icon\b[^>]*\bvalue="([^"]*)"')
RESULT_TAGS = {'Unit': 1, 'Upgrade': 2}


@lru_cache(maxsize=1)
def stock():
    """Ordered per-package catalog entries: package -> type -> [name, ...]."""
    with gzip.open(BLOB, 'rt', encoding='utf-8') as handle:
        return json.load(handle)


def package_key(reference):
    """Normalize a DocumentInfo dependency value to a stock package key."""
    value = reference.split('file:', 1)[-1].split(',', 1)[0]
    return value.replace('/', '\\').strip().lower()


def local_directory(key, layers):
    """The component layer whose path ends with this dependency reference."""
    for layer in layers:
        if Path(layer).as_posix().lower().endswith(key.replace('\\', '/')):
            return Path(layer)
    return None


def document_dependencies(directory):
    path = Path(directory) / 'DocumentInfo'
    if not path.is_file():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []
    dependencies = root.find('Dependencies')
    return [node.text or '' for node in dependencies.findall('Value')] if dependencies is not None else []


def resolve(references, layers=()):
    """Editor-order sources for a dependency list: stock leaves and local layers."""
    sources, seen = [], set()

    def visit(reference):
        key = package_key(reference)
        if not key or key in seen:
            return
        seen.add(key)
        directory = local_directory(key, layers)
        if directory is not None:
            for nested in document_dependencies(directory):
                visit(nested)
            sources.append(('local', directory))
        else:
            sources.append(('stock', key))

    for reference in references:
        visit(reference)
    return sources


def catalog_files(directory):
    """Catalog files of a component in the editor's load order."""
    root = Path(directory) / 'Base.SC2Data'
    files = []
    data = root / 'GameData'
    if data.is_dir():
        files.extend(sorted((path for path in data.iterdir()
                             if path.is_file() and path.name.lower().endswith('data.xml')),
                            key=lambda path: path.name.lower()))
    manifest = root / 'GameData.xml'
    if manifest.is_file():
        for entry in ET.parse(manifest).getroot():
            relative = entry.get('path')
            if relative:
                files.append(root / relative.replace('\\', '/'))
    return files


def definitions(directory):
    """Ordered Unit/Upgrade definitions of a component.

    Returns ``{'Unit': OrderedDict(name -> {'parent', 'score', 'icon'}), ...}``
    where later files override earlier fields without changing the entry
    position. ScoreResult and Icon are resolved through local parent chains.
    """
    found = {'Unit': OrderedDict(), 'Upgrade': OrderedDict()}
    for path in catalog_files(directory):
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        for match in ENTRY.finditer(text):
            tag, name = match.group(1), match.group(2)
            if tag not in found:
                continue
            entry = found[tag].setdefault(name, {'parent': None, 'score': None, 'icon': None})
            parent = PARENT.search(match.group(0))
            if parent:
                entry['parent'] = parent.group(1)
            score = SCORE.search(match.group(3) or '')
            if score:
                entry['score'] = bool(score.group(1))
            icon = ICON.search(match.group(3) or '')
            if icon:
                entry['icon'] = icon.group(1)
    for entries in found.values():
        for name, entry in entries.items():
            seen = {name}
            parent = entry['parent']
            while parent and parent not in seen:
                seen.add(parent)
                inherited = entries.get(parent)
                if inherited is None:
                    break
                if entry['score'] is None and inherited['score'] is not None:
                    entry['score'] = inherited['score']
                if entry['icon'] is None and inherited['icon'] is not None:
                    entry['icon'] = inherited['icon']
                if entry['score'] is not None and entry['icon'] is not None:
                    break
                parent = inherited['parent']
    return found


@dataclass
class Catalog:
    """Dependency-merged catalog positions for one map or mod."""

    positions: dict = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    stock: dict = field(default_factory=dict)

    def add(self, tag, name, stock=False):
        table = self.positions.setdefault(tag, {})
        if name not in table:
            table[name] = self.totals.get(tag, 0)
            self.totals[tag] = table[name] + 1
        if stock:
            self.stock.setdefault(tag, set()).add(name)

    def is_stock(self, tag, name):
        return name in self.stock.get(tag, ())

    def result_entries(self, directory):
        """Local Unit/Upgrade entries that carry a ScoreResult.

        Entries that override an existing stock entry keep that stock position,
        so only mod-only definitions become result definitions. Returns a list
        of ``(name, tag, index, name_chain, icon)`` ordered by type and merged
        index. The chain lists the entry and its local parents so callers can
        resolve an inherited display name; the icon is the inherited catalog
        Icon field, if any.
        """
        found = definitions(directory)
        selected = []
        for tag, entries in found.items():
            if tag not in RESULT_TAGS:
                continue
            for name, entry in entries.items():
                if self.is_stock(tag, name) or not entry['score']:
                    continue
                chain, seen, parent = [name], {name}, entry['parent']
                while parent and parent not in seen and parent in entries:
                    chain.append(parent)
                    seen.add(parent)
                    parent = entries[parent]['parent']
                selected.append((name, tag, self.positions[tag][name], chain, entry['icon']))
        selected.sort(key=lambda item: (RESULT_TAGS[item[1]], item[2]))
        return selected


def merge(references, root=None, layers=()):
    """Merge stock dependency catalogs and local components.

    Stock packages are merged first, in dependency order, then each local
    component. Entries already defined keep their original index, so stock
    overrides do not consume a new position and mod-only entries append after
    the stock portion.
    """
    catalog = Catalog()
    sources = resolve(references, layers)
    if root is not None:
        sources.append(('local', Path(root)))
    for kind, source in sources:
        if kind == 'stock':
            for tag, names in stock().get(source, {}).items():
                for name in names:
                    catalog.add(tag, name, stock=True)
        else:
            for tag, entries in definitions(source).items():
                for name in entries:
                    catalog.add(tag, name)
    return catalog
