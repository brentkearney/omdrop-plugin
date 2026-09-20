#!/usr/bin/env python3
"""Serve AirDrop's HTTPS endpoints (/Discover, /Ask, /Upload) on awdl0.

OpenDrop's receiver, minus its announcer: the driver package's radio helper
hand-builds the AWDL service announcements that actually reach the Macs (an
ordinary multicast rarely makes it to the air; its 40-frame bursts do), so
this serves under the identity that helper advertises -- instance
<awdl0 MAC hex>, host <awdl host>.local, port 8771 -- and registers nothing
over mDNS unless --announce is given.

Runs as the invoking user, never root: it needs no privilege, and received
files belong to the user without a chown.
"""
import argparse
import io
import json
import logging
import socket
import os
import plistlib
import pwd
import re
import signal
import stat as stat_mod
import struct
import sys
import time
import unicodedata
import zlib

import libarchive
import libarchive.extract
from http.server import ThreadingHTTPServer

import opendrop.server as od_server
from opendrop.config import AirDropConfig
from opendrop.server import AirDropServer


# Path separators, NUL, control characters (Cc) and format characters (Cf:
# bidi overrides, zero-width joiners) -- the last because a name like
# "gpj.<U+202E>exe" renders as "exe.jpg" in a file manager. Used for received
# file names and for the receiver name we advertise.
UNSAFE_CHAR = re.compile(r'[/\\\x00]|[\u202a-\u202e\u2066-\u2069\u200b-\u200f\ufeff]|[\x01-\x1f\x7f-\x9f]')
# The config file is the source of truth: ~/.config/airdrop/config.toml with
# name / model / download_dir, written by `omdrop setup` and `omdrop name`.
# CLI flags override individual keys, so the file can be absent entirely.
# Validation lives here, never in whatever writes the file: a GUI is a
# convenience for writing a value, not what makes it safe. Reload = restart.
CONFIG_PATH = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.join(pwd.getpwuid(os.getuid()).pw_dir, '.config'),
                           'airdrop', 'config.toml')
CONFIG_KEYS = ('name', 'model', 'download_dir')


def awdl_host():
    """The host label this machine answers to on AWDL, without a trailing
    '.local'. The announcer, the mDNS responder and this receiver must derive
    it identically: a Mac that resolves a name nobody answers gets a tile it
    cannot upload to. An explicit override wins, then the system-wide file the
    driver package writes, then the short hostname -- lowercased and reduced to
    the characters a DNS label may carry, because a label with a capital or an
    underscore in it will not round-trip."""
    override = os.environ.get('OMDROP_AWDL_HOST')
    if not override:
        try:
            with open('/etc/omdrop/awdl-host') as f:
                override = f.read().strip()
        except OSError:
            override = ''
    if override:
        return override.rstrip('.').removesuffix('.local')
    label = re.sub(r'[^a-z0-9-]', '', socket.gethostname().split('.')[0].lower()).strip('-')
    return f'{label}-awdl' if label else 'omdrop-awdl'


def default_receiver_name():
    """What the Mac sheet draws under our tile when config.toml says nothing:
    the short hostname, as the user already knows this machine."""
    return socket.gethostname().split('.')[0] or 'omdrop'


def load_config(path):
    """Read the TOML; unknown keys are an error (a typo must not silently fall
    back to a default), missing file is fine (defaults apply)."""
    import tomllib
    try:
        with open(path, 'rb') as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f'{path}: {e}')
    bad = sorted(set(cfg) - set(CONFIG_KEYS))
    if bad:
        raise SystemExit(f'{path}: unknown key(s) {", ".join(bad)}; known: {", ".join(CONFIG_KEYS)}')
    for k, v in cfg.items():
        if not isinstance(v, str) or not v.strip():
            raise SystemExit(f'{path}: {k} must be a non-empty string')
    return cfg


MODEL_RE = re.compile(r'^[A-Za-z]+[0-9]+,[0-9]+$')  # MacBookPro18,3 / iMac21,1 / Macmini9,1


def validate_name(name):
    # What the Mac sheet will draw. No control/format characters, bounded length.
    if UNSAFE_CHAR.search(name) or len(name) > 63:
        raise SystemExit(f'name {name!r}: 1-63 characters, no path separators or control/format characters')
    return name


