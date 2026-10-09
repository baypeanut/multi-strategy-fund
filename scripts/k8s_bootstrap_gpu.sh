#!/usr/bin/env bash
# Dedicated Ubuntu GPU VM only. Does not rent/provision cloud resources or touch the fund server.
# Reviewed primary references: docs.k3s.io/advanced NVIDIA runtime; NVIDIA toolkit install guide.
set -euo pipefail
if [[ ${EUID} -ne 0 || ${FUND_GPU_BOOTSTRAP:-} != 1 ]]; then
  echo 'Run as root on an approved dedicated GPU VM with FUND_GPU_BOOTSTRAP=1.' >&2
  exit 2
fi
if [[ -e /etc/rancher/k3s/k3s.yaml || -e /home/trader/trading/config/config.yaml ]]; then
  echo 'Refusing to bootstrap an existing Kubernetes/fund server.' >&2
  exit 2
fi
command -v nvidia-smi >/dev/null || { echo 'Install the approved NVIDIA driver and reboot first; nvidia-smi is required.' >&2; exit 2; }
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
# Installer uses SHA256 release artifact verification; explicit versions avoid latest/channel drift.
K3S_PIN='v1.37.1+k3s1'
TOOLKIT_PIN='1.20.1-1'
PLUGIN_PIN='v0.20.1'
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl gnupg python3 python3-yaml
curl --fail --silent --show-error --location https://nvidia.github.io/libnvidia-container/gpgkey -o /tmp/fund-nvidia-gpgkey
# This is a fresh dedicated machine; no existing repository key is overwritten silently.
gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg /tmp/fund-nvidia-gpgkey
curl --fail --silent --show-error --location https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list -o /tmp/fund-nvidia-repository.list
sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' /tmp/fund-nvidia-repository.list >/etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update -qq
apt-get install -y --no-install-recommends \
  "nvidia-container-toolkit=${TOOLKIT_PIN}" "nvidia-container-toolkit-base=${TOOLKIT_PIN}" \
  "libnvidia-container-tools=${TOOLKIT_PIN}" "libnvidia-container1=${TOOLKIT_PIN}"
command -v nvidia-container-runtime >/dev/null
curl --fail --silent --show-error --location "https://raw.githubusercontent.com/k3s-io/k3s/${K3S_PIN}/install.sh" -o /tmp/fund-install-k3s.sh
INSTALL_K3S_VERSION="${K3S_PIN}" sh /tmp/fund-install-k3s.sh server --disable traefik --disable servicelb --write-kubeconfig-mode 600
# K3s auto-detects the previously installed nvidia runtime and supplies RuntimeClass nvidia.
k3s kubectl wait --for=condition=Ready nodes --all --timeout=180s
k3s kubectl get runtimeclass nvidia
curl --fail --silent --show-error --location "https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/${PLUGIN_PIN}/deployments/static/nvidia-device-plugin.yml" -o /tmp/fund-device-plugin.yaml
python3 - <<'PY'
from pathlib import Path
import yaml
p=Path('/tmp/fund-device-plugin.yaml')
docs=list(yaml.safe_load_all(p.read_text()))
for d in docs:
    if d and d.get('kind')=='DaemonSet':
        d['spec']['template']['spec']['runtimeClassName']='nvidia'
p.write_text(yaml.safe_dump_all(docs,sort_keys=False))
PY
k3s kubectl apply -f /tmp/fund-device-plugin.yaml
k3s kubectl -n kube-system rollout status daemonset/nvidia-device-plugin-daemonset --timeout=180s
k3s kubectl get nodes -o custom-columns='NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu'
printf '%s\n' 'Bootstrap prepared. GPU resource advertisement is not actual vLLM/CUDA inference proof; deploy and run the verifier next.'
