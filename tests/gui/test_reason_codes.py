"""Presentation of technical reason codes (A-P): short German text, tooltip with description and original code, readable fallback."""

from __future__ import annotations

import ast
import copy
import html
import re
import unittest
from datetime import datetime

import support
from support import HAVE_QT, qapp

from gui.viewmodels import reason_codes as rc
from gui.viewmodels.portfolio import build_dashboard, build_planner
from gui.viewmodels.reason_codes import (
    KNOWN_REASON_CODES,
    UNKNOWN_DESCRIPTION,
    Presented,
    format_reason_code,
    format_reason_codes,
    get_reason_presentation,
)

try:
    import fixtures

    OPEN = fixtures.orchestrator_envelope(5_000.0)
    BLOCKED = fixtures.orchestrator_envelope(40_000.0)
    SKIP = ""
except ImportError as exc:  # the trading modules are needed to build the fixtures
    OPEN = BLOCKED = None
    SKIP = str(exc)

if HAVE_QT:
    from gui.views.dashboard import DashboardView
    from gui.views.planner import PlannerView

QT = unittest.skipUnless(HAVE_QT, "PySide6 not installed (GUI venv only)")
RAW = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$")  # what a raw technical code looks like
NOW = datetime(2026, 10, 13, 9, 0, 0)

REQUESTED = {  # the codes and texts named for this block (A-H)
    "STOPPED_SWING_MAX_REACHED": "Swing-Limit erreicht",
    "EXISTING_POSITION_DEFAULT_HOLD": "Bestehende Position – halten",
    "BLOCKED_BY_SECTOR_CONCENTRATION": "Sektorlimit erreicht",
    "BLOCKED_BY_CORRELATION": "Zu stark korreliert",
    "WAIT_FOR_TRIGGER": "Einstiegssignal fehlt",
    "BLOCKED_BY_EXISTING_POSITION": "Position bereits vorhanden",
    "STRATEGY_ASSIGNMENT_REQUIRED": "Strategiezuordnung fehlt",
    "MARKET_DATA_MISSING": "Marktdaten fehlen",
}


def plain(tip: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "\n", tip))


def realistic(envelope: dict, *, reasons=("EXISTING_POSITION_DEFAULT_HOLD", "TP1_REACHED"), candidate_codes=("ENTRY_READY", "SCORE_BELOW_THRESHOLD")) -> dict:
    """The synthetic envelope with the codes the real engine produces (the fixtures use placeholder codes)."""
    env = copy.deepcopy(envelope)
    res = env["result"]
    for record in res["existing_position_results"]:
        record["decision"]["reasons"] = list(reasons)
    plan = res["portfolio_action_plan"]
    for cand in plan["entry_candidates"]:
        cand["reason_codes"] = list(candidate_codes)
        cand["entry_status"] = "WAIT_FOR_TRIGGER"
    plan["limiting_guard"] = plan.get("limiting_guard") or "STOPPED_SWING_MAX_REACHED"
    plan.setdefault("capital", {})["limiting_guard"] = "STOPPED_SWING_MAX_REACHED"
    plan["warnings"] = ["EXTERNAL_FUNDING_REQUIRED_FOR_PLAN", "RISK_SECTOR_UNKNOWN:2", "SOME_NEW_WARNING"]
    res["global_issues"] = [{"code": "ALLOCATION_OUTSIDE_TARGET", "severity": "NON_BLOCKING_WARNING"}, {"code": "SOME_NEW_ISSUE", "severity": "GLOBAL_BLOCKING", "symbol": "AAA"}]
    res["portfolio_context"]["allocation_guardrails"] = ["SWING_ALLOCATION_BELOW_MIN", "LONG_TERM_ALLOCATION_ABOVE_MAX"]
    return env


