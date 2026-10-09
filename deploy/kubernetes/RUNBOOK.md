# Optional GPU model service

This is an isolated research service. It receives text and returns validated research output; it has no broker credentials, order endpoint, fund database, trading configuration, or host mount. Existing paper services keep their current configuration unless a separate deliberate integration is made.

**Deployment evidence is required before saying “deployed on GPU-enabled Kubernetes.”** YAML, offline tests, a Pending GPU pod, a simulated device plugin, and a local HTTP test double do not establish that claim. The verifier requires actual CUDA tensor execution, successful model generation, Ready pods, a Bound model-cache PVC, and pinned package/model evidence. Concurrent benchmark results must identify their backend and be retained separately.

## Stack and prerequisites

- Default model: `Qwen/Qwen2.5-1.5B-Instruct`, Apache-2.0, revision `989aa7980e4cf806f80c7fef2b1adb7bc71aa306`. This is a small serving demonstrator; no trading-quality superiority is assumed. The 3B variant has a different research-only license and is deliberately not the default.
- vLLM: `vllm/vllm-openai:v0.31.0@sha256:c1c9f6fd5c109ba7f0546a59f5b2f15fb87f64c77782e90a27b648b42a8e67c3`. The tag/index digest was confirmed against Docker Hub on 2026-10-09. The selected release image uses CUDA 13.0. The initially tested CUDA 12.9 variant failed to import TorchCodec because it required `libnvrtc.so.13`; that failed attempt is retained separately. A CUDA 13 compatible driver is required. It still needs a compatible installed NVIDIA driver and a supported physical GPU; image presence is not an inference test.
- One Linux/amd64 NVIDIA GPU node (L4/Ampere preferred), NVIDIA Container Toolkit and NVIDIA device plugin or GPU Operator. If the GPU container runtime is not the default, pass an existing `--runtime-class nvidia`. k3s is suitable when these prerequisites are configured.
- One GPU request and limit; one model pod, `Recreate` rollout, no autoscaler. Backend requests 4 CPU and 16 GiB memory; limits 8 CPU and 24 GiB. Gateway requests 0.5 CPU/512 MiB; limits 1 CPU/1 GiB. Reserve additional node memory for Kubernetes and driver services.
- A 20 GiB ReadWriteOnce PVC, using a default StorageClass or `--storage-class NAME`. Hugging Face weights, tokenizer and vLLM caches survive pod replacement. A disk-backed persistent StorageClass is required; emptyDir is only used for bounded `/dev/shm` and gateway temporary files.
- Private ClusterIP services, no Ingress/NodePort/LoadBalancer. CNI must enforce NetworkPolicy. Gateway can reach only the backend and DNS; backend can reach DNS and HTTPS for model downloads. API keys alone do not protect every vLLM endpoint.
- Separate random backend and gateway keys in `fund-llm-secrets`, keys `vllm-api-key` and `gateway-api-key`. The example Secret is excluded from kustomization and must not be applied with placeholders. Never put real keys in Git, command-line arguments, CV evidence, or chat.

## Build, configure and deploy

Build the gateway from `serving/Dockerfile` on Linux/amd64, push it to an authorized private registry, and obtain its immutable `image@sha256:digest`. Keep the image build record. The renderer refuses floating tags and the example gateway image cannot be deployed accidentally.

Before applying, establish the exact cluster context, approved cost ceiling and shutdown deadline. This project does not provision cloud GPUs. Deleting Kubernetes pods does **not** stop billing for a cloud GPU node.

```bash
# CONTEXT is an explicit approved kubeconfig context, never the implicit current context.
python scripts/k8s_model_serving.py preflight --context CONTEXT --storage-class local-path --runtime-class nvidia --output /tmp/fund-preflight.json
python scripts/k8s_model_serving.py render --gateway-image REGISTRY/IMAGE@sha256:DIGEST --storage-class local-path --runtime-class nvidia --output /tmp/fund-manifests.yaml
```

Create the dedicated `fund-llm` namespace and the two-key Secret through secure standard input or an external secret manager. `secret.example.yaml` documents the shape only. Generate at least 32 random bytes per key and use distinct keys. `deploy` requires the Secret to exist, checks it without printing values, runs server-side dry-run, applies, waits for both deployments, then verifies real hardware and inference.

```bash
python scripts/k8s_model_serving.py deploy --context CONTEXT --gateway-image REGISTRY/IMAGE@sha256:DIGEST --storage-class local-path --runtime-class nvidia --execute --output /tmp/fund-gpu-verification.json
```

The model startup probe allows 30 minutes for a first download/load. Liveness is held back until startup succeeds. Gateway readiness verifies that `fund-llm` is present at the real upstream `/v1/models`; merely starting its web process is insufficient. If startup fails, inspect Kubernetes events and local container logs, fix the cause, and rerun verification. Do not relabel failure as success.

## Verify and benchmark

```bash
python scripts/k8s_model_serving.py verify --context CONTEXT --output /tmp/fund-gpu-verification.json
kubectl --context CONTEXT -n fund-llm port-forward --address 127.0.0.1 service/model-gateway 8081:8081
```

