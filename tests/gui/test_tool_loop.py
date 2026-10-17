# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The chat tool loop (pure Python): schema mapping, read-only offering, denial, validation, rounds, limits, errors, cancel (E-I, L-P, R, V, W)."""

from __future__ import annotations

import json
import threading
import time
import unittest
from dataclasses import dataclass
from typing import Any, Optional

import support  # noqa: F401
from fake_ollama import FakeOllama, Script, call, done, say
from gui.services import ollama_client as oc
from gui.services import tool_loop as tl
from gui.services import tool_policy as tp


@dataclass
class Result:
    ok: bool = True
    data: Any = None
    error: Optional[str] = None
    kind: str = "OK"
    seconds: float = 0.01
    chars: int = 0

    @property
    def tool_ok(self) -> bool:
        return self.ok and not (isinstance(self.data, dict) and self.data.get("ok") is False)

    @property
    def message(self) -> str:
        return self.error or ""


def schema(props: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


DETAIL = {"default": False, "type": "boolean", "title": "Detail"}
CATALOG = {
    "approve_swing_promotion": {"description": "approve", "input_schema": schema({"security_id": {"type": "integer"}, "plan_token": {"type": "string"}, "effective_from": {"type": "string"}}, ("security_id", "plan_token", "effective_from"))},
    "database_schema": {"description": "schema", "input_schema": schema({})},
    "database_status": {"description": "status", "input_schema": schema({})},
    "database_tables": {"description": "tables", "input_schema": schema({})},
    "evaluate_swing_candidate": {"description": "one", "input_schema": schema({"security_id": {"type": "integer"}}, ("security_id",))},
    "evaluate_swing_candidates": {"description": "all", "input_schema": schema({})},
    "evaluate_watchlist_candidates": {"description": "watch", "input_schema": schema({"as_of": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}})},
    "get_candidate_discovery": {"description": "discovery", "input_schema": schema({"status": {"type": "string", "default": "ALL"}, "limit": {"type": "integer", "default": 20}, "detail": DETAIL, "symbol": {"anyOf": [{"type": "string"}, {"type": "null"}]}})},
    "get_market_intelligence": {"description": "news", "input_schema": schema({"scope": {"type": "string", "default": "ALL"}, "importance": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}, "symbol": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}, "limit": {"type": "integer", "default": 20}, "detail": DETAIL})},
    "get_opportunity_view": {"description": "view", "input_schema": schema({"source": {"type": "string", "default": "ALL"}, "limit": {"type": "integer", "default": 30}, "detail": DETAIL})},
    "rank_watchlist": {"description": "rank", "input_schema": schema({})},
    "run_trading_orchestrator": {"description": "orchestrator", "input_schema": schema({"as_of": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}, "detail": DETAIL})},
    "sql_execute": {"description": "sql", "input_schema": schema({"sql": {"type": "string"}}, ("sql",))},
    "suggest_strategy_assignments": {"description": "suggest", "input_schema": schema({"as_of": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}})},
    "table_info": {"description": "info", "input_schema": schema({"table_name": {"type": "string"}}, ("table_name",))},
}
READ_NAMES = sorted(n for n in CATALOG if tp.is_read(n))


class Calls:
    """A fake ``call_tool`` that records what reached the 'MCP server'."""

    def __init__(self, result=None):
        self.calls: list[tuple[str, dict]] = []
        self.result = result or (lambda name, args: Result(data={"ok": True, "tool": name}, chars=30))

    def __call__(self, name, args, token):
        self.calls.append((name, args))
        return self.result(name, args)


class LoopBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeOllama().start()
        self.addCleanup(self.fake.stop)
        self.calls = Calls()
        self.events: list[tl.LoopEvent] = []

    def loop(self, **kw) -> tl.ToolLoop:
        kw.setdefault("model", "fake-model:latest")
        return tl.ToolLoop(oc.OllamaClient(self.fake.url, timeout=10), CATALOG, self.calls, **kw)

    def run_loop(self, text="Frage", history=None, token=None, **kw) -> tl.LoopResult:
        return self.loop(**kw).run(history or [], text, self.events.append, token or oc.CancelToken())

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def tool_messages(self, request_index: int) -> list[dict]:
        return [m for m in self.fake.chat_requests[request_index]["messages"] if m["role"] == "tool"]


