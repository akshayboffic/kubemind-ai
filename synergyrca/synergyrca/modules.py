"""The five LLM modules of SynergyRCA (paper Sec. III-B). Each has an LLM path and a
deterministic fallback so the pipeline runs without an API key."""
import re

from .graph import metapath_to_cypher

# ---------------- Triage ----------------
TRIAGE_SYS = (
    "You are a Kubernetes root-cause triage module. Given a source resource kind and an "
    "error message, choose the single most likely root-cause kind from the allowed list. "
    "Respond ONLY with JSON: {\"DestinationKind\": \"<kind>\"}"
)

HEURISTIC_RULES = [
    ("exceeded quota", "ResourceQuota"),
    ("unbound", "PersistentVolumeClaim"),
    ("cannot find volume", "ConfigMap"),
    ("low on resource", "Node"),
    ("diskpressure", "Node"),
    ("failed to pull image", "Image"),
    ("errimagepull", "Image"),
    ("imagepullbackoff", "Image"),
    ("being terminated", "Namespace"),
    ("serviceaccount", "ServiceAccount"),
    ("storageclass", "StorageClass"),
    ("oomkilled", "Container"),
    ("createcontainerconfigerror", "Container"),
]


def triage(llm, src_kind, message, allowed, exclude):
    candidates = [k for k in allowed if k not in exclude]
    if llm.available:
        out = llm.ask_json(TRIAGE_SYS, f"srcKind: {src_kind}\nallowed: {candidates}\nmessage: {message}")
        if out and out.get("DestinationKind") in candidates:
            return out["DestinationKind"]
    return heuristic_triage(src_kind, message, candidates)


def heuristic_triage(src_kind, message, candidates):
    m = message.lower()
    # kubelet wording for a missing volume source: 'configmap "x" not found'
    if "not found" in m:
        if "secret" in m and "Secret" in candidates:
            return "Secret"
        if "configmap" in m and "ConfigMap" in candidates:
            return "ConfigMap"
    for pattern, kind in HEURISTIC_RULES:
        if pattern in m and kind in candidates:
            # Naming convention from the paper: -cert / -token are Secrets, -conf is a ConfigMap.
            if kind == "ConfigMap" and re.search(r"-(cert|token)\b", m):
                return "Secret" if "Secret" in candidates else kind
            return kind
    return None  # no rule matched: report "unknown" rather than guess a kind


# ---------------- PathQueryGen ----------------
def path_query(steps):
    """Metapath -> Cypher text. Execution is performed by instantiate_statepaths in graph.py."""
    return metapath_to_cypher(steps)


# ---------------- StateChecker ----------------
ALWAYS_KEYS = {"phase", "reason", "type", "status", "storageClassName", "exitCode", "nodeName"}
STATE_SYS = (
    "You are a StateChecker. Summarize only facts present in the JSON. Do not infer root "
    "causes from the error message. Respond ONLY with JSON: {\"observations\": [\"...\"]}"
)


def _flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _flatten(v, f"{prefix}.{i}")
    else:
        yield prefix, obj


def state_check(llm, entity_id, state, message):
    if state is None:
        return {"entity": entity_id, "missing": True,
                "observations": ["No STATE vertex exists for this entity; the state cannot be checked."],
                "fields": {}}
    words = {w for w in re.findall(r"[a-z]{4,}", message.lower())}
    fields = {}
    for path, value in _flatten({"spec": state["spec"], "status": state["status"]}):
        leaf = path.split(".")[-1]
        if leaf in ALWAYS_KEYS or any(w in path.lower() for w in words):
            fields[path] = value
    observations = [f"{p} = {v}" for p, v in fields.items()]
    if llm.available:
        out = llm.ask_json(STATE_SYS, f"entity: {entity_id}\nerror: {message}\nJSON fields: {fields}")
        if out and isinstance(out.get("observations"), list):
            observations = out["observations"]
    return {"entity": entity_id, "missing": False, "observations": observations, "fields": fields}


# ---------------- Rule evidence (shared by ReportGen fallback and quality checker) ----------------
_QTY_SUFFIX = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
               "k": 1e3, "M": 1e6, "G": 1e9, "m": 1e-3}


def parse_quantity(q):
    """Parse a Kubernetes resource quantity such as '50', '2Gi' or '500m' into a float."""
    q = str(q).strip()
    for suffix in sorted(_QTY_SUFFIX, key=len, reverse=True):
        if q.endswith(suffix):
            return float(q[:-len(suffix)]) * _QTY_SUFFIX[suffix]
    return float(q)


