"""Guards the GitHub security configuration in .github/: minimal workflow permissions, SHA-pinned official actions, no risky triggers.

Text-based on purpose (no YAML dependency): it must also run in the Trading venv.  Whether GitHub accepts and runs the workflows can only be
confirmed on GitHub itself.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)\s*(?:#\s*(\S+))?\s*$", re.M)
OFFICIAL_OWNERS = ("actions/", "github/")


class WorkflowHardeningTests(unittest.TestCase):
    def test_the_expected_workflows_exist(self) -> None:
        self.assertEqual([p.name for p in WORKFLOWS], ["codeql.yml", "dependency-review.yml"])

    def test_every_workflow_declares_permissions_at_top_level_and_per_job(self) -> None:
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            self.assertRegex(text, r"(?m)^permissions:\s*\{\}\s*$", f"{path.name}: top-level permissions must be empty")
            jobs = re.findall(r"(?m)^  [A-Za-z0-9_-]+:\s*$", text.split("\njobs:", 1)[1])
            self.assertEqual(len(re.findall(r"(?m)^    permissions:\s*$", text)), len(jobs), f"{path.name}: each job needs its own permissions block")
            self.assertNotRegex(text, r"(?i)(write-all|permissions:\s*write)", path.name)

    def test_write_permissions_are_limited_to_code_scanning_upload(self) -> None:
        for path in WORKFLOWS:
            writes = re.findall(r"(?m)^\s+([a-z-]+):\s*write\b", path.read_text(encoding="utf-8"))
            self.assertEqual(writes, ["security-events"] if path.name == "codeql.yml" else [], path.name)

    def test_no_untrusted_trigger_or_secret_or_injection_surface(self) -> None:
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("pull_request_target", text, path.name)
            self.assertNotIn("workflow_run", text, path.name)
            self.assertNotRegex(text, r"\$\{\{\s*(github\.event\.|github\.head_ref|secrets\.)", path.name)
            self.assertNotRegex(text, r"(?m)^\s+run:", f"{path.name}: no shell steps")

    def test_actions_are_official_and_pinned_to_a_full_commit_sha_with_a_version_comment(self) -> None:
        for path in WORKFLOWS:
            uses = USES.findall(path.read_text(encoding="utf-8"))
            self.assertTrue(uses, path.name)
            for ref, comment in uses:
                name, _, sha = ref.partition("@")
                self.assertTrue(name.startswith(OFFICIAL_OWNERS), f"{path.name}: {name} is not an official action")
                self.assertRegex(sha, r"^[0-9a-f]{40}$", f"{path.name}: {name} must be pinned to a commit SHA")
                self.assertRegex(comment or "", r"^v\d+\.\d+\.\d+$", f"{path.name}: {name} needs a '# vX.Y.Z' comment")

    def test_checkout_does_not_persist_credentials(self) -> None:
        for path in WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("actions/checkout@"), text.count("persist-credentials: false"), path.name)

    def test_codeql_configuration(self) -> None:
        text = (ROOT / ".github" / "workflows" / "codeql.yml").read_text(encoding="utf-8")
        for needle in ("languages: python", "build-mode: none", "queries: security-extended", "branches: [main]", "cron:"):
            self.assertIn(needle, text)
        self.assertEqual(len(re.findall(r"(?m)^\s+languages:", text)), 1)  # one language, no matrix

    def test_dependabot_is_weekly_low_volume_and_only_for_existing_manifests(self) -> None:
        text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        self.assertIn("package-ecosystem: github-actions", text)
        ecosystems = re.findall(r"(?m)^\s*-\s*package-ecosystem:\s*(\S+)", text)
        self.assertEqual(len(re.findall(r"interval: weekly", text)), len(ecosystems))
        self.assertEqual(len(re.findall(r"open-pull-requests-limit:\s*[1-3]\b", text)), len(ecosystems))
        self.assertNotIn("interval: daily", text)
        manifests = [p for pattern in ("requirements*.txt", "pyproject.toml", "Pipfile", "poetry.lock", "uv.lock") for p in ROOT.rglob(pattern)
                     if ".git" not in p.parts]
        # pip is configured exactly when a manifest exists; major updates are never proposed
        self.assertEqual("pip" in ecosystems, bool(manifests))
        if manifests:
            self.assertIn("version-update:semver-major", text)

    def test_requirements_are_exact_pins_of_direct_packages_without_local_paths(self) -> None:
        raw = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        packages = [line for line in (l.split("#", 1)[0].strip() for l in raw.splitlines()) if line]
        for line in packages:
            self.assertRegex(line, r"^[A-Za-z0-9_.\-]+==\d+(\.\d+)*$", line)
        self.assertEqual(sorted(line.split("==")[0] for line in packages), ["mcp", "pydantic", "pypdf"])
        self.assertNotRegex(raw, r"(?i)[a-z]:\\|/users/|file://|git\+|^-e ")

    def test_requirements_match_the_third_party_imports_of_the_code(self) -> None:
        local = {p.stem for p in ROOT.rglob("*.py") if ".git" not in p.parts} | {p.stem.replace("-", "_") for p in ROOT.rglob("*.py")}
        imported = set()
        for root in ("tools", "mcp-tools", "deploy", "scheduler", "tests"):
            for path in (ROOT / root).rglob("*.py"):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else []
                    imported.update(n.split(".")[0] for n in names)
        third_party = {n for n in imported if n not in sys.stdlib_module_names and n not in local and n != "tools"}
        self.assertEqual(third_party, {"mcp", "pydantic", "pypdf"})  # a new import needs a requirements.txt entry (and vice versa)


if __name__ == "__main__":
    unittest.main()
