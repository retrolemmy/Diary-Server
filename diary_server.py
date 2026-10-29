#!/usr/bin/env python3
"""Diary Server: keeps the Diary entries of all your retro machines in one place.

A tiny HTTP/1.0 server for the Diary apps (classic Mac OS, AmigaOS 3.2,
Mac OS X, NeXTSTEP, ...).
Entries are stored as UTF-8 text files named YYYY-MM-DD with LF line ends;
every client gets and sends them in its own character set and line ends.
See PROTOCOL.md for the requests; browsers get a web page with a Diary of
their own. Only the Python standard library is used.

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

VERSION = '1.0.1'
MAX_ENTRY_BYTES = 64 * 1024
ENTRY_NAME = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')

# Character sets a client may ask for, and the line end each uses by default.
CHARSETS = {
    'utf-8': ('utf-8', '\n'),
    'latin1': ('latin-1', '\n'),        # AmigaOS
    'macroman': ('mac_roman', '\r'),    # classic Mac OS
    'nextstep': ('nextstep', '\n'),     # NeXTSTEP
    'windows-1252': ('cp1252', '\r\n'), # Windows 95/98
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

    def reply(self, status, body=b'', headers=None, content_type='text/plain'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
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
            self.fail(400, 'Unknown charset; use utf-8, latin1, macroman, nextstep or windows-1252.')
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
        # A browser asking for / gets the web page (which holds no entries,
        # so it needs no key); everyone else gets the status text.
        if urlsplit(self.path).path == '/' and 'text/html' in self.headers.get('Accept', ''):
            self.reply(200, WEB_PAGE.encode('utf-8'), {'Cache-Control': 'no-cache'},
                       'text/html; charset=utf-8')
            return
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


# The web page served to browsers at /: a Diary in the browser, using the
# same requests as the retro clients (in UTF-8).
WEB_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Diary</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect x='3' y='2' width='11' height='13' fill='%23fff' stroke='%23000'/%3E%3Crect x='1' y='0.5' width='11' height='13' fill='%23c00' stroke='%23000'/%3E%3Crect x='2' y='1' width='2' height='12' fill='%23600'/%3E%3Crect x='6' y='3' width='5' height='3' fill='%23fff' stroke='%23c90' stroke-width='.6'/%3E%3Crect x='10' y='7' width='3' height='3' fill='%23fc0' stroke='%23000' stroke-width='.6'/%3E%3C/svg%3E">
<style>
:root {
  --bg: #e8e6e1; --panel: #fbfaf7; --text: #1d1c1a; --muted: #6f6b64;
  --line: #c9c5bc; --accent: #b3141b; --accent-text: #fff; --entry: #1d1c1a;
  --field: #fff; --shadow: 0 1px 2px rgba(0,0,0,.08);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #1b1a19; --panel: #262523; --text: #ecebe8; --muted: #a29e96;
    --line: #403e3a; --accent: #e0484e; --accent-text: #fff; --entry: #ecebe8;
    --field: #1f1e1c; --shadow: none;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  background: var(--bg); color: var(--text);
  font: 15px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  display: flex; flex-direction: column;
}
header {
  display: flex; align-items: center; gap: 10px; padding: 10px 16px;
  border-bottom: 1px solid var(--line); background: var(--panel);
}
header h1 { font-size: 17px; margin: 0; font-weight: 650; }
header .where { color: var(--muted); font-size: 13px; margin-left: auto; text-align: right; }
main {
  flex: 1; min-height: 0; display: grid; gap: 16px; padding: 16px;
  grid-template-columns: 260px 1fr;
}
.side { display: flex; flex-direction: column; gap: 12px; min-height: 0; }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  box-shadow: var(--shadow);
}
.cal { padding: 10px; }
.cal .head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
.cal .head strong { font-size: 14px; }
.cal table { width: 100%; border-collapse: collapse; table-layout: fixed; }
.cal th { font-size: 11px; color: var(--muted); font-weight: 600; padding: 2px 0; }
.cal td { padding: 1px; text-align: center; }
.cal td button {
  width: 100%; aspect-ratio: 1.25; border: 1px solid transparent; border-radius: 5px;
  background: none; color: var(--text); font: inherit; font-size: 13px; cursor: pointer;
}
.cal td button:hover { background: var(--bg); }
.cal td button.has { font-weight: 750; }
.cal td button.today { border-color: var(--text); }
.cal td button.open { background: var(--accent); color: var(--accent-text); }
.search { display: flex; gap: 6px; }
.search input { flex: 1; min-width: 0; }
.list { flex: 1; min-height: 120px; overflow: auto; padding: 4px; }
.list button {
  display: block; width: 100%; text-align: left; border: 0; border-radius: 5px;
  background: none; color: var(--text); font: inherit; font-size: 14px; padding: 5px 8px; cursor: pointer;
  font-variant-numeric: tabular-nums;
}
.list button:hover { background: var(--bg); }
.list button.open { background: var(--accent); color: var(--accent-text); }
.list .empty { color: var(--muted); padding: 8px; font-size: 13px; }
.day { display: flex; flex-direction: column; min-height: 0; padding: 12px; gap: 10px; }
.day .bar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.day h2 { font-size: 17px; margin: 0 0 0 8px; font-weight: 650; }
textarea {
  flex: 1; min-height: 240px; width: 100%; resize: none; padding: 12px;
  border: 1px solid var(--line); border-radius: 6px; background: var(--field); color: var(--entry);
  font: 16px/1.55 Georgia, "Times New Roman", serif;
}
textarea:focus, input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.foot { display: flex; align-items: center; gap: 8px; }
.status { color: var(--muted); font-size: 13px; margin-right: auto; }
.status.error { color: var(--accent); }
input {
  font: inherit; font-size: 14px; padding: 5px 8px; border: 1px solid var(--line);
  border-radius: 5px; background: var(--field); color: var(--text);
}
.btn {
  font: inherit; font-size: 14px; padding: 5px 12px; border: 1px solid var(--line); border-radius: 5px;
  background: var(--panel); color: var(--text); cursor: pointer;
}
.btn:hover { background: var(--bg); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-text); }
.btn:disabled { opacity: .5; cursor: default; }
.icon { padding: 5px 9px; }
.keybox { padding: 16px; max-width: 420px; margin: 40px auto; display: none; }
.keybox p { margin: 0 0 10px; }
.keybox form { display: flex; gap: 6px; }
.keybox input { flex: 1; }
@media (max-width: 720px) {
  html, body { height: auto; }
  main { flex: none; grid-template-columns: 1fr; padding: 12px 16px; }
  .side { min-height: auto; }
  .list { flex: none; max-height: 200px; }
  textarea { flex: none; min-height: 50vh; }
}
</style>
</head>
<body>
<header>
  <h1>Diary</h1>
  <span class="where" id="where"></span>
</header>

<div class="card keybox" id="keybox">
  <p><strong>This Diary Server needs a key.</strong></p>
  <p id="keymsg">Enter the key it was started with; this browser remembers it.</p>
  <form id="keyform"><input id="key" type="password" autocomplete="current-password" aria-label="Key">
    <button class="btn primary">Open</button></form>
</div>

<main id="app" hidden>
  <div class="side">
    <div class="card cal">
      <div class="head">
        <button class="btn icon" id="prevMonth" aria-label="Previous month">‹</button>
        <strong id="month"></strong>
        <button class="btn icon" id="nextMonth" aria-label="Next month">›</button>
      </div>
      <table><thead><tr><th>S</th><th>M</th><th>T</th><th>W</th><th>T</th><th>F</th><th>S</th></tr></thead>
        <tbody id="days"></tbody></table>
    </div>
    <form class="search" id="searchform">
      <input id="search" type="search" placeholder="Find in all entries" aria-label="Find">
      <button class="btn">Find</button>
    </form>
    <div class="card list" id="list"></div>
  </div>

  <div class="card day">
    <div class="bar">
      <button class="btn icon" id="prevDay" aria-label="Previous day">‹</button>
      <button class="btn" id="today">Today</button>
      <button class="btn icon" id="nextDay" aria-label="Next day">›</button>
      <h2 id="date"></h2>
    </div>
    <textarea id="text" spellcheck="true" aria-label="Entry"></textarea>
    <div class="foot">
      <span class="status" id="status"></span>
      <button class="btn primary" id="save">Save</button>
    </div>
  </div>
</main>

<script>
"use strict";
const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"];
const $ = id => document.getElementById(id);

let key = "";
try { key = localStorage.getItem("diaryKey") || ""; } catch (e) {}
let allDays = [];          // "YYYY-MM-DD", newest first
let shownDays = [];        // the same, or a search's matches
let search = "";
let day = "";              // the open day
let version = "none";      // the server's version of the open entry
let loadedText = "";
let calYear = 0, calMonth = 0;

// ---------- Dates (as YYYY-MM-DD strings, computed in UTC) ----------
const pad = n => String(n).padStart(2, "0");
const toName = d => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
const fromName = s => new Date(Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)));
function todayName() {
  const now = new Date();
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}
function addDays(name, n) {
  const d = fromName(name);
  d.setUTCDate(d.getUTCDate() + n);
  return toName(d);
}
function longDate(name) {
  const d = fromName(name);
  return `${WEEKDAYS[d.getUTCDay()]}, ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}
function shortDate(name) {
  const d = fromName(name);
  return `${WEEKDAYS[d.getUTCDay()].slice(0, 3)} ${pad(d.getUTCDate())} ${MONTHS[d.getUTCMonth()].slice(0, 3)} ${d.getUTCFullYear()}`;
}

// ---------- Server ----------
class KeyError extends Error {}

async function request(method, path, body, base) {
  const headers = {};
  if (key) headers["X-Diary-Key"] = key;
  if (base !== undefined) headers["X-Diary-Base"] = base;
  if (body !== undefined) headers["Content-Type"] = "text/plain; charset=utf-8";
  const response = await fetch(path + (path.includes("?") ? "&" : "?") + "charset=utf-8",
                               { method, headers, body, cache: "no-store" });
  if (response.status === 403) throw new KeyError();
  return { status: response.status, text: await response.text(),
           version: response.headers.get("X-Diary-Version") || "none" };
}

const daysIn = text => text.split("\n").map(l => l.slice(0, 10)).filter(s => /^\d{4}-\d\d-\d\d$/.test(s));

function setStatus(message, isError) {
  $("status").textContent = message;
  $("status").classList.toggle("error", !!isError);
}

const dirty = () => $("text").value.replace(/\n+$/, "") !== loadedText;

function showKeyBox(message) {
  $("app").hidden = true;
  $("keybox").style.display = "block";
  if (message) $("keymsg").textContent = message;
  $("key").focus();
}

// Runs a server action; on a wrong key, asks for it instead.
async function guarded(action) {
  try {
    return await action();
  } catch (e) {
    if (e instanceof KeyError) showKeyBox(key ? "That key isn't right. Try again:" : null);
    else setStatus("The Diary Server can't be reached: " + e.message, true);
    return undefined;
  }
}

// ---------- Entries ----------
async function loadList() {
  const r = await request("GET", "/entries");
  allDays = daysIn(r.text);
  if (!search) shownDays = allDays.slice();
}

async function loadEntry() {
  const r = await request("GET", "/entries/" + day);
  loadedText = r.status === 200 ? r.text : "";
  version = r.status === 200 ? r.version : "none";
  $("text").value = loadedText;
  $("date").textContent = longDate(day);
  document.title = "Diary – " + shortDate(day);
  setStatus(r.status === 200 || r.status === 404 ? "" : "The server refused the entry: " + r.text.trim(), r.status !== 200 && r.status !== 404);
}

// Saves the open entry if it changed; an empty entry is deleted.
async function save() {
  if (!dirty()) return true;
  const text = $("text").value.replace(/\n+$/, "");
  let base = version;
  for (;;) {
    setStatus("Saving…");
    const r = text ? await request("PUT", "/entries/" + day, text, base)
                   : await request("DELETE", "/entries/" + day, undefined, base);
    if (r.status === 200) {
      version = r.version;
      loadedText = text;
      const known = allDays.includes(day);
      if (!text) {
        allDays = allDays.filter(d => d !== day);
        shownDays = shownDays.filter(d => d !== day);
      } else if (!known) {
        allDays = [...allDays, day].sort().reverse();
        if (!search) shownDays = allDays.slice();
      }
      setStatus("Saved");
      render();
      return true;
    }
    if (r.status !== 409) {
      setStatus("The Diary Server refused to save the entry: " + r.text.trim(), true);
      return false;
    }
    if (!confirm("This entry was changed on another computer since you opened it.\n\nSave your version over it?")) {
      setStatus("Not saved: the entry was changed elsewhere.", true);
      return false;
    }
    base = undefined;      // replace it unconditionally
  }
}

async function openDay(name, select) {
  if (name !== day) {
    if (!(await save())) return;
    day = name;
    const d = fromName(day);
    calYear = d.getUTCFullYear();
    calMonth = d.getUTCMonth();
    await loadEntry();
  }
  render();
  if (select) selectMatch(0);
}

// ---------- Search ----------
function selectMatch(from) {
  if (!search) return false;
  const area = $("text");
  const at = area.value.toLowerCase().indexOf(search.toLowerCase(), from);
  if (at < 0) return false;
  area.focus();
  area.setSelectionRange(at, at + search.length);
  return true;
}

async function find(words) {
  words = words.trim();
  if (!words) {
    search = "";
    shownDays = allDays.slice();
    render();
    return;
  }
  if (!(await save())) return;
  const r = await request("GET", "/search?q=" + encodeURIComponent(words));
  const matches = daysIn(r.text);
  if (!matches.length) {
    setStatus("No entries contain that text.");
    return;
  }
  search = words;
  shownDays = matches;
  if (!shownDays.includes(day)) await openDay(shownDays[0], true);
  else { render(); selectMatch(0); }
}

// ---------- Drawing ----------
function render() {
  const shown = new Set(shownDays);
  $("month").textContent = `${MONTHS[calMonth]} ${calYear}`;
  const first = new Date(Date.UTC(calYear, calMonth, 1));
  const length = new Date(Date.UTC(calYear, calMonth + 1, 0)).getUTCDate();
  const today = todayName();
  const body = $("days");
  body.textContent = "";
  let row = body.insertRow();
  for (let i = 0; i < first.getUTCDay(); i++) row.insertCell();
  for (let n = 1; n <= length; n++) {
    if (row.cells.length === 7) row = body.insertRow();
    const name = `${calYear}-${pad(calMonth + 1)}-${pad(n)}`;
    const button = document.createElement("button");
    button.textContent = n;
    button.className = (shown.has(name) ? "has " : "") + (name === today ? "today " : "") + (name === day ? "open" : "");
    button.setAttribute("aria-label", longDate(name) + (shown.has(name) ? ", has an entry" : ""));
    button.onclick = () => guarded(() => openDay(name, true));
    row.insertCell().appendChild(button);
  }

  const list = $("list");
  list.textContent = "";
  if (search) {
    const all = document.createElement("button");
    all.textContent = `Show all entries (finding “${search}”)`;
    all.style.color = "var(--muted)";
    all.onclick = () => { $("search").value = ""; find(""); };
    list.appendChild(all);
  }
  for (const name of shownDays) {
    const button = document.createElement("button");
    button.textContent = shortDate(name);
    if (name === day) button.className = "open";
    button.onclick = () => guarded(() => openDay(name, true));
    list.appendChild(button);
  }
  if (!shownDays.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No entries yet.";
    list.appendChild(empty);
  }
  const open = list.querySelector(".open");
  if (open) open.scrollIntoView({ block: "nearest" });
}

// ---------- Start ----------
async function start() {
  $("keybox").style.display = "none";
  $("app").hidden = false;
  $("where").textContent = "Diary Server at " + location.host;
  await loadList();
  const name = day || todayName();
  day = "";
  await openDay(name, false);
  $("text").focus();
}

$("keyform").onsubmit = e => {
  e.preventDefault();
  key = $("key").value;
  try { localStorage.setItem("diaryKey", key); } catch (err) {}
  guarded(start);
};
$("prevMonth").onclick = () => { if (--calMonth < 0) { calMonth = 11; calYear--; } render(); };
$("nextMonth").onclick = () => { if (++calMonth > 11) { calMonth = 0; calYear++; } render(); };
$("prevDay").onclick = () => guarded(() => openDay(addDays(day, -1), false));
$("nextDay").onclick = () => guarded(() => openDay(addDays(day, 1), false));
$("today").onclick = () => guarded(() => openDay(todayName(), false));
$("save").onclick = () => guarded(save);
$("searchform").onsubmit = e => { e.preventDefault(); guarded(() => find($("search").value)); };
$("text").oninput = () => setStatus(dirty() ? "Not saved yet" : "");
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    guarded(save);
  }
});
window.addEventListener("beforeunload", e => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } });
guarded(start);
</script>
</body>
</html>
"""


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
