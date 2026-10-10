"""Documentation language rules: README and the project meta files are English only; every technical document under docs/ exists as a complete
German + English pair (<name>.de.md / <name>.en.md) with the same structure and the same technical terms; links work; no private data."""

from __future__ import annotations

import re
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
CODE_SPAN = re.compile(r"`([^`]+)`")
FENCE = re.compile(r"^```.*?^```", re.S | re.M)
GERMAN_WORDS = {"und", "ist", "nicht", "für", "mit", "wird", "werden", "eine", "oder", "sind", "keine", "auf", "nach", "bei", "wie", "dass", "sowie", "kein", "nur", "auch"}
ENGLISH_WORDS = {"the", "and", "with", "for", "is", "not", "are", "of", "to", "from", "that", "this", "only", "also", "which"}
ENGLISH_ONLY_FILES = ["README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md"]


def prose(text: str) -> str:
    """Text without fenced blocks, code spans, link targets and the one-line language switch."""
    text = FENCE.sub("", text)
    text = CODE_SPAN.sub("", text)
    text = re.sub(r"\]\([^)]*\)", "]", text)
    return "\n".join(line for line in text.splitlines() if not line.startswith(("Deutsche Version:", "English version:")))


def words(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-zÄÖÜäöüß]+", text)]


def doc_pairs() -> dict[str, dict[str, Path]]:
    pairs: dict[str, dict[str, Path]] = {}
    for path in sorted(DOCS.glob("*.md")):
        match = re.fullmatch(r"(.+)\.(de|en)\.md", path.name)
        pairs.setdefault(match.group(1) if match else path.name, {})[match.group(2) if match else "none"] = path
    return pairs


def sections(text: str) -> list[str]:
    return re.split(r"^(?=#{1,6} )", FENCE.sub(lambda m: "\n", text), flags=re.M)


def headings(text: str) -> list[str]:
    return [line for line in FENCE.sub("", text).splitlines() if re.match(r"#{1,6} ", line)]


def slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.lower().lstrip("# ").strip(), flags=re.U).replace(" ", "-")


