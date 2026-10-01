"""The identity commands `omdrop` runs: windows, the 1Password cache, import.

Called as `python3 identity.py <command>`. `bin/omdrop` holds the identity lock
itself around `window`, `stop-begin` and `stop-end`, so that the check that no
`off` has happened and the start of the receiver and radio are one step; every
other command takes the lock here. The only step that ever runs without the
lock is the 1Password fetch, so `off` and `identity lock` can always interrupt
one.

Machine-readable results go to stdout as key=value lines; anything for a person
goes to stderr. Nothing here prints a key, a record, an account identifier or a
hash.
"""
import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time

import identity as ident

FETCH_SECONDS = 60
DEFAULT_HOURS = 12
FILES = ('certificate.pem', 'key.pem', 'validation_record.cms')
# The attachments' names in the 1Password item. Without dots: in an
# `op item create` assignment a dot separates a section from a field, so
# "certificate.pem[file]=" made a section "certificate" holding a file "pem".
OP_NAMES = {'certificate.pem': 'certificate', 'key.pem': 'key',
            'validation_record.cms': 'validation_record'}
DEFAULT_TITLE = 'Omdrop AirDrop identity'


def now():
    return int(time.time())


# ------------------------------------------------------------------ small state

def read_state(user):
    return ident.parse_settings(ident._read(user.state) or '')


def write_state(user, **changes):
    state = read_state(user)
    state.update({k: str(v) for k, v in changes.items() if v is not None})
    for k in [k for k, v in changes.items() if v is None]:
        state.pop(k, None)
    ident._write_private(user.state, ''.join(f'{k}={v}\n' for k, v in sorted(state.items())))


def counter(user, name):
    value = read_state(user).get(name, '0')
    return int(value) if value.isdigit() else 0


def bump(user, name):
    value = counter(user, name) + 1
    write_state(user, **{name: value})
    return value


def settings(user):
    return ident.parse_settings(ident._read(user.settings) or '')


def setting_set(user, **changes):
    """Rewrite settings the way `omdrop` does: drop the key's lines, append one."""
    lines = (ident._read(user.settings) or '').splitlines()
    keep = [line for line in lines if line.split('=', 1)[0] not in changes]
    keep += [f'{k}={v}' for k, v in changes.items()]
    ident._write_private(user.settings, '\n'.join(keep) + '\n')


def mode(user):
    return settings(user).get('identity_source', 'disk')


def cache_hours(user):
    value = settings(user).get('identity_cache_hours', '')
    if value.isdigit() and 1 <= int(value) <= 24:
        return int(value)
    return DEFAULT_HOURS


# The pid of a running `op`, in its own file rather than in `state`: op_run
# records it without the lock, and a read-modify-write of `state` there could
# otherwise overwrite a concurrent bump of `seq` or `gen`.
def _fetch_pid_path(user):
    return os.path.join(user.state_dir, 'fetch.pid')


def kill_fetch(user):
    pid = (ident._read(_fetch_pid_path(user)) or '').strip()
    if pid.isdigit():
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        os.unlink(_fetch_pid_path(user))
    except FileNotFoundError:
        pass


def say(**fields):
    for k, v in fields.items():
        print(f'{k}={v}')


def warn(message):
    print(message, file=sys.stderr)


# ------------------------------------------------------------------ the cache

def current_cache(user):
    """The parsed cache, or None when there is none worth using."""
    try:
        raw = ident.Keyring().read()
    except ident.KeyringError:
        return None
    if raw is None:
        return None
    payload = ident.parse_payload(raw)
    if payload is None or payload['hard_expiry'] <= now():
        return None
    return payload


def usable_for_new_window(payload):
    return payload is not None and payload['period_ends'] > now()


class FetchFailed(Exception):
    pass


def op_binary():
    return os.environ.get('OMDROP_OP') or shutil.which('op')


