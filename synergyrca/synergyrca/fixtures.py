"""Mock cluster state and incidents for offline runs and tests.
Each scenario mirrors one incident type from the paper's Table I."""
from .driver import Event
from .graph import StateGraph

T = 1000.0      # snapshot time
T_EVENT = 1010.0


def _base_graph():
    sg = StateGraph()
    sg.add_object("Namespace", "foobar3", "", {}, {"phase": "Active"}, T)
    sg.add_object("Namespace", "foobar4", "", {}, {"phase": "Active"}, T)
    sg.add_object("Namespace", "foobar2", "", {}, {"phase": "Active"}, T)
    sg.add_object("Namespace", "analytics", "", {}, {"phase": "Active"}, T)
    return sg


def quota_scenario():
    sg = _base_graph()
    sg.add_object("ResourceQuota", "compute-resources-foobar3", "foobar3",
                  {"hard": {"pods": "50"}}, {"hard": {"pods": "50"}, "used": {"pods": "50"}}, T)
    sg.add_object("Job", "es-cronjob-1607637300", "foobar3",
                  {"template": {"spec": {"containers": [{"name": "es"}]}}}, {}, T)
    msg = ('Error creating: pods "es-cronjob-1607637300-fpb68" is forbidden: exceeded quota: '
           "compute-resources-foobar3, requested: pods=1, used: pods=50, limited: pods=50")
    ev = Event("Job", "es-cronjob-1607637300", "foobar3", "FailedCreate", msg, T_EVENT)
    return sg, ev


def pvc_scenario():
    sg = _base_graph()
    sg.add_object("PersistentVolumeClaim", "data-mysql-0", "foobar4",
                  {"storageClassName": "nfs-client"}, {"phase": "Pending"}, T)
    sg.add_object("Pod", "mysql-0", "foobar4",
                  {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data-mysql-0"}}]},
                  {"phase": "Pending"}, T)
    msg = "pod has unbound immediate PersistentVolumeClaims"
    ev = Event("Pod", "mysql-0", "foobar4", "FailedScheduling", msg, T_EVENT)
    return sg, ev


def configmap_scenario():
    sg = _base_graph()
    # The ConfigMap is referenced but never observed, so it has no STATE vertex.
    sg.add_object("Pod", "es-crontab-job", "foobar2",
                  {"volumes": [{"name": "list", "configMap": {"name": "gen-white-list-conf"}}]},
                  {"phase": "Pending"}, T)
    msg = 'Error: cannot find volume "gen-white-list-conf" to mount into container "es-crontab-job"'
    ev = Event("Pod", "es-crontab-job", "foobar2", "FailedMount", msg, T_EVENT)
    return sg, ev


def node_pressure_scenario():
    sg = _base_graph()
    sg.add_object("Node", "worker-03", "",
                  {}, {"conditions": [{"type": "DiskPressure", "status": "True"}]}, T)
    sg.add_object("Pod", "batch-report-7f9c", "analytics",
                  {"nodeName": "worker-03"}, {"phase": "Failed", "reason": "Evicted"}, T)
    msg = "The node was low on resource: ephemeral-storage"
    ev = Event("Pod", "batch-report-7f9c", "analytics", "Evicted", msg, T_EVENT)
    return sg, ev


SCENARIOS = {
    "quota": quota_scenario,
    "pvc": pvc_scenario,
    "configmap": configmap_scenario,
    "node": node_pressure_scenario,
}
