"""Which AirDrop identity this machine presents, per docs/identity-contract.md.

The receiver and `omdrop` both choose through here, and the driver's sender,
`send-to-peer` and Bluetooth advertiser implement the same contract on their
side; tests/identity-vectors.json, byte-identical in both repositories, holds
the cases every implementation must pass.

The parsers and select() are pure: they take the state already read and return
a decision, so the vectors can drive them without a keyring, a window or a
filesystem. The functions below them do the reading.
"""
import json
import os
import pwd
import re
import subprocess
import sys

MAGIC = b'OMDROP-IDENTITY 1'
HEADER_KEYS = {'certificate', 'fetch_id', 'fetched_at', 'hard_expiry', 'key',
               'period_ends', 'record'}
MAX_LIFETIME = 86400
SOURCES = ('1password', 'disk', 'self-signed')
MAX_PART = 16384
MAX_PAYLOAD = 32767
KEY_DESCRIPTION = 'omdrop:identity'
_FETCH_ID = re.compile(r'[0-9a-f]{32}')


# ------------------------------------------------------------------ parsers

def parse_settings(text):
    """key=value lines; the last occurrence of a key wins."""
    out = {}
    for line in text.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            out[k] = v
    return out


def _nonneg_int(value):
    return (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def parse_window(text):
    """The window file's contents, or None when it is unparseable."""
    fields = parse_settings(text)
    source = fields.get('source')
    if source not in SOURCES:
        return None
    if source != '1password':
        return {'source': source}
    fetch_id, expiry = fields.get('fetch_id', ''), fields.get('hard_expiry', '')
    if not _FETCH_ID.fullmatch(fetch_id) or not re.fullmatch(r'[0-9]+', expiry):
        return None
    return {'source': source, 'fetch_id': fetch_id, 'hard_expiry': int(expiry)}


def parse_payload(data):
    """The cache payload as a dict, or None when it is malformed."""
    if len(data) > MAX_PAYLOAD:
        return None
    first = data.find(b'\n')
    if first < 0 or data[:first] != MAGIC:
        return None
    second = data.find(b'\n', first + 1)
    if second < 0:
        return None
    try:
        header = json.loads(data[first + 1:second])
    except ValueError:
        return None
    if not isinstance(header, dict) or set(header) != HEADER_KEYS:
        return None
    if not isinstance(header['fetch_id'], str) or not _FETCH_ID.fullmatch(header['fetch_id']):
        return None
    t0, t1, t2 = header['fetched_at'], header['period_ends'], header['hard_expiry']
    if not all(_nonneg_int(t) for t in (t0, t1, t2)):
        return None
    if not (t0 <= t1 <= t2) or t2 - t0 > MAX_LIFETIME:
        return None
    lengths = [header[k] for k in ('certificate', 'key', 'record')]
    if not all(_nonneg_int(n) and 1 <= n <= MAX_PART for n in lengths):
        return None
    body = data[second + 1:]
    if len(body) != sum(lengths):
        return None
    c, k, _ = lengths
    return {'fetch_id': header['fetch_id'], 'fetched_at': t0, 'period_ends': t1,
            'hard_expiry': t2, 'certificate': body[:c], 'key': body[c:c + k],
            'record': body[c + k:]}


def encode_payload(fetch_id, fetched_at, period_ends, hard_expiry, certificate, key, record):
    header = {'certificate': len(certificate), 'fetch_id': fetch_id,
              'fetched_at': fetched_at, 'hard_expiry': hard_expiry, 'key': len(key),
              'period_ends': period_ends, 'record': len(record)}
    data = (MAGIC + b'\n' + json.dumps(header, separators=(',', ':'), sort_keys=True).encode()
            + b'\n' + certificate + key + record)
    if parse_payload(data) is None:
        raise ValueError('identity payload would not parse')
    return data


# ------------------------------------------------------------------ selection

def _self_signed(pair, root, warnings):
    if pair['certificate'] and pair['key']:
        return {'identity': 'self-signed', 'record': False, 'create': False,
                'warnings': sorted(warnings)}
    if pair['certificate'] or pair['key']:
        return {'error': 'self-signed-half-pair'}
    if root:
        return {'error': 'self-signed-missing'}
    return {'identity': 'self-signed', 'record': False, 'create': True,
            'warnings': sorted(warnings)}


def _disk(disk):
    return {'identity': 'disk', 'record': bool(disk['record']), 'create': False,
            'warnings': []}


def select(settings, window, cache, disk, self_signed, root, now):
    """The identity to present. See docs/identity-contract.md, "Selection"."""
    if window is not None:
        if window == 'unparseable':
            return {'error': 'window-unparseable'}
        source = window['source']
        if source == '1password':
            if window['hard_expiry'] <= now:
                return {'error': 'window-expired'}
            if cache is None:
                return {'error': 'cache-missing'}
            if cache == 'malformed':
                return {'error': 'cache-malformed'}
            if cache['fetch_id'] != window['fetch_id']:
                return {'error': 'cache-mismatch'}
            if cache['hard_expiry'] <= now:
                return {'error': 'cache-expired'}
            return {'identity': 'cache', 'record': True, 'create': False, 'warnings': []}
        if source == 'disk':
            if not (disk['certificate'] and disk['key']):
                return {'error': 'disk-missing'}
            if not disk['match']:
                return {'error': 'disk-mismatch'}
            return _disk(disk)
        return _self_signed(self_signed, root, [])

    mode = settings.get('identity_source', 'disk')
    if mode in ('1password', 'self-signed'):
        return _self_signed(self_signed, root, [])
    if mode != 'disk':
        return _self_signed(self_signed, root, ['unknown-mode'])
    have_cert, have_key = disk['certificate'], disk['key']
    if have_cert and have_key:
        if disk['match']:
            return _disk(disk)
        return _self_signed(self_signed, root, ['disk-mismatch'])
    if have_cert or have_key:
        return _self_signed(self_signed, root, ['disk-incomplete'])
    return _self_signed(self_signed, root, [])


# ------------------------------------------------------------------ the real state

class User:
    """Whose identity this is: the invoking user when running as root.

    `keys_dir` is an explicit --keys directory: it replaces ~/.omdrop for the
    disk trio and the self-signed pair, and nothing else.
    """

    def __init__(self, keys_dir=None):
        self.root = os.geteuid() == 0
        uid = os.getuid()
        if self.root:
            for var in ('PKEXEC_UID', 'SUDO_UID'):
                value = os.environ.get(var, '')
                if value.isdigit():
                    uid = int(value)
                    break
        entry = pwd.getpwuid(uid)
        self.uid, self.gid, self.home = uid, entry.pw_gid, entry.pw_dir
        if self.root:
            self.config = os.path.join(self.home, '.config')
            self.runtime = f'/run/user/{uid}'
        else:
            self.config = os.environ.get('XDG_CONFIG_HOME') or os.path.join(self.home, '.config')
            self.runtime = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{uid}'
        base = keys_dir if keys_dir else os.path.join(self.home, '.omdrop')
        self.keys = os.path.join(base, 'keys')
        self.settings = os.path.join(self.config, 'omdrop', 'settings')
        self.state_dir = os.path.join(self.runtime, 'omdrop')
        self.window = os.path.join(self.state_dir, 'window')
        self.lock = os.path.join(self.state_dir, 'identity.lock')
        self.state = os.path.join(self.state_dir, 'state')

    def path(self, name):
        return os.path.join(self.keys, name)


def _read(path, mode='r'):
    try:
        with open(path, mode) as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _write_private(path, data):
    """Replace `path` with `data`, mode 0600, by renaming a temporary file."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode() if isinstance(data, str) else data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def key_matches(cert_pem, key_pem):
    """Does this private key belong to this certificate?"""
    def pub(args, data):
        run = subprocess.run(['openssl', *args], input=data, capture_output=True)
        return run.stdout if run.returncode == 0 else None
    a = pub(['x509', '-noout', '-pubkey'], cert_pem)
    b = pub(['pkey', '-pubout'], key_pem)
    return a is not None and a == b


# ------------------------------------------------------------------ the kernel keyring

KEY_SPEC_PROCESS_KEYRING = -2
KEY_SPEC_USER_KEYRING = -4
KEY_PERM = 0x3f010000          # possessor: all; user: view


class KeyringError(Exception):
    pass


class Keyring:
    """The user keyring, through libkeyutils in this process.

    In-process rather than `keyctl` so the key can be staged in this process's
    own keyring, given its timeout there, and only then linked where others see
    it: a key is never visible without a timeout.
    """

    def __init__(self):
        import ctypes
        self.c = ctypes
        try:
            lib = ctypes.CDLL('libkeyutils.so.1', use_errno=True)
        except OSError as e:
            raise KeyringError('libkeyutils is not installed (package keyutils)') from e
        lib.add_key.restype = ctypes.c_int32
        lib.add_key.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                                ctypes.c_size_t, ctypes.c_int32]
        lib.keyctl_search.restype = ctypes.c_long
        lib.keyctl_search.argtypes = [ctypes.c_int32, ctypes.c_char_p, ctypes.c_char_p,
                                      ctypes.c_int32]
        lib.keyctl_read_alloc.restype = ctypes.c_long
        lib.keyctl_read_alloc.argtypes = [ctypes.c_int32, ctypes.POINTER(ctypes.c_void_p)]
        for name in ('keyctl_set_timeout', 'keyctl_setperm', 'keyctl_link', 'keyctl_unlink',
                     'keyctl_revoke'):
            getattr(lib, name).restype = ctypes.c_long
        self.lib = lib
        self.libc = ctypes.CDLL(None)

    def _fail(self, what):
        err = self.c.get_errno()
        raise KeyringError(f'{what}: {os.strerror(err)}')

    def find(self):
        kid = self.lib.keyctl_search(KEY_SPEC_USER_KEYRING, b'user', KEY_DESCRIPTION.encode(), 0)
        return kid if kid >= 0 else None

    def read(self, kid=None):
        kid = self.find() if kid is None else kid
        if kid is None:
            return None
        buf = self.c.c_void_p()
        n = self.lib.keyctl_read_alloc(kid, self.c.byref(buf))
        if n < 0:
            err = self.c.get_errno()
            if err in (126, 127, 128, 2):      # ENOKEY, EKEYEXPIRED, EKEYREVOKED, ENOENT
                return None
            raise KeyringError(f'reading the identity cache: {os.strerror(err)}')
        try:
            return self.c.string_at(buf, n)
        finally:
            self.libc.free(buf)

    def publish(self, payload, timeout):
        """Stage in @p, set timeout and permissions, then link into @u."""
        if timeout < 1:
            raise KeyringError('refusing to publish a cache that has already expired')
        old = self.find()
        kid = self.lib.add_key(b'user', KEY_DESCRIPTION.encode(), payload, len(payload),
                               KEY_SPEC_PROCESS_KEYRING)
        if kid < 0:
            self._fail('adding the identity cache')
        try:
            if self.lib.keyctl_set_timeout(kid, int(timeout)) < 0:
                self._fail('setting the cache timeout')
            if self.lib.keyctl_setperm(kid, KEY_PERM) < 0:
                self._fail('setting the cache permissions')
            if self.lib.keyctl_link(kid, KEY_SPEC_USER_KEYRING) < 0:
                self._fail('linking the identity cache')
        except KeyringError:
            self.lib.keyctl_revoke(kid)
            raise
        self.lib.keyctl_unlink(kid, KEY_SPEC_PROCESS_KEYRING)
        if old is not None and old != kid:
            self.lib.keyctl_revoke(old)
        return kid

    def set_timeout(self, timeout):
        kid = self.find()
        if kid is None:
            return False
        if timeout < 1:
            self.revoke()
            return False
        if self.lib.keyctl_set_timeout(kid, int(timeout)) < 0:
            self._fail('setting the cache timeout')
        return True

    def revoke(self):
        kid = self.find()
        if kid is None:
            return False
        self.lib.keyctl_revoke(kid)
        self.lib.keyctl_unlink(kid, KEY_SPEC_USER_KEYRING)
        return True


def cache_read(user=None):
    """The raw cache payload, or None.

    A sandboxed service -- the receiver, with ProtectHome and friends -- runs
    in its own user namespace, where `@u` names a different, empty user
    keyring, but the key itself is still readable by serial. So when the
    search finds nothing, try the `cache_serial` the window file records.

    As root for another user, read directly if the kernel allows it; if not,
    read from a child that has dropped to that user.
    """
    user = user or User()
    try:
        keyring = Keyring()
        data = keyring.read()
        if data is None:
            serial = parse_settings(_read(user.window) or '').get('cache_serial', '')
            if serial.isdigit():
                data = keyring.read(int(serial))
        if data is not None or not (user.root and user.uid != 0):
            return data
    except KeyringError:
        if not (user.root and user.uid != 0):
            raise
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            os.setgid(user.gid)
            os.setuid(user.uid)
            payload = Keyring().read() or b''
            os.write(w, payload)
            os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(w)
    chunks = []
    while True:
        chunk = os.read(r, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(r)
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) != 0:
        raise KeyringError('cache-unreadable-as-root')
    return b''.join(chunks) or None


# ------------------------------------------------------------------ resolving

class Identity:
    """What select() chose, with the bytes to present."""

    def __init__(self, kind, certificate, key, record, warnings):
        self.kind, self.certificate, self.key = kind, certificate, key
        self.record, self.warnings = record, warnings


class IdentityError(Exception):
    """The window's identity is unavailable; never fall back from here."""


def read_window(user):
    text = _read(user.window)
    if text is None:
        return None
    return parse_window(text) or 'unparseable'


def disk_state(user):
    """The on-disk Apple identity. An unreadable file counts as present but not
    matching, so selection falls back or errors as it would for a mismatch."""
    def read(name):
        path = user.path(name)
        try:
            return _read(path, 'rb'), os.path.lexists(path)
        except OSError:
            return None, True
    (cert, have_cert), (key, have_key) = read('certificate.pem'), read('key.pem')
    return cert, key, {'certificate': have_cert, 'key': have_key,
                       'match': bool(cert and key and key_matches(cert, key)),
                       'record': os.path.exists(user.path('validation_record.cms'))}


NO_DISK = {'certificate': False, 'key': False, 'match': False, 'record': False}


def disk_in_play(settings, window):
    """Whether selection can consult the disk identity at all."""
    if isinstance(window, dict):
        return window['source'] == 'disk'
    if window is not None:
        return False                      # unparseable: an error before disk
    return settings.get('identity_source', 'disk') == 'disk'


def pair_state(user):
    return {'certificate': os.path.exists(user.path('certificate.self-signed.pem')),
            'key': os.path.exists(user.path('key.self-signed.pem'))}


def resolve(user=None, now=None, name=None, read_cache=None):
    """Read the real state, select, and return an Identity.

    `name` is the computer name for a self-signed pair that must be created;
    without one, a missing pair is an error.
    """
    import time
    user = user or User()
    now = int(time.time()) if now is None else now
    settings = parse_settings(_read(user.settings) or '')
    window = read_window(user)
    cache = None
    if isinstance(window, dict) and window['source'] == '1password':
        try:
            raw = (read_cache or cache_read)(user)
        except KeyringError as e:
            if str(e) == 'cache-unreadable-as-root':
                raise IdentityError('cache-unreadable-as-root') from e
            raw = None
        if raw is not None:
            cache = parse_payload(raw) or 'malformed'
    # The disk files are read only when selection can use them: an unrelated,
    # unreadable legacy key must not stop a self-signed or cached identity.
    if disk_in_play(settings, window):
        cert, key, disk = disk_state(user)
    else:
        cert, key, disk = None, None, NO_DISK
    result = select(settings, window, cache, disk, pair_state(user), user.root, now)
    if 'error' in result:
        raise IdentityError(result['error'])
    if result['identity'] == 'cache':
        return Identity('cache', cache['certificate'], cache['key'], cache['record'],
                        result['warnings'])
    if result['identity'] == 'disk':
        record = _read(user.path('validation_record.cms'), 'rb') if result['record'] else None
        return Identity('disk', cert, key, record, result['warnings'])
    if result['create']:
        if not name:
            raise IdentityError('self-signed-missing')
        create_self_signed(user, name)
    return Identity('self-signed', _read(user.path('certificate.self-signed.pem'), 'rb'),
                    _read(user.path('key.self-signed.pem'), 'rb'), None, result['warnings'])


def load_into(ctx, ident):
    """Hand the identity to an SSL context through memfds, closed straight after."""
    fds = []
    try:
        for label, data in (('certificate', ident.certificate), ('key', ident.key)):
            fd = os.memfd_create(f'omdrop-{label}', os.MFD_CLOEXEC)
            fds.append(fd)
            os.write(fd, data)
        ctx.load_cert_chain(f'/proc/self/fd/{fds[0]}', keyfile=f'/proc/self/fd/{fds[1]}')
    finally:
        for fd in fds:
            os.close(fd)


def not_dumpable():
    """Keep keys out of core dumps and away from same-UID ptrace."""
    try:
        import ctypes
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)      # PR_SET_DUMPABLE, 0
    except (OSError, AttributeError):
        pass
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass


