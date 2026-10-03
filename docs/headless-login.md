# Blizzard authentication

## Normal use

```sh
uv run sc2publish auth
```

Supply your Blizzard email and password. `auth` signs in immediately and checks
the complete SC2 connection, including account-region discovery and character
selection. `login` is the same command, not a separate token-generation script.
`auth --dry-run` checks inputs offline and writes nothing.

Use `-p my-project auth` to inherit your map's region and locale. Without a
project, auth uses the saved region or EU; `auth --region US` selects a different
publish destination. You never need to supply a toon name, realm, game-account
region or web credential. The destination region is a project setting, while
the SC2 account region is read from the server's native handoff.

## Storage

- **Session token:** cached and rotated in the platform's user configuration
  directory (`~/.config/sc2publish/.env` on Linux). New caches default outside
  the project, even when `-p` selects a project. Unix files use mode `600`.
- **Password:** used in memory; not written to the token cache. `auth --remember`
  stores it in the OS keyring. An unavailable keyring produces a clear error
  after login; there is no plaintext password fallback.
- **Email:** saved with the token, binding it to the account that obtained it.
  Credentials for a different email do not reuse that account's token.
- **Automation:** use your CI's secret environment or an external credentials
  file. Neither needs to live in the source tree.

`SC2PUBLISH_CONFIG_HOME` overrides the user configuration directory. For legacy
setups, an existing `.env` beside the project manifest or in the working directory
is read before the default cache. `--env PATH` / `SC2PUBLISH_ENV` explicitly selects
the file for both login and publishing; it never falls back to another account's
file if that selected path does not exist.

Existing manually imported `SC2PUBLISH_WEB_CREDENTIAL` values still work. New
users do not have to create or manage them. Normal auth does not write a second
binary-token file or HTTP-response diagnostics.

## Unattended use

Provide an external credentials file (email on the first line, password on the
second; JSON and labeled `email=...` / `password=...` formats also work):

```sh
uv run sc2publish --credentials /path/to/private/credentials.txt -p my-project push
```

Alternatively provide `SC2PUBLISH_EMAIL` and `SC2PUBLISH_PASSWORD` via the process
environment. `SC2PUBLISH_CREDENTIALS_FILE` selects an external file without a CLI
option. A `credentials.txt` file in the user configuration directory is also
recognized. Passwords are not accepted as command-line arguments.

The credential-source order is an explicit file, configured/default external
file, environment/.env email and password, OS keyring, then an interactive
prompt. `--credentials -` reads email/password from stdin. A noninteractive run
without credentials stops with instructions instead of hanging on a prompt.

For a one-time login using the same file:

```sh
uv run sc2publish auth --credentials /path/to/private/credentials.txt
```

## Renewal and MFA

All online commands use one account-session implementation:

1. Reuse a cached token for the same identity and region, when present.
2. Open the publishing Front connection and send its logon request.
3. On a server challenge, run the login page's SRP6a exchange using your Blizzard
   credentials and submit the resulting token on that same Front connection.
4. Replace the accepted token with the renewed credential issued by Battle.net.
5. Decode the SC2 account region from the native logon handoff, receive the
   character list, select the previously configured character if available or
   the server's first character, and verify the selection response.

An expired token follows step 3 automatically. A valid token does not require a
password login. Failed password logins are not retried automatically. An account
with no SC2 character in the destination region produces a specific error.

MFA codes are requested through a hidden prompt when Blizzard asks for them.
Noninteractive MFA stops with an instruction to run `sc2publish auth` on a
terminal. Form parsing is best-effort and device-proof JavaScript is not run;
Blizzard can change its login flow. Avoid concurrent game sessions when testing
authentication.

## Offline verification

The SRP implementation has synthetic reference vectors for both password
versions, including signed two's-complement version-2 PBKDF2 interpretation.
The HTTP tests simulate SRP and MFA. Publishing-connection tests cover first
login, cached-token reuse, expired-token renewal, rejected login without retry,
native account-region decoding, and fragmented character-list responses.
The test suite blocks real network connections and isolates OS/user credentials.

```sh
uv run --locked --extra dev pytest
```

The underlying SRP login was verified against EU in September 2026. The unified
publishing flow is tested offline; a live end-to-end account check is a separate
step and is not performed by the test suite.
