#!/usr/bin/env python3
"""Explicit-context operations and evidence for the optional GPU research service.

Never provisions cloud infrastructure. GPU verification requires a real CUDA tensor
operation inside the vLLM pod, not only a Kubernetes GPU resource or declared label.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "deploy" / "kubernetes"
NAMESPACE = "fund-llm"
VLLM_IMAGE = "vllm/vllm-openai:v0.31.0@sha256:c1c9f6fd5c109ba7f0546a59f5b2f15fb87f64c77782e90a27b648b42a8e67c3"
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
REVISION = "989aa7980e4cf806f80c7fef2b1adb7bc71aa306"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def kubectl(context, *args, stdin=None, timeout=30):
    if not context:
        raise ValueError("An explicit --context is required; current-context is never inferred")
    proc = subprocess.run(
        ["kubectl", "--context", context, *args], input=stdin,
        text=True, capture_output=True, timeout=timeout,
    )
    if proc.returncode:
        # Never echo input (Secret data) or arbitrary stderr containing auth URLs.
        raise RuntimeError(f"kubectl {args[0]} failed (exit {proc.returncode}); inspect locally")
    return proc.stdout


def kjson(context, *args):
    return json.loads(kubectl(context, *args, "-o", "json"))


def manifests(gateway_image, namespace=NAMESPACE, storage_class=None, runtime_class=None):
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", gateway_image or ""):
        raise ValueError("--gateway-image must be a built/pushed immutable image@sha256:digest")
    listing = yaml.safe_load((BASE / "kustomization.yaml").read_text())["resources"]
    docs = [yaml.safe_load((BASE / name).read_text()) for name in listing]
    for doc in docs:
        meta = doc["metadata"]
        if doc["kind"] == "Namespace":
            meta["name"] = namespace
        else:
            meta["namespace"] = namespace
        if doc["kind"] == "PersistentVolumeClaim" and storage_class:
            doc["spec"]["storageClassName"] = storage_class
        if doc["kind"] == "Deployment" and meta["name"] == "vllm" and runtime_class:
            doc["spec"]["template"]["spec"]["runtimeClassName"] = runtime_class
        if doc["kind"] == "Deployment" and meta["name"] == "model-gateway":
            doc["spec"]["template"]["spec"]["containers"][0]["image"] = gateway_image
    return docs


def effective_gpu_request(pod):
    spec = pod.get("spec", {})
    def count(container):
        r = container.get("resources", {})
        return int(r.get("requests", {}).get("nvidia.com/gpu", r.get("limits", {}).get("nvidia.com/gpu", 0)))
    regular = sum(count(c) for c in spec.get("containers", []))
    init = max([count(c) for c in spec.get("initContainers", [])] or [0])
    return max(regular, init) + int(spec.get("overhead", {}).get("nvidia.com/gpu", 0))


def preflight(context, storage_class=None, runtime_class=None):
    nodes = kjson(context, "get", "nodes")["items"]
    pods = kjson(context, "get", "pods", "--all-namespaces")["items"]
    usage = {}
    for pod in pods:
        if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        name = pod.get("spec", {}).get("nodeName")
        usage[name] = usage.get(name, 0) + effective_gpu_request(pod)
    records = []
    for node in nodes:
        meta, spec, status = node["metadata"], node.get("spec", {}), node.get("status", {})
        labels = meta.get("labels", {})
        capacity = int(status.get("allocatable", {}).get("nvidia.com/gpu", 0))
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
        records.append({"name": meta["name"], "ready": ready, "unschedulable": spec.get("unschedulable", False),
                        "architecture": labels.get("kubernetes.io/arch"), "os": labels.get("kubernetes.io/os"),
                        "allocatable_gpu": capacity, "requested_gpu": usage.get(meta["name"], 0),
                        "gpu_product_label": labels.get("nvidia.com/gpu.product"),
                        "time_slicing_replicas_label": labels.get("nvidia.com/gpu.replicas"),
                        "taints": [{k:t[k] for k in ("key", "effect") if k in t} for t in spec.get("taints", [])]})
    classes = kjson(context, "get", "storageclasses")["items"]
    eligible_storage = any(c["metadata"]["name"] == storage_class for c in classes) if storage_class else any(
        c["metadata"].get("annotations", {}).get("storageclass.kubernetes.io/is-default-class") == "true" for c in classes)
    eligible_gpu = any(n["ready"] and not n["unschedulable"] and n["os"] == "linux" and n["architecture"] == "amd64"
                       and n["allocatable_gpu"] - n["requested_gpu"] >= 1 for n in records)
    runtime_ok = True
    if runtime_class:
        runtime_ok = bool(kjson(context, "get", "runtimeclass", runtime_class))
    problems = []
    if not eligible_gpu: problems.append("No ready schedulable Linux/amd64 node advertises an unused nvidia.com/gpu")
    if not eligible_storage: problems.append("Choose an existing --storage-class or configure a default StorageClass")
    if not runtime_ok: problems.append("Requested GPU RuntimeClass does not exist")
    return {"checked_at": utc_now(), "context": context, "preflight_passed": not problems,
            "gpu_deployment_verified": False, "nodes": records,
            "storage_classes": [c["metadata"]["name"] for c in classes], "problems": problems,
            "limitations": ["Scheduler acceptance, driver compatibility, CUDA hardware, PVC binding and NetworkPolicy enforcement require live verification",
                            "Advertised GPU resource is not evidence of physical allocation; time-slicing labels may indicate shared compute"]}


# Runs only in the actual vLLM container. No API keys, prompts or full environment are printed.
GPU_PROBE = r'''
import importlib.metadata, json, os, pathlib, urllib.request
import torch
r={"cuda_available":torch.cuda.is_available(),"vllm_version":importlib.metadata.version("vllm"),"torch_version":torch.__version__,"torch_cuda_runtime":torch.version.cuda}
if not r["cuda_available"]: raise SystemExit("CUDA unavailable")
r["device_count"]=torch.cuda.device_count()
r["device_name"]=torch.cuda.get_device_name(0)
r["compute_capability"]=list(torch.cuda.get_device_capability(0))
a=torch.ones((32,32),device="cuda")
b=a@a
torch.cuda.synchronize()
r["cuda_tensor_operation_passed"]=bool(b.device.type=="cuda" and b[0,0].item()==32)
revision=os.environ["FUND_MODEL_REVISION"]
model=os.environ["FUND_MODEL_ID"]
cache=pathlib.Path(os.environ["HF_HOME"])/"hub"/("models--"+model.replace("/","--"))/"snapshots"/revision
r["model_cache_revision_exists"]=cache.is_dir()
r["expected_revision"]=revision
# Confirm the actual running server invocation, not only declared environment.
argv=[x.decode("utf-8",errors="replace") for x in pathlib.Path("/proc/1/cmdline").read_bytes().split(b"\0") if x]
def option(name):
    try: return argv[argv.index(name)+1]
    except (ValueError,IndexError): return None
try: actual_model=argv[argv.index("serve")+1]
except (ValueError,IndexError): actual_model=option("--model")
r["actual_model"]=actual_model
r["actual_model_revision"]=option("--revision")
r["actual_tokenizer_revision"]=option("--tokenizer-revision")
r["actual_served_model_name"]=option("--served-model-name")
key=os.environ["VLLM_API_KEY"]
headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"}
def request(path,payload=None):
    req=urllib.request.Request("http://127.0.0.1:8000"+path,headers=headers,data=json.dumps(payload).encode() if payload else None)
    return json.load(urllib.request.urlopen(req,timeout=45))
r["served_models"]=[m["id"] for m in request("/v1/models")["data"]]
out=request("/v1/chat/completions",{"model":os.environ["MODEL_NAME"],"messages":[{"role":"user","content":"Reply with the single word ready."}],"max_tokens":16,"temperature":0})
r["generation_passed"]=bool(out.get("choices") and out["choices"][0].get("message",{}).get("content"))
r["completion_tokens"]=out.get("usage",{}).get("completion_tokens")
print(json.dumps(r))
'''
GATEWAY_PROBE = r'''
import json,urllib.request
with urllib.request.urlopen("http://127.0.0.1:8081/health/ready",timeout=10) as r:
    print(json.dumps({"gateway_ready":r.status==200}))
'''


def pod_summary(pod):
    spec, status = pod.get("spec", {}), pod.get("status", {})
    return {"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
            "node": spec.get("nodeName"), "phase": status.get("phase"),
            "ready": any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])),
            "volumes": [{"name":v["name"], "persistentVolumeClaim":v.get("persistentVolumeClaim")} for v in spec.get("volumes", [])],
            "containers": [{"name": c["name"], "image": c["image"], "resources": c.get("resources", {}),
                            "volumeMounts":c.get("volumeMounts", []),
                            "command": c.get("command", []), "args": c.get("args", [])} for c in spec.get("containers", [])],
            "statuses": [{k:c.get(k) for k in ("name", "ready", "restartCount", "imageID")} for c in status.get("containerStatuses", [])]}


def evaluate_verification(backend, gateway, pvc, runtime, gateway_runtime):
    problems = []
    if not backend.get("ready") or backend.get("phase") != "Running": problems.append("vLLM pod is not Ready/Running")
    if not gateway.get("ready") or gateway.get("phase") != "Running": problems.append("Gateway pod is not Ready/Running")
    containers = backend.get("containers", [])
    vllm = next((c for c in containers if c.get("name") == "vllm"), {})
    resources = vllm.get("resources", {})
    def one_gpu(value):
        # Kubernetes Quantity JSON serializes extended resource values as strings.
        return (type(value) is int and value == 1) or (type(value) is str and bool(re.fullmatch(r"[0-9]+", value)) and int(value) == 1)
    if not one_gpu(resources.get("requests", {}).get("nvidia.com/gpu")) or not one_gpu(resources.get("limits", {}).get("nvidia.com/gpu")):
        problems.append("vLLM pod does not explicitly request and limit one NVIDIA GPU")
    if vllm.get("image") != VLLM_IMAGE: problems.append("vLLM pod image differs from pinned reviewed CUDA image")
    if not any(s.get("imageID") and "sha256:" in s["imageID"] for s in backend.get("statuses", [])):
        problems.append("Runtime image digest is absent")
    if pvc.get("phase") != "Bound": problems.append("Persistent model cache PVC is not Bound")
    cache_volumes = {v["name"] for v in backend.get("volumes", []) if v.get("persistentVolumeClaim") and v["persistentVolumeClaim"].get("claimName") == pvc.get("name")}
    if not any(m.get("name") in cache_volumes and m.get("mountPath") == "/model-cache" for m in vllm.get("volumeMounts", [])):
        problems.append("Bound PVC is not mounted as the actual backend model cache")
    if not runtime.get("cuda_available") or not runtime.get("cuda_tensor_operation_passed") or runtime.get("device_count", 0) < 1:
        problems.append("Actual CUDA hardware/tensor operation proof failed; advertised or simulated GPU is insufficient")
    if str(runtime.get("vllm_version", "")).split("+", 1)[0] != "0.31.0":
        problems.append("Running vLLM package version differs from pin")
    if runtime.get("expected_revision") != REVISION or not runtime.get("model_cache_revision_exists"):
        problems.append("Pinned model snapshot is absent from persistent cache")
    if (runtime.get("actual_model") != MODEL or runtime.get("actual_model_revision") != REVISION
            or runtime.get("actual_tokenizer_revision") != REVISION or runtime.get("actual_served_model_name") != "fund-llm"):
        problems.append("Actual server process does not use the pinned model/tokenizer revision and alias")
    if "fund-llm" not in runtime.get("served_models", []) or not runtime.get("generation_passed"):
        problems.append("Expected model did not complete actual inference")
    if not gateway_runtime.get("gateway_ready"): problems.append("Gateway did not confirm its upstream model readiness")
    return {"gpu_deployment_verified": not problems, "problems": problems}


def verify(context, namespace=NAMESPACE):
    pods = kjson(context, "get", "pods", "-n", namespace, "-l", "app.kubernetes.io/part-of=fund-llm")["items"]
    by_name = {p.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name"): p for p in pods}
    backend, gateway = by_name.get("vllm"), by_name.get("model-gateway")
    report = {"checked_at": utc_now(), "context": context, "namespace": namespace, "gpu_deployment_verified": False,
              "pods": [pod_summary(p) for p in pods]}
    if not backend or not gateway:
        report["problems"] = ["Both deployed pods are required; manifests alone are not deployment evidence"]
        return report
    pvc_doc = kjson(context, "get", "pvc", "vllm-model-cache", "-n", namespace)
    pvc = {"name": "vllm-model-cache", "phase": pvc_doc.get("status", {}).get("phase"),
           "volume_name": pvc_doc.get("spec", {}).get("volumeName"), "storage_class": pvc_doc.get("spec", {}).get("storageClassName")}
    runtime, gateway_runtime = {}, {}
    probe_failures = []
    for pod, script, target in ((backend, GPU_PROBE, runtime), (gateway, GATEWAY_PROBE, gateway_runtime)):
        try:
            target.update(json.loads(kubectl(context, "exec", "-n", namespace, pod["metadata"]["name"], "--", "python3", "-c", script, timeout=60)))
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError):
            probe_failures.append(f"Live probe failed for {pod['metadata']['name']}; no hardware/inference success claimed")
    report.update(evaluate_verification(pod_summary(backend), pod_summary(gateway), pvc, runtime, gateway_runtime))
    report["problems"].extend(probe_failures)
    if probe_failures: report["gpu_deployment_verified"] = False
    report.update({"pvc": pvc, "runtime": runtime, "gateway_runtime": gateway_runtime,
                   "evidence_scope": "GPU allocation and generation at check time; not uptime, throughput, quality or profitability"})
    return report


def emit(report, output):
    text = json.dumps(report, indent=2)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n")
    print(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["render", "preflight", "deploy", "verify", "status", "cleanup"])
    parser.add_argument("--context")
    parser.add_argument("--namespace", default=NAMESPACE)
    parser.add_argument("--gateway-image")
    parser.add_argument("--storage-class")
    parser.add_argument("--runtime-class")
    parser.add_argument("--output")
    parser.add_argument("--execute", action="store_true", help="Required to mutate the named cluster; never creates cloud GPU resources")
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", args.namespace) or len(args.namespace) > 63:
            raise ValueError("Invalid DNS namespace")
        if args.action == "render":
            docs = manifests(args.gateway_image, args.namespace, args.storage_class, args.runtime_class)
            text = yaml.safe_dump_all(docs, sort_keys=False)
            if args.output: Path(args.output).write_text(text)
            else: print(text)
            return 0
        if args.namespace in ("default", "kube-system", "kube-public", "kube-node-lease"):
            raise ValueError("Use a dedicated fund-llm namespace, never a system/default namespace")
        if not args.context: raise ValueError("--context must explicitly identify the target cluster")
        if args.action == "preflight":
            report = preflight(args.context, args.storage_class, args.runtime_class)
            emit(report, args.output)
            return 0 if report["preflight_passed"] else 2
        if args.action in ("verify", "status"):
            report = verify(args.context, args.namespace)
            emit(report, args.output)
            return 0 if report["gpu_deployment_verified"] else 2
        if not args.execute: raise ValueError("Mutation requires --execute and an explicit --context")
        if args.action == "cleanup":
            # Preserve PVC, Secret and Namespace: model storage or unrelated data is never deleted here.
            kubectl(args.context, "delete", "deployment", "vllm", "model-gateway", "-n", args.namespace, "--ignore-not-found=true")
            kubectl(args.context, "delete", "service", "vllm", "model-gateway", "-n", args.namespace, "--ignore-not-found=true")
            emit({"compute_workloads_removed": True, "persistent_cache_preserved": True,
                  "cloud_gpu_billing_stopped": False, "note": "Scale down or delete provider GPU nodes separately; workload deletion alone does not stop node billing"}, args.output)
            return 0
        docs = manifests(args.gateway_image, args.namespace, args.storage_class, args.runtime_class)
        report = preflight(args.context, args.storage_class, args.runtime_class)
        if not report["preflight_passed"]:
            emit(report, args.output); return 2
        # A preexisting Secret is required; placeholder example is intentionally excluded.
        secret = kjson(args.context, "get", "secret", "fund-llm-secrets", "-n", args.namespace)
        key_values = [base64.b64decode(secret.get("data", {}).get(key, "")).decode() for key in ("vllm-api-key", "gateway-api-key")]
        if any(len(v) < 16 or "REPLACE_WITH" in v for v in key_values) or key_values[0] == key_values[1]:
            raise ValueError("Two distinct non-placeholder API keys of at least 16 characters are required")
        del key_values, secret
        kubectl(args.context, "apply", "--dry-run=server", "-f", "-", stdin=yaml.safe_dump_all(docs, sort_keys=False))
        kubectl(args.context, "apply", "-f", "-", stdin=yaml.safe_dump_all(docs, sort_keys=False))
        kubectl(args.context, "rollout", "status", "deployment/vllm", "-n", args.namespace, "--timeout=30m", timeout=1830)
        kubectl(args.context, "rollout", "status", "deployment/model-gateway", "-n", args.namespace, "--timeout=2m", timeout=150)
        report = verify(args.context, args.namespace)
        emit(report, args.output)
        return 0 if report["gpu_deployment_verified"] else 2
    except (ValueError, RuntimeError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        emit({"checked_at": utc_now(), "gpu_deployment_verified": False, "error": str(exc)}, args.output)
        return 2


if __name__ == "__main__":
    sys.exit(main())