def rule_evidence(statepath, sg, t):
    """Deterministic checks of the reconciliation rules along a statepath.
    Returns (violation_text or None, list of checked facts)."""
    checks = []
    violation = None
    for eid in statepath:
        e = sg.entities[eid]
        st = e.state_at(t)
        if e.kind == "ConfigMap" or e.kind == "Secret":
            if st is None:
                checks.append((f"{eid} exists", False))
                violation = violation or f"{e.kind} '{e.name}' referenced by the pod does not exist in namespace '{e.namespace}'."
            else:
                checks.append((f"{eid} exists", True))
        elif e.kind == "ResourceQuota" and st:
            for key, hard in st["spec"].get("hard", {}).items():
                used = st["status"].get("used", {}).get(key)
                if used is not None and parse_quantity(used) >= parse_quantity(hard):
                    checks.append((f"used.{key} >= hard.{key} ({used} >= {hard})", False))
                    violation = violation or (f"ResourceQuota '{e.name}' in namespace '{e.namespace}' "
                                              f"has reached its {key} limit ({used}/{hard}).")
                else:
                    checks.append((f"used.{key} < hard.{key}", True))
        elif e.kind == "PersistentVolumeClaim" and st:
            phase = st["status"].get("phase")
            ok = phase == "Bound"
            checks.append((f"{eid} phase is Bound (phase={phase})", ok))
            if not ok:
                sc = st["spec"].get("storageClassName", "(none)")
                violation = violation or (f"PersistentVolumeClaim '{e.name}' is {phase}; its StorageClass "
                                          f"'{sc}' is not provisioning volumes.")
        elif e.kind == "Image":
            pods = [sg.entities[x] for x in statepath if sg.entities[x].kind == "Pod"]
            reason, msg = _image_pull_failure(pods[0].state_at(t)) if pods else (None, None)
            checks.append((f"image {e.name} can be pulled (waiting reason={reason})", reason is None))
            if reason:
                violation = violation or f"Container image '{e.name}' cannot be pulled ({reason}): {msg}"
        elif e.kind == "Container":
            pods = [sg.entities[x] for x in statepath if sg.entities[x].kind == "Pod"]
            cname = e.name.split("/")[-1]
            cs = _container_status(pods[0].state_at(t), cname) if pods else None
            reason, msg = _container_failure(cs)
            checks.append((f"container {cname} has no OOMKilled or CreateContainerConfigError failure "
                           f"(found={reason})", reason is None))
            if reason:
                violation = violation or f"Container '{cname}' failed: {reason}. {msg}".strip()
        elif e.kind == "Namespace" and st:
            phase = st["status"].get("phase")
            ok = phase != "Terminating"
            checks.append((f"namespace {e.name} phase is not Terminating (phase={phase})", ok))
            if not ok:
                violation = violation or f"Namespace '{e.name}' is Terminating, so new objects cannot be created in it."
        elif e.kind == "ServiceAccount":
            ok = st is not None
            checks.append((f"serviceaccount {e.name} exists", ok))
            if not ok:
                violation = violation or (f"ServiceAccount '{e.name}' referenced by the pod does not exist "
                                          f"in namespace '{e.namespace}'.")
        elif e.kind == "StorageClass":
            ok = st is not None
            checks.append((f"storageclass {e.name} exists", ok))
            if not ok:
                violation = violation or (f"StorageClass '{e.name}' referenced by a PersistentVolumeClaim "
                                          f"does not exist.")
        elif e.kind == "Node" and st:
            for cond in st["status"].get("conditions", []):
                if cond.get("type") in ("DiskPressure", "MemoryPressure", "PIDPressure") and cond.get("status") == "True":
                    checks.append((f"{e.name} condition {cond['type']} = True", False))
                    violation = violation or (f"Node '{e.name}' reports {cond['type']}=True, so the kubelet "
                                              f"is evicting pods to recover resources.")
                else:
                    checks.append((f"{e.name} condition {cond.get('type')} is not active", True))
    return violation, checks


def _container_status(pod_state, container_name):
    if not pod_state:
        return None
    for cs in pod_state["status"].get("containerStatuses", []):
        if cs.get("name") == container_name:
            return cs
    return None


def _container_failure(cs):
    """Return (reason, message) for failures whose cause is confirmed by the container's status."""
    if not cs:
        return None, ""
    last = (cs.get("lastState") or {}).get("terminated") or {}
    now = (cs.get("state") or {}).get("terminated") or {}
    if "OOMKilled" in (last.get("reason"), now.get("reason")):
        code = last.get("exitCode", now.get("exitCode"))
        return "OOMKilled", f"Exit code {code}: the container exceeded its memory limit."
    waiting = (cs.get("state") or {}).get("waiting") or {}
    if waiting.get("reason") == "CreateContainerConfigError":
        return "CreateContainerConfigError", waiting.get("message", "")
    return None, ""


