"""Hints for near-duplicate screenshots: each result compared with the one before it."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi import HTTPException
from PIL import Image

import server
import storage_manager


class SimilarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(server, 'WORK', self.root),
            patch.object(server, '_storage', storage_manager.Manager(server)),
        ]
        for p in self.patches:
            p.start()
        server._similar_cache.clear()
        rng = np.random.default_rng(3)
        base = rng.integers(0, 255, (90, 160, 3), dtype=np.uint8)
        noisy = np.clip(base.astype(int) + rng.integers(-4, 5, base.shape), 0, 255).astype(np.uint8)
        other = rng.integers(0, 255, (90, 160, 3), dtype=np.uint8)
        images = [base, noisy, other, other]
        for i, image in enumerate(images):
            folder = 'thumbs' if i != 3 else 'frames'  # the last one has no thumbnail: falls back to the full frame
            path = self.root / 's1' / folder / 'r1' / f'{i}.jpg'
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(image).save(path, quality=95)
        cuts = [{'kind': 'cut', 'frame_index': i * 10, 'label': ''} for i in range(5)]
        server._sessions['s1'] = {
            'results': {'r1': {'run': 'r1', 'cuts': cuts}},
            'latest_run': 'r1',
            'worker_lock': threading.Lock(),
        }

    def tearDown(self):
        server._sessions.clear()
        server._similar_cache.clear()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_scores_compare_each_shot_with_the_previous_one(self):
        out = server.similar('s1', 'r1')
        scores = out['scores']
        self.assertIsNone(scores[0])
        self.assertGreaterEqual(scores[1], out['threshold'])  # same picture with a little noise
        self.assertLess(scores[2], out['threshold'])  # a different picture
        self.assertGreaterEqual(scores[3], 95)  # identical, read from the full frame
        self.assertIsNone(scores[4])  # missing image gives no score
        self.assertIs(server.similar('s1', 'r1'), out)  # cached per result

    def test_unknown_result(self):
        with self.assertRaises(HTTPException):
            server.similar('s1', 'old')


if __name__ == '__main__':
    unittest.main()


class StaleScriptTests(unittest.TestCase):
    def test_pages_and_scripts_are_revalidated_but_api_responses_untouched(self):
        import asyncio
        from types import SimpleNamespace

        from starlette.responses import Response

        async def call(path):
            request = SimpleNamespace(url=SimpleNamespace(path=path))

            async def call_next(_):
                return Response('ok')

            return await server._no_stale_pages(request, call_next)

        self.assertEqual(asyncio.run(call('/app.js')).headers['cache-control'], 'no-cache')
        self.assertEqual(asyncio.run(call('/')).headers['cache-control'], 'no-cache')
        self.assertNotIn('cache-control', asyncio.run(call('/api/status/x')).headers)


class AssetVersionTests(unittest.TestCase):
    def test_page_links_scripts_with_a_content_version_that_changes_with_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            static = Path(tmp)
            (static / 'vendor').mkdir()
            (static / 'app.js').write_text('one', encoding='utf-8')
            (static / 'vendor' / 'lib.js').write_text('lib', encoding='utf-8')
            (static / 'style.css').write_text('a{}', encoding='utf-8')
            (static / 'index.html').write_text(
                '<link rel="stylesheet" href="/style.css"><link href="https://fonts.example/x.css">'
                '<script src="/app.js"></script><script src="/vendor/lib.js"></script><script src="/missing.js"></script>',
                encoding='utf-8',
            )
            with patch.object(server, 'STATIC', static):
                server._asset_versions.clear()
                first = server.index_page()
                html = first.body.decode('utf-8')
                self.assertEqual(first.headers['cache-control'], 'no-cache')
                self.assertRegex(html, r'src="/app\.js\?v=[0-9a-f]{10}"')
                self.assertRegex(html, r'src="/vendor/lib\.js\?v=[0-9a-f]{10}"')
                self.assertRegex(html, r'href="/style\.css\?v=[0-9a-f]{10}"')
                self.assertIn('href="https://fonts.example/x.css"', html)
                self.assertIn('src="/missing.js"', html)
                (static / 'app.js').write_text('two, changed', encoding='utf-8')
                second = server.index_page().body.decode('utf-8')
                version = lambda text: text.split('/app.js?v=')[1][:10]
                self.assertNotEqual(version(html), version(second))
                self.assertEqual(html.split('/style.css?v=')[1][:10], second.split('/style.css?v=')[1][:10])
