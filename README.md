# sc2publish

[![CI](https://github.com/koda-27/sc2publish/actions/workflows/ci.yml/badge.svg)](https://github.com/koda-27/sc2publish/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![MIT License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Build and publish StarCraft II maps from the command line.**

sc2publish turns map component sources into an MPQ archive, compiles the Arcade
listing and lobby metadata, and publishes directly to Battle.net. Sign in with
your Blizzard email and password; account, character and listing IDs are
discovered automatically.

The entire workflow is headless. It requires no StarCraft II installation,
editor, Wine, browser, game cache or saved publication template.

## Features

- Merge component layers, validate XML and Galaxy includes, and build MPQ archives.
- Compile lobby attributes, game variants, localized strings, images and score results from source.
- Generate Arcade patch notes from a plain-text file.
- Authenticate with Blizzard credentials and renew cached sessions.
- Upload changed files with progress reporting and verify the published revision.
- Validate a release offline with `push --dry-run`.

**Scope:** updates existing map listings. Creating new listings and publishing
dependency mods are not supported yet. Region settings support EU, US and KR;
live end-to-end publishing has been verified on EU. CI runs on Linux, macOS and Windows.

## Installation

Requires **Python 3.11+** and **Git**. Install with
[uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv tool install git+https://github.com/koda-27/sc2publish.git
sc2publish --help
```

Or install with pip in a virtual environment:

```sh
python -m pip install "git+https://github.com/koda-27/sc2publish.git"
```

Python dependencies, the protocol schema and stock catalog names are installed
with the package. Your map sources and assets are supplied separately.

## Quick start

### 1. Configure your map

Create `sc2publish.toml` in your map project:

```toml
[project]
name = "My Map" # Must match your existing Battle.net listing.
region = "EU"
locale = "enUS"
extra_dependencies = [] # Published dependency product IDs, if required.

[build]
layers = ["Maps/MyMap.SC2Map"]
output = "build"

[publish]
# notes = "publish-notes.txt"
```

`layers` contains **component directories**, not packed map archives. List mods
in merge order and the map last. Paths are relative to the manifest. Sources
must include `DocumentInfo`, `MapInfo` and Galaxy code including `MapScript.galaxy`.

Declare required published dependencies by **product ID**, not attribute
namespace. The Teams 15 lobby dependency is inferred automatically when used.
See the [example manifest](examples/project.toml) and
[source and dependency requirements](docs/publishing.md#inputs).

### 2. Validate offline

From your map project directory:

```sh
sc2publish push --dry-run
```

This builds the archive and publication metadata without logging in or reserving
a Battle.net revision.

### 3. Sign in and publish

```sh
sc2publish auth
sc2publish push
```

Authentication prompts for your Blizzard email and a hidden password. The
publisher resolves your existing listing, reserves the next revision, uploads
the files and confirms the live version before reporting success.

Session tokens are stored in your platform's user configuration directory
(`~/.config/sc2publish/.env` on Linux). Passwords are not stored in that file.
Use `sc2publish auth --remember` to save your password in the OS keyring.

## Automation

Supply `SC2PUBLISH_EMAIL` and `SC2PUBLISH_PASSWORD` through your CI secret store,
or use an external credentials file with email on the first line and password
on the second:

```sh
sc2publish --credentials /path/to/private/credentials.txt -p /path/to/sc2publish.toml push
```

No separate authentication command is needed. Login and renewal are unattended
unless Blizzard requests MFA or another interactive challenge. Credentials and
session tokens should stay outside version control.

## Commands

| Command | Purpose | Network |
| --- | --- | --- |
| `init <directory> --name "My Map"` | Create a project manifest | Offline |
| `build` | Build the archive and publication files | Offline |
| `check` | Validate sources and metadata after a build | Offline |
| `doctor` | Inspect local configuration | Offline |
| `push --dry-run` | Build and validate a release | Offline |
| `auth` | Sign in; `login` is an alias | Battle.net |
| `push` | Build, publish and verify; skip unchanged sources | Battle.net when publishing |
| `status` | Show the last recorded publication | Offline |
| `status --remote` | Query the live listing | Battle.net |

Use `-p <manifest>` before a command to select another project, `--json` for
structured output, or `push --force` to publish unchanged sources again.
Run `sc2publish <command> --help` for command-specific options.

## Documentation

- [Authentication, token storage, renewal and MFA](docs/headless-login.md)
- [Publishing, dependencies, release notes and migration](docs/publishing.md)
- [Project configuration example](examples/project.toml)
- [Protocol references and third-party attribution](THIRD_PARTY.md)

## Development

```sh
git clone https://github.com/koda-27/sc2publish.git
cd sc2publish
uv sync --locked --extra dev
uv run --locked --extra dev pytest
uv run sc2publish --help
uv build
```

Tests use synthetic credentials and simulated HTTP and native protocol
exchanges. They isolate real account settings and block network connections.
Live verification is documented in the [publishing guide](docs/publishing.md#live-verification).

Bug reports and pull requests are welcome. Include the command, platform and
redacted error output when [reporting an issue](https://github.com/koda-27/sc2publish/issues).

## License

[MIT](LICENSE). See [THIRD_PARTY.md](THIRD_PARTY.md) for attribution.

StarCraft II and Battle.net are trademarks of Blizzard Entertainment.
This is an independent community project.