def op_run(user, args, deadline, input=None):
    """Run the 1Password CLI, recorded so `off` or `identity lock` can kill it.

    In this process's own session, deliberately: 1Password's app integration
    grants an approval per session, so a new session for each call made every
    call prompt again. `op` is a single binary with no children, so killing its
    pid is enough.
    """
    op = op_binary()
    if not op:
        raise FetchFailed('1Password CLI (op) is not installed')
    left = deadline - time.monotonic()
    if left <= 0:
        raise FetchFailed(f'1Password did not answer within {FETCH_SECONDS} s')
    proc = subprocess.Popen([op, *args], stdin=subprocess.PIPE if input else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ident._write_private(_fetch_pid_path(user), f'{proc.pid}\n')
    try:
        out, err = proc.communicate(input=input, timeout=left)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise FetchFailed(f'1Password did not answer within {FETCH_SECONDS} s')
    finally:
        try:
            os.unlink(_fetch_pid_path(user))
        except FileNotFoundError:
            pass
    if proc.returncode < 0:
        raise FetchFailed('the 1Password request was cancelled')
    if proc.returncode != 0:
        import re
        detail = err.decode(errors='replace').strip().splitlines()
        reason = detail[-1] if detail else f'exit {proc.returncode}'
        # op writes "[ERROR] 2026/09/28 23:06:41 message"; keep the message.
        reason = re.sub(r'^\[\w+\]\s+\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\s+', '', reason)[:300]
        raise FetchFailed(f'1Password: {reason}')
    return out


def item_ref(user):
    s = settings(user)
    account, vault, item = (s.get(k, '') for k in
                            ('identity_op_account', 'identity_op_vault', 'identity_op_item'))
    if not (vault and item):
        raise FetchFailed('no 1Password item is configured (run: omdrop identity 1password import)')
    return account, vault, item


def op_read_files(user, account, vault, item, deadline):
    files = {}
    for name in FILES:
        args = ['read', '--no-newline']
        if account:
            args += ['--account', account]
        files[name] = op_run(user, [*args, f'op://{vault}/{item}/{OP_NAMES[name]}'], deadline)
    return files


def fetch(user):
    """Fetch, check and encode the identity. Returns the payload bytes."""
    deadline = time.monotonic() + FETCH_SECONDS
    account, vault, item = item_ref(user)
    files = op_read_files(user, account, vault, item, deadline)
    problem = ident.check_apple(files['certificate.pem'], files['key.pem'],
                                files['validation_record.cms'])
    if problem:
        raise FetchFailed(problem)
    t0 = now()
    t2 = t0 + ident.MAX_LIFETIME
    t1 = min(t0 + cache_hours(user) * 3600, t2)
    return ident.encode_payload(secrets.token_hex(16), t0, t1, t2, files['certificate.pem'],
                                files['key.pem'], files['validation_record.cms'])


def publish_if_current(user, payload, generation):
    """Publish under the lock, only if nothing invalidated this fetch."""
    with ident._Flock(user.lock):
        if counter(user, 'gen') != generation:
            return 'superseded'
        window = ident.read_window(user)
        if isinstance(window, dict) and window['source'] == '1password':
            return 'window-open'          # never replace a live window's cache
        parsed = ident.parse_payload(payload)
        ident.Keyring().publish(payload, parsed['period_ends'] - now())
        return 'published'


def fill_cache(user):
    """Fetch and publish unless a usable cache exists. Returns an outcome and reason."""
    with ident._Flock(user.lock):
        generation = counter(user, 'gen')
    try:
        payload = fetch(user)
    except FetchFailed as e:
        return 'failed', str(e)
    try:
        return publish_if_current(user, payload, generation), ''
    except ident.KeyringError as e:
        return 'failed', str(e)


# ------------------------------------------------------------------ window commands

def cmd_begin(args, user):
    """Before a window starts: the self-signed pair, the rename, and the cache."""
    try:
        ident.create_self_signed(user, args.name)
    except ident.IdentityError as e:
        warn(f'omdrop: the self-signed identity is unusable: {e}')
        return 1
    ident.migrate_legacy(user)
    with ident._Flock(user.lock):
        seq = counter(user, 'seq')
    say(seq=seq)
    if mode(user) != '1password' or usable_for_new_window(current_cache(user)):
        return 0
    if isinstance(ident.read_window(user), dict):
        return 0                         # a repeated `on`: the window keeps its identity
    outcome, reason = fill_cache(user)
    if outcome == 'failed':
        say(notice=f'Using the self-signed identity: {reason}. '
                   "Contacts Only devices won't see or accept this computer.")
    return 0


def cmd_window(args, user):
    """With `omdrop` holding the lock: fix this window's identity, or refuse."""
    if counter(user, 'seq') != args.seq:
        return 3                         # `off` ran meanwhile: start nothing
    existing = ident.read_window(user)
    if isinstance(existing, dict):
        say(source=existing['source'])   # a repeated `on` keeps the window's choice
        if existing['source'] == '1password':
            say(expiry_in=max(0, existing['hard_expiry'] - now()))
        return 0
    m = mode(user)
    lines = []
    if m == '1password':
        payload = current_cache(user)
        if usable_for_new_window(payload):
            # cache_serial is for sandboxed readers such as the receiver,
            # which cannot search the user keyring (see identity.cache_read).
            lines = ['source=1password', f"fetch_id={payload['fetch_id']}",
                     f"hard_expiry={payload['hard_expiry']}",
                     f"cache_serial={ident.Keyring().find()}"]
            ident.Keyring().set_timeout(payload['hard_expiry'] - now())
            say(source='1password', expiry_in=payload['hard_expiry'] - now())
    elif m == 'disk':
        cert, key, disk = ident.disk_state(user)
        if disk['certificate'] and disk['key'] and disk['match']:
            problem = None
            if disk['record']:
                problem = ident.check_apple(cert, key, ident._read(user.path('validation_record.cms'), 'rb'))
            if problem:
                say(notice=f'Using the self-signed identity: {problem}. '
                           "Contacts Only devices won't see or accept this computer.")
            else:
                lines = ['source=disk']
                say(source='disk')
    if not lines:
        lines = ['source=self-signed']
        say(source='self-signed')
    ident._write_private(user.window, '\n'.join(lines) + '\n')
    return 0


def cmd_stop_begin(args, user):
    bump(user, 'seq')
    return 0


def cmd_stop_end(args, user):
    try:
        os.unlink(user.window)
    except FileNotFoundError:
        pass
    payload = current_cache(user)
    if payload is not None:
        try:
            ident.Keyring().set_timeout(payload['period_ends'] - now())
        except ident.KeyringError as e:
            warn(f'omdrop: {e}')
    return 0


# ------------------------------------------------------------------ identity commands

def window_open(user):
    return os.path.exists(user.window)


def cmd_lock(args, user):
    with ident._Flock(user.lock):
        if window_open(user):
            warn('Turn Omdrop off first.')
            return 1
        bump(user, 'gen')
        kill_fetch(user)
        try:
            revoked = ident.Keyring().revoke()
        except ident.KeyringError as e:
            warn(f'omdrop: {e}')
            return 1
    print('Cleared the identity cache.' if revoked else 'The identity cache was already empty.',
          file=sys.stderr)
    return 0


def cmd_unlock(args, user):
    if mode(user) != '1password':
        warn('Omdrop is not set to use 1Password (identity_source is not 1password).')
        return 1
    if current_cache(user) is not None:
        warn('The identity cache is already filled.')
        return 0
    outcome, reason = fill_cache(user)
    if outcome == 'published':
        warn('Fetched the AirDrop identity from 1Password.')
        return 0
    warn({'superseded': 'Cancelled: the identity settings changed meanwhile.',
          'window-open': 'Turn Omdrop off first.'}.get(outcome, f'Could not fetch: {reason}'))
    return 1


def describe_expiry(t):
    return time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(t))


