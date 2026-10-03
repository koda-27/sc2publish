"""Build a source-only sharing ZIP from the reviewed SHARE_FILES.txt allowlist."""

import argparse
import base64
import gzip
import hashlib
import re
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TOKEN = re.compile(rb'\b(?:EU|US|KR)-[A-Za-z0-9_+/=-]{40,}\b')
PRIVATE_KEY = re.compile(rb'-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----')
HOME_PATH = re.compile(rb'(?:/home/|/Users/|[A-Za-z]:\\Users\\)[A-Za-z0-9_.-]+')


def reviewed_files(root):
    """Only explicitly reviewed regular files; no globs or symbolic links."""
    names = (root / 'SHARE_FILES.txt').read_text().splitlines()
    seen = set()
    for name in names:
        if not name or name.startswith('#'):
            continue
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name
                or name in seen or name != relative.as_posix()):
            raise ValueError('Invalid or duplicate path in SHARE_FILES.txt')
        seen.add(name)
        forbidden = {'.git', '.venv', '.private', '__pycache__', 'publish-baseline', 'logs'}
        if any(part in forbidden or part == '.env' or part.startswith('credentials')
               for part in relative.parts):
            raise ValueError(f'Private path in sharing allowlist: {name}')
        path = root / relative
        if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError(f'Missing or symbolic-linked sharing input: {name}')
        data = path.read_bytes()
        content = gzip.decompress(data) if name.endswith('.gz') else data
        if PRIVATE_KEY.search(content) or TOKEN.search(content) or HOME_PATH.search(content):
            raise ValueError(f'Potential private data in sharing input: {name}')
        for encoded in re.findall(rb'(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{60,}={0,2}', content):
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except ValueError:
                continue
            if TOKEN.search(decoded) or PRIVATE_KEY.search(decoded):
                raise ValueError(f'Potential encoded credential in sharing input: {name}')
        yield name, data


def build_zip(root, output):
    inputs = list(reviewed_files(root))
    output = output.resolve()
    if output in {(root / name).resolve() for name, _ in inputs}:
        raise ValueError('Output would overwrite a sharing input')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in inputs:
            info = zipfile.ZipInfo(f'sc2publish/{name}', date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(f'{digest}  {output.name}\n')
    return len(inputs), digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/sc2publish-share.zip')
    args = parser.parse_args()
    try:
        count, digest = build_zip(ROOT, args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'{exc}\n')
    print(f'Created {args.output}: {count} reviewed files; SHA-256 {digest}')


if __name__ == '__main__':
    main()
