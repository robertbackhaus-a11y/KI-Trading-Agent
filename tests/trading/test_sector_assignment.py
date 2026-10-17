# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""set_security_sector and the two-layer sector map: resolution, the narrow local write, the risk guard and the readers; synthetic maps and securities only."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "tools" / "trading", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import market_intelligence as mi  # noqa: E402
import portfolio_risk as pr  # noqa: E402
import sector_assignment as sa  # noqa: E402
import test_watchlist_admin as twa  # noqa: E402

PUBLIC = {"map_id": "sector_map_v1", "version": 1, "description": "synthetic public map", "sectors": {"ALPHA": ["AAA", "BBB.DE"], "BETA": ["CCC"], "GAMMA": []}}
LOCAL_TEXT = ('{\n  "map_id": "sector_map_local",\n  "version": 1,\n  "description": "synthetic local map",\n  "sectors": {\n    "BETA": ["GGG"],\n    "LOCALONLY": ["OLD"]\n  }\n}\n')


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12] if path.exists() else "absent"


class SectorCase(twa.WatchlistDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.universe_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.universe_tmp.cleanup)
        self.universe = Path(self.universe_tmp.name)
        (self.universe / sa.PUBLIC_NAME).write_text(json.dumps(PUBLIC, indent=2), encoding="utf-8")
        self.public_digest = digest(self.universe / sa.PUBLIC_NAME)
        patcher = mock.patch.object(sa, "DEFAULT_UNIVERSE", self.universe)
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def local(self) -> Path:
        return self.universe / sa.LOCAL_NAME

    def set_sector(self, security_id, sector) -> dict:
        return asyncio.run(self.tools.set_security_sector(security_id, sector))

    def write_local(self, text: str = LOCAL_TEXT) -> None:
        self.local.write_text(text, encoding="utf-8")

    def layers(self) -> sa.Layers:
        return sa.read_layers(self.universe / sa.PUBLIC_NAME)

    def resolved(self, key: str) -> dict:
        return sa.resolve(self.layers(), key)


class ResolutionTests(SectorCase):
    def test_the_local_layer_overrides_the_public_one_and_the_groups_are_the_union(self) -> None:
        self.write_local('{"sectors": {"BETA": ["AAA"], "LOCALONLY": ["OLD"]}}')
        layers = self.layers()
        self.assertEqual(sa.effective(layers)["AAA"], "BETA")  # in both files: local wins, no error
        self.assertEqual(self.resolved("AAA"), {"sector": "BETA", "source": "local", "public": "ALPHA"})
        self.assertEqual(self.resolved("CCC"), {"sector": "BETA", "source": "public", "public": "BETA"})
        self.assertEqual(self.resolved("NOPE"), {"sector": None, "source": "none", "public": None})
        self.assertEqual(layers.groups, ("ALPHA", "BETA", "GAMMA", "LOCALONLY"))

    def test_the_readers_of_the_guards_and_of_the_news_mapping_use_the_same_rule(self) -> None:
        self.write_local('{"sectors": {"BETA": ["AAA"]}}')
        self.assertEqual(pr.read_sector_map(self.universe / sa.PUBLIC_NAME)["AAA"], "BETA")
        payload = sa.merged_payload(PUBLIC, json.loads(self.local.read_text(encoding="utf-8")))
        self.assertEqual(mi.load_sector_map(payload)["AAA"], "BETA")
        self.assertNotIn("AAA", payload["sectors"]["ALPHA"])  # removed from its public group, not listed twice
        spec = importlib.util.spec_from_file_location("collect_for_sector_tests", ROOT / "tools" / "trading" / "Collect-TradingMarketIntelligence.py")
        collect = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collect)
        self.assertEqual(collect.read_sector_map(self.universe / sa.PUBLIC_NAME)["AAA"], "BETA")

    def test_a_symbol_in_two_sectors_of_the_same_file_is_still_an_error(self) -> None:
        self.write_local('{"sectors": {"BETA": ["AAA"], "GAMMA": ["AAA"]}}')
        with self.assertRaises(sa.SectorMapError):
            self.layers()
        with self.assertRaises(ValueError):
            pr.read_sector_map(self.universe / sa.PUBLIC_NAME)