def _image_pull_failure(pod_state):
    """Return (reason, message) if a container of this pod is waiting on an image pull error."""
    if not pod_state:
        return None, None
    for cs in pod_state["status"].get("containerStatuses", []):
        waiting = (cs.get("state") or {}).get("waiting") or {}
        if waiting.get("reason") in ("ErrImagePull", "ImagePullBackOff"):
            return waiting["reason"], waiting.get("message", "")
    return None, None


RESOLUTIONS = {
    "Namespace": "kubectl get all -n {name}  # wait for termination, or remove objects that block it",
    "ServiceAccount": "kubectl create serviceaccount {name} -n {ns}  # or correct serviceAccountName in the pod spec",
    "StorageClass": "kubectl get storageclass  # create StorageClass '{name}' or correct storageClassName on the PVC",
    "ResourceQuota": "kubectl patch resourcequota {name} -n {ns} -p '{{\"spec\":{{\"hard\":{{\"pods\":\"60\"}}}}}}' --dry-run=server",
    "PersistentVolumeClaim": "kubectl describe pvc {name} -n {ns}  # then check the provisioner for StorageClass '{sc}'",
    "ConfigMap": "kubectl get configmap {name} -n {ns}  # recreate from source manifest if it was removed",
    "Node": "kubectl describe node {name}  # then free disk space on the node (logs, image cache)",
}


def resolution_for(dest_kind, statepath, sg):
    if dest_kind == "Image":
        pod = next((sg.entities[x] for x in statepath if sg.entities[x].kind == "Pod"), None)
        if pod:
            return (f"kubectl describe pod {pod.name} -n {pod.namespace}  "
                    f"# confirm the image tag exists in the registry, then fix the image in the pod spec")
    if dest_kind == "Container":
        pod = next((sg.entities[x] for x in statepath if sg.entities[x].kind == "Pod"), None)
        cont = next((sg.entities[x] for x in statepath if sg.entities[x].kind == "Container"), None)
        if pod and cont:
            cname = cont.name.split("/")[-1]
            return (f"kubectl logs {pod.name} -c {cname} -n {pod.namespace} --previous  "
                    f"# then: kubectl describe pod {pod.name} -n {pod.namespace}")
    for eid in statepath:
        e = sg.entities[eid]
        if e.kind == dest_kind:
            st = e.state_at(float("inf")) or {}
            sc = st.get("spec", {}).get("storageClassName", "<storageclass>")
            return RESOLUTIONS.get(dest_kind, "").format(name=e.name, ns=e.namespace or "<namespace>", sc=sc)
    return "Manual investigation required."


# ---------------- ReportGen ----------------
REPORT_SYS = (
    "You are ReportGen for Kubernetes RCA. Using only the provided diagnostic summaries, "
    "produce JSON: {\"conclusion\": str, \"overall_score\": int 0-10, \"resolution\": str}. "
    "Do not state facts absent from the summaries."
)


def report_gen(llm, incident_msg, summaries, violation, dest_kind, statepath, sg, t):
    fallback = {
        "conclusion": violation or f"The evidence does not confirm a root cause in {dest_kind}.",
        "overall_score": 9 if violation else 4,
        "resolution": resolution_for(dest_kind, statepath, sg),
        "method": "rules",
    }
    if llm.available:
        out = llm.ask_json(REPORT_SYS, f"error: {incident_msg}\nsummaries: {summaries}")
        if out and {"conclusion", "overall_score", "resolution"} <= out.keys():
            out["method"] = "llm"
            return out
    return fallback


# ---------------- ReportQualityChecker ----------------
QC_SYS = (
    "You are a ReportQualityChecker. Score 0-10 how fully the conclusion explains the error. "
    "Respond ONLY with JSON: {\"score\": int, \"further_investigation\": bool}"
)


def quality_check(llm, report, incident_msg):
    score = int(report["overall_score"])
    if llm.available:
        out = llm.ask_json(QC_SYS, f"error: {incident_msg}\nconclusion: {report['conclusion']}")
        if out and "score" in out:
            score = int(out["score"])
    # Paper thresholds: >9 done; 7-9 with summary; <5 needs further investigation.
    return {"score": score, "further_investigation": score < 7}
