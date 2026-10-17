# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The SEC User-Agent of the two SEC collectors: a GitHub noreply contact is rejected by the SEC with HTTP 403 (live observation, October 2026: the same request answers 200
with the placeholder or a real address and 403 with "...@users.noreply.github.com"), so it is never sent; the request actually built carries the corrected header."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "trading"
NOREPLY = "Trading-Agent Robert Backhaus <1234567+someone@users.noreply.github.com>"  # the shape that produced the 403
REAL = "Trading-Agent Example Person person@example.org"
PLACEHOLDER = "Trading-Agent contact@example.com"
SCRIPTS = ("Backfill-TradingEventsNews.py", "Backfill-TradingFundamentalsSEC.py")


def load(script: str, user_agent):
    """The script as the scheduler would run it with ``SEC_USER_AGENT`` set (or unset); returns (module, what it printed to stderr)."""
    env = {k: v for k, v in os.environ.items() if k != "SEC_USER_AGENT"}
    if user_agent is not None:
        env["SEC_USER_AGENT"] = user_agent
    spec = importlib.util.spec_from_file_location("sec_script_" + script.replace(".", "_").replace("-", "_"), TOOLS / script)
    module = importlib.util.module_from_spec(spec)
    err = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stderr(err):
        spec.loader.exec_module(module)
    return module, err.getvalue()


class UserAgentTests(unittest.TestCase):
    def test_the_old_value_is_the_one_the_sec_rejects(self) -> None:
        for script in SCRIPTS:
            module, _ = load(script, PLACEHOLDER)
            self.assertTrue(any(bad in NOREPLY.lower() for bad in module.SEC_REJECTED_CONTACTS), script)  # this is the request that answered 403
            self.assertFalse(any(bad in REAL.lower() or bad in PLACEHOLDER.lower() for bad in module.SEC_REJECTED_CONTACTS), script)

    def test_a_github_noreply_contact_is_replaced_and_reported(self) -> None:
        for script in SCRIPTS:
            module, err = load(script, NOREPLY)
            self.assertEqual(module.SEC_USER_AGENT, PLACEHOLDER, script)
            self.assertEqual(module.SEC_HEADERS["User-Agent"], PLACEHOLDER, script)
            self.assertNotIn("noreply", module.SEC_HEADERS["User-Agent"])
            self.assertIn("rejects with HTTP 403", err)  # the operator is told why

    def test_a_real_contact_and_the_default_pass_through_unchanged(self) -> None:
        for script in SCRIPTS:
            module, err = load(script, REAL)
            self.assertEqual((module.SEC_HEADERS["User-Agent"], err), (REAL, ""), script)
            module, err = load(script, None)
            self.assertEqual((module.SEC_HEADERS["User-Agent"], err), (PLACEHOLDER, ""), script)
            self.assertEqual(module.SEC_HEADERS["Accept"], "application/json")

    def test_the_resolver_is_case_insensitive(self) -> None:
        module, _ = load(SCRIPTS[0], None)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(module.resolve_sec_user_agent("X Y 1+z@Users.NoReply.GitHub.com"), PLACEHOLDER)
        self.assertEqual(module.resolve_sec_user_agent(REAL), REAL)

    def test_the_events_request_actually_sent_carries_the_corrected_header(self) -> None:
        module, _ = load("Backfill-TradingEventsNews.py", NOREPLY)
        sent = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"filings": {"recent": {}}}'

        def fake_urlopen(request, timeout=None):
            sent.append(request)
            return Response()

        with mock.patch.object(module.urllib.request, "urlopen", fake_urlopen):
            module.fetch_sec_events("1730168")
        self.assertEqual(len(sent), 1)
        request = sent[0]
        self.assertEqual(request.full_url, "https://data.sec.gov/submissions/CIK0001730168.json")
        self.assertEqual(request.get_header("User-agent"), PLACEHOLDER)  # not the noreply contact
        self.assertEqual(request.get_header("Accept"), "application/json")

    def test_the_fundamentals_request_carries_the_same_corrected_header(self) -> None:
        module, _ = load("Backfill-TradingFundamentalsSEC.py", NOREPLY)
        text = (TOOLS / "Backfill-TradingFundamentalsSEC.py").read_text(encoding="utf-8")
        self.assertIn("headers=SEC_HEADERS", text)  # the single request path of that script uses the (corrected) headers
        self.assertEqual(module.SEC_HEADERS["User-Agent"], PLACEHOLDER)


if __name__ == "__main__":
    unittest.main()