class OfferedToolsTests(LoopBase):
    # V / W
    def test_v_w_only_the_13_read_tools_are_offered_with_the_mcp_schemas(self) -> None:
        tools, schemas = tl.offered_tools(CATALOG)
        names = [t["function"]["name"] for t in tools]
        self.assertEqual(len(CATALOG), 15)
        self.assertEqual(names, READ_NAMES)
        self.assertEqual(len(names), 13)
        for forbidden in ("sql_execute", "approve_swing_promotion"):
            self.assertNotIn(forbidden, names)
        for tool in tools:
            fn = tool["function"]
            self.assertEqual((tool["type"], fn["description"]), ("function", CATALOG[fn["name"]]["description"]))
            expected = json.loads(json.dumps(CATALOG[fn["name"]]["input_schema"]))
            expected["properties"].pop("detail", None)
            self.assertEqual(fn["parameters"], expected)  # the MCP schema, unchanged except for the forced detail switch
        self.assertEqual({n for n in CATALOG if "detail" in CATALOG[n]["input_schema"]["properties"]}, {"get_candidate_discovery", "get_market_intelligence", "get_opportunity_view", "run_trading_orchestrator"})
        self.assertTrue(all("detail" not in t["function"]["parameters"]["properties"] for t in tools))
        self.assertIn("detail", CATALOG["run_trading_orchestrator"]["input_schema"]["properties"])  # the catalog itself is not modified

    def test_the_request_to_ollama_carries_exactly_these_tools(self) -> None:
        self.fake.scripts.append(say("ok"))
        self.run_loop()
        sent = [t["function"]["name"] for t in self.fake.chat_requests[0]["tools"]]
        self.assertEqual(sent, READ_NAMES)

    def test_an_unknown_tool_is_never_offered_and_an_empty_catalog_offers_nothing(self) -> None:
        tools, _ = tl.offered_tools({"brand_new_tool": {"description": "x", "input_schema": schema({})}, "database_status": CATALOG["database_status"]})
        self.assertEqual([t["function"]["name"] for t in tools], ["database_status"])
        self.assertEqual(tl.offered_tools({})[0], [])

    def test_schema_hash_changes_with_the_tool_surface(self) -> None:
        a = tl.schema_hash(CATALOG)
        self.assertEqual(a, tl.schema_hash(dict(reversed(list(CATALOG.items())))))
        changed = json.loads(json.dumps(CATALOG))
        changed["database_status"]["description"] = "different"
        self.assertNotEqual(a, tl.schema_hash(changed))
        self.assertEqual(len(a), 16)

    def test_the_system_prompt_is_small_and_names_no_action_tool(self) -> None:
        text = tl.SYSTEM_PROMPT
        self.assertLess(len(text), 1500)
        for word in ("sql_execute", "approve_swing_promotion", "DROP", "INSERT"):
            self.assertNotIn(word, text)
        for expected in ("nur lesend", "Erfinde keine", "simulation_only", "Deutsch"):
            self.assertIn(expected, text)
        self.fake.scripts.append(say("ok"))
        self.run_loop()
        self.assertEqual(self.fake.chat_requests[0]["messages"][0], {"role": "system", "content": tl.SYSTEM_PROMPT})


