"""requirements-gui.txt: exact pins, no meta package, same mcp pin as the runtime requirements."""

from __future__ import annotations

import re
import unittest

import support


def pins(name: str) -> dict[str, str]:
    raw = (support.ROOT / name).read_text(encoding="utf-8")
    out = {}
    for line in (l.split("#", 1)[0].strip() for l in raw.splitlines()):
        if line:
            package, version = line.split("==")
            out[package] = version
    return out


class GuiRequirementsTests(unittest.TestCase):
    def test_exact_pins_of_exactly_two_direct_packages(self) -> None:
        raw = (support.ROOT / "requirements-gui.txt").read_text(encoding="utf-8")
        gui = pins("requirements-gui.txt")
        self.assertEqual(gui, {"PySide6-Essentials": "6.11.2", "mcp": "2.3.0"})
        for line in (l.split("#", 1)[0].strip() for l in raw.splitlines()):
            if line:
                self.assertRegex(line, r"^[A-Za-z0-9_.\-]+==\d+(\.\d+)*$")
        code = "\n".join(l.split("#", 1)[0] for l in raw.splitlines())  # comments may explain what is excluded
        self.assertNotRegex(code, r"(?im)^\s*PySide6\s*==")  # not the meta package (it pulls WebEngine and the Addons)
        self.assertNotRegex(code, r"(?i)webengine|addons|qasync|langchain|crewai")

    def test_the_mcp_pin_equals_the_runtime_pin(self) -> None:
        self.assertEqual(pins("requirements-gui.txt")["mcp"], pins("requirements.txt")["mcp"])

    def test_the_runtime_requirements_do_not_contain_the_gui(self) -> None:
        self.assertNotIn("PySide6-Essentials", pins("requirements.txt"))


if __name__ == "__main__":
    unittest.main()
