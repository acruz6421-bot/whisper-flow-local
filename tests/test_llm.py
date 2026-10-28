"""
Tests for llm.py and the dictation safety guards. No network, no GPU, no mic:
a stdlib HTTP server fakes both Ollama and an OpenAI-compatible endpoint.

    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("WFL_DATA", tempfile.mkdtemp(prefix="wfl_test_"))

import llm  # noqa: E402
import textproc  # noqa: E402


class Fake(BaseHTTPRequestHandler):
    calls = []
    reply = "texto limpo"
    delay = 0.0

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, OSError):
            pass  # client gave up on purpose (timeout test)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._send(200, {"models": [{"name": "qwen3:4b"}, {"name": "gemma3:4b"}]})
        if self.path == "/v1/models":
            if self.headers.get("Authorization") != "Bearer k1":
                return self._send(401, {"error": "no key"})
            return self._send(200, {"data": [{"id": "m-a"}, {"id": "m-b"}]})
        self._send(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        Fake.calls.append((self.path, dict(self.headers), body))
        if Fake.delay:
            time.sleep(Fake.delay)
        if self.path == "/api/chat":
            return self._send(200, {"message": {"content": Fake.reply}})
        if self.path == "/api/generate":
            return self._send(200, {"done": True})
        if self.path == "/v1/chat/completions":
            if self.headers.get("Authorization") != "Bearer k1":
                return self._send(401, {"error": "no key"})
            return self._send(200, {"choices": [{"message": {"content": Fake.reply}}]})
        self._send(404, {})


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Fake)
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Fake.calls, Fake.reply, Fake.delay = [], "texto limpo", 0.0


class LLMTest(Base):
    def test_off_raises(self):
        self.assertFalse(llm.enabled({"provider": "off"}))
        with self.assertRaises(llm.LLMError):
            llm.chat({"provider": "off"}, "s", "u")

    def test_ollama_native_chat_with_keep_alive(self):
        cfg = {"provider": "ollama", "ollama": {"url": self.url, "model": "qwen3:4b",
                                                "keep_alive": "5m", "think": False}}
        self.assertEqual(llm.chat(cfg, "sys", "oi"), "texto limpo")
        path, _h, body = Fake.calls[-1]
        self.assertEqual(path, "/api/chat")
        self.assertEqual(body["keep_alive"], "5m")
        self.assertIs(body["think"], False)
        self.assertFalse(body["stream"])

    def test_custom_sends_key_and_appends_path(self):
        cfg = {"provider": "custom", "custom": {"url": self.url + "/v1", "api_key": "k1",
                                                "model": "m-a"}}
        self.assertEqual(llm.chat(cfg, "sys", "oi"), "texto limpo")
        path, headers, body = Fake.calls[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers.get("Authorization"), "Bearer k1")
        self.assertEqual(body["model"], "m-a")

    def test_custom_accepts_full_chat_url(self):
        cfg = {"provider": "custom", "custom": {"url": self.url + "/v1/chat/completions",
                                                "api_key": "k1", "model": "m-a"}}
        llm.chat(cfg, "s", "u")
        self.assertEqual(Fake.calls[-1][0], "/v1/chat/completions")

    def test_per_task_model_override(self):
        cfg = {"provider": "custom", "models": {"command": "m-b"},
               "custom": {"url": self.url + "/v1", "api_key": "k1", "model": "m-a"}}
        llm.chat(cfg, "s", "u", task="command")
        self.assertEqual(Fake.calls[-1][2]["model"], "m-b")
        llm.chat(cfg, "s", "u", task="clean")
        self.assertEqual(Fake.calls[-1][2]["model"], "m-a")

    def test_fallback_after_auth_error(self):
        cfg = {"provider": "custom",
               "custom": {"url": self.url + "/v1", "api_key": "WRONG", "model": "m-a"},
               "fallback": {"provider": "ollama", "url": self.url, "model": "qwen3:4b"}}
        self.assertEqual(llm.chat(cfg, "s", "u"), "texto limpo")
        self.assertEqual([c[0] for c in Fake.calls], ["/v1/chat/completions", "/api/chat"])

    def test_all_fail_raises_with_both_errors(self):
        cfg = {"provider": "custom", "custom": {"url": "http://127.0.0.1:1/v1", "model": "x"},
               "fallback": {"provider": "custom", "url": "http://127.0.0.1:1/v1", "model": "y"}}
        with self.assertRaises(llm.LLMError) as ctx:
            llm.chat(cfg, "s", "u", timeout=5)
        self.assertIn("custom/x", str(ctx.exception))
        self.assertIn("custom/y", str(ctx.exception))

    def test_timeout_budget_is_shared(self):
        Fake.delay = 1.5
        cfg = {"provider": "ollama", "ollama": {"url": self.url, "model": "a"},
               "fallback": {"provider": "ollama", "url": self.url, "model": "b"}}
        t0 = time.time()
        with self.assertRaises(llm.LLMError):
            llm.chat(cfg, "s", "u", timeout=1.0)
        self.assertLess(time.time() - t0, 1.6)  # fallback did not get a fresh 1 s

    def test_strips_think_block(self):
        Fake.reply = "<think>hmm</think>\nResultado final"
        cfg = {"provider": "ollama", "ollama": {"url": self.url, "model": "qwen3:4b"}}
        self.assertEqual(llm.chat(cfg, "s", "u"), "Resultado final")

    def test_list_models(self):
        self.assertEqual(llm.list_models("ollama", {"url": self.url}), ["gemma3:4b", "qwen3:4b"])
        self.assertEqual(llm.list_models("custom", {"url": self.url + "/v1", "api_key": "k1"}),
                         ["m-a", "m-b"])

    def test_warm_only_for_ollama(self):
        self.assertFalse(llm.warm({"provider": "custom", "auto_clean": True}))
        cfg = {"provider": "ollama", "auto_clean": True,
               "ollama": {"url": self.url, "model": "qwen3:4b"}}
        self.assertTrue(llm.warm(cfg))
        self.assertEqual(Fake.calls[-1][0], "/api/generate")

    def test_test_reports_each_provider(self):
        cfg = {"provider": "custom", "custom": {"url": self.url + "/v1", "api_key": "k1",
                                                "model": "m-a"},
               "fallback": {"provider": "custom", "url": "http://127.0.0.1:1/v1", "model": "z"},
               "timeout_s": 3}
        res = llm.test(cfg)
        self.assertEqual([r["ok"] for r in res], [True, False])
        self.assertTrue(res[1]["fallback"])


class TextProcTest(unittest.TestCase):
    def test_voice_commands_and_capitalize(self):
        out = textproc.process("primeira frase nova linha segunda", {"capitalize": True})
        self.assertEqual(out, "Primeira frase\nsegunda")

    def test_send_command_needs_pause(self):
        self.assertEqual(textproc.split_send("tudo certo, envia"), ("tudo certo", True))
        self.assertEqual(textproc.split_send("o comprovante voce envia")[1], False)

    def test_snippet_matches_whole_utterance_only(self):
        sn = [{"trigger": "meu email", "aliases": ["meu e-mail"], "text": "x@y.com"}]
        self.assertIsNotNone(textproc.match_snippet("Meu e-mail.", sn))
        self.assertIsNone(textproc.match_snippet("manda meu email pro joao", sn))


if __name__ == "__main__":
    unittest.main()
