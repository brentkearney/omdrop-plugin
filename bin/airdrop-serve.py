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
import ssl
import stat as stat_mod
import struct
import sys
import tempfile
import threading
import time
import unicodedata
import zlib

import libarchive
from http.server import ThreadingHTTPServer

import opendrop.server as od_server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contacts  # noqa: E402  -- sibling module, found via the line above
from opendrop.config import AirDropConfig
from opendrop.server import AirDropServer


# Path separators, NUL, control characters (Cc) and format characters (Cf:
# bidi overrides, zero-width joiners) -- the last because a name like
# "gpj.<U+202E>exe" renders as "exe.jpg" in a file manager. Used for received
# file names and for the receiver name we advertise.
UNSAFE_CHAR = re.compile(r'[/\\\x00]|[\u202a-\u202e\u2066-\u2069\u200b-\u200f\ufeff]|[\x01-\x1f\x7f-\x9f]')
# The config file is the source of truth: ~/.config/airdrop/config.toml,
# written by `omdrop setup` and the configuration commands in `omdrop`.
# CLI flags override individual keys, so the file can be absent entirely.
# Validation lives here, never in whatever writes the file: a GUI is a
# convenience for writing a value, not what makes it safe. Reload = restart.
CONFIG_PATH = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.join(pwd.getpwuid(os.getuid()).pw_dir, '.config'),
                           'airdrop', 'config.toml')
CONFIG_KEYS = ('name', 'model', 'download_dir', 'max_receive_percent')
STRING_CONFIG_KEYS = ('name', 'model', 'download_dir')
DEFAULT_MAX_RECEIVE_PERCENT = 30


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
    for k in STRING_CONFIG_KEYS:
        if k in cfg and (not isinstance(cfg[k], str) or not cfg[k].strip()):
            raise SystemExit(f'{path}: {k} must be a non-empty string')
    if 'max_receive_percent' in cfg:
        validate_receive_percent(cfg['max_receive_percent'])
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


def validate_receive_percent(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 90:
        raise SystemExit('max_receive_percent must be an integer from 1 to 90')
    return value


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


ap = argparse.ArgumentParser(
    description=f'AirDrop receiver. Config: {CONFIG_PATH} '
                '(name, model, download_dir, max_receive_percent); flags override.')
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
ap.add_argument('--max-receive-percent', type=int, default=None,
                help='maximum transfer size as a percentage of currently free output disk space')
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
MAX_RECEIVE_PERCENT = validate_receive_percent(
    args.max_receive_percent if args.max_receive_percent is not None
    else _cfg.get('max_receive_percent', DEFAULT_MAX_RECEIVE_PERCENT))

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


KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB

# Metadata, framing, member count, and idle time have fixed ceilings. Transfer
# bytes use a percentage of free space computed when the upload starts; users
# configure that percentage with `omdrop limit`.
READ_IDLE_TIMEOUT_SECONDS = 30
INITIAL_READ_SECONDS = 60
MIN_READ_BYTES_PER_SECOND = 64 * KIB
MAX_METADATA_BYTES = 1 * MIB
MAX_ARCHIVE_MEMBERS = 512
MIN_FREE_BYTES = 1 * GIB
DISK_CHECK_INTERVAL = 16 * MIB
IO_CHUNK_BYTES = 64 * KIB
MAX_CHUNK_LINE_BYTES = 128
MAX_TRAILER_BYTES = 8 * KIB
MAX_CONCURRENT_UPLOADS = 2
UPLOAD_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_UPLOADS)


class RequestBodyError(ValueError):
    status = 400


class BodyTooLarge(RequestBodyError):
    status = 413


class UploadLimitError(ValueError):
    pass


class ReceiveBudget:
    """Byte budget for one upload, based on current free output-disk space."""

    def __init__(self, dest, percent):
        fs = os.statvfs(dest)
        self.free_bytes = fs.f_bavail * fs.f_frsize
        percent_bytes = self.free_bytes * percent // 100
        reservable = max(0, self.free_bytes - MIN_FREE_BYTES)
        self.byte_limit = min(percent_bytes, reservable)
        self.percent = percent
        if self.byte_limit <= 0:
            raise UploadLimitError(
                f'not enough free space while preserving {MIN_FREE_BYTES} bytes')



