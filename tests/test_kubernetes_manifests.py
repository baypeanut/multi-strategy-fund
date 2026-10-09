"""Offline contract checks; these are not evidence of an actual GPU deployment."""
import importlib.util
from pathlib import Path
import copy

import pytest


import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "deploy" / "kubernetes"
spec = importlib.util.spec_from_file_location("k8s_model_serving", ROOT / "scripts/k8s_model_serving.py")
k8s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k8s)


def test_pods_disable_service_env_collision_and_backend_scopes_settings():
    docs = k8s.manifests("example.invalid/gateway@sha256:" + "1" * 64)
    deployments = [d for d in docs if d["kind"] == "Deployment"]
    assert all(d["spec"]["template"]["spec"]["enableServiceLinks"] is False for d in deployments)
    backend = next(d for d in deployments if d["metadata"]["name"] == "vllm")
    container = backend["spec"]["template"]["spec"]["containers"][0]
    assert "envFrom" not in container
    names = {item["name"] for item in container["env"]}
    assert "FUND_MODEL_ID" in names and "FUND_MODEL_REVISION" in names
    assert "VLLM_PORT" not in names and "VLLM_VERSION" not in names


def docs():
    listing = yaml.safe_load((BASE / "kustomization.yaml").read_text())["resources"]
    return [yaml.safe_load((BASE / name).read_text()) for name in listing]


