"""Run with:  python tests/test_pipeline.py   (pytest also discovers these)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.pop("OPENAI_API_KEY", None)  # tests are always offline

from synergyrca import modules  # noqa: E402
from synergyrca.driver import diagnose  # noqa: E402
from synergyrca.fixtures import SCENARIOS  # noqa: E402
from synergyrca.graph import MetaGraph  # noqa: E402
from synergyrca.llm import LLM  # noqa: E402

EXPECTED_DEST = {
    "quota": "ResourceQuota",
    "pvc": "PersistentVolumeClaim",
    "configmap": "ConfigMap",
    "node": "Node",
}


def test_end_to_end_all_scenarios():
    for name, dest in EXPECTED_DEST.items():
        sg, event = SCENARIOS[name]()
        d = diagnose(event, sg, LLM())
        assert d.dest_kind == dest, (name, d.dest_kind)
        assert d.status == "DIAGNOSED", (name, d.status)
        assert d.statepath, name
        assert d.conclusion and d.resolution, name


def test_quota_statepath_goes_through_namespace():
    sg, event = SCENARIOS["quota"]()
    d = diagnose(event, sg, LLM())
    assert d.statepath == [
        "Job/foobar3/es-cronjob-1607637300",
        "Namespace/foobar3",
        "ResourceQuota/foobar3/compute-resources-foobar3",
    ]


def test_missing_configmap_is_referenced_but_has_no_state():
    sg, _ = SCENARIOS["configmap"]()
    cm = sg.find("ConfigMap", "gen-white-list-conf", "foobar2")
    assert cm is not None and cm.snapshots == []


def test_metagraph_has_pod_to_pvc_path():
    sg, _ = SCENARIOS["pvc"]()
    steps = MetaGraph(sg).metapath("Pod", "PersistentVolumeClaim")
    assert steps and steps[0][1] == "PersistentVolumeClaim"


def test_low_quality_score_requests_further_investigation():
    report = {"conclusion": "x", "overall_score": 4, "resolution": "y"}
    assert modules.quality_check(LLM(), report, "msg")["further_investigation"] is True


def test_heuristic_triage_naming_convention():
    kind = modules.heuristic_triage("Pod", 'cannot find volume "db-cert" to mount',
                                    ["ConfigMap", "Secret"])
    assert kind == "Secret"


def test_image_pull_failure_is_diagnosed_as_image():
    from synergyrca.graph import StateGraph
    sg = StateGraph()
    sg.add_object("Pod", "bad-image", "demo-img",
                  {"containers": [{"name": "c", "image": "nginx:no-such-tag"}]},
                  {"containerStatuses": [{"state": {"waiting": {
                      "reason": "ImagePullBackOff",
                      "message": "failed to resolve reference: not found"}}}]}, 1000.0)
    from synergyrca.driver import Event
    ev = Event("Pod", "bad-image", "demo-img", "Failed",
               "Error: ImagePullBackOff", 1010.0)
    d = diagnose(ev, sg, LLM())
    assert d.dest_kind == "Image", d.dest_kind
    assert "cannot be pulled" in d.conclusion


def test_unknown_error_is_not_guessed():
    assert modules.heuristic_triage("Pod", "something nobody has seen", ["ConfigMap", "Node"]) is None


def test_quantities_parse_for_memory_and_cpu():
    assert modules.parse_quantity("2Gi") == 2 * 1024**3
    assert modules.parse_quantity("500m") == 0.5
    assert modules.parse_quantity("50") == 50


class _FakeLLM:
    """Stands in for a model that always suggests the wrong kind and scores itself 9/10."""
    available = True
    calls = 0
    total_tokens = 0
    last_error = None

    def __init__(self, suggestion):
        self.suggestion = suggestion

    def ask_json(self, system, user):
        if "triage" in system:
            return {"DestinationKind": self.suggestion}
        if "ReportGen" in system:
            return {"conclusion": "Suggested cause from the model", "overall_score": 9, "resolution": "x"}
        if "ReportQualityChecker" in system:
            return {"score": 9, "further_investigation": False}
        return None


def test_gate_blocks_unverified_llm_diagnosis():
    sg, event = SCENARIOS["quota"]()
    sg2, event2 = quota_not_full()
    d = diagnose(event2, sg2, _FakeLLM("Namespace"))
    assert d.status == "NEEDS_REVIEW", d.status


def test_gate_allows_verified_llm_diagnosis():
    sg, event = SCENARIOS["quota"]()
    d = diagnose(event, sg, _FakeLLM("ResourceQuota"))
    assert d.status == "DIAGNOSED", d.status
    assert "reached its pods limit" in d.conclusion


def quota_not_full():
    from synergyrca.eval_cases import quota_full
    return quota_full("team-c", 3, 1)


def _pod_with_container_status(status_entry, msg, reason_name):
    from synergyrca.driver import Event
    from synergyrca.graph import StateGraph
    sg = StateGraph()
    sg.add_object("Namespace", "app", "", {}, {"phase": "Active"}, 1000.0)
    sg.add_object("Pod", "api-0", "app",
                  {"containers": [{"name": "api", "image": "api:1.0"}]},
                  {"containerStatuses": [dict(name="api", **status_entry)]}, 1000.0)
    ev = Event("Pod", "api-0", "app", reason_name, msg, 1010.0)
    return diagnose(ev, sg, LLM())


def test_oomkilled_container_is_verified():
    d = _pod_with_container_status(
        {"lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}},
        "Container api was OOMKilled", "BackOff")
    assert d.dest_kind == "Container" and d.status == "DIAGNOSED", (d.dest_kind, d.status)
    assert "memory limit" in d.conclusion


def test_createcontainerconfigerror_is_verified():
    d = _pod_with_container_status(
        {"state": {"waiting": {"reason": "CreateContainerConfigError",
                               "message": "couldn't find key db in ConfigMap app/settings"}}},
        "Error: CreateContainerConfigError", "Failed")
    assert d.dest_kind == "Container" and d.status == "DIAGNOSED", (d.dest_kind, d.status)


def test_plain_crashloop_stays_unknown():
    d = _pod_with_container_status(
        {"state": {"waiting": {"reason": "CrashLoopBackOff"}}},
        "Back-off restarting failed container api", "BackOff")
    assert d.status == "NEEDS_REVIEW", d.status


def test_history_round_trip():
    import tempfile
    from synergyrca.export import load_history, save_history
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "history.json")
        save_history({"a": {"id": "a", "status": "DIAGNOSED"}}, path)
        assert load_history(path) == {"a": {"id": "a", "status": "DIAGNOSED"}}
        assert load_history(os.path.join(d, "missing.json")) == {}


def test_dry_run_only_runs_patch_commands():
    from synergyrca.export import run_dry_run
    assert run_dry_run("kubectl describe node worker-03") == ""
    assert run_dry_run("rm -rf /  # not a kubectl patch") == ""
    assert isinstance(run_dry_run("kubectl patch resourcequota q -n ns -p '{}' --dry-run=server"), str)


if __name__ == "__main__":
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