class ReadDeadline:
    """Require sustained progress as well as an idle-socket timeout."""

    def __init__(self):
        self.started = time.monotonic()
        self.transferred = 0

    def note(self, size):
        self.transferred += size
        allowed = INITIAL_READ_SECONDS + (
            self.transferred / MIN_READ_BYTES_PER_SECOND)
        if time.monotonic() > self.started + allowed:
            raise socket.timeout('request body did not meet minimum read rate')

class LimitedWriter:
    """Count writes, enforce a byte ceiling, and preserve free disk space."""

    def __init__(self, target, limit, label, limit_error=UploadLimitError,
                 check_disk=False):
        self.target = target
        self.limit = limit
        self.label = label
        self.limit_error = limit_error
        self.check_disk = check_disk
        self.total = 0
        self._until_disk_check = 0

    @property
    def remaining(self):
        return self.limit - self.total

    def write(self, data):
        if not data:
            return 0
        size = len(data)
        if size > self.remaining:
            raise self.limit_error(
                f'{self.label} exceeds {self.limit}-byte limit')
        if self.check_disk and size >= self._until_disk_check:
            fs = os.fstatvfs(self.target.fileno())
            free = fs.f_bavail * fs.f_frsize
            if free < MIN_FREE_BYTES + DISK_CHECK_INTERVAL:
                raise UploadLimitError(
                    f'{self.label} stopped to preserve {MIN_FREE_BYTES} bytes free')
            self._until_disk_check = DISK_CHECK_INTERVAL
        written = self.target.write(data)
        if written != size:
            raise OSError(f'short write for {self.label}: {written} of {size} bytes')
        self.total += written
        self._until_disk_check -= written
        return written


def body_plan(headers, limit):
    """Validate framing before sending 100 Continue or reading any body."""
    transfer = headers.get('Transfer-Encoding', '')
    if transfer:
        encodings = [part.strip().lower() for part in transfer.split(',')]
        if encodings != ['chunked']:
            raise RequestBodyError(f'unsupported Transfer-Encoding: {transfer!r}')
        return 'chunked', None
    value = headers.get('Content-Length')
    if value is None:
        raise RequestBodyError('request body needs Content-Length or chunked encoding')
    try:
        length = int(value)
    except (TypeError, ValueError):
        raise RequestBodyError(f'invalid Content-Length: {value!r}') from None
    if length < 0:
        raise RequestBodyError(f'invalid Content-Length: {value!r}')
    if length > limit:
        raise BodyTooLarge(f'request body exceeds {limit}-byte limit')
    return 'length', length


def _copy_exact(source, writer, size, truncated, deadline):
    left = size
    while left:
        data = source.read(min(left, IO_CHUNK_BYTES))
        if not data:
            raise RequestBodyError(truncated)
        deadline.note(len(data))
        writer.write(data)
        left -= len(data)


def _read_chunked(rfile, writer, deadline):
    while True:
        line = rfile.readline(MAX_CHUNK_LINE_BYTES + 1)
        deadline.note(len(line))
        if not line or len(line) > MAX_CHUNK_LINE_BYTES or not line.endswith(b'\n'):
            raise RequestBodyError('invalid or overlong chunk-size line')
        token = line.split(b';', 1)[0].strip()
        try:
            size = int(token, 16)
        except ValueError:
            raise RequestBodyError(f'invalid chunk size: {token[:40]!r}') from None
        if size < 0:
            raise RequestBodyError('negative chunk size')
        if size == 0:
            trailers = 0
            while True:
                line = rfile.readline(MAX_CHUNK_LINE_BYTES + 1)
                deadline.note(len(line))
                trailers += len(line)
                if (not line or len(line) > MAX_CHUNK_LINE_BYTES
                        or trailers > MAX_TRAILER_BYTES):
                    raise RequestBodyError('invalid or overlong chunk trailers')
                if line in (b'\r\n', b'\n'):
                    return
        if size > writer.remaining:
            raise BodyTooLarge(
                f'request body exceeds {writer.limit}-byte limit')
        _copy_exact(rfile, writer, size, 'truncated chunk data', deadline)
        ending = rfile.read(2)
        deadline.note(len(ending))
        if ending != b'\r\n':
            raise RequestBodyError('chunk data missing CRLF')


