"""Convert diagnoses into the dashboard's record format and publish them to dashboard/data.js.

Run `python -m synergyrca.export` to write offline demo data without a cluster.
"""
import json
import os
import shlex
import subprocess
import time

DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboard")
EXTERNAL_KINDS = {"Image", "StorageClass", "NFSPath"}
MAX_EVIDENCE_ROWS = 8


def _confidence(score):
    return "HIGH" if score >= 9 else "MEDIUM" if score >= 7 else "LOW"


def to_record(diag, event, sg):
    nodes = []
    for eid in diag.statepath:
        e = sg.entities[eid]
        nodes.append({
            "kind": e.kind,
            "name": e.name,
            "root": e.kind == diag.dest_kind,
            "missing": not e.snapshots,
            "external": e.kind in EXTERNAL_KINDS,
        })

    evidence = []
    for summary in diag.summaries:
        if summary["missing"]:
            evidence.append([summary["entity"], "STATE", "none observed"])
            continue
        for path, value in summary["fields"].items():
            evidence.append([summary["entity"], path, str(value)])
    evidence = evidence[:MAX_EVIDENCE_ROWS]

    return {
        "id": f"{event.reason}-{event.involved_namespace}-{event.involved_name}",
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event.time)),
        "reason": event.reason,
        "subtype": diag.dest_kind or "unknown",
        "object": f"{event.involved_kind}/{event.involved_name}",
        "namespace": event.involved_namespace,
        "status": diag.status,
        "confidence": _confidence(diag.score),
        "method": diag.method or "n/a",
        "message": event.message,
        "rootCause": diag.conclusion,
        "graph": nodes,
        "evidence": evidence,
        "checks": [[desc, bool(ok)] for desc, ok in diag.checks],
        "fix": [diag.resolution] if diag.resolution else [],
        "logTail": [],
        "dryRun": "",
    }


def publish(records):
    os.makedirs(DASHBOARD_DIR, exist_ok=True)
    path = os.path.join(DASHBOARD_DIR, "data.js")
    with open(path, "w", encoding="utf-8") as f:
        f.write("window.KUBEMIND_DATA = " + json.dumps(records, indent=2) + ";\n")
    return path


HISTORY_PATH = os.path.join(DASHBOARD_DIR, "history.json")


def load_history(path=HISTORY_PATH):
    """Previously saved incident records, keyed by id. Empty if none exist yet."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return {r["id"]: r for r in json.load(f)}


def save_history(records_by_id, path=HISTORY_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(list(records_by_id.values()), f, indent=2)


def run_dry_run(command):
    """Run a suggested kubectl patch with --dry-run=server and return its output.
    Only this exact kind of command is ever executed; anything else returns an empty string."""
    if not command or "kubectl patch" not in command or "--dry-run=server" not in command:
        return ""
    argv = shlex.split(command.split("  #")[0].strip())
    if not argv or argv[0] != "kubectl":
        return ""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        return (result.stdout or result.stderr).strip()
    except Exception as e:  # kubectl missing, cluster unreachable, etc.
        return f"dry-run could not run: {e}"


if __name__ == "__main__":
    from .driver import diagnose
    from .fixtures import SCENARIOS
    from .llm import LLM

    records = []
    for name, build in SCENARIOS.items():
        sg, event = build()
        records.append(to_record(diagnose(event, sg, LLM()), event, sg))
    print("Wrote", publish(records))
