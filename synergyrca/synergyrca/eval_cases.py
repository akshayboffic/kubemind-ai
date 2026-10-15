"""Labeled evaluation cases.

expected = the correct root-cause kind, or None when the correct answer is "unknown"
(the system should flag the incident for review rather than guess a cause).
"""
from .driver import Event
from .fixtures import T, T_EVENT, configmap_scenario, node_pressure_scenario, pvc_scenario, quota_scenario
from .graph import StateGraph


def _namespace(sg, name):
    sg.add_object("Namespace", name, "", {}, {"phase": "Active"}, T)


def quota_full(ns, hard, used):
    sg = StateGraph()
    _namespace(sg, ns)
    sg.add_object("ResourceQuota", "compute", ns, {"hard": {"pods": str(hard)}},
                  {"hard": {"pods": str(hard)}, "used": {"pods": str(used)}}, T)
    sg.add_object("Job", "batch", ns, {"template": {}}, {}, T)
    msg = (f'Error creating: pods "batch-x1" is forbidden: exceeded quota: compute, '
           f"requested: pods=1, used: pods={used}, limited: pods={hard}")
    return sg, Event("Job", "batch", ns, "FailedCreate", msg, T_EVENT)


def pvc_bound_stale_event():
    sg = StateGraph()
    _namespace(sg, "db")
    sg.add_object("PersistentVolumeClaim", "data", "db", {"storageClassName": "nfs-client"},
                  {"phase": "Bound"}, T)
    sg.add_object("Pod", "app-0", "db",
                  {"volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": "data"}}]}, {}, T)
    msg = "pod has unbound immediate PersistentVolumeClaims"
    return sg, Event("Pod", "app-0", "db", "FailedScheduling", msg, T_EVENT)


def secret_missing():
    sg = StateGraph()
    _namespace(sg, "web")
    sg.add_object("Pod", "web-0", "web",
                  {"volumes": [{"name": "creds", "secret": {"secretName": "db-creds"}}]},
                  {"phase": "Pending"}, T)
    msg = 'MountVolume.SetUp failed for volume "creds" : secret "db-creds" not found'
    return sg, Event("Pod", "web-0", "web", "FailedMount", msg, T_EVENT)


def node_healthy_evicted():
    sg = StateGraph()
    sg.add_object("Node", "worker-01", "", {}, {"conditions": [{"type": "Ready", "status": "True"}]}, T)
    sg.add_object("Pod", "job-1", "analytics", {"nodeName": "worker-01"},
                  {"phase": "Failed", "reason": "Evicted"}, T)
    msg = "The node was low on resource: memory"
    return sg, Event("Pod", "job-1", "analytics", "Evicted", msg, T_EVENT)


def _image_pod(waiting):
    sg = StateGraph()
    _namespace(sg, "img")
    status = {"containerStatuses": [{"state": {"waiting": waiting}}]} if waiting else {}
    sg.add_object("Pod", "app", "img", {"containers": [{"name": "c", "image": "app:v9-missing"}]},
                  status, T)
    return sg


def image_bad_tag():
    sg = _image_pod({"reason": "ErrImagePull", "message": "manifest unknown: not found"})
    msg = 'Failed to pull image "app:v9-missing": manifest unknown'
    return sg, Event("Pod", "app", "img", "Failed", msg, T_EVENT)


def image_error_no_state():
    sg = _image_pod(None)  # message says pull failed, but the pod status shows no waiting error
    msg = 'Failed to pull image "app:v9-missing": manifest unknown'
    return sg, Event("Pod", "app", "img", "Failed", msg, T_EVENT)


