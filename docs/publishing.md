# Source-only publishing

`sc2publish push` builds the map, compiles its publication files, authenticates
with Blizzard credentials, resolves your existing listing, reserves a revision,
uploads the files and verifies the live version and visibility. Success is
recorded only after that verification.

## Inputs

- Component directories in `[build].layers`, with the map last. A single
  already-consolidated component directory is also supported.
- `DocumentInfo`, binary `MapInfo`, Galaxy source including `MapScript.galaxy`,
  catalog XML and any images referenced by the publication. `Attributes` and
  `<locale>.SC2Data/LocalizedData/*.txt` supply lobby definitions and text.
- Published dependency product IDs in `[project].extra_dependencies`. These
  declare the dependencies whose lobby/score definitions the header needs;
  archive dependencies remain in `DocumentInfo` / `DocumentHeader` (or the
  `[build].dependencies` override). Product IDs and attribute namespaces differ.
  The Teams 15 product is automatically included when attribute `999/2018` is used.
- Blizzard email/password via `--credentials FILE`, environment variables or
  `auth`. Character/account details are discovered automatically. MFA challenges
  are reported explicitly when unattended authentication cannot proceed.

No editor executable, game installation, captured packets or prior publication
files are read. Stock catalog names and the protocol schema ship with the tool.
The generated `build/publication/` files are outputs and can be deleted/rebuilt.

`push --dry-run` performs compilation and wire-format checks offline. It neither
authenticates nor reserves a revision. With no pinned destination IDs it uses
placeholder IDs; a live push resolves the identity before its final compilation.

## Arcade notes

Set `[publish].notes = "publish-notes.txt"` to replace the `DocumentInfo` patch
notes with source-controlled bullets. Blank lines and `#` comments are ignored.
Up to 100 bullets of 200 characters each are supported. The section title uses
the server-reserved version and the date uses the publisher's local date.
Changes to this file invalidate the unchanged-source shortcut.

Without this setting, patch notes are compiled from `DocumentInfo` and localized
source strings. Lobby edits, result additions/removals and description changes
are also compiled anew on every publication.

## Migrating older manifests

Remove the old `[publish].metadata`, `baseline` and `cache` settings, and remove
`--metadata ...` from commands. There is no snapshot command or alternate
metadata mode. Old snapshot directories can be archived or deleted.

Make sure `extra_dependencies` contains the published dependencies your map
requires. An incomplete declaration may fail as **3249 (default attribute
values)** even though the offending defaults come from a missing dependency.

## Live verification

Star Battle Classic, EU map 268504, was verified at **1.42** with:

- a fresh credentials-only login and automatic SC2 character discovery;
- all listing metadata compiled from component sources;
- no saved result-handle overrides or publication templates;
- automatic map/namespace discovery and the inferred Teams 15 dependency;
- Arcade notes generated from the source bullet file for revision 1.42;
- successful header upload and public-revision confirmation.

Verified header SHA-256:
`a96b62faba904f592eb78024cd91fe4a63589d7dfdd413fdbce86c819cfd3bdb`.

The former **3244** failure was a corrupt-localization-file error. The compiler
now emits the server-compatible escaped XML/CRLF serialization. The independent
dependency declaration issue was diagnosed using Battle.net's public error
catalog; supplying the required dependency products allowed source-only headers.
