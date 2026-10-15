"""Evaluation harness: run labeled cases in rules-only or LLM mode and report accuracy.

  python -m synergyrca.evaluate                       # rules only, all cases
  python -m synergyrca.evaluate --set unseen          # only cases no rule was written for
  OPENAI_API_KEY=... python -m synergyrca.evaluate --llm --set all

Writes evaluation/results_<mode>_<set>.csv and summary_<mode>_<set>.json.
"""
import argparse
import csv
import json
import os
import sys
import time

from .driver import diagnose
from .eval_cases import select_cases
from .llm import LLM

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evaluation")


def classify(expected, d):
    diagnosed = d.status == "DIAGNOSED"
    if expected is None:
        return "correct_unknown" if not diagnosed else "wrong_confident"
    if diagnosed and d.dest_kind == expected:
        return "correct"
    if diagnosed:
        return "wrong_confident"
    return "missed"


def run(cases, use_llm):
    if not use_llm:
        os.environ.pop("OPENAI_API_KEY", None)  # rules-only runs never touch the network
    llm = LLM()  # one shared client, so its recorded errors reach the report
    rows = []
    for name, expected, build in cases:
        sg, event = build()
        start = time.time()
        d = diagnose(event, sg, llm)
        rows.append({
            "case": name,
            "expected": expected or "unknown",
            "got": d.dest_kind or "unknown",
            "status": d.status,
            "outcome": classify(expected, d),
            "llm_calls": d.llm_calls,
            "seconds": round(time.time() - start, 4),
        })
    return rows, llm


def summarize(rows):
    def ratio(a, b):
        return round(a / b, 3) if b else None

    kind = [r for r in rows if r["expected"] != "unknown"]
    unknown = [r for r in rows if r["expected"] == "unknown"]
    diagnosed = [r for r in rows if r["status"] == "DIAGNOSED"]
    count = lambda o: sum(1 for r in rows if r["outcome"] == o)
    return {
        "cases": len(rows),
        "diagnosable_cases": len(kind),
        "unknown_cases": len(unknown),
        "precision": ratio(count("correct"), len(diagnosed)),
        "recall": ratio(count("correct"), len(kind)),
        "unknown_handled_correctly": ratio(count("correct_unknown"), len(unknown)),
        "wrong_confident": count("wrong_confident"),
        "missed": count("missed"),
        "llm_calls": sum(r["llm_calls"] for r in rows),
        "avg_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 4),
    }


def main():
    parser = argparse.ArgumentParser(description="SynergyRCA evaluation harness")
    parser.add_argument("--llm", action="store_true", help="enable LLM stages (needs OPENAI_API_KEY)")
    parser.add_argument("--set", default="all", choices=["builtin", "unseen", "all"])
    args = parser.parse_args()

    if args.llm and not os.environ.get("OPENAI_API_KEY"):
        print("--llm needs the OPENAI_API_KEY environment variable. Nothing was run.")
        sys.exit(1)

    mode = "llm" if args.llm else "rules"
    rows, llm = run(select_cases(args.set), args.llm)
    summary = summarize(rows)

    print(f"mode: {mode}   set: {args.set}")
    print(f"{'case':34} {'expected':24} {'got':24} {'status':13} {'outcome':17} llm")
    for r in rows:
        print(f"{r['case']:34} {r['expected']:24} {r['got']:24} {r['status']:13} {r['outcome']:17} {r['llm_calls']}")
    print()
    for k, v in summary.items():
        print(f"{k:28} {v}")

    if args.llm and summary["llm_calls"] == 0:
        print("\nWARNING: the LLM was never called. These results are rules-only and must not be "
              "reported as LLM results.")
        print("Last LLM error: " + str(llm.last_error or "none recorded"))

    os.makedirs(OUT_DIR, exist_ok=True)
    stem = f"{mode}_{args.set}"
    with open(os.path.join(OUT_DIR, f"results_{stem}.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    with open(os.path.join(OUT_DIR, f"summary_{stem}.json"), "w", encoding="utf-8") as f:
        json.dump({"mode": mode, "set": args.set, **summary}, f, indent=2)
    print(f"\nSaved to {os.path.normpath(OUT_DIR)}")


if __name__ == "__main__":
    main()