class SetSectorTests(SectorCase):
    def test_a_set_a_sector_for_a_symbol_without_an_entry_even_when_it_is_held(self) -> None:
        before = self.fingerprint(exclude=())
        result = self.set_sector(3, "ALPHA")  # security 3 is held and has an open campaign: a classification is not blocked
        self.assertEqual((result["ok"], result["result"], result["changed"], result["key"], result["sector"], result["source"]), (True, "SECTOR_SET", True, "GGG", "ALPHA", "local"))
        self.assertEqual(json.loads(self.local.read_text(encoding="utf-8"))["sectors"], {"ALPHA": ["GGG"]})
        self.assertEqual(digest(self.universe / sa.PUBLIC_NAME), self.public_digest)  # the public map is never touched
        self.assertEqual(self.fingerprint(exclude=()), before)  # no database change at all (strategy, campaign, position, watchlist, ...)

    def test_b_the_key_is_the_yahoo_symbol_like_for_the_guards(self) -> None:
        result = self.set_sector(2, "BETA")  # BBB has the Yahoo symbol BBB.DE, which the public map lists under ALPHA
        self.assertEqual((result["key"], result["result"], result["previous_sector"], result["previous_source"], result["public_sector"]), ("BBB.DE", "SECTOR_SET", "ALPHA", "public", "ALPHA"))
        self.assertEqual(self.resolved("BBB.DE"), {"sector": "BETA", "source": "local", "public": "ALPHA"})

    def test_c_an_override_can_be_changed_and_removed_and_the_public_value_returns(self) -> None:
        self.set_sector(1, "BETA")
        self.assertEqual(self.resolved("AAA")["sector"], "BETA")
        again = self.set_sector(1, "GAMMA")
        self.assertEqual((again["result"], again["previous_sector"], again["sector"]), ("SECTOR_SET", "BETA", "GAMMA"))
        removed = self.set_sector(1, None)
        self.assertEqual((removed["result"], removed["changed"], removed["sector"], removed["source"]), ("SECTOR_OVERRIDE_REMOVED", True, "ALPHA", "public"))  # falls back to the public value
        self.assertNotIn("AAA", [s for symbols in json.loads(self.local.read_text(encoding="utf-8"))["sectors"].values() for s in symbols])
        self.assertTrue((self.universe / sa.BACKUP_NAME).exists())  # the previous local file is kept

    def test_a_sector_equal_to_the_public_one_does_not_create_a_redundant_entry(self) -> None:
        self.assertEqual(self.set_sector(1, "ALPHA")["result"], "UNCHANGED")  # already ALPHA through the public map
        self.assertFalse(self.local.exists())
        self.set_sector(1, "BETA")
        back = self.set_sector(1, "ALPHA")  # the public value again: the override is removed instead of duplicated
        self.assertEqual((back["result"], back["source"]), ("SECTOR_OVERRIDE_REMOVED", "public"))

    def test_removing_without_an_override_changes_nothing(self) -> None:
        for value in (None, "none", ""):
            self.assertEqual(self.set_sector(1, value)["result"], "UNCHANGED")
        self.assertFalse(self.local.exists())

    def test_d_an_invalid_sector_is_blocked_and_nothing_is_written(self) -> None:
        self.write_local()
        before = digest(self.local)
        for value in ("NOPE", "beta", " Beta ", "ALPHA;DROP", 5, ["ALPHA"], True):
            with self.subTest(value=value):
                result = self.set_sector(1, value)
                self.assertEqual((result["ok"], result["result"], result["changed"]), (True, "INVALID_SECTOR", False))
        self.assertEqual(self.set_sector(1, "NOPE")["valid"], ["ALPHA", "BETA", "GAMMA", "LOCALONLY"])  # only existing groups are offered, none is invented
        self.assertEqual(digest(self.local), before)
        self.assertFalse((self.universe / sa.BACKUP_NAME).exists())

    def test_a_group_that_exists_only_in_the_local_map_is_valid(self) -> None:
        self.write_local()
        self.assertEqual(self.set_sector(1, "LOCALONLY")["result"], "SECTOR_SET")

    def test_e_unknown_and_invalid_ids(self) -> None:
        self.assertEqual(self.set_sector(9999, "ALPHA")["result"], "NOT_FOUND")
        for value in ("abc", None, 1.5, True, ["1"]):
            self.assertEqual(self.set_sector(value, "ALPHA")["result"], "INVALID_IDENTIFIER")
        self.assertFalse(self.local.exists())

    def test_a_security_without_any_symbol_cannot_be_keyed(self) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO security(id, name) VALUES (500, 'No Symbol Corp')")
            conn.commit()
        result = self.set_sector(500, "ALPHA")
        self.assertEqual((result["result"], result["changed"]), ("NO_MAP_KEY", False))

    def test_the_existing_local_file_keeps_its_content_and_format(self) -> None:
        self.write_local()
        self.set_sector(1, "BETA")
        text = self.local.read_text(encoding="utf-8")
        data = json.loads(text)
        self.assertEqual((data["map_id"], data["description"]), ("sector_map_local", "synthetic local map"))
        self.assertEqual(data["sectors"], {"BETA": ["GGG", "AAA"], "LOCALONLY": ["OLD"]})
        self.assertIn('    "BETA": ["GGG", "AAA"],\n', text)  # one line per group like the hand-maintained file
        self.assertTrue(text.endswith("}\n"))
        self.assertFalse(list(self.universe.glob("*.tmp")))

    def test_a_broken_or_missing_map_blocks_without_a_write(self) -> None:
        self.write_local("{not json")
        self.assertEqual(self.set_sector(1, "BETA")["result"], "LOCAL_MAP_INVALID")
        self.assertEqual(self.local.read_text(encoding="utf-8"), "{not json")
        self.write_local('{"sectors": {"BETA": ["AAA"], "GAMMA": ["AAA"]}}')
        self.assertEqual(self.set_sector(1, "BETA")["result"], "LOCAL_MAP_INVALID")
        (self.universe / sa.PUBLIC_NAME).unlink()
        self.assertEqual(self.set_sector(1, "BETA")["result"], "SECTOR_MAP_UNAVAILABLE")

    def test_a_failed_check_restores_the_previous_local_file(self) -> None:
        self.write_local()
        before = self.local.read_text(encoding="utf-8")
        real = sa.read_layers
        calls = []

        def wrong_after_the_write(path=None):
            calls.append(1)
            layers = real(path)
            return layers if len(calls) == 1 else sa.Layers(layers.public, {}, layers.groups, layers.local_exists)  # the second read does not show the new entry

        with mock.patch.object(sa, "read_layers", wrong_after_the_write):
            result = self.set_sector(1, "BETA")
        self.assertEqual((result["ok"], result["result"], result["changed"]), (False, "ERROR", False))
        self.assertEqual(self.local.read_text(encoding="utf-8"), before)

    def test_the_risk_guard_sees_the_new_effective_sector(self) -> None:
        public_path = self.universe / sa.PUBLIC_NAME
        with self.connect() as conn:
            self.assertEqual(pr.load_risk_inputs(conn, [1, 3], as_of=None, sector_map_path=public_path).sector_of, {1: "ALPHA", 3: None})
        self.set_sector(1, "BETA")
        self.set_sector(3, "GAMMA")
        with self.connect() as conn:
            self.assertEqual(pr.load_risk_inputs(conn, [1, 3], as_of=None, sector_map_path=public_path).sector_of, {1: "BETA", 3: "GAMMA"})
        self.set_sector(1, None)
        with self.connect() as conn:
            self.assertEqual(pr.load_risk_inputs(conn, [1], as_of=None, sector_map_path=public_path).sector_of, {1: "ALPHA"})  # the public value is back

    def test_nothing_in_the_database_changes_and_no_order_exists(self) -> None:
        before = self.fingerprint(exclude=())
        for sector in ("ALPHA", "BETA", None, "GAMMA"):
            self.set_sector(1, sector)
            self.set_sector(3, sector)
        self.assertEqual(self.fingerprint(exclude=()), before)
        with self.connect() as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%order%'")], [])


