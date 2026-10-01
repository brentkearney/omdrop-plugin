# Identity contract

This file is kept byte-identical in `brentkearney/omdrop-awdl` and `brentkearney/omdrop-plugin`. It defines how every Omdrop program chooses the AirDrop identity it presents, so that the driver's programs (sender, `send-to-peer`, Bluetooth advertiser) and the plugin's (receiver, `omdrop`) always agree. `tests/identity-vectors.json`, also byte-identical in both, holds the cases each implementation must pass.

## Identities

| Identity | Where it comes from |
|---|---|
| `cache` | The Apple identity fetched from 1Password, held in the kernel keyring (below). |
| `disk` | The Apple identity in `~/.omdrop/keys`: `certificate.pem`, `key.pem`, and optionally `validation_record.cms`. |
| `self-signed` | `~/.omdrop/keys/certificate.self-signed.pem` and `key.self-signed.pem`. |

A validation record is presented only with the certificate it belongs to: with `cache`, the payload's record; with `disk`, `validation_record.cms` if it exists. `self-signed` never presents a record.

## Settings

`${XDG_CONFIG_HOME:-$HOME/.config}/omdrop/settings`, lines of `key=value`; when a key appears more than once, the last line wins. Lines that don't contain `=` are ignored.

`identity_source` is one of `1password`, `disk`, or `self-signed`. If it's absent, it means `disk`. Any other value is treated as `self-signed`, with the warning `unknown-mode`.

## Cache

- Keyring: the invoking user's user keyring (`@u`).
- Key: type `user`, description `omdrop:identity`, permissions `0x3f010000` (possessor all, user view).
- Payload:

  ```text
  OMDROP-IDENTITY 1\n
  {"certificate":C,"fetch_id":"F","fetched_at":T0,"hard_expiry":T2,"key":K,"period_ends":T1,"record":R}\n
  <C bytes of certificate><K bytes of key><R bytes of record>
  ```

  - Line 1 is exactly `OMDROP-IDENTITY 1`. Any other first line is malformed; a different number is an unsupported version, also malformed.
  - Line 2 is a JSON object with exactly these seven keys. `fetch_id` is 32 lowercase hex digits. The three times are integer Unix seconds with `T0 <= T1 <= T2` and `T2 - T0 <= 86400`; JSON booleans aren't integers. `C`, `K`, and `R` are integer byte counts, each from 1 to 16384.
  - The bytes after line 2 total exactly `C + K + R`. Trailing bytes are malformed, as are missing ones.
  - The whole payload is at most 32767 bytes, the kernel's limit for a `user` key.
  - Anything else is malformed.
- The certificate and key are PEM; the record is DER. Consumers don't parse or verify them; `omdrop` checks them before publishing.
- "Now" is the wall clock, `time.time()` truncated to whole seconds. A cache whose `hard_expiry` is at or before now is expired and never used.

### Publishing (`omdrop` only)

1. Add the key to the publishing process's own keyring (`@p`).
2. Set its timeout.
3. Set its permissions.
4. Link it into `@u`, which replaces any key with the same type and description.
5. Unlink it from `@p`.

If any step fails, revoke the key, report the failure, and use `self-signed`.

## Window file

`$XDG_RUNTIME_DIR/omdrop/window`, mode `0600`, `key=value` lines. Written only by `omdrop on`, removed only by `omdrop off`.

| Key | Required when | Value |
|---|---|---|
| `source` | always | `1password`, `disk`, or `self-signed` |
| `fetch_id` | `source=1password` | the cache's `fetch_id` |
| `hard_expiry` | `source=1password` | the cache's `hard_expiry` |
| `cache_serial` | optional | the cache key's serial number, in decimal |

A file that is present but missing a required key, has an unknown `source`, or has a malformed value is unparseable. Other keys are ignored; parsing a window, and the vectors, never include `cache_serial`.

`cache_serial` exists for sandboxed readers. A systemd user service with mount-namespace hardening (`ProtectHome=`, `ProtectSystem=`, `PrivateTmp=` and similar) runs in its own user namespace, where `@u` is a different, empty user keyring, so searching for `omdrop:identity` finds nothing. The key is still readable by serial there. A reader that finds nothing by search may read the key with this serial; selection then applies unchanged, including the `fetch_id` match.

## Selection

Inputs: settings, window file (absent, unparseable, or parsed), cache (absent, malformed, or parsed), the state of the disk trio, the state of the self-signed pair, and the current time. The same inputs always give the same result.

### With a window file

| Window | Result |
|---|---|
| Unparseable | error `window-unparseable` |
| `source=1password`, `hard_expiry` at or before now | error `window-expired` |
| `source=1password`, cache absent | error `cache-missing` |
| `source=1password`, cache malformed | error `cache-malformed` |
| `source=1password`, cache `fetch_id` differs | error `cache-mismatch` |
| `source=1password`, cache expired | error `cache-expired` |
| `source=1password`, otherwise | `cache`, with record |
| `source=disk`, certificate or key missing | error `disk-missing` |
| `source=disk`, key doesn't match certificate | error `disk-mismatch` |
| `source=disk`, otherwise | `disk`, with record if `validation_record.cms` exists |
| `source=self-signed` | `self-signed` |