Port forwarding stays on localhost. Normal in-cluster clients need the label `fund-llm-client=true` within this namespace to pass the gateway ingress policy, plus the gateway API key. Backend access is restricted to gateway pods.

`verify` uses `kubectl exec` in the pinned vLLM container to execute an actual CUDA matrix multiplication, inspect device name/capability, check the installed vLLM version and cached model revision, and call the running model. It records only sanitized pod identity/resources/image IDs, cache/health flags and token counts. It does not dump Secret objects or prompt/completion text. Exit code 2 means unverified, including scheduling, readiness or hardware failures. A successful verification covers the check time only, not continuous uptime or profitable trading.

Use the repository inference benchmark against this localhost gateway. Keep concurrent request counts, successful/failed request counts, wall time, requests/second, successful completion tokens/second, latency percentiles, failure categories, model/version/revision, hardware verification file and timestamps together. Mark test-double results as test-double results; do not use them to claim GPU model throughput.

## Status and cleanup

```bash
python scripts/k8s_model_serving.py status --context CONTEXT --output /tmp/fund-gpu-status.json
python scripts/k8s_model_serving.py cleanup --context CONTEXT --execute
```

Cleanup removes the two compute Deployments and two Services only. It preserves namespace, PVC and Secret. Remove the cloud GPU VM/node pool through the provider separately at the approved shutdown deadline, and check the provider confirms termination. Delete persistent disks only after preserving necessary evidence. Storage/registry/network charges may remain after compute stops.

## Primary references checked 2026-10-09

- [vLLM versioned Kubernetes deployment guidance](https://docs.vllm.ai/en/v0.31.0/deployment/k8s/) — persistent cache and GPU serving health probes.
- [vLLM v0.31.0 release](https://github.com/vllm-project/vllm/releases/tag/v0.31.0) and [versioned CLI](https://docs.vllm.ai/en/v0.31.0/cli/serve/) — CUDA image variant, revision arguments, request logging controls and authentication scope.
- [Qwen model card](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct) — model and Apache-2.0 license; [3B license](https://huggingface.co/Qwen/Qwen2.5-3B-Instruct/blob/main/LICENSE) differs.
- [Kubernetes GPU scheduling](https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/) and [NVIDIA device plugin](https://github.com/NVIDIA/k8s-device-plugin) — extended GPU resource and runtime prerequisites.
- [NVIDIA CUDA compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html) — installed driver/runtime compatibility must be checked on the actual node.

## Optional bounded GKE demo helper

`scripts/gke_gpu_demo.py` is an explicit opt-in cloud helper for a separately approved demo, not a change to the running fund server. `preflight` and `status` are read-only; cloud creation/deletion require `--execute`. It checks both global and regional L4 quotas, billing, enabled APIs, zone compatibility, a dedicated node account with `roles/container.defaultNodeServiceAccount`, and repository-scoped Artifact Registry reader. Google lists G2 CPU quota as N/A, so it does not invent a G2 CPU requirement.

The create plan contains exactly one zonal `g2-standard-8` node and one L4 with COS/latest GPU driver, Dataplane V2, disabled autoscaling/autoprovisioning, and zero upgrade surge nodes. Private state is mode 0600; public output omits service-account email, project ID and operator IP. Only the exact cluster bearing the saved demo ownership marker can be deleted. A root-orchestrated cluster created independently must be cleaned up by that orchestrator; this helper deliberately refuses to adopt it.

Creation requires `--watch`; the process remains available for the <=120-minute lifetime and requests provider cluster deletion at expiration or failure. **This is a local foreground watcher, not a provider-side hard TTL.** Killing its host/process can defeat cleanup, and managed deletion can take additional time. If the operator cannot keep it alive, independently configure provider cleanup before starting. The $20 validation is an approved budget and conservative cost envelope, not a hard Google billing cap or measured invoice. Remaining disks/images/network charges must be reviewed after cluster deletion.

```bash
python scripts/gke_gpu_demo.py preflight --project PROJECT --zone ZONE --node-service-account DEDICATED_NODE_SA --artifact-repository REPOSITORY --authorized-cidr OPERATOR_IPV4/32
# Requires the approved project and the dedicated service account/repository to already exist.
python scripts/gke_gpu_demo.py create --project PROJECT --zone ZONE --node-service-account DEDICATED_NODE_SA --artifact-repository REPOSITORY --authorized-cidr OPERATOR_IPV4/32 --lifetime-minutes 120 --budget-usd 20 --execute --watch
python scripts/gke_gpu_demo.py cleanup --project PROJECT --zone ZONE --execute
```

Official sources: [GKE create CLI](https://docs.cloud.google.com/sdk/gcloud/reference/container/clusters/create), [GPU nodes](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/gpus), [Dataplane V2 policy enforcement](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/dataplane-v2), [node least privilege](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/hardening-your-cluster), [G2/GPU quota rules](https://docs.cloud.google.com/compute/resource-usage).