def cmd_status(args, user):
    payload = current_cache(user)
    window = ident.read_window(user)
    _, _, disk = ident.disk_state(user)
    pair = ident.pair_state(user)
    s = settings(user)
    info = {
        'mode': s.get('identity_source', 'disk'),
        'window': window if isinstance(window, str) else (window or {}).get('source'),
        'cache': None if payload is None else {
            'period_ends': payload['period_ends'], 'hard_expiry': payload['hard_expiry']},
        'onepassword_configured': bool(s.get('identity_op_item')),
        'op_installed': bool(op_binary()),
        'apple_files_on_disk': [f for f in FILES if os.path.exists(user.path(f))],
        'self_signed': pair['certificate'] and pair['key'],
        'cache_hours': cache_hours(user),
    }
    if args.json:
        print(json.dumps(info, sort_keys=True))
        return 0
    lines = [f"Identity source: {info['mode']}"]
    if info['window']:
        lines.append(f"This window uses: {info['window']}")
    if info['mode'] == '1password':
        if payload:
            lines.append(f"Cached from 1Password until {describe_expiry(payload['period_ends'])}")
        else:
            lines.append('Nothing cached; the next `omdrop on` asks 1Password.')
        if not info['op_installed']:
            lines.append('The 1Password CLI (op) is not installed.')
        if info['apple_files_on_disk']:
            lines.append('Apple identity files are still on disk: '
                         + ', '.join(info['apple_files_on_disk'])
                         + ' (run: omdrop identity 1password import)')
    elif disk['certificate'] and disk['key']:
        lines.append('Apple identity on disk.' if disk['record'] else 'Identity on disk.')
    lines.append('Self-signed identity: ' + ('present' if info['self_signed'] else 'not yet created'))
    print('\n'.join(lines))
    return 0


