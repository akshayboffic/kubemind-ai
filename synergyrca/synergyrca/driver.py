"""Driver program (paper Sec. III-A, workflow in Fig. 1)."""
import time
from dataclasses import dataclass, field

from .graph import MetaGraph, instantiate_statepaths
from .llm import LLM
from .modules import (path_query, quality_check, report_gen, rule_evidence,
                      state_check, triage)

NATIVE_KINDS = ["Pod", "Job", "ReplicaSet", "Namespace", "ResourceQuota", "PersistentVolumeClaim",
                "PersistentVolume", "ConfigMap", "Secret", "Node"]
EXTERNAL_KINDS = ["StorageClass", "NFSPath"]
MAX_TRIALS = 3


@dataclass
class Event:
    involved_kind: str
    involved_name: str
    involved_namespace: str
    reason: str
    message: str
    time: float


@dataclass
class Diagnosis:
    incident: str
    status: str = "NEEDS_REVIEW"
    trials: int = 0
    dest_kind: str = ""
    metapath: list = field(default_factory=list)
    cypher: str = ""
    statepath: list = field(default_factory=list)
    summaries: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    conclusion: str = ""
    resolution: str = ""
    score: int = 0
    method: str = ""
    llm_calls: int = 0
    llm_tokens: int = 0
    seconds: float = 0.0


def diagnose(event: Event, sg, llm: LLM = None, max_trials: int = MAX_TRIALS) -> Diagnosis:
    llm = llm or LLM()
    start = time.time()
    calls0, tokens0 = llm.calls, llm.total_tokens
    incident = f"{event.reason}-{event.involved_kind}-{event.involved_name}"
    diag = Diagnosis(incident=incident)

    src = sg.find(event.involved_kind, event.involved_name, event.involved_namespace)
    if src is None:
        diag.conclusion = "Source resource not found in StateGraph."
        return diag
    mg = MetaGraph(sg)
    allowed = sorted(mg.kinds())  # triage may only pick kinds that exist in the MetaGraph
    previous = []
    best = None

    for trial in range(1, max_trials + 1):
        diag.trials = trial
        dest = triage(llm, event.involved_kind, event.message, allowed, previous)
        previous.append(dest)
        if dest is None:
            break

        steps = mg.metapath(event.involved_kind, dest)
        if not steps:
            continue
        statepaths = instantiate_statepaths(sg, src.id, steps)
        for sp in statepaths:
            summaries = [state_check(llm, eid, (sg.entities[eid].state_at(event.time)), event.message)
                         for eid in sp]
            violation, checks = rule_evidence(sp, sg, event.time)
            report = report_gen(llm, event.message, summaries, violation, dest, sp, sg, event.time)
            quality = quality_check(llm, report, event.message)
            candidate = dict(dest=dest, steps=steps, sp=sp, summaries=summaries, checks=checks,
                             report=report, quality=quality, violation=violation)
            if best is None or quality["score"] > best["quality"]["score"]:
                best = candidate
        if best and best["violation"] is not None and not best["quality"]["further_investigation"]:
            break  # stop early only when the cluster state confirms the answer

    if best is None:
        diag.conclusion = "No diagnostic rule matched this incident. Cause unknown; needs manual review."
    if best:
        r = best["report"]
        diag.dest_kind = best["dest"]
        diag.metapath = best["steps"]
        diag.cypher = path_query(best["steps"])
        diag.statepath = best["sp"]
        diag.summaries = best["summaries"]
        diag.checks = best["checks"]
        diag.resolution = r["resolution"]
        diag.score = best["quality"]["score"]
        diag.method = r.get("method", "rules")
        # The gate: a diagnosis is only DIAGNOSED when the cluster state confirms it.
        # Quality scores (from an LLM or rules) can never confirm a cause on their own.
        verified = best["violation"] is not None and not best["quality"]["further_investigation"]
        if verified:
            diag.status = "DIAGNOSED"
            diag.conclusion = best["violation"]  # the text backed by the cluster state
        else:
            diag.status = "NEEDS_REVIEW"
            diag.conclusion = ("Not verified by cluster state; needs review. "
                               "Unconfirmed suggestion: " + r["conclusion"])

    diag.llm_calls = llm.calls - calls0
    diag.llm_tokens = llm.total_tokens - tokens0
    diag.seconds = round(time.time() - start, 3)
    return diag
