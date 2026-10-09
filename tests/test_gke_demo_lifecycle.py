"""Mocked cloud lifecycle regressions. No cloud process, real state, or resources."""
import copy
import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def demo(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/gke_gpu_demo.py"
    spec = importlib.util.spec_from_file_location("gke_lifecycle_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    saved, emitted = [], []

    def no_cloud(*args, **kwargs):
        raise AssertionError("Every cloud interaction must be explicitly mocked")

    monkeypatch.setattr(module, "cloud", no_cloud)
    monkeypatch.setattr(module, "save_private", lambda path, state: saved.append(copy.deepcopy(state)))
    monkeypatch.setattr(module, "emit", lambda data, output=None: emitted.append(copy.deepcopy(data)))
    monkeypatch.setattr(module, "signal", SimpleNamespace(SIGTERM=15, signal=lambda *args: None))
    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    args = SimpleNamespace(project="synthetic-project", zone="us-central1-a",
                           name="fund-llm-demo-synthetic", state="unused-synthetic-state")
    state = {"project": args.project, "zone": args.zone, "name": args.name,
             "demo_id": "synthetic-owner", "create_attempted": True,
             "observed_cluster": False, "cluster_deleted": False,
             "cleanup_due": "2026-10-09T20:00:00+00:00",
             "create_operation": "operation-synthetic-create", "create_operation_status": "RUNNING"}
    return SimpleNamespace(module=module, args=args, state=state, saved=saved,
                           emitted=emitted, clock=clock)


def operation(demo, status="DONE"):
    return {"name": "operation-synthetic-create", "operationType": "CREATE_CLUSTER",
            "status": status,
            "targetLink": f"https://container.googleapis.com/v1/projects/123/zones/{demo.args.zone}/clusters/{demo.args.name}"}


def owned_cluster(demo):
    return {"status": "RUNNING", "resourceLabels": {"fund-demo-id": demo.state["demo_id"]}}


def argv(monkeypatch, action):
    monkeypatch.setattr(sys, "argv", ["gke_gpu_demo.py", action,
        "--project", "synthetic-project", "--zone", "us-central1-a",
        "--name", "fund-llm-demo-synthetic", "--state", "unused-synthetic-state",
        "--node-service-account", "node@synthetic-project.iam.gserviceaccount.com",
        "--authorized-cidr", "192.0.2.1/32", "--execute", "--watch"])


def test_delayed_creation_is_pending_then_owned_cluster_can_be_deleted(demo, monkeypatch):
    status, exists, calls = ["RUNNING"], [False], []

    def cloud(project, *cmd, **kwargs):
        assert project == demo.args.project
        calls.append(cmd)
        if cmd[:3] == ("container", "operations", "describe"):
            return operation(demo, status[0])
        if cmd[:3] == ("container", "clusters", "describe"):
            if not exists[0]:
                raise demo.module.NotFound("synthetic absence")
            return owned_cluster(demo)
        if cmd[:3] == ("container", "clusters", "delete"):
            exists[0] = False
            return {"name": "operation-synthetic-delete"}
        pytest.fail(f"unexpected cloud call {cmd}")

    monkeypatch.setattr(demo.module, "cloud", cloud)
    result = demo.module.cleanup(demo.args, demo.state)
    assert result["cleanup_pending"] and not result["cluster_deleted"]
    assert len(calls) == 1  # Pending operation does not trigger a speculative delete.
    assert not demo.saved[-1]["cleanup_confirmed"]

    status[0], exists[0] = "DONE", True
    result = demo.module.cleanup(demo.args, demo.state)
    assert result["cluster_deleted"] and result["cleanup_confirmed"]
    assert not result["cleanup_pending"]
    assert len([cmd for cmd in calls if cmd[:3] == ("container", "clusters", "delete")]) == 1
    assert calls[-3][:3] == ("container", "clusters", "describe")
    assert calls[-1][:3] == ("container", "clusters", "describe")


def test_interruption_preserves_accepted_operation_before_cleanup(demo, monkeypatch):
    argv(monkeypatch, "create")
    monkeypatch.setattr(demo.module, "preflight", lambda args: {"preflight_passed": True})
    calls = []

    def cloud(project, *cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ("container", "clusters", "create"):
            return operation(demo, "RUNNING")
        if cmd[:3] == ("container", "operations", "describe"):
            return operation(demo, "RUNNING")
        pytest.fail(f"unexpected cloud call {cmd}")

    def emit(data, output=None):
        demo.emitted.append(copy.deepcopy(data))
        if data.get("cloud_create_requested"):
            assert demo.saved[-1]["create_operation"] == "operation-synthetic-create"
            raise KeyboardInterrupt()

    monkeypatch.setattr(demo.module, "cloud", cloud)
    monkeypatch.setattr(demo.module, "emit", emit)
    assert demo.module.main() == 2
    assert demo.saved[-1]["create_operation"] == "operation-synthetic-create"
    assert demo.saved[-1]["cleanup_pending"] and not demo.saved[-1]["cluster_deleted"]
    assert demo.emitted[-1]["cleanup_must_be_confirmed"]
    assert not any(cmd[:3] == ("container", "clusters", "delete") for cmd in calls)


def test_create_timeout_without_operation_identity_never_claims_cleanup(demo, monkeypatch):
    argv(monkeypatch, "create")
    monkeypatch.setattr(demo.module, "preflight", lambda args: {"preflight_passed": True})
    calls = []

    def cloud(project, *cmd, **kwargs):
        calls.append(cmd)
        assert cmd[:3] == ("container", "clusters", "create")
        raise subprocess.TimeoutExpired("synthetic create", 120)

    monkeypatch.setattr(demo.module, "cloud", cloud)
    assert demo.module.main() == 2
    assert len(calls) == 1
    assert demo.saved[-1]["create_attempted"]
    assert not demo.saved[-1].get("create_operation")
    assert demo.saved[-1]["cleanup_pending"] and not demo.saved[-1]["cluster_deleted"]
    assert any(item.get("create_operation_status") == "UNKNOWN" for item in demo.emitted)
    assert demo.emitted[-1]["cleanup_must_be_confirmed"]


@pytest.mark.parametrize("error", [None, {"code": 1, "message": "synthetic cancellation"}])
def test_terminal_create_and_fresh_absence_confirm_cleanup(demo, monkeypatch, error):
    calls = []

    def cloud(project, *cmd, **kwargs):
        calls.append(cmd[:3])
        if cmd[:3] == ("container", "operations", "describe"):
            response = operation(demo)
            if error:
                response["error"] = error
            return response
        if cmd[:3] == ("container", "clusters", "describe"):
            raise demo.module.NotFound("synthetic completed absence")
        pytest.fail("absent completed cluster must not trigger deletion")

    monkeypatch.setattr(demo.module, "cloud", cloud)
    result = demo.module.cleanup(demo.args, demo.state)
    assert result["cluster_deleted"] and result["cleanup_confirmed"]
    assert not demo.saved[-1]["cleanup_pending"]
    assert calls == [("container", "operations", "describe"), ("container", "clusters", "describe")]


def test_foreign_cluster_is_never_deleted(demo, monkeypatch):
    def cloud(project, *cmd, **kwargs):
        if cmd[:3] == ("container", "operations", "describe"):
            return operation(demo)
        if cmd[:3] == ("container", "clusters", "describe"):
            return {"resourceLabels": {"fund-demo-id": "someone-else"}}
        pytest.fail("foreign cluster must not receive a delete")

    monkeypatch.setattr(demo.module, "cloud", cloud)
    with pytest.raises(demo.module.CloudError, match="ownership"):
        demo.module.cleanup(demo.args, demo.state)
    assert demo.saved[-1]["cleanup_pending"] and not demo.saved[-1]["cluster_deleted"]


@pytest.mark.parametrize("status", ["RUNNING", "PENDING", "ABORTING", "UNKNOWN", "DONE"])
def test_status_does_not_equate_early_not_found_with_deleted(demo, monkeypatch, status):
    argv(monkeypatch, "status")
    demo.state["cluster_deleted"] = True  # A stale earlier report must not override current evidence.
    if status == "UNKNOWN":
        demo.state.pop("create_operation")
    monkeypatch.setattr(demo.module, "read_state", lambda args: copy.deepcopy(demo.state))

    def cloud(project, *cmd, **kwargs):
        if cmd[:3] == ("container", "operations", "describe"):
            return operation(demo, status)
        if cmd[:3] == ("container", "clusters", "describe"):
            raise demo.module.NotFound("synthetic absence")
        pytest.fail("status must never mutate cloud resources")

    monkeypatch.setattr(demo.module, "cloud", cloud)
    assert demo.module.main() == (0 if status == "DONE" else 2)
    assert demo.emitted[-1]["cluster_deleted"] is (status == "DONE")
    assert demo.emitted[-1]["cleanup_pending"] is (status != "DONE")
    assert not demo.saved  # Status is read-only, including the private state.


@pytest.mark.parametrize("failure", ["not_found", "timeout", "wrong_target", "wrong_operation", "bad_response"])
def test_unverifiable_create_operation_fails_closed(demo, monkeypatch, failure):
    def cloud(project, *cmd, **kwargs):
        assert cmd[:3] == ("container", "operations", "describe")
        if failure == "not_found":
            raise demo.module.NotFound("synthetic missing operation")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("synthetic operation lookup", 45)
        if failure == "bad_response":
            return []
        response = operation(demo)
        if failure == "wrong_target":
            response["targetLink"] += "-other"
        else:
            response["operationType"] = "UPGRADE_MASTER"
        return response

    monkeypatch.setattr(demo.module, "cloud", cloud)
    result = demo.module.cleanup(demo.args, demo.state)
    assert result["cleanup_pending"] and not result["cluster_deleted"]
    assert result["create_operation_status"] == "UNKNOWN"


def test_delete_wait_is_bounded_and_remains_unconfirmed(demo, monkeypatch):
    deleted = []

    def cloud(project, *cmd, **kwargs):
        if cmd[:3] == ("container", "operations", "describe"):
            return operation(demo)
        if cmd[:3] == ("container", "clusters", "describe"):
            return owned_cluster(demo)
        if cmd[:3] == ("container", "clusters", "delete"):
            deleted.append(cmd)
            return {}
        pytest.fail(f"unexpected cloud call {cmd}")

    monkeypatch.setattr(demo.module, "cloud", cloud)
    with pytest.raises(demo.module.CloudError, match="not confirmed"):
        demo.module.cleanup(demo.args, demo.state)
    assert len(deleted) == 1
    assert demo.clock[0] == 600
    assert demo.saved[-1]["cleanup_pending"] and not demo.saved[-1]["cleanup_confirmed"]