class PresentationTests(unittest.TestCase):
    # A - H
    def test_a_to_h_the_requested_codes_have_their_short_text(self) -> None:
        for code, text in REQUESTED.items():
            with self.subTest(code=code):
                self.assertEqual(format_reason_code(code), text)
                self.assertEqual(get_reason_presentation(code).short_text, text)
                self.assertEqual(get_reason_presentation(code).technical_code, code)

    def test_the_requested_descriptions_are_exact(self) -> None:
        self.assertEqual(get_reason_presentation("STOPPED_SWING_MAX_REACHED").description,
                         "Der maximal zulässige Anteil für Swing-Positionen ist erreicht, daher wird kein weiterer Einstieg geplant.")
        self.assertEqual(get_reason_presentation("BLOCKED_BY_CORRELATION").description,
                         "Der Kandidat würde zusammen mit bereits gehaltenen oder geplanten Positionen ein zu stark korreliertes Klumpenrisiko erzeugen.")
        self.assertEqual(get_reason_presentation("MARKET_DATA_MISSING").description, "Für eine belastbare Bewertung fehlen aktuelle oder ausreichende Kursdaten.")

    # I, J
    def test_i_j_the_tooltip_has_the_description_and_the_original_code(self) -> None:
        for code in REQUESTED:
            with self.subTest(code=code):
                tip = format_reason_code(code).tip
                self.assertIn(get_reason_presentation(code).description, plain(tip))
                self.assertIn(f"Code: {code}", plain(tip))
        self.assertEqual(plain(format_reason_code("STOPPED_SWING_MAX_REACHED").tip).split(), (
            "Der maximal zulässige Anteil für Swing-Positionen ist erreicht, daher wird kein weiterer Einstieg geplant. Code: STOPPED_SWING_MAX_REACHED").split())

    # K, L
    def test_k_an_unknown_code_is_readable_and_keeps_its_original_in_the_tooltip(self) -> None:
        self.assertEqual(format_reason_code("FOO_BAR_BAZ"), "Foo bar baz")
        tip = plain(format_reason_code("FOO_BAR_BAZ").tip)
        self.assertIn(UNKNOWN_DESCRIPTION, tip)
        self.assertIn("Code: FOO_BAR_BAZ", tip)
        self.assertEqual(format_reason_code("SINGLE"), "Single")

    def test_l_nothing_raises_and_nothing_is_empty(self) -> None:
        for value in (None, "", "   ", 0, 5, 1.5, True, [], {}, (), object(), b"x", "lower case text", "A:B:C", "X" * 5000, "UPPER:", ":X", "_", "A__B", "<script>x</script>", "ÄÖÜ_ß", "FOO:", "\n"):
            with self.subTest(value=repr(value)[:30]):
                shown = format_reason_code(value)
                self.assertIsInstance(shown, str)
                self.assertTrue(shown.strip())
                self.assertIsInstance(shown.tip, str)
                self.assertTrue(format_reason_codes([value, value]).strip())
        self.assertEqual(format_reason_code(None), rc.EMPTY)
        self.assertEqual(format_reason_codes([]), rc.EMPTY)

    def test_free_text_is_shown_as_it_is_without_a_made_up_tooltip(self) -> None:
        shown = format_reason_code("capital state unavailable")
        self.assertEqual((shown, shown.tip), ("capital state unavailable", ""))

    def test_a_detail_after_a_colon_is_kept_and_the_tooltip_names_the_full_code(self) -> None:
        shown = format_reason_code("RISK_SECTOR_UNKNOWN:3")
        self.assertEqual(shown, "Sektor unbekannt (3)")
        self.assertIn("Code: RISK_SECTOR_UNKNOWN:3", plain(shown.tip))
        self.assertEqual(format_reason_code("NEW_THING:AAA"), "New thing (AAA)")

    def test_several_codes_in_one_cell(self) -> None:
        shown = format_reason_codes(["EXISTING_POSITION_DEFAULT_HOLD", "TP1_REACHED", None, ""])
        self.assertEqual(shown, "Bestehende Position – halten, Kursziel 1 erreicht")
        tip = plain(shown.tip)
        self.assertIn("Code: EXISTING_POSITION_DEFAULT_HOLD", tip)
        self.assertIn("Code: TP1_REACHED", tip)
        self.assertEqual(format_reason_codes(["TP1_REACHED"]).tip, format_reason_code("TP1_REACHED").tip)

    def test_the_presented_text_is_a_plain_string_for_every_consumer(self) -> None:
        shown = format_reason_code("WAIT_FOR_TRIGGER")
        self.assertEqual(shown, "Einstiegssignal fehlt")
        self.assertEqual(f"x {shown}", "x Einstiegssignal fehlt")
        self.assertEqual(hash(shown), hash("Einstiegssignal fehlt"))
        self.assertEqual([str(shown)], ["Einstiegssignal fehlt"])
        self.assertIsInstance(Presented("t", "tip"), str)

    def test_the_table_is_clean(self) -> None:
        tree = ast.parse((support.ROOT / "gui" / "viewmodels" / "reason_codes.py").read_text(encoding="utf-8"))
        table = next(n.value for n in ast.walk(tree) if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "_TEXTS")
        keys = [k.value for k in table.keys]
        self.assertEqual(len(keys), len(set(keys)), "duplicate code in the table")
        self.assertEqual(set(keys), set(KNOWN_REASON_CODES))
        for code, (short, description) in rc._TEXTS.items():
            with self.subTest(code=code):
                self.assertRegex(code, r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$")
                self.assertTrue(short and len(short) <= 40, short)
                self.assertFalse(RAW.match(short), short)
                self.assertTrue(description.endswith(".") and len(description) > 20, description)
                self.assertNotIn("<", short + description)

    def test_the_codes_of_the_planner_and_the_engines_are_all_mapped(self) -> None:
        import portfolio_action_planner as planner
        import portfolio_risk as risk
        from analysis_contracts import AvailabilityStatus

        produced = set(planner.ENTRY_STATUSES) | set(planner._REASON_STATUS) | set(planner._REASON_STATUS.values()) | set(planner._DEFERRAL_REASON) | set(planner._DEFERRAL_REASON.values())
        produced |= {risk.SECTOR_BLOCK, risk.CORRELATION_BLOCK}
        produced |= {f"TECHNICAL_DATA_{s.name}" for s in AvailabilityStatus if s is not AvailabilityStatus.AVAILABLE}
        produced |= {"STOPPED_SWING_MAX_REACHED", "STOPPED_CASH_RESERVE_REACHED", "STOPPED_CAPITAL_EXHAUSTED", "ALL_READY_CANDIDATES_PLANNED", "NO_ENTRY_PLANNED", "ENTRY_BLOCKED"}
        produced |= {"OK", "BLOCKED", "PARTIAL", "UNAVAILABLE", "SECTOR_UNKNOWN", "NO_OTHER_POSITION", "AVAILABLE", "GLOBAL_BLOCKING"}
        produced |= {"EXISTING_POSITION_DEFAULT_HOLD", "HARD_STOP_TRIGGERED", "HARD_STOP_EVALUATION_BLOCKED", "SWING_MAX_HORIZON_REACHED", "RUNNER_BELOW_SMA200", "RUNNER_BELOW_SMA50",
                     "RUNNER_TECHNICAL_INPUT_UNAVAILABLE", "TP1_REACHED", "TP2_REACHED", "TP1_TRIM_RECOMMENDED", "TP2_CUMULATIVE_TRIM_RECOMMENDED", "ADD_RECOMMENDED",
                     "NO_POSITION_DEFAULT_WATCH", "NEW_ENTRY_RECOMMENDED", "POSITION_STATE_DATA_BLOCKER", "TECHNICAL_DATA_BLOCKER"}
        self.assertEqual(sorted(c for c in produced if c not in KNOWN_REASON_CODES), [])

    def test_the_orchestrator_and_sizing_codes_are_all_mapped(self) -> None:
        source = {name: (support.ROOT / "tools" / "trading" / f"{name}.py").read_text(encoding="utf-8") for name in ("trading_orchestrator", "entry_sizing")}
        issues = set(re.findall(r'_issue\("(?:GLOBAL|SECURITY)", "([A-Z_]+)"', source["trading_orchestrator"]))
        blocks = set(re.findall(r'"(ENTRY_[A-Z0-9_]+)"', source["entry_sizing"]))
        self.assertGreaterEqual(len(issues), 10)
        self.assertGreaterEqual(len(blocks), 15)
        self.assertEqual(sorted(c for c in issues | blocks if c not in KNOWN_REASON_CODES), [])


@unittest.skipIf(OPEN is None, SKIP)
class ViewModelTests(unittest.TestCase):
    def cells(self, table) -> list[str]:
        return [str(c) for row in table.rows for c in row]

    # M
    def test_m_the_planner_model_has_no_raw_code_left(self) -> None:
        for base in (OPEN, BLOCKED):
            vm = build_planner(realistic(base), NOW)
            for name in ("positions", "planned", "deferred", "candidates"):
                for cell in self.cells(getattr(vm, name)):
                    for part in cell.split(", "):
                        self.assertFalse(RAW.match(part), f"{name}: raw code {part!r}")
            for _, value in vm.header:
                self.assertFalse(RAW.match(value), value)
            for note in vm.notes:
                self.assertFalse(RAW.match(note), note)
        vm = build_planner(realistic(BLOCKED), NOW)
        self.assertIn("Sektorlimit erreicht", [r[-1] for r in vm.deferred.rows])
        position = next(r for r in vm.positions.rows if r[0] == "AAA")
        self.assertEqual(position[6], "Bestehende Position – halten, Kursziel 1 erreicht")
        candidate = next(r for r in vm.candidates.rows if r[1] == "BBB")
        self.assertEqual((candidate[2], candidate[4]), ("Einstiegssignal fehlt", "Einstieg möglich, Score unter Schwelle"))
        self.assertEqual(vm.notes[:2], ["External Funding nötig", "Sektor unbekannt (2)"])
        self.assertEqual(vm.notes[2], "Some new warning")  # an unknown warning is readable too

    # N
    def test_n_the_dashboard_model_has_no_raw_code_left(self) -> None:
        for base in (OPEN, BLOCKED):
            vm = build_dashboard(realistic(base), {"ok": True}, NOW)
            for _, value in (*vm.portfolio, *vm.planner):
                self.assertFalse(RAW.match(value), value)
                for part in value.split(", "):
                    self.assertFalse(RAW.match(part), part)
            for name in ("planned", "deferred"):
                for cell in self.cells(getattr(vm, name)):
                    self.assertFalse(RAW.match(cell), cell)
            for _, text in vm.issues:
                self.assertFalse(RAW.match(text.split(" (")[0].removeprefix("Planner: ")), text)
        vm = build_dashboard(realistic(BLOCKED), {"ok": True}, NOW)
        pl, pf = dict(vm.planner), dict(vm.portfolio)
        self.assertEqual((pl["Planner-Status"], pl["Globaler Status"], pl["Begrenzung"]), ("Verfügbar", "Verfügbar", "Swing-Limit erreicht"))
        self.assertEqual(pf["Allokations-Hinweise"], "Swing-Anteil unter Ziel, Long-Term-Anteil über Ziel")
        self.assertEqual([r[-1] for r in vm.deferred.rows], ["Sektorlimit erreicht"])
        texts = {text: level for level, text in vm.issues}
        self.assertEqual(texts["Aufteilung außerhalb Ziel"], "warning")
        self.assertEqual(texts["Some new issue (AAA)"], "blocker")  # the unknown code is readable and keeps its level
        self.assertIn("Planner: External Funding nötig", texts)

    def test_a_missing_value_still_shows_a_dash(self) -> None:
        env = realistic(OPEN)
        env["result"]["portfolio_action_plan"]["capital"].pop("limiting_guard", None)
        env["result"]["global_status"] = None
        vm = build_dashboard(env, None, NOW)
        self.assertEqual(dict(vm.planner)["Begrenzung"], "—")
        self.assertEqual(dict(vm.planner)["Globaler Status"], "—")

    # O
    def test_o_the_backend_payload_stays_unchanged(self) -> None:
        for base in (OPEN, BLOCKED):
            env = realistic(base)
            before = copy.deepcopy(env)
            build_dashboard(env, {"ok": True}, NOW)
            build_planner(env, NOW)
            self.assertEqual(env, before)
        reasons = realistic(OPEN)["result"]["existing_position_results"][0]["decision"]["reasons"]
        self.assertEqual(reasons, ["EXISTING_POSITION_DEFAULT_HOLD", "TP1_REACHED"])  # the codes in the payload are the original ones

    def test_the_backend_and_the_gui_services_do_not_know_the_presentation(self) -> None:
        for folder in (support.ROOT / "tools" / "trading", support.ROOT / "mcp-tools", support.ROOT / "gui" / "services"):
            for path in folder.glob("*.py"):
                text = path.read_text(encoding="utf-8")
                for name in ("viewmodels.reason_codes", "format_reason_code", "get_reason_presentation", "Presented"):
                    self.assertNotIn(name, text, f"{path.name} must not know the presentation ({name})")
        self.assertNotIn("PySide6", (support.ROOT / "gui" / "viewmodels" / "reason_codes.py").read_text(encoding="utf-8"))

    def test_an_error_code_without_a_message_is_shown_readable(self) -> None:
        self.assertEqual(build_planner({"ok": False, "error": "ORCHESTRATOR_FAILED"}).error, "Orchestrator failed")
        self.assertEqual(build_planner({"ok": False, "error": "ORCHESTRATOR_FAILED", "message": "unable to open database file"}).error, "unable to open database file")


@QT
@unittest.skipIf(OPEN is None, SKIP)
class OffscreenViewTests(unittest.TestCase):
    def setUp(self) -> None:
        qapp()

    def table_items(self, table):
        return [table.item(r, c) for r in range(table.rowCount()) for c in range(table.columnCount())]

    # M, N, P
    def test_p_the_planner_tables_show_the_short_text_and_set_the_tooltip(self) -> None:
        view = PlannerView()
        view.show_vm(build_planner(realistic(BLOCKED), NOW))
        deferred = view.tables["deferred"]
        item = deferred.item(0, deferred.columnCount() - 1)
        self.assertEqual(item.text(), "Sektorlimit erreicht")
        tip = plain(item.toolTip())
        self.assertIn("Ein weiterer Kauf würde den Anteil dieses Sektors im Portfolio über das festgelegte Limit erhöhen.", tip)
        self.assertIn("Code: BLOCKED_BY_SECTOR_CONCENTRATION", tip)
        positions = view.tables["positions"]
        reasons = next(positions.item(r, 6) for r in range(positions.rowCount()) if positions.item(r, 0).text() == "AAA")
        self.assertEqual(reasons.text(), "Bestehende Position – halten, Kursziel 1 erreicht")
        self.assertIn("Code: EXISTING_POSITION_DEFAULT_HOLD", plain(reasons.toolTip()))
        self.assertIn("Code: TP1_REACHED", plain(reasons.toolTip()))
        candidates = view.tables["candidates"]
        for item in self.table_items(candidates):
            self.assertFalse(RAW.match(item.text()), item.text())
        status = next(candidates.item(r, 2) for r in range(candidates.rowCount()) if candidates.item(r, 1).text() == "BBB")
        self.assertEqual(status.text(), "Einstiegssignal fehlt")
        self.assertIn("Code: WAIT_FOR_TRIGGER", plain(status.toolTip()))
        notes = [view.notes.item(i) for i in range(view.notes.count())]
        self.assertEqual(notes[1].text(), "Sektor unbekannt (2)")
        self.assertIn("Code: RISK_SECTOR_UNKNOWN:2", plain(notes[1].toolTip()))
        self.assertIn("Code: SOME_NEW_WARNING", plain(notes[2].toolTip()))

    def test_p_the_dashboard_shows_the_short_text_and_sets_the_tooltip(self) -> None:
        view = DashboardView()
        view.show_vm(build_dashboard(realistic(BLOCKED), {"ok": True}, NOW))
        grund = view.deferred.item(0, view.deferred.columnCount() - 1)
        self.assertEqual(grund.text(), "Sektorlimit erreicht")
        self.assertIn("Code: BLOCKED_BY_SECTOR_CONCENTRATION", plain(grund.toolTip()))
        for table in (view.planned, view.deferred):
            for item in self.table_items(table):
                self.assertFalse(RAW.match(item.text()), item.text())
        labels = {}
        for form in (view.portfolio_form, view.planner_form):
            for row in range(form.rowCount()):
                label, value = form.itemAt(row, form.ItemRole.LabelRole).widget(), form.itemAt(row, form.ItemRole.FieldRole).widget()
                labels[label.text()] = value
        limit = labels["Begrenzung:"]
        self.assertEqual(limit.text(), "Swing-Limit erreicht")
        self.assertIn("Code: STOPPED_SWING_MAX_REACHED", plain(limit.toolTip()))
        status = labels["Planner-Status:"]
        self.assertEqual(status.text(), "Verfügbar")
        self.assertIn("Code: AVAILABLE", plain(status.toolTip()))
        self.assertEqual(labels["Datenbank:"].toolTip(), "")  # plain values have no tooltip
        issues = [view.issues.item(i) for i in range(view.issues.count())]
        self.assertEqual(issues[0].text(), "Aufteilung außerhalb Ziel")
        self.assertIn("Code: ALLOCATION_OUTSIDE_TARGET", plain(issues[0].toolTip()))
        for item in issues:
            self.assertFalse(RAW.match(item.text()), item.text())

    def test_plain_cells_keep_their_text_as_tooltip(self) -> None:
        view = DashboardView()
        view.show_vm(build_dashboard(realistic(OPEN), {"ok": True}, NOW))
        rank = view.planned.item(0, 0)
        self.assertEqual(rank.toolTip(), rank.text())



class ContextTests(unittest.TestCase):
    """EXISTING_POSITION_DEFAULT_HOLD next to the action of its row (A-F) and a status that equals its reason (G-J)."""

    HOLD_TEXT = "Bestehende Position – halten"
    HOLD_CODE = "EXISTING_POSITION_DEFAULT_HOLD"

    # A, B
    def test_a_b_without_a_contrary_action_it_says_hold(self) -> None:
        for action in (None, "HOLD", "WATCH", "BUY", "", "unknown_action", 5):
            with self.subTest(action=action):
                self.assertEqual(get_reason_presentation(self.HOLD_CODE, action).short_text, self.HOLD_TEXT)
                self.assertEqual(format_reason_code(self.HOLD_CODE, action), self.HOLD_TEXT)
        self.assertEqual(format_reason_code(self.HOLD_CODE), self.HOLD_TEXT)

    # C, D
    def test_c_d_next_to_sell_or_trim_it_does_not_say_hold(self) -> None:
        for action in ("SELL", "TRIM", "sell", " Trim "):
            with self.subTest(action=action):
                self.assertEqual(format_reason_code(self.HOLD_CODE, action), "Bestehende Position")
                self.assertEqual(get_reason_presentation(self.HOLD_CODE, action).short_text, "Bestehende Position")
        self.assertEqual(format_reason_codes([self.HOLD_CODE, "HARD_STOP_TRIGGERED"], action="SELL"), "Bestehende Position, Stop unterschritten")
        self.assertEqual(format_reason_codes([self.HOLD_CODE, "TP1_REACHED"], action="HOLD"), "Bestehende Position – halten, Kursziel 1 erreicht")

    def test_the_other_action_that_contradicts_hold_is_covered_too(self) -> None:
        self.assertEqual(format_reason_code(self.HOLD_CODE, "ADD"), "Bestehende Position")
        self.assertIn("Aufstockung", plain(format_reason_code(self.HOLD_CODE, "ADD").tip))

    # E, F
    def test_e_f_the_tooltip_explains_the_context_and_keeps_the_original_code(self) -> None:
        for action in ("SELL", "TRIM"):
            tip = plain(format_reason_code(self.HOLD_CODE, action).tip)
            self.assertIn("Die Position war im Ausgangszustand als bestehende Position markiert. Eine weitere Entscheidungsregel hat jedoch "
                          "eine Verkaufs- oder Reduktionsaktion ausgelöst.", " ".join(tip.split()))
            self.assertNotIn("kein stärkeres Kauf- oder Verkaufssignal", tip)
            self.assertIn(f"Code: {self.HOLD_CODE}", tip)
        plain_tip = plain(format_reason_code(self.HOLD_CODE, "HOLD").tip)
        self.assertIn("Für die bereits vorhandene Position liegt aktuell kein stärkeres Kauf- oder Verkaufssignal vor.", plain_tip)
        self.assertIn(f"Code: {self.HOLD_CODE}", plain_tip)

    def test_the_action_changes_no_other_code(self) -> None:
        for code in KNOWN_REASON_CODES - {self.HOLD_CODE}:
            self.assertEqual(get_reason_presentation(code, "SELL"), get_reason_presentation(code), code)
        self.assertEqual(get_reason_presentation("FOO_BAR", "SELL"), get_reason_presentation("FOO_BAR"))

    # G
    def test_g_the_status_entry_ready_reads_einstieg_moeglich(self) -> None:
        status, reasons = rc.format_status_and_reasons("ENTRY_READY", [])
        self.assertEqual((status, reasons), ("Einstieg möglich", rc.EMPTY))

    # H
    def test_h_the_same_status_and_reason_is_shown_once(self) -> None:
        status, reasons = rc.format_status_and_reasons("ENTRY_READY", ["ENTRY_READY"])
        self.assertEqual((status, reasons), ("Einstieg möglich", rc.EMPTY))

    # I
    def test_i_another_reason_stays_visible(self) -> None:
        status, reasons = rc.format_status_and_reasons("ENTRY_READY", ["ENTRY_READY", "SCORE_BELOW_THRESHOLD", "FOO_BAR"])
        self.assertEqual((status, reasons), ("Einstieg möglich", "Score unter Schwelle, Foo bar"))
        self.assertIn("Code: SCORE_BELOW_THRESHOLD", plain(reasons.tip))
        status, reasons = rc.format_status_and_reasons("WAIT_FOR_TRIGGER", ["SCORE_BELOW_THRESHOLD"])
        self.assertEqual((status, reasons), ("Einstiegssignal fehlt", "Score unter Schwelle"))

    # J
    def test_j_the_status_tooltip_keeps_description_and_code(self) -> None:
        status, _ = rc.format_status_and_reasons("ENTRY_READY", ["ENTRY_READY"])
        tip = plain(status.tip)
        self.assertIn("Der Kandidat erfüllt aktuell die Voraussetzungen für einen geplanten Einstieg.", tip)
        self.assertIn("Code: ENTRY_READY", tip)

    def test_a_reason_that_reads_like_the_status_is_merged_and_its_code_stays_in_the_tooltip(self) -> None:
        status, reasons = rc.format_status_and_reasons("BLOCKED_EXISTING_POSITION", ["ENTRY_EXISTING_POSITION"])
        self.assertEqual((status, reasons), ("Position bereits vorhanden", rc.EMPTY))
        self.assertIn("Code: BLOCKED_EXISTING_POSITION", plain(status.tip))
        self.assertIn("Code: ENTRY_EXISTING_POSITION", plain(status.tip))
        status, reasons = rc.format_status_and_reasons("BLOCKED_BY_ALLOCATION", ["ENTRY_SWING_ALLOCATION_LIMIT"])  # different words: both stay
        self.assertEqual((status, reasons), ("Allokationslimit erreicht", "Swing-Limit erreicht"))

    def test_status_and_reasons_never_fail(self) -> None:
        for status in (None, "", 5, "FOO", "free text"):
            for codes in ([], [None], [""], ["FOO"], ["FOO", "FOO"], [5, object()]):
                shown, reasons = rc.format_status_and_reasons(status, codes)
                self.assertTrue(shown.strip() and reasons.strip())


@unittest.skipIf(OPEN is None, SKIP)
class ContextViewModelTests(unittest.TestCase):
    def test_a_sell_or_trim_row_never_says_hold_and_a_hold_row_does(self) -> None:
        env = realistic(OPEN)
        res = env["result"]
        records = res["existing_position_results"]
        self.assertGreaterEqual(len(records), 1)
        records[0]["decision"]["action"] = "SELL"
        records[0]["decision"]["reasons"] = ["EXISTING_POSITION_DEFAULT_HOLD", "HARD_STOP_TRIGGERED"]
        vm = build_planner(env, NOW)
        sell = next(r for r in vm.positions.rows if r[0] == records[0]["symbol"])
        self.assertEqual((sell[2], sell[6]), ("SELL", "Bestehende Position, Stop unterschritten"))
        self.assertNotIn("halten", sell[6])
        self.assertIn("Verkaufs- oder Reduktionsaktion", plain(sell[6].tip))
        self.assertIn("Code: EXISTING_POSITION_DEFAULT_HOLD", plain(sell[6].tip))
        for row in vm.positions.rows:
            if row[2] in ("SELL", "TRIM", "ADD"):
                self.assertNotIn("halten", row[6])
            elif row[2] == "HOLD":
                self.assertIn("Bestehende Position – halten", row[6])

    def test_the_candidate_table_shows_entry_ready_once(self) -> None:
        env = realistic(OPEN, candidate_codes=("ENTRY_READY",))
        for cand in env["result"]["portfolio_action_plan"]["entry_candidates"]:
            cand["entry_status"] = "ENTRY_READY"
        vm = build_planner(env, NOW)
        header = vm.candidates.headers
        for row in vm.candidates.rows:
            self.assertEqual((row[header.index("Status")], row[header.index("Codes")]), ("Einstieg möglich", "—"))
            self.assertIn("Code: ENTRY_READY", plain(row[header.index("Status")].tip))
        env = realistic(OPEN, candidate_codes=("ENTRY_READY", "SCORE_BELOW_THRESHOLD"))
        for cand in env["result"]["portfolio_action_plan"]["entry_candidates"]:
            cand["entry_status"] = "ENTRY_READY"
        row = build_planner(env, NOW).candidates.rows[0]
        self.assertEqual((row[header.index("Status")], row[header.index("Codes")]), ("Einstieg möglich", "Score unter Schwelle"))


@QT
@unittest.skipIf(OPEN is None, SKIP)
class MnemonicTests(unittest.TestCase):
    # K
    def test_k_the_tab_shows_kandidaten_und_risiko(self) -> None:
        from PySide6.QtGui import QAccessible

        qapp()
        view = PlannerView()
        view.show_vm(build_planner(realistic(OPEN), NOW))
        view.show()
        bar = view.tabs.tabBar()
        self.assertEqual(bar.tabText(3), "Kandidaten && Risiko")  # && is a literal ampersand (a single & would mark a mnemonic)
        iface = QAccessible.queryAccessibleInterface(bar)
        shown = [iface.child(i).text(QAccessible.Text.Name) for i in range(4)]  # the text as it is displayed (what a screen reader / UI automation sees)
        self.assertEqual(shown[3], "Kandidaten & Risiko")
        self.assertNotIn("Kandidaten  Risiko", shown)
        self.assertTrue(shown[1].startswith("Geplante Entries (") and shown[2].startswith("Zurückgestellt ("))


if __name__ == "__main__":
    unittest.main()