The rows are checked in order and the first match wins. With a window file, the settings are ignored: the window's choice was made when it started, and it holds until the window ends.

### Without a window file

| `identity_source` | Result |
|---|---|
| `1password` | `self-signed`. The cache is used only through a window. |
| `self-signed` | `self-signed` |
| unknown value | `self-signed`, warning `unknown-mode` |
| `disk` or absent, with certificate and key present and matching | `disk`, with record if `validation_record.cms` exists |
| `disk` or absent, with exactly one of certificate and key present | `self-signed`, warning `disk-incomplete` |
| `disk` or absent, with a key that doesn't match its certificate | `self-signed`, warning `disk-mismatch` |
| `disk` or absent, with neither present | `self-signed` |

### The self-signed pair

Whenever the result is `self-signed`:

| Pair state | Result |
|---|---|
| Both files present | use them |
| Neither present | `create` the pair (below), then use it |
| Exactly one present | error `self-signed-half-pair` |

The advertiser and any program running as root report `create` as error `self-signed-missing` instead of creating the pair.

## Creating the self-signed pair

Under `flock` on `~/.omdrop/keys/.identity.lock`:

1. Check the pair's state again. If both files now exist, use them. If exactly one exists, fail with `self-signed-half-pair`.
2. Make `~/.omdrop/keys` (mode `0700`) if it's missing, and a temporary directory inside it.
3. Run `openssl req -newkey rsa:2048 -nodes -x509 -days 365 -subj /CN=<computer name> -keyout key.pem -out certificate.pem` in the temporary directory. Set both files to mode `0600`.
4. `link()` the key to `key.self-signed.pem`, then the certificate to `certificate.self-signed.pem`. If either link fails because the name exists, remove any link this run made, and use the pair already there.
5. Remove the temporary directory.

Nothing creates, replaces, or renames `certificate.pem`, `key.pem`, or `validation_record.cms`, except `omdrop identity 1password import`, which deletes them.

## An explicit keys directory

`airdrop-send.py --keys DIR` and the receiver's `--keys DIR` replace `~/.omdrop` for the disk trio and the self-signed pair only: they're read from `DIR/keys/`, and the self-signed pair is created there, under `DIR/keys/.identity.lock`. Settings, the window file, and the cache are unaffected, and selection is unchanged.

## Validation

Consumers check only what selection needs: that files exist, and that a disk key matches its certificate. They don't verify certificate chains, CMS signatures, or account IDs. `omdrop` does that when it imports an identity, adopts one with `use`, fetches one, or opens a window with the disk identity.

## Running as root

A program running as UID 0 acts for the user named by `PKEXEC_UID`, then `SUDO_UID`; if neither is set, it acts for root itself. For that user:

- Home is the password-database entry, not `$HOME`.
- Settings are `<home>/.config/omdrop/settings`.
- The runtime directory is `/run/user/<uid>`.
- The keyring is that user's user keyring. Root reads the cache directly if it can. If the read is refused, it reads it from a child process that has dropped to that user's UID and GID. If that fails too, the advertiser reports error `cache-unreadable-as-root`; it never substitutes zero hashes for a window whose source is `1password`.

A program running as root only reads, and never creates the self-signed pair.

## Debug dumps

- By default, no dumps are written.
- With `OMDROP_DEBUG=1`, a program writes each Discover and Ask request and response to `$XDG_RUNTIME_DIR/omdrop/debug/<name>.plist`, mode `0600`, in a `0700` directory, by renaming a temporary file into place. In each dictionary, at any depth, a value whose key ends in `RecordData` or `Certificate` is replaced by the string `<redacted N bytes>`, where `N` is the value's length in bytes. A body that isn't a property list is written as `<opaque N bytes>` instead.
- `airdrop-send.py --verbose` and `omdrop send --verbose` set `OMDROP_DEBUG=1`.
- With `OMDROP_DEBUG_SENSITIVE=1`, the same dumps are written without redaction. It implies `OMDROP_DEBUG=1`.

## Test vectors

`tests/identity-vectors.json` has four groups. Every implementation must pass all of them.

### `selection`

Each vector has:

- `name`
- `input`:
  - `settings`: an object of already-parsed settings.
  - `window`: `null`, the string `"unparseable"`, or a parsed window.
  - `cache`: `null`, the string `"malformed"`, or an object with `fetch_id` and `hard_expiry`.
  - `disk`: an object with booleans `certificate`, `key`, `match`, and `record`.
  - `self_signed`: an object with booleans `certificate` and `key`.
  - `root`: a boolean.
  - `now`: an integer.
- `expect`: either an object with `identity`, `record` (boolean), `create` (boolean), and `warnings` (a sorted list), or an object with `error`.

`disk.match` is meaningful only when both `certificate` and `key` are true.

### `payloads`

`payload_b64` is a cache payload, base64-encoded for the file. `expect` is either `"malformed"` or the parsed fields: `fetch_id`, `fetched_at`, `period_ends`, `hard_expiry`, and `certificate_b64`, `key_b64`, `record_b64`.

### `window_files`

`text` is a window file's contents. `expect` is either `"unparseable"` or the parsed window, which includes only the keys the window's `source` requires, with `hard_expiry` as an integer.

### `settings_files`

`text` is a settings file's contents; `expect` is the parsed object.
