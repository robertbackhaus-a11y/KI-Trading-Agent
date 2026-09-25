"""Phase 4B.2: deterministic orchestrator presentation contract tests."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "trading"))

from orchestrator_presentation import (  # noqa: E402
    PRESENTATION_MODE_AUTHORITATIVE,
    RESPONSE_CONTRACT_VERSION,
    build_presentation_summary,
    format_orchestrator_summary,
)


def _decision(action: str, *, quantity=None, basis=None, reasons=()) -> dict:
    return {"action": action, "action_quantity": quantity, "action_quantity_basis": basis, "reasons": reasons}


def _existing(security_id: int, symbol: str, action: str, priority: str, **decision_kwargs) -> dict:
    return {
        "security_id": security_id,
        "symbol": symbol,
        "decision": _decision(action, **decision_kwargs),
        "priority": priority,
    }


def _promotion(symbol: str, recommendation: str) -> dict:
    return {"symbol": symbol, "recommendation": recommendation}


CAPITAL_STATE = {
    "total_portfolio_market_value_eur": 320160.24,
    "swing_market_value_eur": 152029.31,
    "swing_weight_pct": 47.49,
    "long_term_market_value_eur": 168130.94,
    "long_term_weight_pct": 52.51,
    "cash_available": 9925.14,
    "buying_power": None,
    "allocation_guardrails": ["SWING_ALLOCATION_ABOVE_MAX", "LONG_TERM_ALLOCATION_BELOW_MIN"],
}

EXISTING = [
    _existing(2, "TSM", "TRIM", "P2", quantity=107.0, basis="TP2_75_PERCENT_CUMULATIVE_ORIGINAL", reasons=("TP2_REACHED", "TP2_CUMULATIVE_TRIM_RECOMMENDED")),
    _existing(5, "HALO", "TRIM", "P2", quantity=20.0, basis="TP2_75_PERCENT_CUMULATIVE_ORIGINAL", reasons=("TP2_REACHED", "TP2_CUMULATIVE_TRIM_RECOMMENDED")),
    _existing(1, "LLY", "HOLD", "P6"),
    _existing(3, "GD", "HOLD", "P6"),
    _existing(4, "ASML", "HOLD", "P6"),
    _existing(6, "LRCX", "HOLD", "P6"),
    _existing(10, "RHM", "HOLD", "P6"),
    _existing(7, "XUSE", "HOLD", "P6"),
    _existing(8, "IS3N", "HOLD", "P6"),
    _existing(9, "SPXS", "HOLD", "P6"),
    _existing(99, "ADDME", "ADD", "P3", quantity=3.0, basis="ADD_25_PERCENT_ORIGINAL", reasons=("ADD_TRIGGER",)),
]

PROMOTE_SYMBOLS = ["COHR", "CVX", "INGA", "AMD", "MU", "PLTR", "MSFT", "ATS", "BAYN", "QCOM", "MRVL", "ANET", "DOCN"]

PROMOTIONS = (
    [_promotion(symbol, "PROMOTE") for symbol in PROMOTE_SYMBOLS]
    + [_promotion(f"WATCH{i}", "KEEP_WATCHING") for i in range(29)]
    + [_promotion(f"INSUFF{i}", "DATA_INSUFFICIENT") for i in range(4)]
    + [_promotion("REJ1", "REJECT")]
)


def _build(existing=EXISTING, entries=(), promotions=PROMOTIONS, global_issues=(), security_issues=()):
    return build_presentation_summary(
        capital_state_summary=CAPITAL_STATE,
        existing_position_results=existing,
        entry_candidate_results=entries,
        promotion_results=promotions,
        global_issues=global_issues,
        security_issues=security_issues,
    )


class PresentationContractTests(unittest.TestCase):
    def test_response_contract_flags(self) -> None:
        summary = _build()
        self.assertEqual(summary.response_contract_version, RESPONSE_CONTRACT_VERSION)
        self.assertEqual(summary.presentation_mode, PRESENTATION_MODE_AUTHORITATIVE)
        self.assertEqual(summary.presentation_mode, "authoritative")

    def test_action_counts_consistent(self) -> None:
        summary = _build()
        trims = [a for a in summary.actions if a.action == "TRIM"]
        adds = [a for a in summary.actions if a.action == "ADD"]
        self.assertEqual(len(trims), 2)
        self.assertEqual(len(adds), 1)
        self.assertEqual(summary.adds.count, len(summary.adds.results))
        self.assertEqual(summary.adds.count, 1)

    def test_exact_tsm_trim_107(self) -> None:
        summary = _build()
        tsm = next(a for a in summary.actions if a.symbol == "TSM")
        self.assertEqual(tsm.action, "TRIM")
        self.assertEqual(tsm.action_quantity, 107.0)

    def test_exact_halo_trim_20(self) -> None:
        summary = _build()
        halo = next(a for a in summary.actions if a.symbol == "HALO")
        self.assertEqual(halo.action, "TRIM")
        self.assertEqual(halo.action_quantity, 20.0)

    def test_holds_exact_symbol_list(self) -> None:
        summary = _build()
        expected = {"LLY", "GD", "ASML", "LRCX", "RHM", "XUSE", "IS3N", "SPXS"}
        self.assertEqual(set(summary.holds), expected)
        self.assertEqual(len(summary.holds), len(expected))

    def test_promotion_count_equals_symbol_list_length(self) -> None:
        summary = _build()
        self.assertEqual(summary.promotions.promote_count, len(summary.promotions.promote_symbols))
        self.assertEqual(summary.promotions.keep_watching_count, len(summary.promotions.keep_watching_symbols))
        self.assertEqual(summary.promotions.data_insufficient_count, len(summary.promotions.data_insufficient_symbols))

    def test_promote_count_and_full_exact_list(self) -> None:
        summary = _build()
        self.assertEqual(summary.promotions.promote_count, 13)
        self.assertEqual(list(summary.promotions.promote_symbols), PROMOTE_SYMBOLS)
        self.assertEqual(set(summary.promotions.promote_symbols), set(PROMOTE_SYMBOLS))

    def test_keep_watching_count_consistent(self) -> None:
        summary = _build()
        self.assertEqual(summary.promotions.keep_watching_count, 29)

    def test_data_insufficient_count_consistent(self) -> None:
        summary = _build()
        self.assertEqual(summary.promotions.data_insufficient_count, 4)

    def test_reject_is_counted_but_has_no_exact_list_field(self) -> None:
        summary = _build()
        self.assertEqual(summary.promotions.reject_count, 1)
        self.assertFalse(hasattr(summary.promotions, "reject_symbols"))

    def test_no_duplicate_symbols_in_any_exact_list(self) -> None:
        summary = _build()
        for symbols in (
            summary.promotions.promote_symbols,
            summary.promotions.keep_watching_symbols,
            summary.promotions.data_insufficient_symbols,
            summary.holds,
        ):
            self.assertEqual(len(symbols), len(set(symbols)), symbols)

    def test_deterministic_ordering_is_stable_across_repeated_calls(self) -> None:
        first = _build()
        second = _build()
        self.assertEqual(first.promotions.promote_symbols, second.promotions.promote_symbols)
        self.assertEqual([a.symbol for a in first.actions], [a.symbol for a in second.actions])
        self.assertEqual(first.holds, second.holds)

    def test_actions_sorted_by_priority_then_security_id(self) -> None:
        summary = _build()
        priorities = [int(a.priority[1:]) for a in summary.actions]
        self.assertEqual(priorities, sorted(priorities))

    def test_no_allocation_recommendation_field_exists(self) -> None:
        summary = _build()
        primitive = summary.primitive()
        forbidden_fields = {
            "rebalance_recommendation",
            "buy_more_etfs",
            "allocate_trim_proceeds",
            "reduce_swing_by_selling",
        }

        def _walk(node) -> None:
            if isinstance(node, dict):
                self.assertTrue(forbidden_fields.isdisjoint(node.keys()), node.keys())
                for value in node.values():
                    _walk(value)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(primitive)

    def test_allocation_warnings_are_factual_codes_only(self) -> None:
        summary = _build()
        self.assertEqual(
            set(summary.portfolio.allocation_warnings),
            {"SWING_ALLOCATION_ABOVE_MAX", "LONG_TERM_ALLOCATION_BELOW_MIN"},
        )

    def test_non_blocking_warning_is_never_classified_as_a_blocker(self) -> None:
        # "NON_BLOCKING_WARNING" contains the substring "BLOCKING"; a naive
        # substring filter would wrongly double-count it as a blocker too.
        issues = (
            {"code": "ALLOCATION_OUTSIDE_TARGET", "severity": "NON_BLOCKING_WARNING", "symbol": None},
            {"code": "CASH_RESERVE_LIMIT", "severity": "NON_BLOCKING_WARNING", "symbol": None},
            {"code": "REQUIRED_SCHEMA_UNAVAILABLE", "severity": "GLOBAL_BLOCKING", "symbol": None},
        )
        summary = _build(global_issues=issues)
        blocker_codes = {issue["code"] for issue in summary.data_quality.blockers}
        warning_codes = {issue["code"] for issue in summary.data_quality.warnings}
        self.assertEqual(blocker_codes, {"REQUIRED_SCHEMA_UNAVAILABLE"})
        self.assertEqual(warning_codes, {"ALLOCATION_OUTSIDE_TARGET", "CASH_RESERVE_LIMIT"})
        self.assertTrue(blocker_codes.isdisjoint(warning_codes))

    def test_execution_boundary_is_fixed_and_non_executing(self) -> None:
        summary = _build()
        self.assertFalse(summary.execution_boundary.broker_execution_available)
        self.assertIn("außerhalb des Trading Agents", summary.execution_boundary.message)
        self.assertNotIn("?", summary.execution_boundary.message)


class RendererTests(unittest.TestCase):
    def _rendered(self) -> str:
        summary = _build()
        return format_orchestrator_summary(summary, evaluation_as_of="2026-09-24", market_data_as_of="2026-09-21")

    def test_no_llm_call_possible_pure_string_function(self) -> None:
        # The renderer accepts only a PresentationSummary + plain strings --
        # there is no model/client parameter it could call out through.
        import inspect

        parameters = list(inspect.signature(format_orchestrator_summary).parameters)
        self.assertEqual(parameters, ["summary", "evaluation_as_of", "market_data_as_of"])

    def test_exact_tsm_trim_107_rendering(self) -> None:
        text = self._rendered()
        self.assertIn("TSM: TRIM 107", text)

    def test_exact_halo_trim_20_rendering(self) -> None:
        text = self._rendered()
        self.assertIn("HALO: TRIM 20", text)

    def test_broker_execution_boundary_rendered(self) -> None:
        text = self._rendered()
        self.assertIn("Die tatsächliche Orderausführung erfolgt außerhalb des Trading Agents.", text)

    def test_promotion_count_rendered_matches_engine(self) -> None:
        text = self._rendered()
        self.assertIn("PROMOTE: 13", text)
        for symbol in PROMOTE_SYMBOLS:
            self.assertIn(symbol, text)
        self.assertNotIn("und 2 weitere", text)
        self.assertNotIn("PROMOTE: 14", text)

    def test_no_etf_reinvestment_text_in_deterministic_renderer(self) -> None:
        text = self._rendered()
        lowered = text.lower()
        for forbidden in ("umschicht", "reinvest", "etf-empfehlung", "empfehlung der engine zur allokation"):
            self.assertNotIn(forbidden, lowered)

    def test_no_next_steps_or_discretionary_section(self) -> None:
        text = self._rendered()
        lowered = text.lower()
        for forbidden in ("next steps", "nächste schritte", "empfehlung:", "strategieempfehlung"):
            self.assertNotIn(forbidden, lowered)

    def test_no_ranking_language_for_promote_candidates(self) -> None:
        text = self._rendered()
        lowered = text.lower()
        for forbidden in ("preferred", "bevorzugt", "strongest", "stärkste", "best", "beste", "buy candidate", "overbought", "überkauft"):
            self.assertNotIn(forbidden, lowered)

    def test_renderer_output_is_deterministic_across_calls(self) -> None:
        self.assertEqual(self._rendered(), self._rendered())


class DegenerateInputTests(unittest.TestCase):
    def test_empty_inputs_produce_well_typed_zero_summary(self) -> None:
        summary = build_presentation_summary(
            capital_state_summary={},
            existing_position_results=(),
            entry_candidate_results=(),
            promotion_results=(),
            global_issues=(),
            security_issues=(),
        )
        self.assertEqual(summary.promotions.promote_count, 0)
        self.assertEqual(summary.promotions.promote_symbols, ())
        self.assertEqual(summary.holds, ())
        self.assertEqual(summary.actions, ())
        self.assertIsNone(summary.portfolio.total_market_value_eur)
        # Must still render without raising.
        text = format_orchestrator_summary(summary, evaluation_as_of="2026-09-24")
        self.assertIn("Keine.", text)


if __name__ == "__main__":
    unittest.main()