def crashloop():
    sg = StateGraph()
    _namespace(sg, "pay")
    sg.add_object("Pod", "api-1", "pay", {"containers": [{"name": "api", "image": "api:1.0"}]},
                  {"containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}, T)
    return sg, Event("Pod", "api-1", "pay", "BackOff", "Back-off restarting failed container api", T_EVENT)


def scheduling_insufficient_memory():
    sg = StateGraph()
    _namespace(sg, "ml")
    sg.add_object("Pod", "train-0", "ml", {}, {"phase": "Pending"}, T)
    msg = "0/3 nodes are available: 3 Insufficient memory."
    return sg, Event("Pod", "train-0", "ml", "FailedScheduling", msg, T_EVENT)


CASES = [
    ("quota-demo", "ResourceQuota", quota_scenario),
    ("quota-team-a-3of3", "ResourceQuota", lambda: quota_full("team-a", 3, 3)),
    ("quota-team-b-2of2", "ResourceQuota", lambda: quota_full("team-b", 2, 2)),
    ("quota-not-full-stale-event", None, lambda: quota_full("team-c", 3, 1)),
    ("pvc-pending", "PersistentVolumeClaim", pvc_scenario),
    ("pvc-bound-stale-event", None, pvc_bound_stale_event),
    ("configmap-missing", "ConfigMap", configmap_scenario),
    ("secret-missing", "Secret", secret_missing),
    ("node-disk-pressure", "Node", node_pressure_scenario),
    ("node-healthy-evicted", None, node_healthy_evicted),
    ("image-bad-tag", "Image", image_bad_tag),
    ("image-error-no-state", None, image_error_no_state),
    ("crashloop-exit-1", None, crashloop),
    ("scheduling-insufficient-memory", None, scheduling_insufficient_memory),
]


# ---------- Unseen cases: no rule was written for these. They test generalization. ----------
def _ns_phase(sg, name, phase):
    sg.add_object("Namespace", name, "", {}, {"phase": phase}, T)


def service_account_missing():
    sg = StateGraph()
    _namespace(sg, "api")
    sg.add_object("Pod", "svc-1", "api", {"serviceAccountName": "builder"}, {"phase": "Pending"}, T)
    msg = 'error looking up service account api/builder: serviceaccount "builder" not found'
    return sg, Event("Pod", "svc-1", "api", "FailedCreate", msg, T_EVENT)


def namespace_terminating():
    sg = StateGraph()
    _ns_phase(sg, "demo", "Terminating")
    sg.add_object("Job", "batch", "demo", {"template": {}}, {}, T)
    msg = "Error creating: unable to create new content in namespace demo because it is being terminated"
    return sg, Event("Job", "batch", "demo", "FailedCreate", msg, T_EVENT)


def storageclass_missing():
    sg = StateGraph()
    _namespace(sg, "data")
    sg.add_object("PersistentVolumeClaim", "cache", "data", {"storageClassName": "fast-ssd"},
                  {"phase": "Pending"}, T)
    msg = 'storageclass.storage.k8s.io "fast-ssd" not found'
    return sg, Event("PersistentVolumeClaim", "cache", "data", "ProvisioningFailed", msg, T_EVENT)


def liveness_probe_failed():
    sg = StateGraph()
    _namespace(sg, "shop")
    sg.add_object("Pod", "web-2", "shop", {"containers": [{"name": "web", "image": "web:2.1"}]},
                  {"phase": "Running"}, T)
    msg = "Liveness probe failed: HTTP probe failed with statuscode: 500"
    return sg, Event("Pod", "web-2", "shop", "Unhealthy", msg, T_EVENT)


def insufficient_cpu():
    sg = StateGraph()
    _namespace(sg, "ml")
    sg.add_object("Pod", "train-1", "ml", {}, {"phase": "Pending"}, T)
    msg = "0/3 nodes are available: 3 Insufficient cpu."
    return sg, Event("Pod", "train-1", "ml", "FailedScheduling", msg, T_EVENT)


def memory_quota_full():
    sg = StateGraph()
    _namespace(sg, "gpu")
    sg.add_object("ResourceQuota", "limits", "gpu", {"hard": {"limits.memory": "2Gi"}},
                  {"hard": {"limits.memory": "2Gi"}, "used": {"limits.memory": "2Gi"}}, T)
    sg.add_object("Job", "train", "gpu", {"template": {}}, {}, T)
    msg = ('Error creating: pods "train-x9" is forbidden: exceeded quota: limits, '
           "requested: limits.memory=1Gi, used: limits.memory=2Gi, limited: limits.memory=2Gi")
    return sg, Event("Job", "train", "gpu", "FailedCreate", msg, T_EVENT)


UNSEEN_CASES = [
    ("unseen-service-account-missing", "ServiceAccount", service_account_missing),
    ("unseen-namespace-terminating", "Namespace", namespace_terminating),
    ("unseen-storageclass-missing", "StorageClass", storageclass_missing),
    ("unseen-liveness-probe-failed", None, liveness_probe_failed),
    ("unseen-insufficient-cpu", None, insufficient_cpu),
    ("unseen-memory-quota-full", "ResourceQuota", memory_quota_full),
]


def select_cases(name):
    return {"builtin": CASES, "unseen": UNSEEN_CASES, "all": CASES + UNSEEN_CASES}[name]
