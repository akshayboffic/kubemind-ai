"""Live mode: watch a real Kubernetes cluster and diagnose Warning events as they happen.

Setup:  pip install kubernetes
Run:    python -m synergyrca.live
Stop:   Ctrl+C
"""
import os
import sys
import threading
import time

from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException

from .__main__ import render
from .driver import Event, diagnose
from .graph import StateGraph
from .export import load_history, publish, run_dry_run, save_history, to_record
from .llm import LLM

RESULTS = {}  # incident id -> dashboard record, rewritten to dashboard/data.js
CORE = None  # CoreV1Api, set in main, used to read pod logs

SENSITIVE = {"ConfigMap", "Secret"}  # only existence is stored, never the data
DIAGNOSABLE = {"Pod", "Job"}         # source kinds the triage rules support
RETRY_SECONDS = 2
# Cluster internals are skipped by default. Override with a comma-separated list, e.g.
#   set SYNERGYRCA_IGNORE_NAMESPACES=kube-system,my-team-ns
DEFAULT_IGNORED = "kube-system,kube-public,kube-node-lease,local-path-storage"
IGNORED_NAMESPACES = set(filter(None, os.environ.get("SYNERGYRCA_IGNORE_NAMESPACES", DEFAULT_IGNORED).split(",")))
WATCH_TIMEOUT = 300


def make_listers(core, batch, storage):
    """kind -> (list function, namespaced)"""
    return {
        "Pod": (core.list_pod_for_all_namespaces, True),
        "Job": (batch.list_job_for_all_namespaces, True),
        "ResourceQuota": (core.list_resource_quota_for_all_namespaces, True),
        "PersistentVolumeClaim": (core.list_persistent_volume_claim_for_all_namespaces, True),
        "ConfigMap": (core.list_config_map_for_all_namespaces, True),
        "Secret": (core.list_secret_for_all_namespaces, True),
        "Namespace": (core.list_namespace, False),
        "Node": (core.list_node, False),
        "ServiceAccount": (core.list_service_account_for_all_namespaces, True),
        "StorageClass": (storage.list_storage_class, False),
    }


def ingest(sg, kind, raw, namespaced):
    meta = raw["metadata"]
    ns = meta.get("namespace", "") if namespaced else ""
    if kind in SENSITIVE:
        spec, status = {}, {}
    else:
        spec, status = raw.get("spec") or {}, raw.get("status") or {}
    now = time.time()
    existing = sg.find(kind, meta["name"], ns)
    if existing and existing.snapshots:
        last = existing.snapshots[-1]
        if last["spec"] == spec and last["status"] == status:
            last["time"] = now  # consecutive duplicate: keep the last one (paper Sec. III-C)
            return
    sg.add_object(kind, meta["name"], ns, spec, status, now)


def run_watcher(kind, lister, namespaced, api, sg, lock, stop):
    """Load the current state of one kind, then keep it updated from the watch stream."""
    while not stop.is_set():
        try:
            listed = lister()
            with lock:
                for item in listed.items:
                    ingest(sg, kind, api.sanitize_for_serialization(item), namespaced)
            for ev in watch.Watch().stream(lister, resource_version=listed.metadata.resource_version,
                                           timeout_seconds=WATCH_TIMEOUT):
                if ev["type"] in ("ADDED", "MODIFIED"):
                    raw = api.sanitize_for_serialization(ev["object"])
                    with lock:
                        ingest(sg, kind, raw, namespaced)
        except ApiException as e:
            print(f"[{kind}] API error {e.status}, reconnecting", flush=True)
        except Exception as e:
            print(f"[{kind}] {type(e).__name__}: {e}, reconnecting", flush=True)
        time.sleep(RETRY_SECONDS)


def fetch_log_tail(sg, event):
    """Last lines of the pod's log, shown as evidence only. Never used to make a claim."""
    if event.involved_kind != "Pod" or CORE is None:
        return []
    pod = sg.find("Pod", event.involved_name, event.involved_namespace)
    state = pod.state_at(time.time()) if pod else None
    containers = ((state or {}).get("spec") or {}).get("containers") or []
    if not containers:
        return []
    for previous in (True, False):  # the crashed run's log is usually the useful one
        try:
            text = CORE.read_namespaced_pod_log(name=event.involved_name, namespace=event.involved_namespace,
                                                container=containers[0]["name"], previous=previous,
                                                tail_lines=15)
            return [line for line in text.splitlines() if line.strip()][-15:]
        except Exception:
            continue
    return []


