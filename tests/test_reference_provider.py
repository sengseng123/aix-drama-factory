"""Real local HTTP protocol, with fixture responses (no model or credentials)."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from reference_api import analyze_shot
from reference_media import ReferenceError


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'frame.jpg').write_bytes(b'fixture-image')
        self.frames = [dict(file='frame.jpg', time=t) for t in (.3, 1, 1.7)]
        self.payloads = []
        self.status = 200
        self.result = dict(description='人物A转身', camera='中景', palette='暖色', review='动作需复核')
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.payloads.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                data = json.dumps({'choices': [{'message': {'content': json.dumps(owner.result)}}]}).encode()
                self.send_response(owner.status)
                self.send_header('Content-Length', str(len(data)))
                if owner.status == 302:
                    self.send_header('Location', '/unexpected-destination')
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.provider = dict(url=f'http://127.0.0.1:{self.server.server_port}/v1', model='fixture-vision', key='')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_images_timestamps_and_model_sent_to_separate_endpoint(self):
        result = analyze_shot(self.provider, self.frames, self.root, lambda: None)
        self.assertEqual(result, self.result)
        path, headers, body = self.payloads[0]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertNotIn('Authorization', headers)
        self.assertEqual(body['model'], 'fixture-vision')
        content = body['messages'][0]['content']
        self.assertEqual(sum(item['type'] == 'image_url' for item in content), 3)
        self.assertIn('0.3000', content[1]['text'])
        self.assertTrue(content[2]['image_url']['url'].startswith('data:image/jpeg;base64,'))

    def test_malformed_schema_is_rejected(self):
        for result in ([], dict(description='invented', camera=123, palette='red', review=''),
                       dict(description='', camera='wide', palette='red', review='')):
            self.result = result
            with self.assertRaises(ReferenceError):
                analyze_shot(self.provider, self.frames, self.root, lambda: None)

    def test_redirect_not_followed(self):
        self.status = 302
        with self.assertRaisesRegex(ReferenceError, 'HTTP 302'):
            analyze_shot(self.provider, self.frames, self.root, lambda: None)
        self.assertEqual(len(self.payloads), 1)

    def test_stop_check_after_provider_response(self):
        def check():
            if self.payloads:
                raise RuntimeError('stopped')
        with self.assertRaisesRegex(RuntimeError, 'stopped'):
            analyze_shot(self.provider, self.frames, self.root, check)


if __name__ == '__main__':
    unittest.main()
