import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FrontMatterTests(unittest.TestCase):
    def test_splits_front_matter(self):
        fm, body = sync.split_front_matter("---\ntitle: Hello\n---\n# Body\n")
        self.assertEqual(fm, "title: Hello\n")
        self.assertEqual(body, "# Body\n")

    def test_no_front_matter(self):
        fm, body = sync.split_front_matter("# Body\n---\n")
        self.assertEqual(fm, "")
        self.assertEqual(body, "# Body\n---\n")

    def test_crlf_and_dots_terminator(self):
        fm, body = sync.split_front_matter("---\r\ntitle: Hi\r\n...\r\nText")
        self.assertIn("title: Hi", fm)
        self.assertEqual(body, "Text")

    def test_empty_front_matter(self):
        fm, body = sync.split_front_matter("---\n---\nText")
        self.assertEqual(fm, "")
        self.assertEqual(body, "Text")

    def test_title_quotes_stripped(self):
        self.assertEqual(sync.front_matter_title('title: "A: B"\n'), "A: B")
        self.assertEqual(sync.front_matter_title("title: 'Single'\n"), "Single")
        self.assertEqual(sync.front_matter_title("title: Plain  \n"), "Plain")

    def test_title_missing(self):
        self.assertEqual(sync.front_matter_title("subtitle: x\n"), "")


class H1Tests(unittest.TestCase):
    def test_first_h1(self):
        self.assertEqual(sync.first_h1("intro\n# Title #\n# Second\n"), "Title")

    def test_ignores_fenced_code(self):
        body = "```bash\n# comment\n```\n~~~\n# also code\n~~~\n# Real\n"
        self.assertEqual(sync.first_h1(body), "Real")

    def test_ignores_h2_and_hashtags(self):
        self.assertEqual(sync.first_h1("## Two\n#hashtag\n"), "")


class TitleTests(unittest.TestCase):
    def test_fallback_order(self):
        path = Path("docs/my-page_name.md")
        self.assertEqual(sync.resolve_title(path, "title: FM", "# H1", "frontmatter"), "FM")
        self.assertEqual(sync.resolve_title(path, "", "# H1", "frontmatter"), "H1")
        self.assertEqual(sync.resolve_title(path, "", "text", "frontmatter"), "my page name")
        self.assertEqual(sync.resolve_title(path, "title: FM", "# H1", "h1"), "H1")
        self.assertEqual(sync.resolve_title(path, "title: FM", "# H1", "filename"), "my page name")


class Handler(BaseHTTPRequestHandler):
    queue = []
    received = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        Handler.received.append({
            "auth": self.headers["Authorization"],
            "body": json.loads(self.rfile.read(length)),
        })
        status = Handler.queue.pop(0) if Handler.queue else 201
        payload = json.dumps({"body": "Success: ok" if status < 300 else "Error: nope"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/webtrigger"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Handler.queue = []
        Handler.received = []
        self.tmp = tempfile.TemporaryDirectory()
        self.summary = Path(self.tmp.name) / "summary.md"
        self.output = Path(self.tmp.name) / "output.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def run_sync(self, **inputs):
        env = {
            "INPUT_ENDPOINT": self.endpoint,
            "INPUT_TOKEN": "secret-token",
            "INPUT_SPACE_ID": "98765",
            "INPUT_PARENT_ID": "42",
            "INPUT_PATH": str(FIXTURES),
            "INPUT_DELAY": "0",
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GITHUB_OUTPUT": str(self.output),
        }
        env.update({f"INPUT_{k.upper()}": v for k, v in inputs.items()})
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("sync.time.sleep"), \
                mock.patch("sys.stdout"):
            return sync.main()

    def outputs(self):
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines())

    def test_publishes_all_files(self):
        Handler.queue = [201, 200, 201]
        self.assertEqual(self.run_sync(), 0)
        titles = [r["body"]["pageTitle"] for r in Handler.received]
        self.assertEqual(titles, ["Getting started", "Install the app", "release notes"])
        first = Handler.received[0]
        self.assertEqual(first["auth"], "Bearer secret-token")
        self.assertEqual(first["body"]["spaceId"], "98765")
        self.assertEqual(first["body"]["parentId"], "42")
        self.assertTrue(first["body"]["overwrite"])
        self.assertNotIn("title:", first["body"]["content"])
        self.assertEqual(self.outputs(), {"created": "2", "updated": "1", "skipped": "1", "failed": "0"})
        self.assertIn("skipped (empty)", self.summary.read_text())

    def test_retries_server_errors(self):
        Handler.queue = [500, 429, 201]
        self.assertEqual(self.run_sync(glob="getting-started.md"), 0)
        self.assertEqual(len(Handler.received), 3)

    def test_stops_on_expired_token(self):
        Handler.queue = [403]
        self.assertEqual(self.run_sync(), 1)
        self.assertEqual(len(Handler.received), 1)
        self.assertIn("not sent", self.summary.read_text())

    def test_bad_request_continues(self):
        Handler.queue = [400, 201, 201]
        self.assertEqual(self.run_sync(), 1)
        self.assertEqual(len(Handler.received), 3)
        self.assertEqual(self.outputs()["failed"], "1")

    def test_dry_run_sends_nothing(self):
        self.assertEqual(self.run_sync(dry_run="true", endpoint="", token=""), 0)
        self.assertEqual(Handler.received, [])

    def test_duplicate_titles_fail_before_sending(self):
        with tempfile.TemporaryDirectory() as docs:
            Path(docs, "a.md").write_text("# Same\n")
            Path(docs, "b.md").write_text("# Same\n")
            with self.assertRaises(SystemExit):
                self.run_sync(path=docs)
        self.assertEqual(Handler.received, [])

    def test_space_key_rejected(self):
        with self.assertRaises(SystemExit):
            self.run_sync(space_id="DOCS")
        self.assertEqual(Handler.received, [])

    def test_missing_inputs(self):
        with self.assertRaises(SystemExit):
            self.run_sync(token="")


if __name__ == "__main__":
    unittest.main()
