"""The German and English query guides must stay equivalent and match the code: same chapters, examples and technical terms, all MCP tools with the right read/write flag, working links, no private data."""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
DE = (DOCS / "trading-agent-query-examples.de.md").read_text(encoding="utf-8")
EN = (DOCS / "trading-agent-query-examples.en.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
CODE_SPAN = re.compile(r"`([^`]+)`")
# Examples may only use these neutral tickers and generic uppercase words; any other ticker-like word in an example question fails the test.
ALLOWED_UPPERCASE = {"AAPL", "NVDA", "GOOGL", "SAP", "BUY", "SELL", "TRIM", "HOLD", "ADD", "WATCH", "HIGH", "READY", "MCP"}
NEUTRAL_SYMBOLS = {"AAPL", "NVDA", "GOOGL", "SAP.DE"}


def tool_names() -> list[str]:
    spec = importlib.util.spec_from_file_location("trading_sqlite_docs_test", ROOT / "mcp-tools" / "trading_sqlite.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return sorted(name for name, _f in inspect.getmembers(module.Tools, inspect.isfunction) if not name.startswith("_"))


def chapters(text: str) -> list[tuple[str, str]]:
    parts = re.split(r"^## ", text, flags=re.M)[1:]
    return [(part.splitlines()[0], part) for part in parts]


def slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.lower(), flags=re.U).replace(" ", "-")


def example_rows(body: str) -> int:
    return len(re.findall(r"^\| \d+ \|", body, flags=re.M))


def bullets(body: str) -> int:
    return len(re.findall(r"^- ", body, flags=re.M))


class QueryDocsParityTests(unittest.TestCase):
    def test_both_files_have_the_same_chapter_structure(self) -> None:
        de, en = chapters(DE), chapters(EN)
        self.assertEqual(len(de), 18)  # 16 chapters + technical mapping + limits and semantics
        self.assertEqual([h.split(".")[0] for h, _ in de], [h.split(".")[0] for h, _ in en])
        self.assertEqual([h.split(".")[0] for h, _ in de], [str(n) for n in range(1, 17)] + ["A", "B"])

    def test_every_chapter_has_the_same_number_of_examples_notes_and_technical_terms(self) -> None:
        for (heading, de_body), (_h, en_body) in zip(chapters(DE), chapters(EN)):
            self.assertEqual(example_rows(de_body), example_rows(en_body), heading)
            self.assertEqual(bullets(de_body), bullets(en_body), heading)
            self.assertEqual(Counter(CODE_SPAN.findall(de_body)), Counter(CODE_SPAN.findall(en_body)), heading)
            self.assertEqual(len(re.findall(r"^\|", de_body, flags=re.M)), len(re.findall(r"^\|", en_body, flags=re.M)), heading)

    def test_every_example_chapter_has_examples(self) -> None:
        for heading, body in chapters(DE)[:16]:
            self.assertGreaterEqual(example_rows(body), 4, heading)

    def test_all_mcp_tools_are_documented_once_in_the_mapping_and_in_both_languages(self) -> None:
        names = tool_names()
        self.assertEqual(len(names), 15)
        for label, text in (("de", DE), ("en", EN)):
            mapping = chapters(text)[16][1]
            rows = re.findall(r"^\| `([a-z_]+)` \|", mapping, flags=re.M)
            self.assertEqual(sorted(rows), names, label)
            for name in names:
                self.assertIn(f"`{name}", text, f"{label}: {name}")

    def test_read_write_flags_are_equal_and_correct(self) -> None:
        def flags(text: str) -> dict[str, str]:
            result = {}
            for line in chapters(text)[16][1].splitlines():
                match = re.match(r"^\| `([a-z_]+)` \| ([^|]+) \|", line)
                if match:
                    result[match.group(1)] = "W" if re.search(r"Schreib|Write", match.group(2)) else "R"
            return result

        self.assertEqual(flags(DE), flags(EN))
        self.assertEqual({name for name, flag in flags(DE).items() if flag == "W"}, {"approve_swing_promotion", "sql_execute"})

    def test_documented_status_terms_are_identical_in_both_languages(self) -> None:
        for term in ("PROMOTE", "KEEP_WATCHING", "ENTRY_READY", "WAIT_FOR_TRIGGER", "BLOCKED_BY_ALLOCATION", "BLOCKED_BY_DATA", "BLOCKED_BY_CONCENTRATION",
                     "BLOCKED_EXISTING_POSITION", "BLOCKED_EXISTING_CAMPAIGN", "DISCOVERY_READY", "DISCOVERY_WATCH", "NEWS_HIGH_ATTENTION", "NEWS_ATTENTION",
                     "NEWS_CLEAR", "NEWS_UNAVAILABLE", "STOPPED_CAPITAL_EXHAUSTED", "SELL", "TRIM", "HOLD"):
            self.assertIn(term, DE, term)
            self.assertIn(term, EN, term)

    def test_documented_tool_calls_use_real_parameters(self) -> None:
        spec = importlib.util.spec_from_file_location("trading_sqlite_docs_params", ROOT / "mcp-tools" / "trading_sqlite.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        allowed = {name: set(inspect.signature(getattr(module.Tools, name)).parameters) - {"self"} for name in tool_names()}
        allowed["run_trading_orchestrator"] |= {"detail"}  # added by the MCP adapter
        for text in (DE, EN):
            for tool, args in re.findall(r"`([a-z_]+)\(([^`]*)\)`", text):
                if tool not in allowed:
                    continue
                for key in re.findall(r"(\w+)=", args):
                    self.assertIn(key, allowed[tool], f"{tool}({key}=...)")

    def test_links_and_anchors_work(self) -> None:
        for name, text in (("de", DE), ("en", EN)):
            headings = {slug(h) for h, _ in chapters(text)}
            for label, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", text):
                if target.startswith("#"):
                    self.assertIn(target[1:], headings, f"{name}: {label}")
                else:
                    self.assertTrue((DOCS / target.split("#")[0]).resolve().is_file(), f"{name}: {target}")
        self.assertIn("trading-agent-query-examples.en.md", DE)
        self.assertIn("trading-agent-query-examples.de.md", EN)
        for target in ("docs/trading-agent-query-examples.de.md", "docs/trading-agent-query-examples.en.md"):
            self.assertIn(f"]({target})", README)
            self.assertTrue((ROOT / target).is_file())

    def test_examples_use_only_neutral_tickers_and_no_front_end_names_or_machine_paths(self) -> None:
        for text in (DE, EN):
            for symbols in re.findall(r'symbol="([^"]+)"', text):
                self.assertLessEqual(set(symbols.split(",")), NEUTRAL_SYMBOLS, symbols)
            for _heading, body in chapters(text)[:15]:
                for row in re.findall(r"^\| \d+ \| [„“”](.+?)[“”] \|", body, flags=re.M):
                    self.assertLessEqual(set(re.findall(r"\b[A-Z]{2,5}\b", row)), ALLOWED_UPPERCASE, row)
            self.assertIsNone(re.search(r"\b(Jan|Open ?WebUI|goose)\b", text))
            self.assertIsNone(re.search(r"C:\\", text))  # no machine-specific paths


if __name__ == "__main__":
    unittest.main()