# ------------------------------------------------------------------ the self-signed pair

class _Flock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        import fcntl
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def create_self_signed(user, name):
    """Create the self-signed pair without ever replacing a file. Contract step 1-5."""
    import shutil
    import tempfile
    if user.root:
        raise IdentityError('self-signed-missing')
    os.makedirs(user.keys, mode=0o700, exist_ok=True)
    cert_to, key_to = user.path('certificate.self-signed.pem'), user.path('key.self-signed.pem')
    with _Flock(user.path('.identity.lock')):
        state = pair_state(user)
        if state['certificate'] and state['key']:
            return False
        if state['certificate'] or state['key']:
            raise IdentityError('self-signed-half-pair')
        tmp = tempfile.mkdtemp(prefix='.self-signed.', dir=user.keys)
        made = []
        try:
            run = subprocess.run(
                ['openssl', 'req', '-newkey', 'rsa:2048', '-nodes', '-x509', '-days', '365',
                 '-subj', f'/CN={name}', '-keyout', 'key.pem', '-out', 'certificate.pem'],
                cwd=tmp, capture_output=True)
            if run.returncode != 0:
                errors = [line for line in run.stderr.decode(errors='replace').splitlines()
                          if line.strip('.+*')]
                raise IdentityError('openssl could not create a certificate: '
                                    + ' / '.join(errors))
            for f in ('key.pem', 'certificate.pem'):
                os.chmod(os.path.join(tmp, f), 0o600)
            try:
                for src, dst in (('key.pem', key_to), ('certificate.pem', cert_to)):
                    os.link(os.path.join(tmp, src), dst)
                    made.append(dst)
            except OSError as e:
                # Any failure, not only a name that already exists (ENOSPC,
                # EIO...): leaving one half linked would block every later
                # attempt with self-signed-half-pair.
                for dst in made:
                    try:
                        os.unlink(dst)
                    except OSError:
                        pass
                if isinstance(e, FileExistsError):
                    return False
                raise IdentityError(f'could not create the self-signed pair: {e.strerror}') from e
            return True
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def is_self_signed(cert_pem):
    """Signed by its own key, and not chaining to Apple. openssl, not a name check."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.pem') as fh:
        fh.write(cert_pem)
        fh.flush()
        own = subprocess.run(['openssl', 'verify', '-check_ss_sig', '-CAfile', fh.name, fh.name],
                             capture_output=True)
    if own.returncode != 0:
        return False
    import contacts
    der = subprocess.run(['openssl', 'x509', '-outform', 'DER'], input=cert_pem,
                         capture_output=True).stdout
    return not contacts.certificate_account(der)


def migrate_legacy(user):
    """Rename a self-signed pair stored under the Apple names, when provably safe.

    Returns 'renamed', or the reason nothing was done.
    """
    cert, key = user.path('certificate.pem'), user.path('key.pem')
    if not (os.path.exists(cert) and os.path.exists(key)):
        return 'nothing-to-rename'
    with _Flock(user.path('.identity.lock')):
        if os.path.exists(user.path('validation_record.cms')):
            return 'has-record'
        state = pair_state(user)
        if state['certificate'] or state['key']:
            return 'destination-exists'
        cert_pem, key_pem = _read(cert, 'rb'), _read(key, 'rb')
        if not key_matches(cert_pem, key_pem):
            return 'key-mismatch'
        if not is_self_signed(cert_pem):
            return 'not-self-signed'
        # link, then unlink: no moment where the pair exists under neither name,
        # and no rename can replace a file that appeared meanwhile.
        os.link(key, user.path('key.self-signed.pem'))
        os.link(cert, user.path('certificate.self-signed.pem'))
        os.unlink(key)
        os.unlink(cert)
        return 'renamed'


# ------------------------------------------------------------------ checking an Apple identity

def check_apple(cert_pem, key_pem, record):
    """None when the set is a usable Apple identity, else the failed check."""
    import contacts
    if not (cert_pem and key_pem and record):
        return 'incomplete'
    if not key_matches(cert_pem, key_pem):
        return 'key does not match certificate'
    der = subprocess.run(['openssl', 'x509', '-outform', 'DER'], input=cert_pem,
                         capture_output=True).stdout
    account = contacts.certificate_account(der)
    if not account:
        return 'certificate is not issued by Apple'
    verified = contacts.verify_record(record)
    if verified is None:
        return "validation record does not carry Apple's signature"
    if verified['expired']:
        return 'validation record has expired'
    if verified['account'] != account:
        return 'certificate and validation record belong to different Apple accounts'
    return None


if __name__ == '__main__':
    import lifecycle
    sys.exit(lifecycle.main(sys.argv[1:]))