def validate_model(model):
    if not MODEL_RE.match(model):
        raise SystemExit(f'model {model!r}: must be an Apple model identifier such as MacBookPro18,3')
    return model


def validate_download_dir(path):
    """Resolved once; must exist, be a directory, and be owned by this user --
    received files land here as this user, so a directory we do not own is a
    misconfiguration, not something to create or chown our way out of."""
    dest = os.path.realpath(os.path.expanduser(path))
    try:
        st = os.stat(dest)
    except FileNotFoundError:
        raise SystemExit(f'download_dir {path!r} -> {dest}: does not exist')
    if not stat_mod.S_ISDIR(st.st_mode):
        raise SystemExit(f'download_dir {dest}: not a directory')
    if st.st_uid != os.getuid():
        raise SystemExit(f'download_dir {dest}: owned by uid {st.st_uid}, not us ({os.getuid()})')
    return dest


ap = argparse.ArgumentParser(description=f'AirDrop receiver. Config: {CONFIG_PATH} (name, model, download_dir); flags override.')
ap.add_argument('--iface', default='awdl0')
ap.add_argument('--port', type=int, default=8771)
ap.add_argument('--name', default=None,
                help='ReceiverComputerName: the label under the tile in the Mac sheet (config: name)')
ap.add_argument('--model', default=None,
                help='ReceiverModelName: Apple model identifier; picks the device glyph the sheet draws (config: model)')
ap.add_argument('--host', default=awdl_host(),
                help='AWDL host label, without ".local"; must match what the radio helper advertises')
ap.add_argument('--keys', default=os.path.join(pwd.getpwuid(os.getuid()).pw_dir, '.opendrop'),
                help='opendrop dir holding keys/certificate.pem and keys/key.pem')
ap.add_argument('--outdir', default=None,
                help='where received files go (config: download_dir)')
ap.add_argument('--config', default=CONFIG_PATH, help='config file path')
ap.add_argument('--announce', action='store_true',
                help='also register the service over mDNS (zeroconf)')
args = ap.parse_args()

_cfg = load_config(args.config)
NAME = validate_name(args.name if args.name is not None else _cfg.get('name') or default_receiver_name())
MODEL = validate_model(args.model if args.model is not None else _cfg.get('model', 'MacBookPro18,3'))
_outdir = args.outdir if args.outdir is not None else _cfg.get('download_dir', os.path.join(pwd.getpwuid(os.getuid()).pw_dir, 'Downloads'))
# Resolved once; never chdir -- cwd is process-global and the server is threaded.
DEST = validate_download_dir(_outdir)

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout,
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logging.getLogger('zeroconf').setLevel(logging.INFO)

with open(f'/sys/class/net/{args.iface}/address') as f:
    sid = f.read().strip().replace(':', '')


class ThreadingHTTPServerV6(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

    # A connection that opens and then carries no request is the signature of a
    # sender that gave up or a TLS handshake we lost -- invisible in the
    # request log, which only fires once a request line parses.
    def __init__(self, *a, **k):
        # Sessions still open at shutdown, so stopping can free the port.
        self.live = set()
        super().__init__(*a, **k)

    def process_request(self, request, client_address):
        logging.info('connection from [%s]:%s', client_address[0], client_address[1])
        self.live.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request):
        self.live.discard(request)
        super().shutdown_request(request)

    # A sender keeps its TLS session open after a transfer, and when this
    # process stops the kernel closes that session with a FIN. An iPhone that
    # has already left the AWDL link never acknowledges it, so the socket sits
    # in FIN_WAIT_1 holding port 8771 for minutes -- and the next start cannot
    # bind, which is exactly what "turn it off and on again" does after a
    # transfer (observed 2026-09-18 03:11Z, 23 restart attempts).
    #
    # A reset costs nothing here: the transfer is over, and the peer is either
    # gone or about to be told we are.
    def reset_sessions(self):
        for sock in list(self.live):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                struct.pack('ii', 1, 0))
                sock.close()
            except OSError:
                pass
            self.live.discard(sock)

    def handle_error(self, request, client_address):
        # sharingd closes the keep-alive TLS session with a RST once the
        # transfer is done (211010Z); the resulting ECONNRESET on the next
        # readline is the normal end of a session, not a fault.
        if isinstance(sys.exc_info()[1], ConnectionResetError):
            return
        super().handle_error(request, client_address)


