from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "trading"))

from analysis_contracts import Action, AnalysisSnapshot, AvailabilityStatus, DataQuality, EventRiskAnalysis, FundamentalAnalysis, PositionContext, RiskAnalysis, SnapshotDataQuality, TechnicalAnalysis, ValuationAnalysis, to_primitive  # noqa: E402


class AnalysisContractsTests(unittest.TestCase):
    def test_snapshot_serializes_statuses_and_action_contracts(self) -> None:
        quality = DataQuality(AvailabilityStatus.AVAILABLE, as_of="2026-09-22")
        snapshot = AnalysisSnapshot(
            security_id=1, symbol="ABC", name="Example", as_of="2026-09-22", asset_type="stock",
            technical=TechnicalAnalysis(quality=quality), fundamental=FundamentalAnalysis(quality=quality),
            valuation=ValuationAnalysis(quality=DataQuality(AvailabilityStatus.UNAVAILABLE)),
            event_risk=EventRiskAnalysis(quality=DataQuality(AvailabilityStatus.UNAVAILABLE)),
            risk=RiskAnalysis(quality=quality), position=PositionContext(quality=DataQuality(AvailabilityStatus.NOT_APPLICABLE)),
            data_quality=SnapshotDataQuality(
                technical=quality, fundamental=quality,
                valuation=DataQuality(AvailabilityStatus.UNAVAILABLE),
                event_risk=DataQuality(AvailabilityStatus.UNAVAILABLE),
                risk=quality, position=DataQuality(AvailabilityStatus.NOT_APPLICABLE),
                strategy_assignment=DataQuality(AvailabilityStatus.AVAILABLE),
            ),
        )
        payload = to_primitive(snapshot)
        self.assertEqual(payload["technical"]["quality"]["status"], "available")
        self.assertEqual(Action.HOLD.value, "HOLD")
        self.assertEqual(AvailabilityStatus.STALE.value, "stale")
        self.assertEqual(AvailabilityStatus.INSUFFICIENT.value, "insufficient")