def op_json(user, args, deadline):
    return json.loads(op_run(user, [*args, '--format', 'json'], deadline) or b'{}')


def account_id(user, deadline):
    """The 1Password account to pin, when the app has exactly one.

    Not `op whoami`: with the desktop app integration it reports only an
    existing sign-in and never asks for one, so it fails in a fresh process
    even right after the user approved a prompt.
    """
    accounts = json.loads(op_run(user, ['account', 'list', '--format', 'json'], deadline) or b'[]')
    if len(accounts) == 1:
        return accounts[0].get('account_uuid', '')
    return ''                  # several accounts: op's own default decides


class Superseded(Exception):
    pass


def _check_current(user, generation):
    """Under the lock: refuse if a window opened or the generation moved."""
    if window_open(user):
        raise Superseded('Turn Omdrop off first.')
    if counter(user, 'gen') != generation:
        raise Superseded('Cancelled: the identity settings changed meanwhile.')


def save_item_ids(user, account, vault, item, generation):
    """Record a newly created item's IDs, mode unchanged, under the same checks
    as a commit: a superseded import must not overwrite a newer choice."""
    with ident._Flock(user.lock):
        _check_current(user, generation)
        setting_set(user, identity_op_account=account, identity_op_vault=vault,
                    identity_op_item=item)


def adopt(user, account, vault, item, generation):
    """Commit to a 1Password item, under the lock.

    Refused if a window opened or another identity command ran since this one
    began. The old cache is cleared first, and a failure to clear it refuses
    the commit: the cache may hold a different identity, and the next window
    would present it. Then the IDs are saved and the mode last, the mode write
    being the commit point, and the generation moves so no fetch still out can
    publish.
    """
    with ident._Flock(user.lock):
        _check_current(user, generation)
        try:
            ident.Keyring().revoke()
        except ident.KeyringError as e:
            raise Superseded(f'Could not clear the cached identity ({e}); nothing changed.') from e
        setting_set(user, identity_op_account=account, identity_op_vault=vault,
                    identity_op_item=item)
        setting_set(user, identity_source='1password')
        bump(user, 'gen')
        kill_fetch(user)


def cmd_import(args, user):
    with ident._Flock(user.lock):
        if window_open(user):
            warn('Turn Omdrop off first.')
            return 1
        generation = counter(user, 'gen')
    deadline = time.monotonic() + 5 * FETCH_SECONDS
    present = {f: ident._read(user.path(f), 'rb') for f in FILES}
    s = settings(user)
    try:
        if s.get('identity_op_item'):
            # A rerun: an item exists. Compare what's left on disk with it.
            account, vault, item = item_ref(user)
            stored = op_read_files(user, account, vault, item, deadline)
            for f, data in present.items():
                if data is not None and data != stored[f]:
                    warn(f'{f} on disk differs from the 1Password item; nothing was deleted.')
                    return 1
            if s.get('identity_source') != '1password':
                problem = ident.check_apple(stored['certificate.pem'], stored['key.pem'],
                                            stored['validation_record.cms'])
                if problem:
                    warn(f'The 1Password item is not usable: {problem}; nothing was deleted.')
                    return 1
                adopt(user, account, vault, item, generation)
        else:
            if None in present.values():
                warn('Nothing to import: ' + ', '.join(f for f, d in present.items() if d is None)
                     + ' missing from ' + user.keys)
                return 1
            problem = ident.check_apple(present['certificate.pem'], present['key.pem'],
                                        present['validation_record.cms'])
            if problem:
                warn(f'Not importing: {problem}.')
                return 1
            create = ['item', 'create', '--category', 'Secure Note', '--title', args.title]
            if args.vault:
                create += ['--vault', args.vault]
            create += [f'{OP_NAMES[f]}[file]={user.path(f)}' for f in FILES]
            made = op_json(user, create, deadline)
            item = made.get('id', '')
            vault = (made.get('vault') or {}).get('id', '')
            if not (item and vault):
                warn('1Password did not report the new item; nothing was deleted.')
                return 1
            account = account_id(user, deadline)
            # Saved straight away, with the mode unchanged: if anything below
            # fails, a rerun finds this item instead of creating another.
            try:
                save_item_ids(user, account, vault, item, generation)
            except OSError as e:
                warn(f'Could not save the settings ({e}). The item is {item}; '
                     f'run: omdrop identity 1password use {item}')
                return 1
            except Superseded as e:
                warn(f'{e} The new 1Password item is {item}; nothing was deleted.')
                return 1
            stored = op_read_files(user, account, vault, item, deadline)
            if stored != present:
                warn(f'The 1Password item {item} does not match the files; nothing was deleted.')
                return 1
            try:
                adopt(user, account, vault, item, generation)
            except OSError as e:
                warn(f'Could not save the settings ({e}); nothing was deleted. '
                     'Run the import again to finish.')
                return 1
    except FetchFailed as e:
        warn(f'{e}; nothing was deleted.')
        return 1
    except Superseded as e:
        warn(f'{e} Nothing was deleted.')
        return 1
    # Committed. The key first: it is the one that matters most.
    for f in ('key.pem', 'validation_record.cms', 'certificate.pem'):
        try:
            os.unlink(user.path(f))
        except FileNotFoundError:
            pass
    warn('Your Apple identity is now in 1Password, and the copies on disk were deleted. '
         'The next `omdrop on` fetches it.')
    return 0


