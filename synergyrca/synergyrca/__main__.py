"""CLI: python -m synergyrca [--scenario quota|pvc|configmap|node|all] [--json]"""
import argparse
import json
from dataclasses import asdict

from .driver import diagnose
from .fixtures import SCENARIOS
from .llm import LLM


def render(name, d):
    bar = "=" * 70
    lines = [bar, f"SynergyRCA report - scenario: {name}", bar,
             f"Incident     : {d.incident}",
             f"Status       : {d.status}   (quality score {d.score}/10, trials {d.trials})",
             f"Method       : {d.method or 'n/a'}",
             f"Destination  : {d.dest_kind or 'n/a'}",
             "", "Metapath cypher:", d.cypher or "  (none)", "",
             "Statepath: " + (" -> ".join(d.statepath) if d.statepath else "(none)"),
             "", "Checks:"]
    for desc, ok in d.checks:
        lines.append(f"  [{'OK' if ok else 'PROBLEM'}] {desc}")
    lines += ["", "Root cause:", f"  {d.conclusion}", "",
              "Suggested fix (not executed):", f"  {d.resolution}",
              "", f"LLM calls: {d.llm_calls}  tokens: {d.llm_tokens}  time: {d.seconds}s", bar]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="SynergyRCA reimplementation (offline by default)")
    p.add_argument("--scenario", default="all", choices=list(SCENARIOS) + ["all"])
    p.add_argument("--json", action="store_true", help="print JSON instead of text reports")
    args = p.parse_args()

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results = {}
    for name in names:
        sg, event = SCENARIOS[name]()
        d = diagnose(event, sg, LLM())
        results[name] = d
        if not args.json:
            print(render(name, d))
            print()
    if args.json:
        print(json.dumps({k: asdict(v) for k, v in results.items()}, indent=2, default=str))


if __name__ == "__main__":
    main()
