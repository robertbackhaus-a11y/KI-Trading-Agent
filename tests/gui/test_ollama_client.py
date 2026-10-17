# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Ollama client against a fake HTTP server: reachability, models, streaming, thinking, tool calls, errors, cancel (A-F, J, K, R)."""

from __future__ import annotations

import threading
import time
import unittest

import support  # noqa: F401
from fake_ollama import FakeOllama, Script, call, done, say
from gui.services import ollama_client as oc

MESSAGES = [{"role": "user", "content": "hello"}]


def collect(client, token=None, **kw):
    return list(client.chat_stream("fake-model:latest", MESSAGES, token=token, **kw))


class OllamaClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeOllama().start()
        self.addCleanup(self.fake.stop)
        self.client = oc.OllamaClient(self.fake.url, timeout=10)

    # A
    def test_a_unreachable_ollama_is_a_status_not_an_exception(self) -> None:
        dead = oc.OllamaClient("http://127.0.0.1:1", timeout=1)
        status = dead.status(timeout=1)
        self.assertFalse(status.reachable)
        self.assertIn("nicht erreichbar", status.error)
        with self.assertRaises(oc.OllamaError) as ctx:
            collect(dead)
        self.assertEqual(ctx.exception.kind, oc.UNREACHABLE)
        self.assertFalse(oc.check_ollama("not a url").reachable)
        self.assertFalse(oc.check_ollama("http://127.0.0.1:1", 1).reachable)

    # B
    def test_b_reachability_version_and_model_list_without_loading_a_model(self) -> None:
        status = self.client.status()
        self.assertTrue(status.reachable)
        self.assertEqual((status.version, status.models), ("0.0.0-test", ["fake-model:latest", "other:1b"]))
        self.assertTrue(status.has_model("fake-model:latest") and status.has_model("fake-model") and not status.has_model("missing"))
        self.assertEqual([r["path"] for r in self.fake.requests], ["/api/version", "/api/tags"])  # two GETs; no chat, so no model is loaded

    # C
    def test_c_streaming_text_arrives_in_chunks(self) -> None:
        self.fake.scripts.append(say("Hel", "lo ", "world"))
        chunks = collect(self.client)
        self.assertEqual([c.kind for c in chunks], ["content", "content", "content", "done"])
        self.assertEqual("".join(c.text for c in chunks if c.kind == "content"), "Hello world")
        self.assertEqual(chunks[-1].stats["prompt_eval_count"], 10)

    # D
    def test_d_thinking_chunks_are_separate_from_the_answer(self) -> None:
        self.fake.scripts.append(say("Answer", thinking=("step 1 ", "step 2")))
        chunks = collect(self.client, think=True)
        self.assertEqual([c.text for c in chunks if c.kind == "thinking"], ["step 1 ", "step 2"])
        self.assertEqual([c.text for c in chunks if c.kind == "content"], ["Answer"])

    # E
    def test_e_tool_calls_are_recognised_and_normalised(self) -> None:
        self.fake.scripts.append(call("database_status", {}))
        self.fake.scripts.append(call("get_market_intelligence", '{"symbol": "AAA", "limit": 3}'))  # some models send the arguments as a JSON string
        self.fake.scripts.append(call("x", "not json"))
        first = [c for c in collect(self.client) if c.kind == "tool_calls"][0]
        second = [c for c in collect(self.client) if c.kind == "tool_calls"][0]
        third = [c for c in collect(self.client) if c.kind == "tool_calls"][0]
        self.assertEqual(first.tool_calls, [{"name": "database_status", "arguments": {}}])
        self.assertEqual(second.tool_calls, [{"name": "get_market_intelligence", "arguments": {"symbol": "AAA", "limit": 3}}])
        self.assertEqual(third.tool_calls[0]["arguments"], {"__invalid_json__": "not json"})

    def test_what_is_sent_tools_think_and_no_model_options(self) -> None:
        tool = {"type": "function", "function": {"name": "t", "description": "d", "parameters": {"type": "object", "properties": {}}}}
        self.fake.scripts.extend([say("a"), say("b"), say("c")])
        collect(self.client, tools=[tool], think=False)
        collect(self.client, think=True)
        collect(self.client)
        bodies = self.fake.chat_requests
        self.assertEqual((bodies[0]["tools"], bodies[0]["think"], bodies[0]["stream"], bodies[0]["model"]), ([tool], False, True, "fake-model:latest"))
        self.assertTrue(bodies[1]["think"] is True and "tools" not in bodies[1])
        self.assertNotIn("think", bodies[2])
        for body in bodies:
            self.assertEqual(set(body) - {"model", "messages", "stream", "tools", "think"}, set())  # no options, no num_ctx, no keep_alive
            self.assertNotIn("options", body)

    def test_a_model_that_rejects_think_is_retried_once_without_it(self) -> None:
        self.fake.scripts.extend([Script(status=400, error='"fake-model" does not support thinking: think value not allowed'), say("ok")])
        chunks = collect(self.client, think=True)
        self.assertEqual("".join(c.text for c in chunks if c.kind == "content"), "ok")
        self.assertEqual(["think" in b for b in self.fake.chat_requests], [True, False])

    # R
    def test_r_http_500_and_missing_model_are_typed_errors(self) -> None:
        self.fake.scripts.extend([Script(status=500, error="llama runner crashed"), Script(status=404, error="model 'nope' not found")])
        with self.assertRaises(oc.OllamaError) as ctx:
            collect(self.client)
        self.assertEqual((ctx.exception.kind, ctx.exception.status), (oc.HTTP_ERROR, 500))
        self.assertIn("llama runner crashed", str(ctx.exception))
        with self.assertRaises(oc.OllamaError) as ctx:
            collect(self.client)
        self.assertEqual((ctx.exception.kind, ctx.exception.status), (oc.MODEL_MISSING, 404))

    def test_an_error_line_inside_the_stream_is_raised(self) -> None:
        self.fake.scripts.append(Script([{"message": {"content": "partial"}, "done": False}, {"error": "model runner has unexpectedly stopped"}]))
        got = []
        with self.assertRaises(oc.OllamaError) as ctx:
            for chunk in self.client.chat_stream("m", MESSAGES):
                got.append(chunk)
        self.assertEqual(ctx.exception.kind, oc.HTTP_ERROR)
        self.assertEqual(got[0].text, "partial")

    def test_stream_that_ends_without_done_is_a_broken_stream(self) -> None:
        self.fake.scripts.append(Script([{"message": {"content": "a"}, "done": False}, {"message": {"content": "b"}, "done": False}], drop_after=1))
        with self.assertRaises(oc.OllamaError) as ctx:
            collect(self.client)
        self.assertEqual(ctx.exception.kind, oc.STREAM_BROKEN)

    def test_a_silent_server_is_a_timeout(self) -> None:
        self.fake.scripts.append(Script(hang=3.0))
        fast = oc.OllamaClient(self.fake.url, timeout=0.5)
        started = time.perf_counter()
        with self.assertRaises(oc.OllamaError) as ctx:
            collect(fast)
        self.assertEqual(ctx.exception.kind, oc.TIMEOUT)
        self.assertLess(time.perf_counter() - started, 2.5)

    def test_garbage_in_the_stream_is_a_protocol_error(self) -> None:
        self.fake.scripts.append(Script(lines=[{"message": {"content": "a"}, "done": False}]))
        self.fake.scripts[0].lines.insert(0, "not an object")
        with self.assertRaises(oc.OllamaError) as ctx:
            collect(self.client)
        self.assertEqual(ctx.exception.kind, oc.PROTOCOL)

    # J / K
    def test_j_cancel_stops_this_request_quickly_and_k_the_next_request_works(self) -> None:
        self.fake.scripts.append(Script([{"message": {"content": f"{i} "}, "done": False} for i in range(200)] + [done()], delay=0.1))
        token = oc.CancelToken()
        got: list = []
        errors: list = []

        def reader() -> None:
            try:
                for chunk in self.client.chat_stream("m", MESSAGES, token=token):
                    got.append(chunk)
            except oc.OllamaError as exc:
                errors.append(exc)

        thread = threading.Thread(target=reader)
        thread.start()
        deadline = time.time() + 5
        while len(got) < 3 and time.time() < deadline:
            time.sleep(0.02)
        started = time.perf_counter()
        token.cancel()  # from another thread
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.perf_counter() - started, 1.5)  # far less than the 20 s the stream would take
        self.assertEqual([e.kind for e in errors], [oc.CANCELLED])
        self.assertTrue(token.cancelled)
        # K: a fresh request on the same client works
        self.fake.scripts.append(say("after cancel"))
        self.assertEqual("".join(c.text for c in collect(self.client) if c.kind == "content"), "after cancel")

    def test_cancelling_before_the_request_starts_cancels_it(self) -> None:
        token = oc.CancelToken()
        token.cancel()
        self.fake.scripts.append(say("never"))
        with self.assertRaises(oc.OllamaError) as ctx:
            collect(self.client, token=token)
        self.assertEqual(ctx.exception.kind, oc.CANCELLED)

    def test_the_url_is_validated(self) -> None:
        for bad in ("", "ftp://x", "localhost:11434", "http://"):
            with self.assertRaises(ValueError, msg=bad):
                oc.OllamaClient(bad)
        self.assertEqual(oc.OllamaClient("http://localhost:11434/").port, 11434)

    # static: the client never manages Ollama
    def test_the_client_has_no_lifecycle_or_model_management(self) -> None:
        text = (support.ROOT / "gui" / "services" / "ollama_client.py").read_text(encoding="utf-8")
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for forbidden in ("subprocess", "os.system", "Popen", "num_ctx", "keep_alive", "/api/pull", "/api/delete", "/api/generate", "/api/create", "/api/copy", "/api/push", "ollama serve", "ollama stop"):
            self.assertNotIn(forbidden, code, forbidden)
        for allowed in ("/api/version", "/api/tags", "/api/chat"):
            self.assertIn(allowed, code)
        self.assertNotIn("PySide6", code)


if __name__ == "__main__":
    unittest.main()