def cmd_use(args, user):
    with ident._Flock(user.lock):
        if window_open(user):
            warn('Turn Omdrop off first.')
            return 1
        generation = bump(user, 'gen')
        kill_fetch(user)
    deadline = time.monotonic() + FETCH_SECONDS
    try:
        get = ['item', 'get', args.item]
        if args.vault:
            get += ['--vault', args.vault]
        found = op_json(user, get, deadline)
        item, vault = found.get('id', ''), (found.get('vault') or {}).get('id', '')
        account = args.account or account_id(user, deadline)
        files = op_read_files(user, account, vault, item, deadline)
    except FetchFailed as e:
        warn(str(e))
        return 1
    problem = ident.check_apple(files['certificate.pem'], files['key.pem'],
                                files['validation_record.cms'])
    if problem:
        warn(f'That item is not usable: {problem}.')
        return 1
    try:
        adopt(user, account, vault, item, generation)
    except Superseded as e:
        warn(str(e))
        return 1
    warn('Omdrop now uses that 1Password item. The next `omdrop on` fetches it.')
    return 0


def cmd_onepassword_off(args, user):
    with ident._Flock(user.lock):
        if window_open(user):
            warn('Turn Omdrop off first.')
            return 1
        bump(user, 'gen')
        kill_fetch(user)
        try:
            ident.Keyring().revoke()
        except ident.KeyringError:
            pass
        setting_set(user, identity_source='self-signed')
    warn('Omdrop no longer uses 1Password; the item in 1Password is kept.')
    return 0


def cmd_ensure_self_signed(args, user):
    try:
        made = ident.create_self_signed(user, args.name)
    except ident.IdentityError as e:
        warn(str(e))
        return 2
    say(renamed=ident.migrate_legacy(user))
    return 0 if made else 1


def main(argv):
    ap = argparse.ArgumentParser(prog='identity.py')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('begin'); p.add_argument('--name', required=True)
    p = sub.add_parser('window'); p.add_argument('--seq', type=int, required=True)
    sub.add_parser('stop-begin'); sub.add_parser('stop-end')
    sub.add_parser('lock'); sub.add_parser('unlock')
    p = sub.add_parser('status'); p.add_argument('--json', action='store_true')
    p = sub.add_parser('import'); p.add_argument('--vault'); p.add_argument('--title', default=DEFAULT_TITLE)
    p = sub.add_parser('use'); p.add_argument('item'); p.add_argument('--vault'); p.add_argument('--account')
    sub.add_parser('1password-off')
    p = sub.add_parser('ensure-self-signed'); p.add_argument('--name', required=True)
    args = ap.parse_args(argv)
    user = ident.User()
    handler = {'begin': cmd_begin, 'window': cmd_window, 'stop-begin': cmd_stop_begin,
               'stop-end': cmd_stop_end, 'lock': cmd_lock, 'unlock': cmd_unlock,
               'status': cmd_status, 'import': cmd_import, 'use': cmd_use,
               '1password-off': cmd_onepassword_off,
               'ensure-self-signed': cmd_ensure_self_signed}[args.cmd]
    ident.not_dumpable()
    return handler(args, user)
