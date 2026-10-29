#!/usr/bin/env python3
"""Diary Server: keeps the Diary entries of all your retro machines in one place.

A tiny HTTP/1.0 server for the Diary apps (classic Mac OS, AmigaOS 3.2,
Mac OS X, NeXTSTEP, ...).
Entries are stored as UTF-8 text files named YYYY-MM-DD with LF line ends;
every client gets and sends them in its own character set and line ends.
See PROTOCOL.md for the requests. Only the Python standard library is used.

    python3 diary_server.py --dir ~/diary-entries [--port 8642] [--key SECRET]
"""
import argparse
import codecs
import os
import re
import tempfile
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote_to_bytes, urlsplit

VERSION = '1.0'
MAX_ENTRY_BYTES = 64 * 1024
ENTRY_NAME = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')

# Character sets a client may ask for, and the line end each uses by default.
CHARSETS = {
    'utf-8': ('utf-8', '\n'),
    'latin1': ('latin-1', '\n'),        # AmigaOS
    'macroman': ('mac_roman', '\r'),    # classic Mac OS
    'nextstep': ('nextstep', '\n'),     # NeXTSTEP
}
LINE_ENDS = {'lf': '\n', 'cr': '\r', 'crlf': '\r\n'}

# NeXTSTEP's own character set, which Python doesn't know: bytes 0x80..0xFF
# as in Unicode's NEXTSTEP.TXT (0x00..0x7F are ASCII; 0xFE and 0xFF are
# unused). https://www.unicode.org/Public/MAPPINGS/VENDORS/NEXT/NEXTSTEP.TXT
_NEXTSTEP_HIGH = (
    '\u00a0\u00c0\u00c1\u00c2\u00c3\u00c4\u00c5\u00c7\u00c8\u00c9\u00ca\u00cb\u00cc\u00cd\u00ce\u00cf'  # 80
    '\u00d0\u00d1\u00d2\u00d3\u00d4\u00d5\u00d6\u00d9\u00da\u00db\u00dc\u00dd\u00de\u00b5\u00d7\u00f7'  # 90
    '\u00a9\u00a1\u00a2\u00a3\u2044\u00a5\u0192\u00a7\u00a4\u2019\u201c\u00ab\u2039\u203a\ufb01\ufb02'  # A0
    '\u00ae\u2013\u2020\u2021\u00b7\u00a6\u00b6\u2022\u201a\u201e\u201d\u00bb\u2026\u2030\u00ac\u00bf'  # B0
    '\u00b9\u02cb\u00b4\u02c6\u02dc\u00af\u02d8\u02d9\u00a8\u00b2\u02da\u00b8\u00b3\u02dd\u02db\u02c7'  # C0
    '\u2014\u00b1\u00bc\u00bd\u00be\u00e0\u00e1\u00e2\u00e3\u00e4\u00e5\u00e7\u00e8\u00e9\u00ea\u00eb'  # D0
    '\u00ec\u00c6\u00ed\u00aa\u00ee\u00ef\u00f0\u00f1\u0141\u00d8\u0152\u00ba\u00f2\u00f3\u00f4\u00f5'  # E0
    '\u00f6\u00e6\u00f9\u00fa\u00fb\u0131\u00fc\u00fd\u0142\u00f8\u0153\u00df\u00fe\u00ff\ufffd\ufffd'  # F0
)
_NEXTSTEP_DECODE = ''.join(map(chr, range(0x80))) + _NEXTSTEP_HIGH
_NEXTSTEP_ENCODE = codecs.charmap_build(_NEXTSTEP_DECODE[:0xFE] + '\ufffe\ufffe')


def _find_codec(name):
    if name != 'nextstep':
        return None
    return codecs.CodecInfo(
        name='nextstep',
        encode=lambda text, errors='strict': codecs.charmap_encode(text, errors, _NEXTSTEP_ENCODE),
        decode=lambda data, errors='strict': codecs.charmap_decode(data, errors, _NEXTSTEP_DECODE))


codecs.register(_find_codec)