def read_request_body(rfile, headers, target, limit, *, check_disk=False,
                      plan=None, deadline=None):
    """Stream one framed request body into target under byte and time limits."""
    mode, length = plan or body_plan(headers, limit)
    deadline = deadline or ReadDeadline()
    writer = LimitedWriter(target, limit, 'request body', BodyTooLarge,
                           check_disk)
    if mode == 'chunked':
        _read_chunked(rfile, writer, deadline)
    else:
        _copy_exact(rfile, writer, length, 'truncated request body', deadline)
    return writer.total


def read_small_body(rfile, headers, limit=MAX_METADATA_BYTES):
    body = io.BytesIO()
    read_request_body(rfile, headers, body, limit)
    return body.getvalue()


def _inflate(decoder, data, writer):
    while data:
        output = decoder.decompress(
            data, min(IO_CHUNK_BYTES, writer.remaining + 1))
        writer.write(output)
        if decoder.unconsumed_tail:
            data = decoder.unconsumed_tail
        else:
            return


def _decode_compressed_block(source, size, writer, *, wbits=zlib.MAX_WBITS):
    decoder = zlib.decompressobj(wbits)
    left = size
    while left:
        data = source.read(min(left, IO_CHUNK_BYTES))
        if not data:
            raise ValueError(f'truncated compressed block: {left} bytes missing')
        left -= len(data)
        _inflate(decoder, data, writer)
        if decoder.unused_data:
            raise ValueError('compressed block has trailing data')
    writer.write(decoder.flush())
    if not decoder.eof:
        raise ValueError('truncated compressed block')


def decode_dvzip(source, target, byte_limit):
    """Stream a gzip or block-framed DVZip body into a bounded CPIO stream."""
    writer = LimitedWriter(target, byte_limit,
                           'decompressed archive', check_disk=True)
    source.seek(0)
    magic = source.read(2)
    source.seek(0)
    if magic == b'\x1f\x8b':
        decoder = zlib.decompressobj(31)
        while True:
            data = source.read(IO_CHUNK_BYTES)
            if not data:
                break
            _inflate(decoder, data, writer)
            if decoder.unused_data:
                raise ValueError('gzip body has trailing data')
        writer.write(decoder.flush())
        if not decoder.eof:
            raise ValueError('truncated gzip body')
        return writer.total

    while True:
        header = source.read(4)
        if not header:
            return writer.total
        if len(header) != 4:
            raise ValueError(f'truncated DVZip block header: {len(header)} bytes')
        value = int.from_bytes(header, 'big')
        size = value & 0x7fffffff
        if value & 0x80000000:
            left = size
            while left:
                data = source.read(min(left, IO_CHUNK_BYTES))
                if not data:
                    raise ValueError(f'truncated stored block: {left} bytes missing')
                writer.write(data)
                left -= len(data)
        else:
            _decode_compressed_block(source, size, writer)


def safe_name(name, fallback):
    """Return one non-hidden filesystem component from sender-controlled text."""
    name = unicodedata.normalize('NFC', name.replace('\\', '/').rsplit('/', 1)[-1])
    name = UNSAFE_CHAR.sub('', name).strip().lstrip('.')
    stem, ext = os.path.splitext(name)
    while len((stem + ext).encode('utf-8')) > 255 and stem:
        stem = stem[:-1]
    return (stem + ext) or fallback


