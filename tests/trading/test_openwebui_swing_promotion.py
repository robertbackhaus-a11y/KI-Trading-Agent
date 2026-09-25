"""Safety shape tests for the Phase-4A OpenWebUI operations."""

from __future__ import annotations

import importlib.util
import inspect
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_tool_module():
    path = ROOT / 'openwebui-tools' / 'trading_sqlite.py'
    spec = importlib.util.spec_from_file_location('test_promotion_tool', path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class OpenWebUISwingPromotionTests(unittest.TestCase):
    def test_safe_fixed_operation_signatures(self) -> None:
        tool = load_tool_module().Tools
        self.assertEqual(list(inspect.signature(tool.evaluate_swing_candidate).parameters), ['self', 'security_id'])
        self.assertEqual(list(inspect.signature(tool.evaluate_swing_candidates).parameters), ['self'])
        approval = list(inspect.signature(tool.approve_swing_promotion).parameters)
        self.assertEqual(approval, ['self', 'security_id', 'plan_token', 'effective_from', '__user__'])
        self.assertNotIn('strategy', approval)
        self.assertNotIn('sql', approval)
        self.assertNotIn('quantity', approval)

    def test_orchestrator_exposes_only_optional_as_of(self) -> None:
        parameters = list(inspect.signature(load_tool_module().Tools.run_trading_orchestrator).parameters)
        self.assertEqual(parameters, ['self', 'as_of'])
        self.assertNotIn('sql', parameters)
        self.assertNotIn('write', parameters)


if __name__ == '__main__':
    unittest.main()
