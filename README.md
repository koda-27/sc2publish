# sc2publish

A standalone StarCraft II component builder and Battle.net publisher.
Builds MPQ archives and publication metadata from source in Python, and updates
existing Arcade listings headlessly. No StarCraft II installation, Wine,
Blizzard editor, game cache or previous publication files are required.

## Install

Extract the sharing ZIP (or clone this repository), then:

```sh
uv sync --locked
uv run sc2publish --help
```

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).
The first sync downloads the dependencies pinned in `uv.lock`.
`uv tool install .` installs the CLI globally; `pip install .` also works.

## Authentication: just your Blizzard credentials

```sh
uv run sc2publish auth
```

Enter your **Blizzard email and password** (password input is hidden).
The tool authenticates, discovers your SC2 account region and character from
Battle.net, and caches a renewable session token. No token extraction, toon name,
realm number or game-account settings are required. If Blizzard requests MFA,
the command prompts for the code.

Use `auth --remember` to save the password in the **OS keyring** for automatic
re-login after a token expires. Without it, only the token is saved. Ordinary
`push` and `status --remote` reuse/renew that token and handle a new login
challenge when necessary. `login` is an alias of `auth`.

**Authentication data defaults outside the repository.** The token cache is
`~/.config/sc2publish/.env` on Linux (the platform user configuration directory
on Windows/macOS). Passwords are never copied into this cache. Existing explicitly
configured `.env` files are supported; `--env` or `SC2PUBLISH_ENV` selects one.

For automation, supply an external file containing email on line one and
password on line two, or set `SC2PUBLISH_EMAIL` and `SC2PUBLISH_PASSWORD` through
your CI secret store:

```sh
uv run sc2publish --credentials /path/to/private/credentials.txt -p my-project push
```

No separate auth command is needed in that case. See
[authentication details](docs/headless-login.md) for storage, renewal and MFA.

## Set up your map

1. Copy [examples/project.toml](examples/project.toml) to your map project's
   `sc2publish.toml`. Set the map name, region and component layers. Paths are
   relative to that manifest. The existing listing's ID and namespace are
   discovered from your account; optionally pin them to assert the destination.
2. Declare your published dependency product IDs in `extra_dependencies`.
   These are map product IDs, not the namespaces in `DocumentInfo`. The built-in
   Teams 15 lobby dependency is inferred from the source attributes.
3. Build/check offline, then publish with your Blizzard credentials:

```sh
uv run sc2publish -p /path/to/sc2publish.toml push --dry-run
uv run sc2publish --credentials /path/to/private/credentials.txt -p /path/to/sc2publish.toml push
uv run sc2publish -p /path/to/sc2publish.toml status --remote
```

`build`, `check`, `doctor`, local `status` and `push --dry-run` do not log in.
Online commands ask for Blizzard credentials if no usable token or configured
credentials exist. An external credentials file or saved keyring password makes
renewal unattended unless Blizzard requires MFA.

Publication always compiles lobby attributes, variants, localized text, images,
score results and Arcade information from your components. Set `[publish].notes`
to a UTF-8 bullet file to generate release notes for the reserved revision.
Your map sources are inputs, not bundled assets. Publishing currently updates
**existing listings**; it does not create new listings or publish dependency mods.

Source compilation was verified by publishing Star Battle Classic EU **1.42**
without a metadata template or map-specific result-handle table. See
[publishing details](docs/publishing.md) for source requirements and migration.

## Development and sharing

```sh
uv sync --locked --extra dev
uv run --locked --extra dev pytest
uv build
uv run --locked --extra dev twine check dist/*.whl dist/*.tar.gz
uv run --locked python scripts/share.py --output dist/sc2publish-share.zip
```

Tests use synthetic credentials and simulated HTTP, Front and native exchanges.
Real account settings, OS credentials and network access are isolated in tests.
The schema, stock catalog names and server's public verification key are bundled
resources; no sibling checkout is needed. Stock-name regeneration is optional:
`scripts/build_stock_blob.py --help` documents its explicit install inputs.

The sharing ZIP uses the reviewed `SHARE_FILES.txt` allowlist and includes a
SHA-256 checksum. Git history, virtualenvs, credentials, live `.env` files, logs,
publication artifacts and project-specific caches are excluded. `.env.example`
contains blank placeholders only.

Protocol and cryptography references: [THIRD_PARTY.md](THIRD_PARTY.md).