class NoZeroconf:
    def __init__(self, *a, **k): pass
    def register_service(self, *a, **k): pass
    def unregister_all_services(self): pass


def read_chunked(rfile):
    """Consume one chunked body from rfile, leaving rfile at the next request."""
    body = bytearray()
    while True:
        size = int(rfile.readline().split(b';')[0].strip() or b'0', 16)
        if size == 0:
            while rfile.readline() not in (b'\r\n', b'\n', b''):
                pass
            return bytes(body)
        body += rfile.read(size)
        rfile.readline()


def dvzip_decode(data):
    """DVZip (Content-Type application/x-dvzip): a sequence of blocks, each a
    4-byte big-endian header + payload; the header's low 31 bits are the
    payload length and the high bit marks a stored (uncompressed) block, which
    the sender uses for incompressible data (215526Z: a 22 MB video was 20
    zlib blocks then 151 stored ones). Whole stream may instead be plain gzip.
    The payload is a CPIO archive (arXiv 2606.26967 sec. 3.4; odc on macOS 15).
    """
    if data[:2] == b'\x1f\x8b':
        return zlib.decompress(data, 31)
    out = bytearray()
    pos = 0
    while pos + 4 <= len(data):
        hdr = int.from_bytes(data[pos:pos + 4], 'big')
        n = hdr & 0x7fffffff
        pos += 4
        block = data[pos:pos + n]
        if len(block) != n:
            raise ValueError(f'truncated block at {pos - 4}: header says {n}, {len(block)} left')
        pos += n
        if hdr & 0x80000000:
            out += block
            continue
        try:
            out += zlib.decompress(block)
        except zlib.error as e:
            raise ValueError(f'undecodable block at {pos - n - 4}, len {n}: {e}')
    return bytes(out)


def safe_name(name, fallback):
    """One filesystem component from sender-controlled text. Keeps Unicode --
    'café', '日本語', emoji are legitimate names (231000Z: an ASCII whitelist
    turned 'Ω café — 日本語 test  file 🐔.jpg' into '_caf_test_file_.jpg') --
    and removes only what can change *where* or *as what* the file appears:
    path separators, NUL, control/format characters, leading dots (hidden
    files) and dot-only names. NFC so 'é' is one code point whichever
    normalisation the sender used (macOS sends NFD). Capped at 255 bytes of
    UTF-8, the filesystem limit, cut on a character boundary."""
    name = unicodedata.normalize('NFC', name.replace('\\', '/').rsplit('/', 1)[-1])
    name = UNSAFE_CHAR.sub('', name).strip().lstrip('.')
    stem, ext = os.path.splitext(name)
    while len((stem + ext).encode('utf-8')) > 255 and stem:
        stem = stem[:-1]
    return (stem + ext) or fallback


