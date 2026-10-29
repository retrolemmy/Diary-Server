#!/usr/bin/env python3
"""Tests for diary_server.py: python3 -m unittest -v test_server"""
import http.client
import os
import pathlib
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import diary_server

MAC_TEXT = 'Gr\x9f\xa7e vom Mac: \x8a \x9a \x9f\r\xd2Quotes\xd3 \xd0 ok'.encode('latin-1')
AMIGA_TEXT = 'Gr\xfc\xdfe vom Amiga: \xe4 \xf6 \xfc\nZweite Zeile\n'.encode('latin-1')


class ServerTest(unittest.TestCase):
    key = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = diary_server.Store(self.tmp.name)
        handler = type('H', (diary_server.Handler,), {'store': self.store, 'key': self.key,
                                                      'log_message': lambda *a: None})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1])
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, dict(response.getheaders()), data


class Basics(ServerTest):
    def test_mac_put_then_read_everywhere(self):
        status, headers, _ = self.request('PUT', '/entries/2026-10-28?charset=macroman', MAC_TEXT)
        self.assertEqual(status, 200)
        stored = pathlib.Path(self.tmp.name, '2026-10-28').read_bytes()
        self.assertEqual(stored, 'Grüße vom Mac: ä ö ü\n“Quotes” – ok\n'.encode('utf-8'))
        self.assertEqual(headers['X-Diary-Version'], diary_server.version_of(stored))

        _, _, mac = self.request('GET', '/entries/2026-10-28?charset=macroman')
        self.assertEqual(mac, MAC_TEXT)
        _, _, amiga = self.request('GET', '/entries/2026-10-28?charset=latin1')
        self.assertEqual(amiga, 'Gr\xfc\xdfe vom Mac: \xe4 \xf6 \xfc\n"Quotes" - ok'.encode('latin-1'))
        _, _, utf8 = self.request('GET', '/entries/2026-10-28')
        self.assertEqual(utf8.decode('utf-8'), 'Grüße vom Mac: ä ö ü\n“Quotes” – ok')

    def test_amiga_put_read_on_mac(self):
        self.request('PUT', '/entries/2026-10-27?charset=latin1', AMIGA_TEXT)
        _, _, mac = self.request('GET', '/entries/2026-10-27?charset=macroman')
        self.assertEqual(mac, 'Gr\x9f\xa7e vom Amiga: \x8a \x9a \x9f\rZweite Zeile'.encode('latin-1'))

    def test_nextstep(self):
        # NeXTSTEP's character set: ü=F6 ß=FB, curly quotes AA/BA, en dash B1
        next_text = b'Gr\xf6\xfbe von NeXT\n\xaaQuotes\xba \xb1 ok'
        self.request('PUT', '/entries/2026-10-26?charset=nextstep', next_text)
        stored = pathlib.Path(self.tmp.name, '2026-10-26').read_bytes()
        self.assertEqual(stored, 'Grüße von NeXT\n“Quotes” – ok\n'.encode('utf-8'))
        _, _, back = self.request('GET', '/entries/2026-10-26?charset=nextstep')
        self.assertEqual(back, next_text)
        _, _, mac = self.request('GET', '/entries/2026-10-26?charset=macroman')
        self.assertEqual(mac, b'Gr\x9f\xa7e von NeXT\r\xd2Quotes\xd3 \xd0 ok')
        _, _, days = self.request('GET', '/search?q=GR%F6%FBE&charset=nextstep')
        self.assertEqual(days, b'2026-10-26\n')

    def test_windows(self):
        # Windows-1252: ü=FC ß=DF, curly quotes 93/94, en dash 96, CR LF
        win_text = b'Gr\xfc\xdfe von Windows\r\n\x93Quotes\x94 \x96 ok \x80'
        self.request('PUT', '/entries/2026-10-25?charset=windows-1252', win_text)
        stored = pathlib.Path(self.tmp.name, '2026-10-25').read_bytes()
        self.assertEqual(stored, 'Grüße von Windows\n“Quotes” – ok €\n'.encode('utf-8'))
        _, _, back = self.request('GET', '/entries/2026-10-25?charset=windows-1252')
        self.assertEqual(back, win_text)
        _, _, amiga = self.request('GET', '/entries/2026-10-25?charset=latin1')
        self.assertEqual(amiga, b'Gr\xfc\xdfe von Windows\n"Quotes" - ok EUR')

    def test_web_page_for_browsers_only(self):
        status, headers, body = self.request('GET', '/', headers={'Accept': 'text/html,*/*'})
        self.assertEqual(status, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/html'))
        self.assertIn(b'<title>Diary</title>', body)
        status, headers, body = self.request('GET', '/')
        self.assertEqual(headers['Content-Type'], 'text/plain')
        self.assertTrue(body.startswith(b'Diary Server'))

    def test_list_newest_first_with_versions(self):
        for day in ('2026-10-01', '2026-10-28', '2025-12-31'):
            self.request('PUT', '/entries/' + day, b'x')
        status, _, data = self.request('GET', '/entries')
        lines = data.decode('ascii').splitlines()
        self.assertEqual([l.split()[0] for l in lines], ['2026-10-28', '2026-10-01', '2025-12-31'])
        self.assertTrue(all(len(l.split()[1]) == 8 for l in lines))

    def test_missing_entry_and_bad_names(self):
        self.assertEqual(self.request('GET', '/entries/2026-10-28')[0], 404)
        self.assertEqual(self.request('GET', '/entries/2026-02-30')[0], 404)
        self.assertEqual(self.request('PUT', '/entries/../etc', b'x')[0], 404)
        self.assertEqual(self.request('GET', '/entries/2026-10-28?charset=ebcdic')[0], 400)

    def test_empty_put_deletes(self):
        self.request('PUT', '/entries/2026-10-28', b'hello')
        status, headers, _ = self.request('PUT', '/entries/2026-10-28', b'\n\n')
        self.assertEqual((status, headers['X-Diary-Version']), (200, 'none'))
        self.assertEqual(self.request('GET', '/entries/2026-10-28')[0], 404)
        self.request('PUT', '/entries/2026-10-29', b'hello')
        self.assertEqual(self.request('DELETE', '/entries/2026-10-29')[0], 200)
        self.assertEqual(self.request('GET', '/entries')[2], b'')

    def test_conflict(self):
        _, headers, _ = self.request('PUT', '/entries/2026-10-28', b'one', {'X-Diary-Base': 'none'})
        v1 = headers['X-Diary-Version']
        # Another machine saves a change based on v1 ...
        _, headers, _ = self.request('PUT', '/entries/2026-10-28', b'two', {'X-Diary-Base': v1})
        v2 = headers['X-Diary-Version']
        # ... so a save still based on v1 is refused and nothing is lost.
        status, headers, _ = self.request('PUT', '/entries/2026-10-28', b'three', {'X-Diary-Base': v1})
        self.assertEqual((status, headers['X-Diary-Version']), (409, v2))
        self.assertEqual(self.request('GET', '/entries/2026-10-28')[2], b'two')
        # A new entry with base "none" conflicts if it exists by now.
        self.assertEqual(self.request('PUT', '/entries/2026-10-28', b'x', {'X-Diary-Base': 'none'})[0], 409)

    def test_search_ignores_case_in_client_charset(self):
        self.request('PUT', '/entries/2026-10-28?charset=macroman', MAC_TEXT)
        self.request('PUT', '/entries/2026-10-27?charset=latin1', AMIGA_TEXT)
        self.request('PUT', '/entries/2026-10-26', 'nothing here'.encode())
        # "GRÜSSE" would differ; "grüße" percent-encoded in MacRoman (ü = 0x9F, ß = 0xA7)
        _, _, data = self.request('GET', '/search?q=GR%9F%A7E&charset=macroman')
        self.assertEqual(data, b'2026-10-28\n2026-10-27\n')
        _, _, data = self.request('GET', '/search?q=zweite+zeile&charset=latin1')
        self.assertEqual(data, b'2026-10-27\n')

    def test_too_long(self):
        self.assertEqual(self.request('PUT', '/entries/2026-10-28', b'x' * 70000)[0], 413)


class WithKey(ServerTest):
    key = 'secret'

    def test_web_page_needs_no_key(self):
        # The page holds no entries; it asks for the key itself.
        status, _, body = self.request('GET', '/', headers={'Accept': 'text/html'})
        self.assertEqual(status, 200)
        self.assertIn(b'needs a key', body)

    def test_key_required(self):
        self.assertEqual(self.request('GET', '/entries')[0], 403)
        self.assertEqual(self.request('GET', '/entries', headers={'X-Diary-Key': 'secret'})[0], 200)
        self.assertEqual(self.request('GET', '/entries?key=secret')[0], 200)


class Import(unittest.TestCase):
    def test_detects_mac_and_amiga_files(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            pathlib.Path(src, '2026-10-28').write_bytes(MAC_TEXT)
            pathlib.Path(src, '2026-10-27').write_bytes(AMIGA_TEXT)
            pathlib.Path(src, 'Diary Settings').write_bytes(b'\x00\x0c')
            store = diary_server.Store(dst)
            diary_server.import_entries(store, src)
            self.assertEqual(store.days(), ['2026-10-28', '2026-10-27'])
            self.assertTrue(store.read('2026-10-28').decode().startswith('Grüße vom Mac'))
            self.assertTrue(store.read('2026-10-27').decode().startswith('Grüße vom Amiga'))


if __name__ == '__main__':
    unittest.main()