class DocumentationLanguageTests(unittest.TestCase):
    def test_every_doc_is_a_complete_german_english_pair_and_none_is_language_less(self) -> None:
        pairs = doc_pairs()
        self.assertEqual([name for name, langs in pairs.items() if "none" in langs], [], "language-less documents must be renamed to <name>.de.md / <name>.en.md")
        self.assertEqual([name for name, langs in pairs.items() if set(langs) != {"de", "en"}], [])
        self.assertGreaterEqual(set(pairs), {"trading-agent-architecture", "trading-agent-query-examples", "trading-candidate-decision", "trading-strategy-suggestion",
                                             "trading-swing-promotion", "trading-entry-recommendations"})

    def test_german_and_english_have_the_same_structure_and_technical_terms(self) -> None:
        for name, langs in doc_pairs().items():
            de, en = langs["de"].read_text(encoding="utf-8"), langs["en"].read_text(encoding="utf-8")
            self.assertEqual([h.split(" ")[0] for h in headings(de)], [h.split(" ")[0] for h in headings(en)], f"{name}: heading levels")
            de_sections, en_sections = sections(de), sections(en)
            self.assertEqual(len(de_sections), len(en_sections), name)
            for d, e in zip(de_sections, en_sections):
                title = e.splitlines()[0][:60] if e.strip() else "(start)"
                self.assertEqual(len(re.findall(r"^\|", d, flags=re.M)), len(re.findall(r"^\|", e, flags=re.M)), f"{name}: table rows in {title}")
                self.assertEqual(len(re.findall(r"^\s*(?:- |\d+\. )", d, flags=re.M)), len(re.findall(r"^\s*(?:- |\d+\. )", e, flags=re.M)), f"{name}: list items in {title}")
                self.assertEqual(Counter(CODE_SPAN.findall(d)), Counter(CODE_SPAN.findall(e)), f"{name}: technical terms in {title}")
            self.assertEqual(len(re.findall(r"^```", de, flags=re.M)), len(re.findall(r"^```", en, flags=re.M)), f"{name}: code blocks")

    def test_each_language_file_is_written_in_one_language_only(self) -> None:
        for name, langs in doc_pairs().items():
            de_words, en_words = words(prose(langs["de"].read_text(encoding="utf-8"))), words(prose(langs["en"].read_text(encoding="utf-8")))
            de_in_de, en_in_de = sum(w in GERMAN_WORDS for w in de_words), sum(w in ENGLISH_WORDS for w in de_words)
            de_in_en, en_in_en = sum(w in GERMAN_WORDS for w in en_words), sum(w in ENGLISH_WORDS for w in en_words)
            self.assertGreater(de_in_de, 3 * en_in_de, f"{name}.de.md looks mixed or English (de={de_in_de}, en={en_in_de})")
            self.assertGreater(en_in_en, 3 * de_in_en, f"{name}.en.md looks mixed or German (en={en_in_en}, de={de_in_en})")
            self.assertEqual(len(re.findall(r"[äöüßÄÖÜ]", prose(langs["en"].read_text(encoding="utf-8")))), 0, f"{name}.en.md contains German letters")

    def test_readme_and_project_meta_files_are_english_only(self) -> None:
        for filename in ENGLISH_ONLY_FILES:
            text = prose((ROOT / filename).read_text(encoding="utf-8"))
            self.assertEqual(re.findall(r"[äöüßÄÖÜ]", text), [], f"{filename}: German letters")
            tokens = words(text)
            german = sum(w in GERMAN_WORDS for w in tokens)
            english = sum(w in ENGLISH_WORDS for w in tokens)
            self.assertLessEqual(german, 2, f"{filename}: German words ({german})")
            self.assertGreater(english, 5 * max(german, 1), filename)

    def test_readme_version_matches_the_top_changelog_entry_and_keeps_the_history(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        versions = re.findall(r"^## \[?v?(\d+\.\d+\.\d+)\]?", changelog, flags=re.M)
        self.assertEqual(re.search(r"^Version: v(\d+\.\d+\.\d+) — see \[CHANGELOG\.md\]\(CHANGELOG\.md\)\.$", readme, flags=re.M).group(1), versions[0])
        self.assertEqual(versions[-2:], ["0.1.1", "0.1.0"])  # the history stays below the newest entries
        self.assertIn("0.2.0", versions)
        self.assertNotRegex(readme.lower(), r"release preparation|in preparation|under way")

    def test_readme_links_both_language_versions_of_every_document(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in doc_pairs():
            for lang in ("de", "en"):
                self.assertIn(f"](docs/{name}.{lang}.md", readme, f"README does not link docs/{name}.{lang}.md")

    def test_language_switch_links_exist_in_both_directions(self) -> None:
        for name, langs in doc_pairs().items():
            self.assertIn(f"[{name}.en.md]({name}.en.md)", langs["de"].read_text(encoding="utf-8"), name)
            self.assertIn(f"[{name}.de.md]({name}.de.md)", langs["en"].read_text(encoding="utf-8"), name)

    def test_all_markdown_links_and_anchors_resolve_and_no_language_less_doc_is_referenced(self) -> None:
        files = [ROOT / name for name in ENGLISH_ONLY_FILES] + sorted(DOCS.glob("*.md"))
        for path in files:
            text = FENCE.sub("", path.read_text(encoding="utf-8"))
            for target in re.findall(r"\]\(([^)\s]+)\)", text):
                if re.match(r"[a-z]+://", target):
                    continue
                file_part, _, anchor = target.partition("#")
                resolved = (path.parent / file_part).resolve() if file_part else path
                self.assertTrue(resolved.is_file(), f"{path.name}: broken link {target}")
                if anchor and resolved.suffix == ".md":
                    self.assertIn(anchor, {slug(h) for h in headings(resolved.read_text(encoding="utf-8"))}, f"{path.name}: anchor {target}")
        for path in [*ROOT.glob("tools/**/*.py"), *ROOT.glob("mcp-tools/*.py"), *ROOT.glob("tests/**/*.py"), *ROOT.glob("*.md"), *DOCS.glob("*.md")]:
            self.assertEqual(re.findall(r"docs/[a-z-]+\.md\b", path.read_text(encoding="utf-8")), [], f"{path.name} references a language-less doc file")

    def test_no_front_end_names_personal_names_or_machine_users_in_the_documentation(self) -> None:
        for path in [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md", *DOCS.glob("*.md")]:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"\b(Jan|Open ?WebUI|goose|okami)\b", text), path.name)
            self.assertIsNone(re.search(r"C:\\Users\\", text), path.name)


if __name__ == "__main__":
    unittest.main()