def diagnose_event(d, sg, lock, llm, seen):
    inv = d.get("involvedObject") or {}
    if inv.get("kind") not in DIAGNOSABLE:
        return
    if inv.get("namespace", "") in IGNORED_NAMESPACES:
        return
    uid = d["metadata"]["uid"]
    # One report per incident: retries create new pods and events with new names,
    # so the key is the affected object plus the reason, not the event uid.
    key = (inv["kind"], inv.get("namespace", ""), inv["name"], d.get("reason", ""))
    if uid in seen or key in seen:
        return
    seen.add(uid)
    seen.add(key)
    diag = None
    for _ in range(3):  # the pod's status may not have reached the watcher yet
        # Use the current time on every attempt, so retries see newer cluster state.
        event = Event(inv["kind"], inv["name"], inv.get("namespace", ""),
                      d.get("reason", ""), d.get("message", ""), time.time())
        with lock:
            diag = diagnose(event, sg, llm)
        if diag.status == "DIAGNOSED":
            break
        time.sleep(RETRY_SECONDS)
    print(render(f"{inv['kind']}/{inv['name']}", diag), flush=True)
    record = to_record(diag, event, sg)
    record["logTail"] = fetch_log_tail(sg, event)
    record["dryRun"] = run_dry_run(record["fix"][0] if record["fix"] else "")
    RESULTS[record["id"]] = record
    publish(list(RESULTS.values()))
    save_history(RESULTS)


def run_events(core, api, sg, lock, stop, llm):
    seen = set()
    # Existing warnings are marked as seen, so only new ones trigger a diagnosis.
    listed = core.list_event_for_all_namespaces(field_selector="type=Warning")
    for item in listed.items:
        seen.add(item.metadata.uid)
    rv = listed.metadata.resource_version
    print("Watching for new Warning events. Ctrl+C to stop.", flush=True)
    while not stop.is_set():
        try:
            for ev in watch.Watch().stream(core.list_event_for_all_namespaces, resource_version=rv,
                                           field_selector="type=Warning", timeout_seconds=WATCH_TIMEOUT):
                if ev["type"] in ("ADDED", "MODIFIED"):
                    d = api.sanitize_for_serialization(ev["object"])
                    diagnose_event(d, sg, lock, llm, seen)
        except ApiException as e:
            print(f"[events] API error {e.status}, reconnecting", flush=True)
            rv = core.list_event_for_all_namespaces(field_selector="type=Warning").metadata.resource_version
        except Exception as e:
            print(f"[events] {type(e).__name__}: {e}, reconnecting", flush=True)
        time.sleep(RETRY_SECONDS)


def main():
    try:
        config.load_kube_config()
    except Exception as e:
        if os.environ.get("KUBERNETES_SERVICE_HOST"):  # running inside a pod
            config.load_incluster_config()
        else:
            print(f"Could not load your kubeconfig: {e}")
            print("Fix it with:  kubectl config use-context kind-synergy-test")
            print("If that context is missing, run:  kind export kubeconfig --name synergy-test")
            sys.exit(1)
    global CORE
    core, batch = client.CoreV1Api(), client.BatchV1Api()
    CORE = core
    api = client.ApiClient()
    sg, lock, stop = StateGraph(), threading.Lock(), threading.Event()
    RESULTS.update(load_history())  # keep incidents from earlier sessions
    llm = LLM()
    print(f"LLM: {'enabled' if llm.available else 'offline rules'}", flush=True)

    storage = client.StorageV1Api()
    for kind, (lister, namespaced) in make_listers(core, batch, storage).items():
        threading.Thread(target=run_watcher, args=(kind, lister, namespaced, api, sg, lock, stop),
                         daemon=True).start()
    time.sleep(3)  # give the watchers a moment to load current state

    try:
        run_events(core, api, sg, lock, stop, llm)
    except KeyboardInterrupt:
        stop.set()
        print("Stopped.")


if __name__ == "__main__":
    main()