class LoopFlowTests(LoopBase):
    # G / F
    def test_g_one_tool_loop_from_question_to_answer(self) -> None:
        self.fake.scripts.extend([call("database_status", {}), say("Die ", "Datenbank ist ok.")])
        result = self.run_loop("Wie ist der Datenbankstatus?")
        self.assertEqual((result.outcome, result.rounds, result.tool_calls), ("done", 1, 1))
        self.assertEqual(self.calls.calls, [("database_status", {})])
        self.assertEqual([m["role"] for m in result.history], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(result.history[-1]["content"], "Die Datenbank ist ok.")
        # F: the tool result went back to Ollama as role=tool with the tool name
        second = self.fake.chat_requests[1]["messages"]
        self.assertEqual([m["role"] for m in second], ["system", "user", "assistant", "tool"])
        self.assertEqual(second[2]["tool_calls"][0]["function"]["name"], "database_status")
        self.assertEqual((second[3]["tool_name"], json.loads(second[3]["content"])), ("database_status", {"ok": True, "tool": "database_status"}))
        # the events tell the story
        self.assertEqual([k for k in self.kinds() if k != "status"], ["tool_call", "tool_result", "content", "content", "done"])
        done_event = self.events[-1]
        self.assertEqual((done_event.data["outcome"], done_event.data["rounds"], done_event.data["prompt_tokens"], done_event.data["eval_tokens"]), ("done", 1, 20, 10))

    # H
    def test_h_several_tool_rounds_and_several_calls_in_one_round(self) -> None:
        two_calls = Script([{"message": {"content": "", "tool_calls": [{"function": {"name": "database_status", "arguments": {}}}, {"function": {"name": "rank_watchlist", "arguments": {}}}]}, "done": False}, done()])
        self.fake.scripts.extend([call("database_status", {}), two_calls, call("get_market_intelligence", {"symbol": "AAA"}), say("fertig")])
        result = self.run_loop()
        self.assertEqual((result.outcome, result.rounds, result.tool_calls), ("done", 3, 4))
        self.assertEqual([n for n, _ in self.calls.calls], ["database_status", "database_status", "rank_watchlist", "get_market_intelligence"])
        self.assertEqual(len(self.fake.chat_requests), 4)

    # I
    def test_i_at_most_six_tool_rounds_then_a_clean_stop(self) -> None:
        self.fake.scripts.extend([call("database_status", {}) for _ in range(10)])
        result = self.run_loop()
        self.assertEqual((result.outcome, result.rounds, result.tool_calls), ("round_limit", 6, 6))
        self.assertEqual(len(self.calls.calls), 6)
        self.assertEqual(len(self.fake.chat_requests), 7)  # the seventh answer asked for more tools and was not followed
        warning = next(e for e in self.events if e.kind == "warning")
        self.assertIn("6 Tool-Runden", warning.text)
        self.assertEqual(self.events[-1].data["outcome"], "round_limit")
        self.assertEqual(result.history, [])  # an incomplete turn is not kept

    # L / M / N / O
    def test_l_m_n_o_actions_and_unknown_tools_are_refused_without_reaching_mcp(self) -> None:
        self.fake.scripts.extend([call("sql_execute", {"sql": "DROP TABLE x"}), call("approve_swing_promotion", {"security_id": 1, "plan_token": "t", "effective_from": "2026-10-12"}),
                                  call("made_up_tool", {}), say("verstanden")])
        result = self.run_loop()
        self.assertEqual(result.outcome, "done")
        self.assertEqual(self.calls.calls, [])  # nothing was sent to the MCP server
        results = [e for e in self.events if e.kind == "tool_result"]
        self.assertEqual([(e.text, e.data["kind"], e.data["ok"]) for e in results], [("sql_execute", "TOOL_NOT_ALLOWED", False), ("approve_swing_promotion", "TOOL_NOT_ALLOWED", False), ("made_up_tool", "TOOL_NOT_ALLOWED", False)])
        self.assertIn("Aktion", results[0].data["note"])
        self.assertIn("unbekannt", results[2].data["note"])
        calls = [e for e in self.events if e.kind == "tool_call"]
        self.assertTrue(all(e.data["allowed"] is False for e in calls))
        sent_back = [json.loads(m["content"]) for m in self.tool_messages(3)]
        self.assertEqual([m["error"] for m in sent_back], ["TOOL_NOT_ALLOWED"] * 3)  # the model is told

    # P
    def test_p_invalid_arguments_are_rejected_before_the_call(self) -> None:
        cases = [("get_market_intelligence", {"limit": "many"}), ("table_info", {}), ("get_opportunity_view", {"nope": 1}), ("database_status", {"x": 1}),
                 ("evaluate_swing_candidate", {"security_id": [1]}), ("get_market_intelligence", {"symbol": 5})]
        self.fake.scripts.extend([call(n, a) for n, a in cases] + [say("ok")])
        self.run_loop(max_rounds=10)
        self.assertEqual(self.calls.calls, [])
        results = [e for e in self.events if e.kind == "tool_result"]
        self.assertEqual([e.data["kind"] for e in results], ["TOOL_ARGS_INVALID"] * len(cases))
        self.assertIn("Pflichtargument fehlt", results[1].data["note"])
        self.assertIn("unbekanntes Argument", results[2].data["note"])

    def test_numbers_and_booleans_sent_as_strings_are_coerced_and_detail_is_forced_off(self) -> None:
        self.fake.scripts.extend([call("get_market_intelligence", {"limit": "5", "symbol": "AAA", "detail": True}), call("run_trading_orchestrator", {"detail": "true", "as_of": None}),
                                  call("evaluate_swing_candidate", {"security_id": 7.0}), say("ok")])
        self.run_loop()
        self.assertEqual(self.calls.calls, [("get_market_intelligence", {"limit": 5, "symbol": "AAA", "detail": False}), ("run_trading_orchestrator", {"as_of": None, "detail": False}),
                                            ("evaluate_swing_candidate", {"security_id": 7})])  # detail is always false: the compact views, never the 100k characters
        self.assertTrue(all(c[1].get("detail", False) is False for c in self.calls.calls))
        shown = [e.data["arguments"] for e in self.events if e.kind == "tool_call"]
        self.assertNotIn("detail", shown[0])  # the screen shows what the model chose, not what the loop forced

    def test_the_chat_never_offers_or_sends_table_and_only_to_tools_that_have_it(self) -> None:
        watchlist = {"description": "w", "input_schema": schema({"limit": {"type": "integer", "default": 150}, "detail": DETAIL, "table": {"default": False, "type": "boolean", "title": "Table"}})}
        catalog = {**CATALOG, "get_watchlist": watchlist}
        tools, schemas = tl.offered_tools(catalog)
        self.assertEqual(sorted(schemas["get_watchlist"]["properties"]), ["limit"])  # neither detail nor table is the model's to choose
        self.assertEqual(tl.forced_arguments(catalog, "get_watchlist"), {"detail": False, "table": False})
        self.assertEqual(tl.forced_arguments(catalog, "get_market_intelligence"), {"detail": False})  # no table parameter there: none is sent
        self.assertEqual(tl.forced_arguments(catalog, "database_status"), {})

    def test_the_validator(self) -> None:
        s = CATALOG["get_market_intelligence"]["input_schema"]
        errors: list[str] = []
        self.assertEqual(tl.validate_arguments(s, {"limit": "7", "importance": None}, errors), {"limit": 7, "importance": None})
        self.assertEqual(errors, [])
        tl.validate_arguments(s, {"limit": 1.5, "symbol": ["x"]}, errors)
        self.assertEqual(len(errors), 2)
        enum_errors: list[str] = []
        tl.validate_arguments({"type": "object", "properties": {"m": {"enum": ["A", "B"]}}}, {"m": "C"}, enum_errors)
        self.assertEqual(len(enum_errors), 1)
        nested: list[str] = []
        tl.validate_arguments({"type": "object", "properties": {"xs": {"type": "array", "items": {"type": "integer"}}}}, {"xs": [1, "a"]}, nested)
        self.assertEqual(len(nested), 1)

    # Q
    def test_q_mcp_failures_are_reported_to_the_model_and_the_loop_goes_on(self) -> None:
        results = iter([Result(ok=False, error="Zeitüberschreitung nach 90 s", kind="TIMEOUT"), Result(ok=False, error="MCP nicht verbunden", kind="NOT_CONNECTED"), Result(data={"ok": False, "error": "X", "message": "boom"})])
        self.calls.result = lambda n, a: next(results)
        self.fake.scripts.extend([call("database_status", {}), call("rank_watchlist", {}), call("database_tables", {}), say("trotzdem eine Antwort")])
        result = self.run_loop(max_rounds=10)
        self.assertEqual(result.outcome, "done")
        sent = [json.loads(self.tool_messages(i)[-1]["content"]) for i in (1, 2)]
        self.assertEqual([s["error"] for s in sent], ["TIMEOUT", "NOT_CONNECTED"])
        shown = [e for e in self.events if e.kind == "tool_result"]
        self.assertEqual([(e.data["ok"], e.data["kind"]) for e in shown[:2]], [(False, "TIMEOUT"), (False, "NOT_CONNECTED")])
        self.assertFalse(shown[2].data["ok"])  # the tool itself reported ok=false

    # sizes
    def test_a_huge_tool_result_is_not_handed_to_the_model(self) -> None:
        self.calls.result = lambda n, a: Result(data={"ok": True, "rows": ["x" * 100] * 600})
        self.fake.scripts.extend([call("rank_watchlist", {}), say("ok")])
        self.run_loop(max_result_chars=10_000)
        message = json.loads(self.tool_messages(1)[0]["content"])
        self.assertEqual(message["error"], "TOOL_RESULT_TOO_LARGE")
        shown = next(e for e in self.events if e.kind == "tool_result")
        self.assertEqual((shown.data["ok"], shown.data["kind"]), (False, "TOO_LARGE"))
        self.assertGreater(shown.data["chars"], 10_000)

    def test_the_total_budget_of_one_request_is_enforced(self) -> None:
        self.calls.result = lambda n, a: Result(data={"ok": True, "pad": "x" * 6000})
        self.fake.scripts.extend([call("database_status", {}) for _ in range(4)] + [say("ok")])
        self.run_loop(max_total_chars=15_000, max_rounds=10)
        contents = [json.loads(m["content"]) for m in self.tool_messages(4)]
        self.assertEqual([c.get("error") for c in contents], [None, None, "TOOL_RESULT_BUDGET", "TOOL_RESULT_BUDGET"])

    def test_a_large_but_allowed_result_raises_a_warning(self) -> None:
        self.calls.result = lambda n, a: Result(data={"ok": True, "pad": "x" * 25_000})
        self.fake.scripts.extend([call("rank_watchlist", {}), say("ok")])
        self.run_loop()
        self.assertTrue(any(e.kind == "warning" and "Sehr großes" in e.text for e in self.events))

    # history
    def test_old_tool_results_are_replaced_by_a_placeholder_in_follow_up_requests(self) -> None:
        self.fake.scripts.extend([call("database_status", {}), say("eins"), say("zwei")])
        first = self.run_loop("erste")
        second = self.run_loop("zweite", history=first.history)
        messages = self.fake.chat_requests[2]["messages"]
        old_tool = next(m for m in messages if m["role"] == "tool")
        self.assertIn("nicht mehr im Verlauf", old_tool["content"])
        self.assertNotIn('"ok"', old_tool["content"])
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "tool", "assistant", "user"])
        self.assertEqual(second.history[-1]["content"], "zwei")

    # thinking
    def test_thinking_is_only_forwarded_when_it_is_switched_on(self) -> None:
        self.fake.scripts.extend([say("a", thinking=("denke",)), say("b", thinking=("denke",))])
        self.run_loop(think=False)
        off = [e.kind for e in self.events]
        self.events.clear()
        self.run_loop(think=True)
        on = [e.kind for e in self.events]
        self.assertNotIn("thinking", off)
        self.assertIn("thinking", on)
        self.assertEqual([b.get("think") for b in self.fake.chat_requests], [False, True])

    # errors
    def test_ollama_errors_end_the_request_with_an_error_event(self) -> None:
        self.fake.scripts.append(Script(status=500, error="boom"))
        result = self.run_loop()
        self.assertEqual(result.outcome, "error")
        error = next(e for e in self.events if e.kind == "error")
        self.assertEqual((error.data["kind"], error.data["status"]), (oc.HTTP_ERROR, 500))
        self.assertEqual(self.events[-1].data["outcome"], "error")
        self.assertEqual(result.history, [])
        dead = tl.ToolLoop(oc.OllamaClient("http://127.0.0.1:1", timeout=1), CATALOG, self.calls, "m")
        self.events.clear()
        self.assertEqual(dead.run([], "x", self.events.append, oc.CancelToken()).outcome, "error")
        self.assertEqual(next(e for e in self.events if e.kind == "error").data["kind"], oc.UNREACHABLE)

    # J
    def test_j_cancel_during_the_stream_ends_the_request_and_keeps_the_history_clean(self) -> None:
        self.fake.scripts.append(Script([{"message": {"content": f"{i} "}, "done": False} for i in range(300)] + [done()], delay=0.05))
        token = oc.CancelToken()
        out: list = []
        thread = threading.Thread(target=lambda: out.append(self.run_loop(token=token)))
        thread.start()
        deadline = time.time() + 5
        while sum(1 for e in self.events if e.kind == "content") < 3 and time.time() < deadline:
            time.sleep(0.02)
        token.cancel()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual((out[0].outcome, self.events[-1].data["outcome"]), ("cancelled", "cancelled"))
        self.assertEqual(out[0].history, [])
        # K: the next request with a fresh token works
        self.events.clear()
        self.fake.scripts.append(say("wieder da"))
        again = self.run_loop()
        self.assertEqual((again.outcome, again.history[-1]["content"]), ("done", "wieder da"))

    def test_cancel_during_a_tool_call_stops_before_asking_ollama_again(self) -> None:
        token = oc.CancelToken()
        self.calls.result = lambda n, a: (token.cancel(), Result(data={"ok": True}))[1]
        self.fake.scripts.extend([call("database_status", {}), say("never")])
        result = self.run_loop(token=token)
        self.assertEqual(result.outcome, "cancelled")
        self.assertEqual(len(self.fake.chat_requests), 1)


if __name__ == "__main__":
    unittest.main()