def test_backend_has_one_gpu_persistent_cache_and_load_gated_health():
    deployment = next(d for d in docs() if d["kind"] == "Deployment" and d["metadata"]["name"] == "vllm")
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"  # no second GPU during rollout
    assert container["resources"]["requests"]["nvidia.com/gpu"] == 1
    assert container["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert "@sha256:" in container["image"] and ":latest" not in container["image"]
    assert container["startupProbe"]["failureThreshold"] * container["startupProbe"]["periodSeconds"] >= 1800
    assert all(container[p]["httpGet"]["path"] == "/health" for p in ("startupProbe", "readinessProbe", "livenessProbe"))
    assert any(v.get("persistentVolumeClaim", {}).get("claimName") == "vllm-model-cache" for v in pod["volumes"])
    assert any(m["mountPath"] == "/dev/shm" for m in container["volumeMounts"])
    assert not pod.get("hostNetwork") and not pod.get("hostIPC")
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert not pod["automountServiceAccountToken"]
    assert "--trust-remote-code" not in container["args"]


def test_keys_are_separate_refs_only_and_services_are_private():
    all_docs = docs()
    assert not any(d["kind"] == "Secret" for d in all_docs)
    gateway = next(d for d in all_docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "model-gateway")
    env = {e["name"]:e for e in gateway["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["MODEL_API_KEY"]["valueFrom"]["secretKeyRef"]["key"] != env["MODEL_BACKEND_API_KEY"]["valueFrom"]["secretKeyRef"]["key"]
    assert all(d["spec"]["type"] == "ClusterIP" for d in all_docs if d["kind"] == "Service")
    assert not any(d["kind"] in ("Ingress", "HorizontalPodAutoscaler") for d in all_docs)
    quota = next(d for d in all_docs if d["kind"] == "ResourceQuota")
    assert quota["spec"]["hard"]["requests.nvidia.com/gpu"] == "1"
    assert quota["spec"]["hard"]["services.loadbalancers"] == "0"


def test_render_requires_immutable_gateway_and_supports_cluster_settings():
    with pytest.raises(ValueError):
        k8s.manifests("example/gateway:latest")
    image = "example/gateway@sha256:" + "a" * 64
    rendered = k8s.manifests(image, namespace="fund-llm-demo", storage_class="local-path", runtime_class="nvidia")
    pvc = next(d for d in rendered if d["kind"] == "PersistentVolumeClaim")
    assert pvc["spec"]["storageClassName"] == "local-path"
    backend = next(d for d in rendered if d["kind"] == "Deployment" and d["metadata"]["name"] == "vllm")
    assert backend["spec"]["template"]["spec"]["runtimeClassName"] == "nvidia"
    assert all(d["metadata"].get("namespace", "fund-llm-demo") == "fund-llm-demo" for d in rendered)


def ready_evidence():
    backend = {"ready":True,"phase":"Running","containers":[{"name":"vllm","image":k8s.VLLM_IMAGE,"resources":{"requests":{"nvidia.com/gpu":"1"},"limits":{"nvidia.com/gpu":"1"}},"volumeMounts":[{"name":"cache","mountPath":"/model-cache"}]}],"volumes":[{"name":"cache","persistentVolumeClaim":{"claimName":"vllm-model-cache"}}],"statuses":[{"imageID":"docker-pullable://vllm@sha256:"+"a"*64}]}
    gateway = {"ready":True,"phase":"Running"}
    pvc = {"name":"vllm-model-cache","phase":"Bound"}
    runtime = {"cuda_available":True,"cuda_tensor_operation_passed":True,"device_count":1,"vllm_version":"0.31.0","expected_revision":k8s.REVISION,"model_cache_revision_exists":True,"served_models":["fund-llm"],"generation_passed":True,"actual_model":k8s.MODEL,"actual_model_revision":k8s.REVISION,"actual_tokenizer_revision":k8s.REVISION,"actual_served_model_name":"fund-llm"}
    return backend,gateway,pvc,runtime,{"gateway_ready":True}


@pytest.mark.parametrize("field,value", [("cuda_available",False),("cuda_tensor_operation_passed",False),("device_count",0),("model_cache_revision_exists",False),("generation_passed",False),("vllm_version","stub")])
def test_gpu_verification_rejects_advertised_gpu_without_actual_proof(field,value):
    evidence = ready_evidence()
    evidence[3][field] = value
    assert not k8s.evaluate_verification(*evidence)["gpu_deployment_verified"]


def test_pending_pod_or_pvc_never_counts_as_deployed():
    evidence = ready_evidence()
    evidence[0]["phase"] = "Pending"
    assert not k8s.evaluate_verification(*evidence)["gpu_deployment_verified"]
    evidence = ready_evidence()
    evidence[2]["phase"] = "Pending"
    assert not k8s.evaluate_verification(*evidence)["gpu_deployment_verified"]


def test_init_gpu_request_uses_scheduler_max_not_sum():
    pod = {"spec":{"containers":[{"resources":{"requests":{"nvidia.com/gpu":1}}}],"initContainers":[{"resources":{"requests":{"nvidia.com/gpu":2}}}]}}
    assert k8s.effective_gpu_request(pod) == 2


def test_real_kubernetes_quantity_strings_pass_with_full_hardware_cache_process_proof():
    evidence = ready_evidence()
    assert k8s.evaluate_verification(*evidence)["gpu_deployment_verified"]


@pytest.mark.parametrize("quantity", [True, 1.0, "0", "2", "fake", None])
def test_gpu_verification_rejects_non_one_integer_quantity(quantity):
    evidence = ready_evidence()
    evidence[0]["containers"][0]["resources"]["requests"]["nvidia.com/gpu"] = quantity
    assert not k8s.evaluate_verification(*evidence)["gpu_deployment_verified"]


def test_bound_unmounted_pvc_is_not_storage_evidence():
    evidence = ready_evidence()
    evidence[0]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "unrelated-cache"
    assert not k8s.evaluate_verification(*evidence)["gpu_deployment_verified"]


def test_declared_revision_is_not_actual_loaded_process_revision():
    evidence = ready_evidence()
    evidence[3]["actual_model_revision"] = "main"
    assert not k8s.evaluate_verification(*evidence)["gpu_deployment_verified"]


# Cloud orchestration tests intercept every cloud call; they provision nothing.
cloud_spec = importlib.util.spec_from_file_location("gke_gpu_demo", ROOT / "scripts/gke_gpu_demo.py")
gke = importlib.util.module_from_spec(cloud_spec)
cloud_spec.loader.exec_module(gke)


def cloud_args():
    from types import SimpleNamespace
    return SimpleNamespace(project="example-project", zone="us-central1-a", name="fund-llm-demo-test",
                           lifetime_minutes=120, budget_usd=20, node_service_account="fund-node@example-project.iam.gserviceaccount.com",
                           artifact_repository="fund-models", registry_location="us-central1", authorized_cidr="192.0.2.1/32")


def mock_cloud(global_limit=1, regional_limit=1):
    args=cloud_args()
    def cloud(project,*cmd,**kwargs):
        if cmd[:3] == ("compute","project-info","describe"):
            return {"quotas":[{"metric":"GPUS_ALL_REGIONS","limit":global_limit,"usage":0}]}
        if cmd[:3] == ("compute","regions","describe"):
            return {"quotas":[{"metric":"NVIDIA_L4_GPUS","limit":regional_limit,"usage":0}]}
        if cmd[:3] == ("billing","projects","describe"): return {"billingEnabled":True}
        if cmd[:2] == ("services","list"):
            return [{"config":{"name":"container.googleapis.com"}},{"config":{"name":"compute.googleapis.com"}}]
        if cmd[:3] == ("container","clusters","describe"): raise gke.NotFound("No cluster")
        if cmd[:2] == ("projects","get-iam-policy"):
            return {"bindings":[{"role":"roles/container.defaultNodeServiceAccount","members":["serviceAccount:"+args.node_service_account]}]}
        if cmd[:3] == ("artifacts","repositories","get-iam-policy"):
            return {"bindings":[{"role":"roles/artifactregistry.reader","members":["serviceAccount:"+args.node_service_account]}]}
        return {}
    return cloud


def test_gke_g2_preflight_does_not_invent_cpu_quota(monkeypatch):
    monkeypatch.setattr(gke,"cloud",mock_cloud())
    result=gke.preflight(cloud_args())
    assert result["preflight_passed"]
    assert {q["metric"] for q in result["quotas"]} == {"GPUS_ALL_REGIONS","NVIDIA_L4_GPUS"}
    assert not result["gpu_deployment_verified"]


@pytest.mark.parametrize("global_limit,regional_limit",[(0,1),(1,0)])
def test_zero_global_or_regional_gpu_quota_blocks_gke_creation(monkeypatch,global_limit,regional_limit):
    monkeypatch.setattr(gke,"cloud",mock_cloud(global_limit,regional_limit))
    assert not gke.preflight(cloud_args())["preflight_passed"]


def test_gke_creation_never_adds_second_gpu_node_or_surge():
    args=cloud_args();cmd=gke.create_command(args,"abc123")
    assert "--num-nodes=1" in cmd and "--max-surge-upgrade=0" in cmd
    assert "--enable-dataplane-v2" in cmd and "--enable-network-policy" not in cmd
    assert "--no-enable-autoscaling" in cmd
    assert cmd[cmd.index("--node-locations")+1] == args.zone
    assert "--accelerator=type=nvidia-l4,count=1,gpu-driver-version=latest" in cmd
    assert args.node_service_account not in gke.public_command(args,"abc123")


def test_gke_cleanup_refuses_different_owner(monkeypatch):
    monkeypatch.setattr(gke,"cloud",lambda *args,**kwargs:{"resourceLabels":{"fund-demo-id":"different"}})
    with pytest.raises(gke.CloudError): gke.describe_owned(cloud_args(),{"demo_id":"mine"})


def test_budget_or_deadline_above_approval_is_rejected():
    args=cloud_args();args.budget_usd=21
    with pytest.raises(ValueError): gke.validate_inputs(args)
    args=cloud_args();args.lifetime_minutes=121
    with pytest.raises(ValueError): gke.validate_inputs(args)
