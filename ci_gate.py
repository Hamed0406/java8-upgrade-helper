#!/usr/bin/env python3
import argparse
import json
import sys


def main():
    p = argparse.ArgumentParser(description="CI gate for Java Upgrade Helper reports")
    p.add_argument("report_json", help="Path to java-upgrade-report-*.json")
    p.add_argument("--max-high", type=int, default=0, help="Allowed HIGH findings (default: 0)")
    p.add_argument("--max-medium", type=int, default=999999, help="Allowed MEDIUM findings (default: unlimited)")
    p.add_argument("--max-unresolved-deps", type=int, default=999999, help="Allowed unresolved dependencies (default: unlimited)")
    args = p.parse_args()

    with open(args.report_json, "r", encoding="utf-8") as f:
        report = json.load(f)

    findings = report.get("findings") or []
    dep_results = ((report.get("dependencyChecks") or {}).get("results")) or []
    high = sum(1 for f in findings if str(f.get("severity", "")).lower() == "high")
    medium = sum(1 for f in findings if str(f.get("severity", "")).lower() == "medium")
    unresolved = sum(1 for d in dep_results if not (d.get("checked") and d.get("source") and d.get("source") != "none"))

    ok = True
    if high > args.max_high:
        print(f"FAIL: high findings {high} > allowed {args.max_high}")
        ok = False
    if medium > args.max_medium:
        print(f"FAIL: medium findings {medium} > allowed {args.max_medium}")
        ok = False
    if unresolved > args.max_unresolved_deps:
        print(f"FAIL: unresolved deps {unresolved} > allowed {args.max_unresolved_deps}")
        ok = False

    if ok:
        print(f"PASS: high={high}, medium={medium}, unresolved_deps={unresolved}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