def open_new(dest, name):
    """Create dest/name without clobbering: 'photo.jpg', 'photo-1.jpg', ...
    (shell-friendly, unlike macOS's 'photo (1).jpg'; the scheme is local, the
    sender never learns the stored name).
    O_EXCL makes the check and the create one step, so two threads storing the
    same name cannot both win."""
    stem, ext = os.path.splitext(name)
    for i in range(10000):
        cand = name if i == 0 else f'{stem}-{i}{ext}'
        try:
            fd = os.open(os.path.join(dest, cand), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        return cand, os.fdopen(fd, 'wb')
    raise FileExistsError(name)


def store_upload(raw, dest, tid):
    """Write every regular file in the DVZip's archive into dest by its
    sanitised basename. Directories, symlinks, devices and any path component
    other than the basename are ignored: the archive is untrusted input from
    radio range, and libarchive's extract() would honour '..', absolute paths
    and symlink targets unless told not to. Returns the names written.
    The raw body is kept as <tid>.dvzip only if decoding or extraction fails
    (that is how 215526Z's video was recovered); on success it is not stored."""
    written = []
    try:
        cpio = dvzip_decode(raw)
        logging.info('upload %s: decoded to %d bytes, magic %r', tid, len(cpio), cpio[:6])
        with libarchive.memory_reader(cpio) as archive:
            for entry in archive:
                if not entry.isfile:
                    continue
                # AppleDouble sidecars ('._name'): metadata, not content. Test the
                # raw basename -- safe_name strips leading dots (233146Z stored one
                # as '_Ω café ….jpg').
                if os.path.basename(entry.pathname).startswith('._'):
                    continue
                base = safe_name(entry.pathname, 'file')
                name, f = open_new(dest, base)
                with f:
                    for block in entry.get_blocks():
                        f.write(block)
                written.append(name)
    except Exception:
        name, f = open_new(dest, f'{tid}.dvzip')
        with f:
            f.write(raw)
        logging.error('upload %s: kept raw body as %s after: %s', tid, name,
                      ', '.join(written) or 'nothing extracted')
        raise
    return written


def store_link(url, dest):
    """Write a received URL into dest as an inert .url shortcut and return the
    stored name. INI form ([InternetShortcut]) because it is plain text, needs
    no execute bit, and is what browsers and file managers already open -- a
    .desktop Type=Link would be the freedesktop spelling but invites the
    executable-desktop-entry class of problem for no gain.

    The name comes from the URL's host and last path segment, sanitised the
    same way an uploaded filename is; the URL itself is written as a value, not
    interpreted, and nothing here launches anything."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    stem = '-'.join(p for p in (parts.netloc, parts.path.rstrip('/').rsplit('/', 1)[-1]) if p)
    name, f = open_new(dest, safe_name(stem, 'link') + '.url')
    with f:
        f.write(f'[InternetShortcut]\nURL={url}\n'.encode('utf-8'))
    return name


class Handler(od_server.AirDropServerHandler):
    """sharingd (AirDrop/1.0, macOS 15) differs from what OpenDrop expects:
    - /Discover and /Ask arrive `Transfer-Encoding: chunked` with no
      Content-Length; OpenDrop reads int(Content-Length). Dechunk first.
      rfile is restored afterwards: Ask and Upload must share one TLS
      connection, and a swapped-out rfile made the keep-alive loop see EOF.
    - /Upload arrives as application/x-dvzip (OpenDrop accepts only x-cpio and
      answers 406, which the sender reports as a failed transfer). Decode it.
    """

    def do_POST(self):
        if (self.headers.get('Transfer-Encoding', '').lower() == 'chunked'
                and self.path in ('/Discover', '/Ask')):
            body = read_chunked(self.rfile)
            orig = self.rfile
            self.rfile = io.BytesIO(body)
            del self.headers['Transfer-Encoding']
            self.headers['Content-Length'] = str(len(body))
            logging.debug('dechunked %d-byte body for %s', len(body), self.path)
            try:
                super().do_POST()
            finally:
                self.rfile = orig
            return
        super().do_POST()

    # 2026-09-12, iPhone on iOS 26, two link transfers: /Discover 200, /Ask 200
    # for a 108-byte `x-com.webloc`, then NO /Upload at all while the phone's
    # UI reported "Sent". OpenDrop answers an Ask with the two name keys only;
    # its own Discover answer (and a Mac's, 043402Z) also carries
    # ReceiverMediaCapabilities, which is how the sender learns what the
    # receiver will take. Answer the Ask with the same three keys and log what
    # was asked for: that log line is what separates "the sender never
    # uploaded" from "the upload failed on our side".
    def handle_ask(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        od_server.AirDropUtil.write_debug(self.config, body, 'receive_ask_request.plist')
        try:
            ask = plistlib.loads(body)
        except Exception as e:
            logging.error('ask: undecodable %d-byte body: %r', len(body), e)
            ask = {}
        files = ask.get('Files') or []
        logging.info('ask from %s (%s): type %s, %d item(s): %s',
                     ask.get('SenderComputerName'), ask.get('SenderModelName'),
                     ','.join(ask.get('TransferType') or {}) or '?', len(files),
                     '; '.join(f"{f.get('FileName')} {f.get('FileType')} {f.get('FileSize')}B"
                               for f in files) or (ask.get('Items') or '-'))
        # A `links` transfer is Ask-only: with SUPPORTS_URL (0x01) advertised,
        # iOS puts the URL straight into Items and sends no /Upload at all, so
        # accepting the Ask IS the transfer. Store it, or the link is lost.
        # NEVER open it: in Everyone mode any device in range could otherwise
        # make this machine visit an arbitrary URL. A file the user clicks is
        # the whole interaction we are willing to offer.
        if 'links' in (ask.get('TransferType') or {}):
            for item in ask.get('Items') or []:
                if not isinstance(item, str) or not item.startswith(('http://', 'https://')):
                    logging.warning('ask: ignoring non-http item %.80r', item)
                    continue
                stored = store_link(item, DEST)
                logging.info('link stored as %s -> %s', stored, item)
        resp = plistlib.dumps({
            # Empty capabilities = "send me the legacy formats", as OpenDrop's
            # Discover answer does; the key's presence is the point.
            'ReceiverMediaCapabilities': json.dumps({'Version': 1}).encode(),
            'ReceiverComputerName': self.config.computer_name,
            'ReceiverModelName': self.config.computer_model,
        }, fmt=plistlib.FMT_BINARY)
        od_server.AirDropUtil.write_debug(self.config, resp, 'receive_ask_response.plist')
        self._set_response(len(resp))
        self.wfile.write(resp)

    def handle_upload(self):
        ctype = self.headers.get('Content-Type', '').split(';')[0].strip().lower()
        if ctype != 'application/x-dvzip':
            return super().handle_upload()
        if self.headers.get('Expect', '').lower() == '100-continue':
            self.send_response_only(100)
            self.end_headers()
        if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
            raw = read_chunked(self.rfile)
        else:
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        tid = safe_name(self.headers.get('TransferID', ''), 'upload')
        logging.info('upload %s: %d bytes dvzip (TotalBytes %s)',
                     tid, len(raw), self.headers.get('TotalBytes'))
        try:
            written = store_upload(raw, DEST, tid)
            logging.info('upload %s: stored %s', tid, ', '.join(written))
        except Exception as e:  # the sender still sees success; the raw bytes are kept
            logging.error('upload %s: %r', tid, e)
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, fmt, *a):
        logging.info('%s %s', self.client_address[0], fmt % a)


# sharingd opens several connections at once; a single-threaded server would
# hold /Discover behind a stalled one.
od_server.HTTPServerV6 = ThreadingHTTPServerV6
od_server.AirDropServerHandler = Handler
if not args.announce:
    od_server.Zeroconf = NoZeroconf

config = AirDropConfig(host_name=args.host, computer_name=NAME,
                       computer_model=MODEL, server_port=args.port,
                       airdrop_dir=args.keys, service_id=sid,
                       interface=args.iface, debug=True)

# opendrop answers a busy port by quietly moving to the next one, which
# nothing advertises -- a receiver nobody can reach. Waiting is what the user
# wants instead: the usual reason is the previous run's socket still unwinding,
# which clears in seconds now that stopping resets its sessions.
def bind_server(attempts=5, pause=2):
    for attempt in range(1, attempts + 1):
        config.port = args.port
        candidate = AirDropServer(config)
        if config.port == args.port:
            return candidate
        candidate.http_server.server_close()
        logging.warning('port %d still held; retry %d of %d', args.port,
                        attempt, attempts)
        time.sleep(pause)
    return None


server = bind_server()
if server is None:
    logging.error('port %d is still held by something else after waiting; '
                  'the announcer only ever names %d', args.port, args.port)
    sys.exit(1)
logging.info('config %s: %s', args.config, ', '.join(f'{k}={v!r}' for k, v in sorted(_cfg.items())) or 'absent (defaults)')
logging.info('serving %s._airdrop._tcp.local as %s (%s) on [%s]:%d, receiving into %s',
             sid, NAME, MODEL, server.ip_addr, config.port, DEST)
if args.announce:
    server.start_service()

# systemd stops this unit with SIGTERM, whose default action skips every
# cleanup below -- including the session reset that frees the port.
def on_term(signum, frame):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, on_term)
try:
    server.start_server()
except KeyboardInterrupt:
    pass
finally:
    server.stop()
    server.http_server.reset_sessions()
    server.http_server.server_close()
