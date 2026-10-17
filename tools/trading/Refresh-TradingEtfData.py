# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""Refresh etf_holdings_local.json and etf_lookthrough_local.json from the providers' published data (iShares: XUSE, IS3N; Invesco: SPXS).

Without ``--write`` this is a complete dry run: everything is downloaded and validated and the report shows what would change; no file is touched.  With ``--write`` each file is
replaced atomically only after every refreshed entry passed the validation and the candidate file was read back through the real loaders; the previous file is kept as
``<file>.bak-refresh-<time>``.  A failing ETF keeps its stored entry; the holdings file and the look-through file are independent.  The exit code is 0 only when nothing failed (unchanged data and the manual SPXS sector allocation are no failure); 3 = partly refreshed, 2 = failed - so a scheduled run shows a
provider, parser or validation problem as a failed run.  Scheduled weekly by Manage-TradingTasks.ps1 (Trading-ETF-Refresh); no retry here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import etf_holdings
import etf_refresh as er


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--universe-dir", type=Path, default=etf_holdings.DEFAULT_UNIVERSE, help="folder of the private files (default: the universe folder of this installation)")
    parser.add_argument("--write", action="store_true", help="replace the files (default: dry run)")
    parser.add_argument("--what", choices=("holdings", "lookthrough", "both"), default="both", help="which file to refresh")
    parser.add_argument("--only", action="append", default=[], metavar="SYMBOL", help="only this ETF (XUSE, IS3N, SPXS or its ISIN; repeatable)")
    parser.add_argument("--timeout", type=float, default=er.TIMEOUT_S, help="seconds per download")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    return parser


def _print(report: dict[str, Any]) -> None:
    print(("Refresh (WRITE)" if report["write"] else "Refresh (dry run - nothing is written)") + f": {report['universe_dir']}")
    for row in report["etfs"]:
        head = f"{row['etf']:5} {row['dataset']:11} {row['status']}"
        print(head + (f"  as of {row['as_of']}" if row.get("as_of") else ""))
        if row.get("source"):
            print(f"      source: {row['source']}")
        changes = row.get("changes")
        if changes and row["dataset"] == "holdings":
            new = changes["new"]
            omitted = changes.get("omitted_zero_weight_rows") or {}
            print(f"      provider rows {changes.get('provider_rows') or new['count']}, stored {new['count']} ({new['equity_count']} equity, {new['zero_weight_equity_rows']} of them with a published weight of 0.00), "
                  f"omitted zero-weight rows {sum(omitted.values())}" + (f" ({', '.join(f'{k} {v}' for k, v in omitted.items())})" if omitted else ""))
            print(f"      sum of the published weights {new['total_weight']:.2f} % (the same raw and stored)")
            if changes.get("old"):
                print(f"      stored: as of {changes['as_of_old']}, {changes['old']['count']} holdings, sum {changes['old']['total_weight']:.2f} %; "
                      f"added {changes['added']}, removed {changes['removed']}, weight changed {changes['weight_changed']}, unchanged {changes['unchanged']}")
                for item in changes["biggest_changes"][:3]:
                    print(f"      largest change: {item['name']} {item['old']} -> {item['new']}")
        elif changes:
            print(f"      sectors {changes['sectors_new']}, sum {changes['total_new']:.2f} %; stored as of {changes['as_of_old']}")
            for item in changes.get("changes", [])[:4]:
                print(f"      {item['sector']}: {item['old']} -> {item['new']}")
        for message in row.get("errors", []):
            print(f"      ERROR {message}")
        for message in row.get("warnings", []):
            print(f"      warning {message}")
    for name, info in report["files"].items():
        print(f"{name}: {info['bytes_before']:,} -> {info['bytes_after']:,} bytes; written={'yes' if info['written'] else 'no'}" + (f", backup {info['backup']}" if info.get("backup") else "") + (f", {info['error']}" if info.get("error") else ""))
    print("summary: " + ", ".join(f"{k}={v}" for k, v in report["summary"].items() if v))
    print(f"outcome: {report['outcome']}" + ("" if report["outcome"] == "OK" else " (exit code " + str(er.exit_code(report)) + ")"))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    what = ("holdings", "lookthrough") if args.what == "both" else (args.what,)
    report = er.refresh(args.universe_dir, write=args.write, what=what, only=args.only, fetcher=lambda url: er.fetch(url, args.timeout))
    print(json.dumps(report, sort_keys=True, ensure_ascii=False)) if args.json else _print(report)
    return er.exit_code(report)  # 0 = ok, 3 = some datasets failed (the rest is refreshed), 2 = nothing could be refreshed or a write failed


if __name__ == "__main__":
    sys.exit(main())