def open_new(dest, name):
    """Create dest/name atomically without clobbering an existing file."""
    stem, ext = os.path.splitext(name)
    for i in range(10000):
        cand = name if i == 0 else f'{stem}-{i}{ext}'
        try:
            fd = os.open(os.path.join(dest, cand),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        return cand, os.fdopen(fd, 'wb')
    raise FileExistsError(name)


class ExtractionBudget:
    def __init__(self, byte_limit):
        self.byte_limit = byte_limit
        self.members = 0
        self.output_bytes = 0

    def count_member(self):
        self.members += 1
        if self.members > MAX_ARCHIVE_MEMBERS:
            raise UploadLimitError(
                f'archive exceeds {MAX_ARCHIVE_MEMBERS}-member limit')


class ExtractedFileWriter(LimitedWriter):
    def __init__(self, target, budget, name):
        super().__init__(target, budget.byte_limit, f'file {name!r}',
                         check_disk=True)
        self.budget = budget

    def write(self, data):
        if self.budget.output_bytes + len(data) > self.budget.byte_limit:
            raise UploadLimitError(
                f'archive output exceeds {self.budget.byte_limit}-byte limit')
        written = super().write(data)
        self.budget.output_bytes += written
        return written


def store_upload(raw, dest, tid, receive_budget):
    """Decode and extract regular files, removing every output on failure."""
    created = []
    written = []
    try:
        with tempfile.TemporaryFile(dir=dest) as cpio:
            decoded = decode_dvzip(raw, cpio, receive_budget.byte_limit)
            # The wire copy is no longer needed. Release its blocks before the
            # extracted files begin consuming space.
            raw.seek(0)
            raw.truncate(0)
            cpio.seek(0)
            magic = cpio.read(6)
            cpio.seek(0)
            logging.info('upload %s: decoded to %d bytes, magic %r',
                         tid, decoded, magic)
            budget = ExtractionBudget(receive_budget.byte_limit)
            with libarchive.stream_reader(cpio) as archive:
                for entry in archive:
                    budget.count_member()
                    if not entry.isfile:
                        continue
                    if os.path.basename(entry.pathname).startswith('._'):
                        continue
                    declared = getattr(entry, 'size', 0)
                    if declared > budget.byte_limit:
                        raise UploadLimitError(
                            f'file {entry.pathname!r} declares {declared} bytes')
                    if budget.output_bytes + max(declared, 0) > budget.byte_limit:
                        raise UploadLimitError(
                            f'archive declares more than {budget.byte_limit} output bytes')
                    base = safe_name(entry.pathname, 'file')
                    name, output = open_new(dest, base)
                    created.append(name)
                    with output:
                        sink = ExtractedFileWriter(output, budget, name)
                        for block in entry.get_blocks():
                            sink.write(block)
                    written.append(name)
    except Exception:
        for name in reversed(created):
            try:
                os.unlink(os.path.join(dest, name))
            except FileNotFoundError:
                pass
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

    def setup(self):
        # Applies to request headers and every body read. A peer that stops
        # transmitting cannot hold one server thread forever.
        self.request.settimeout(READ_IDLE_TIMEOUT_SECONDS)
        super().setup()

    def handle_expect_100(self):
        # BaseHTTPRequestHandler would acknowledge an upload before its framing
        # and disk-relative limit are validated. Defer only Upload; the other
        # endpoints still need the normal automatic acknowledgement.
        if self.path == '/Upload':
            return True
        return super().handle_expect_100()

    def reject(self, status, reason):
        logging.warning('%s rejected: %s', self.path, reason)
        self.send_response(status)
        self.send_header('Content-Length', '0')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

    def do_POST(self):
        if PEERCERT_DIR:
            # Research instrumentation must never be able to fail a transfer.
            # It did once: a capture bug raised inside do_POST and killed three
            # real iPhone connections before /Ask was ever parsed, which looked
            # exactly like a receiver that refused them.
            try:
                _capture_peer_cert(self.connection)
            except Exception:
                logging.exception('peer certificate capture failed; continuing')
        if self.path in ('/Discover', '/Ask'):
            try:
                body = read_small_body(self.rfile, self.headers)
            except socket.timeout:
                self.reject(408, 'request body read timed out')
                return
            except RequestBodyError as e:
                self.reject(e.status, str(e))
                return
            orig = self.rfile
            self.rfile = io.BytesIO(body)
            if 'Transfer-Encoding' in self.headers:
                del self.headers['Transfer-Encoding']
            self.headers['Content-Length'] = str(len(body))
            logging.debug('buffered bounded %d-byte body for %s',
                          len(body), self.path)
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
        items = ask.get('Items') or []
        if len(items) > MAX_ARCHIVE_MEMBERS:
            self.reject(413, f'Ask exceeds {MAX_ARCHIVE_MEMBERS}-item limit')
            return
        # Contacts Only is enforced here rather than at /Discover, for two
        # reasons measured on 2026-09-18: a sender presents its Apple-issued
        # client certificate on /Ask and never on /Discover, and /Ask is the
        # moment a transfer is actually proposed. Refusing here costs the
        # sender a clear error instead of an unexplained disappearance.
        mode = contacts.visibility()
        if mode == 'contacts':
            peer_cert = None
            try:
                peer_cert = self.connection.getpeercert(binary_form=True)
            except (AttributeError, ValueError):
                pass
            outcome, detail = contacts.decide(ask.get('SenderRecordData'), peer_cert)
            if outcome != contacts.ACCEPT:
                # No identifier is logged: the record's hashes are reversible
                # to a phone number by brute force.
                logging.warning('ask REFUSED (contacts only): %s%s',
                                contacts.WHY[outcome],
                                f' [{detail}]' if detail else '')
                self.send_response(403)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            logging.info('ask accepted: %s', contacts.WHY[outcome])
            # Remembered per connection: Ask and Upload share one TLS session,
            # so an Upload that never passed an Ask has nothing vouching for it.
            self._contacts_ok = True

        # A `links` transfer is Ask-only: with SUPPORTS_URL (0x01) advertised,
        # iOS puts the URL straight into Items and sends no /Upload at all, so
        # accepting the Ask IS the transfer. Store it, or the link is lost.
        # NEVER open it: in Everyone mode any device in range could otherwise
        # make this machine visit an arbitrary URL. A file the user clicks is
        # the whole interaction we are willing to offer.
        if 'links' in (ask.get('TransferType') or {}):
            for item in items:
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
        # An Upload that never passed an Ask has nothing vouching for it, and
        # the Upload itself carries no validation record to judge.
        if contacts.visibility() == 'contacts' and not getattr(self, '_contacts_ok', False):
            self.reject(403, 'contacts-only upload has no accepted Ask')
            return
        ctype = self.headers.get('Content-Type', '').split(';')[0].strip().lower()
        if ctype != 'application/x-dvzip':
            self.reject(415, f'unsupported Content-Type {ctype!r}')
            return
        if not UPLOAD_SLOTS.acquire(blocking=False):
            self.reject(503, 'too many uploads in progress')
            return
        tid = safe_name(self.headers.get('TransferID', ''), 'upload')
        try:
            receive_budget = ReceiveBudget(DEST, MAX_RECEIVE_PERCENT)
            plan = body_plan(self.headers, receive_budget.byte_limit)
            logging.info(
                'upload %s: %d%% of %d free bytes allows %d bytes',
                tid, receive_budget.percent, receive_budget.free_bytes,
                receive_budget.byte_limit)
            if self.headers.get('Expect', '').lower() == '100-continue':
                self.send_response_only(100)
                self.end_headers()
            with tempfile.TemporaryFile(dir=DEST) as raw:
                wire_bytes = read_request_body(
                    self.rfile, self.headers, raw, receive_budget.byte_limit,
                    check_disk=True, plan=plan)
                raw.seek(0)
                logging.info('upload %s: %d bytes dvzip (TotalBytes %s)',
                             tid, wire_bytes, self.headers.get('TotalBytes'))
                written = store_upload(raw, DEST, tid, receive_budget)
            logging.info('upload %s: stored %s', tid, ', '.join(written))
        except socket.timeout:
            self.reject(408, 'request body read timed out')
            return
        except (BodyTooLarge, UploadLimitError) as e:
            self.reject(413, str(e))
            return
        except RequestBodyError as e:
            self.reject(e.status, str(e))
            return
        except OSError as e:
            self.reject(507, f'storage failed: {e}')
            return
        except Exception as e:
            self.reject(422, f'upload is not a valid bounded DVZip archive: {e}')
            return
        finally:
            UPLOAD_SLOTS.release()
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

# Ask the peer for a client certificate. Required for Contacts Only, because
# that certificate is what binds a sender's Apple-signed validation record to
# the live connection; a record on its own is handed to any prober that asks
# and would otherwise be replayable. opendrop uses CERT_NONE and so never sees
# one.
#
# Two things this has to get right, both learned the hard way on 2026-09-19:
#
# CERT_OPTIONAL means "a certificate may be absent", NOT "a certificate is
# tolerated". A peer that does present one has it verified, and a failure
# aborts the handshake with unknown_ca before any request is read. The context
# opendrop builds trusts only Apple's root, while an Apple leaf is issued by
# the "Apple Application Integration Certification Authority" intermediate, so
# every Apple sender failed to chain and was refused at TLS. The intermediates
# vendored beside contacts.py are the missing link; certificate_account()
# already passes them for the same chain.
#
# Everyone mode goes back to CERT_NONE. Requesting a certificate there buys
# nothing -- no code consults it -- and it can only turn a sender whose
# certificate does not chain to Apple, such as a stock opendrop peer, into a
# handshake failure. The old comment claimed Everyone mode was unaffected;
# that was true of intent and false of behaviour.
#
# Measured scope of the defect, 2026-09-19: a Mac verified successfully against
# a root-only store before this fix, which is only possible if it supplied the
# intermediate, so macOS senders were never affected. iOS was measured directly
# and sends a 2-certificate chain, leaf + AAI intermediate. What broke was any
# sender presenting a bare leaf, which includes this project's own sender --
# and therefore every loopback test of the Contacts Only gate.
#
# OMDROP_PEERCERT saves what arrives, for research; it keeps the certificate
# request alive in Everyone mode so a capture run still sees one.
PEERCERT_DIR = os.environ.get('OMDROP_PEERCERT')
_plain_context = config.get_ssl_context


def _requesting_context():
    ctx = _plain_context()
    if contacts.visibility() != 'contacts' and not PEERCERT_DIR:
        return ctx
    for intermediate in contacts.apple_intermediates():
        try:
            ctx.load_verify_locations(cafile=intermediate)
        except (OSError, ssl.SSLError):
            logging.warning('could not load Apple intermediate %s; a sender '
                            'presenting an Apple certificate may be refused '
                            'at TLS', os.path.basename(intermediate))
    ctx.verify_mode = ssl.CERT_OPTIONAL
    return ctx


config.get_ssl_context = _requesting_context

if PEERCERT_DIR:
    os.makedirs(PEERCERT_DIR, exist_ok=True)

    def _capture_peer_cert(conn):
        try:
            der = conn.getpeercert(binary_form=True)
        except (AttributeError, ValueError):
            return
        path = os.path.join(PEERCERT_DIR,
                            f'client-{time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}.der')
        if der:
            with open(path, 'wb') as fh:
                fh.write(der)
            logging.warning('captured %d-byte client certificate -> %s', len(der), path)
            # getpeercert() returns the LEAF only, so it cannot say whether the
            # sender supplied its issuing intermediates. That distinction
            # decides whether a receiver trusting only Apple's root can build
            # the chain at all, which is exactly what the unknown_ca defect
            # turned on.
            try:
                chain = conn.get_unverified_chain() or []
            except (AttributeError, ValueError, NotImplementedError):
                chain = []
            if chain:
                logging.warning('client sent a %d-certificate chain', len(chain))
                for depth, cert in enumerate(chain):
                    # get_unverified_chain() yields DER bytes on this build;
                    # older/other builds yield Certificate objects. Accept both,
                    # and never let a research capture break a live transfer.
                    blob = cert if isinstance(cert, (bytes, bytearray)) else \
                        cert.public_bytes(ssl.DER)
                    with open(f'{path[:-4]}-depth{depth}.der', 'wb') as fh:
                        fh.write(blob)
            else:
                logging.warning('client chain unavailable; leaf only')
        else:
            logging.warning('peer presented NO client certificate')

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