# Plain look-alikes for characters a client's character set lacks.
LOOK_ALIKES = {
    '‘': "'", '’': "'", '‚': ',', '“': '"', '”': '"',
    '„': '"', '–': '-', '—': '-', '…': '...', '•': '*',
    '†': '+', '‹': '<', '›': '>', '⁄': '/', '€': 'EUR',
    '™': '(TM)', '­': '-', '×': 'x', '¹': '1', '²': '2',
    '³': '3', '¼': '1/4', '½': '1/2', '¾': '3/4',
    'Œ': 'OE', 'œ': 'oe', 'ﬁ': 'fi', 'ﬂ': 'fl',
}

_write_lock = threading.Lock()


def valid_day(name):
    """True for an existing date written as YYYY-MM-DD."""
    m = ENTRY_NAME.match(name)
    if not m:
        return False
    y, mo, d = (int(g) for g in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return False
    days = [31, 29 if (y % 4 == 0 and y % 100 != 0) or y % 400 == 0 else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return d <= days[mo - 1]


def normalize(text):
    """Canonical form: LF line ends, no trailing line ends."""
    return text.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n')


def version_of(data):
    """Version tag of a stored entry: the CRC-32 of its bytes."""
    return '%08x' % zlib.crc32(data)


def encode_for(text, codec, eol):
    if codec != 'utf-8':
        text = ''.join(c if c in '\n' or _encodable(c, codec)
                       else LOOK_ALIKES.get(c, '?') for c in text)
    return text.replace('\n', eol).encode(codec, errors='replace')


def _encodable(c, codec):
    try:
        c.encode(codec)
        return True
    except UnicodeEncodeError:
        return False


def detect_and_decode(data):
    """Decodes an entry file written by one of the Diary apps (for --import)."""
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        pass
    # Diary for AmigaOS ends its files with LF; the Mac one writes CR line
    # ends, and only MacRoman uses the bytes 0x80..0x9F.
    if b'\n' not in data or any(0x80 <= b < 0xA0 for b in data):
        return data.decode('mac_roman')
    return data.decode('latin-1')


class Store:
    def __init__(self, directory):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def path(self, day):
        return os.path.join(self.directory, day)

    def days(self):
        return sorted((n for n in os.listdir(self.directory) if valid_day(n)),
                      reverse=True)

    def read(self, day):
        try:
            with open(self.path(day), 'rb') as f:
                return f.read()
        except FileNotFoundError:
            return None

    def write(self, day, text):
        """Stores text (str) for day; empty text deletes the entry.
        Returns the new version, or None if the entry was deleted."""
        text = normalize(text)
        if not text:
            try:
                os.remove(self.path(day))
            except FileNotFoundError:
                pass
            return None
        data = (text + '\n').encode('utf-8')
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix='.tmp-')
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp, self.path(day))
        return version_of(data)

    def search(self, needle):
        needle = needle.casefold()
        return [day for day in self.days()
                if needle in self.read(day).decode('utf-8').casefold()]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = 'DiaryServer/' + VERSION
    store = None
    key = None

    # ---------- helpers ----------

    def reply(self, status, body=b'', headers=None):
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def fail(self, status, message):
        self.reply(status, (message + '\n').encode('ascii'))

    def parse(self):
        """Splits the request; returns (path parts, query, codec, eol) or None."""
        url = urlsplit(self.path)
        query = parse_qs(url.query, keep_blank_values=True)
        charset = query.get('charset', ['utf-8'])[0].lower()
        if charset not in CHARSETS:
            self.fail(400, 'Unknown charset; use utf-8, latin1, macroman or nextstep.')
            return None
        codec, eol = CHARSETS[charset]
        if 'eol' in query:
            eol = LINE_ENDS.get(query['eol'][0].lower())
            if eol is None:
                self.fail(400, 'Unknown eol; use lf, cr or crlf.')
                return None
        if self.key is not None:
            given = self.headers.get('X-Diary-Key') or query.get('key', [None])[0]
            if given != self.key:
                self.fail(403, 'Wrong or missing key.')
                return None
        parts = [p for p in url.path.split('/') if p]
        return parts, url.query, codec, eol

    def entry_day(self, parts):
        if len(parts) == 2 and parts[0] == 'entries' and valid_day(parts[1]):
            return parts[1]
        self.fail(404, 'No such resource.')
        return None

    def body(self):
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            self.fail(411, 'Content-Length required.')
            return None
        if not 0 <= length <= MAX_ENTRY_BYTES:
            self.fail(413, 'Entry too long.')
            return None
        return self.rfile.read(length)

    # ---------- requests ----------

    def do_GET(self):
        request = self.parse()
        if request is None:
            return
        parts, query, codec, eol = request

        if not parts:
            self.reply(200, ('Diary Server %s\n%d entries\n'
                             % (VERSION, len(self.store.days()))).encode('ascii'))
        elif parts == ['entries']:
            lines = ''.join('%s %s\n' % (day, version_of(self.store.read(day)))
                            for day in self.store.days())
            self.reply(200, lines.encode('ascii'))
        elif parts == ['search']:
            raw = [p[2:] for p in query.split('&') if p.startswith('q=')]
            needle = unquote_to_bytes(raw[0].replace('+', ' ')).decode(codec, errors='replace') if raw else ''
            if not needle:
                self.fail(400, 'Missing q.')
                return
            self.reply(200, ''.join(d + '\n' for d in self.store.search(needle)).encode('ascii'))
        else:
            day = self.entry_day(parts)
            if day is None:
                return
            data = self.store.read(day)
            if data is None:
                self.fail(404, 'No entry for that day.')
                return
            text = normalize(data.decode('utf-8'))
            self.reply(200, encode_for(text, codec, eol),
                       {'X-Diary-Version': version_of(data)})

    do_HEAD = do_GET

    def do_PUT(self):
        request = self.parse()
        if request is None:
            return
        parts, _, codec, _ = request
        day = self.entry_day(parts)
        if day is None:
            return
        data = self.body()
        if data is None:
            return
        with _write_lock:
            current = self.store.read(day)
            current_version = version_of(current) if current is not None else 'none'
            base = self.headers.get('X-Diary-Base')
            if base is not None and base != current_version:
                self.reply(409, b'The entry was changed elsewhere.\n',
                           {'X-Diary-Version': current_version})
                return
            version = self.store.write(day, data.decode(codec, errors='replace'))
        self.reply(200, ((version or 'none') + '\n').encode('ascii'),
                   {'X-Diary-Version': version or 'none'})

    def do_DELETE(self):
        request = self.parse()
        if request is None:
            return
        day = self.entry_day(request[0])
        if day is None:
            return
        with _write_lock:
            current = self.store.read(day)
            base = self.headers.get('X-Diary-Base')
            if current is not None and base is not None and base != version_of(current):
                self.reply(409, b'The entry was changed elsewhere.\n',
                           {'X-Diary-Version': version_of(current)})
                return
            self.store.write(day, '')
        self.reply(200, b'none\n', {'X-Diary-Version': 'none'})

    def log_message(self, fmt, *args):
        print('%s - %s' % (self.address_string(), fmt % args), flush=True)


def import_entries(store, source):
    """Copies Diary entry files from a Mac or Amiga entries folder."""
    count = 0
    for name in sorted(os.listdir(source)):
        if valid_day(name):
            with open(os.path.join(source, name), 'rb') as f:
                if store.write(name, detect_and_decode(f.read())) is not None:
                    count += 1
    print('Imported %d entries from %s' % (count, source))


def main():
    parser = argparse.ArgumentParser(description='Diary Server')
    parser.add_argument('--dir', required=True, help='folder for the entries')
    parser.add_argument('--port', type=int, default=8642)
    parser.add_argument('--bind', default='', help='address to listen on (default: all)')
    parser.add_argument('--key', default=os.environ.get('DIARY_KEY'),
                        help='require this key from clients (or set DIARY_KEY)')
    parser.add_argument('--import', dest='import_dir', metavar='FOLDER',
                        help='import the entries of a Mac or Amiga "Diary Entries" folder and exit')
    args = parser.parse_args()

    store = Store(os.path.expanduser(args.dir))
    if args.import_dir:
        import_entries(store, os.path.expanduser(args.import_dir))
        return

    Handler.store = store
    Handler.key = args.key
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print('Diary Server %s on port %d, entries in %s'
          % (VERSION, args.port, store.directory), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
