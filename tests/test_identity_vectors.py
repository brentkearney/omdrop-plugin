"""The identity contract's common vectors, shared byte for byte with omdrop-awdl."""
import base64
import hashlib
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VECTORS = ROOT / 'tests' / 'identity-vectors.json'
# The same file, byte for byte, in brentkearney/omdrop-awdl. A change here is a
# contract change: update it in both repositories and both pins together.
VECTORS_SHA256 = '7c4df91563d32240ad1dc17c18415cde012b373b9a4afc23f0387f384daed1b9'


def load_identity():
    spec = importlib.util.spec_from_file_location('identity', ROOT / 'bin' / 'identity.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class IdentityVectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = VECTORS.read_bytes()
        cls.digest = hashlib.sha256(raw).hexdigest()
        cls.doc = json.loads(raw)
        cls.id = load_identity()

    def test_vectors_are_the_pinned_contract(self):
        self.assertEqual(self.digest, VECTORS_SHA256)

    def test_selection(self):
        for v in self.doc['selection']:
            i = v['input']
            with self.subTest(v['name']):
                got = self.id.select(i['settings'], i['window'], i['cache'], i['disk'],
                                     i['self_signed'], i['root'], i['now'])
                self.assertEqual(got, v['expect'])

    def test_payloads(self):
        for v in self.doc['payloads']:
            with self.subTest(v['name']):
                got = self.id.parse_payload(base64.b64decode(v['payload_b64']))
                if v['expect'] == 'malformed':
                    self.assertIsNone(got)
                    continue
                want = dict(v['expect'])
                for field in ('certificate', 'key', 'record'):
                    want[field] = base64.b64decode(want.pop(field + '_b64'))
                self.assertEqual(got, want)

    def test_encoding_round_trips_the_valid_payload(self):
        v = next(p for p in self.doc['payloads'] if p['expect'] != 'malformed')
        e = v['expect']
        data = self.id.encode_payload(
            e['fetch_id'], e['fetched_at'], e['period_ends'], e['hard_expiry'],
            *(base64.b64decode(e[f + '_b64']) for f in ('certificate', 'key', 'record')))
        self.assertEqual(data, base64.b64decode(v['payload_b64']))

    def test_window_files(self):
        for v in self.doc['window_files']:
            with self.subTest(v['name']):
                got = self.id.parse_window(v['text'])
                self.assertEqual(got, None if v['expect'] == 'unparseable' else v['expect'])

    def test_settings_files(self):
        for v in self.doc['settings_files']:
            with self.subTest(v['name']):
                self.assertEqual(self.id.parse_settings(v['text']), v['expect'])


if __name__ == '__main__':
    unittest.main()