class WatchlistReadTests(SectorCase):
    def test_the_table_and_detail_shapes_carry_the_sector_and_its_source(self) -> None:
        self.write_local('{"sectors": {"BETA": ["AAA"]}}')
        self.run_tool("add_watchlist_candidate", "AAA")
        table = self.run_tool("get_watchlist", table=True)
        self.assertEqual(table["sector_groups"], ["ALPHA", "BETA", "GAMMA"])
        row = next(r for r in table["rows"] if r["security_id"] == 1)
        self.assertEqual((row["sector"], row["sector_source"], row["sector_public"]), ("BETA", "local", "ALPHA"))  # a local entry replaces the public value, visibly
        detail = self.run_tool("get_watchlist", symbol="AAA", detail=True)
        self.assertEqual(detail["rows"][0]["sector_source"], "local")
        self.assertIn("sector_groups", detail)
        other = next(r for r in table["rows"] if r["security_id"] == 3)
        self.assertEqual((other.get("sector"), other["sector_source"]), (None, "none"))

    def test_the_compact_chat_answer_is_unchanged(self) -> None:
        compact = self.run_tool("get_watchlist")
        self.assertNotIn("sector_groups", compact)
        for row in compact["rows"]:
            self.assertFalse({"sector", "sector_source", "sector_public"} & set(row))


class ToolSurfaceTests(unittest.TestCase):
    def test_the_signature_is_narrow(self) -> None:
        params = inspect.signature(twa.tools_class().set_security_sector).parameters
        self.assertEqual(sorted(p for p in params if p != "self"), ["sector", "security_id"])

    def test_the_module_never_writes_the_database_or_the_public_map(self) -> None:
        source = (ROOT / "tools" / "trading" / "sector_assignment.py").read_text(encoding="utf-8")
        code = re.sub(r'""".*?"""', "", source, flags=re.S)  # without the docstrings
        self.assertIsNone(re.search(r"INSERT INTO|UPDATE \w+ SET|DELETE FROM", code))  # it only reads the database (the key of a security)
        writes = re.findall(r"(?<!def )_write_atomically\(([^,]+),", source)
        self.assertTrue(all("local_path" in w for w in writes), writes)  # only the local file (and its backup) is ever written
        self.assertNotIn("PUBLIC_NAME).write", source)


if __name__ == "__main__":
    unittest.main()
