# Geti v2 Local Deployment — Install & Post-Install Fixes

Tested on: Ubuntu 24.04, k3s v1.31.0+k3s1, VERSION=`2.13.1-de29b7ba`

---

## 1. Pre-Install: Docker Insecure Registry & Proxy

### 1a. Insecure registry (required for local push/pull)

> If you also need subnet isolation (§1e), skip this block and apply §1e instead — it includes the insecure-registry setting.

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  "insecure-registries": ["localhost:5000"]
}
EOF
```

### 1b. Docker proxy (if behind corporate proxy)

```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/proxy.conf > /dev/null <<'EOF'
[Service]
Environment="HTTP_PROXY=http://proxy-dmz.intel.com:911"
Environment="HTTPS_PROXY=http://proxy-dmz.intel.com:912"
Environment="NO_PROXY=localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12"
EOF

sudo systemctl daemon-reload
sudo systemctl restart docker
```

### 1c. Set proxy env (including `no_proxy`) **before** the installer

The installer reads these env vars and bakes them into `impt-configuration` ConfigMap
automatically — no manual `kubectl patch` needed later.

```bash
export http_proxy=http://proxy-dmz.intel.com:911
export https_proxy=http://proxy-dmz.intel.com:912
# Internal IPs/names that must bypass the proxy.
# The installer also auto-appends: 127.0.0.1,localhost,.impt,.svc,.cluster.local
export no_proxy="10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,172.25.80.195,impt-seaweed-fs,impt-seaweed-fs.impt,impt-mongodb,impt-kafka"
```

### 1e. Docker default address pools (avoid 172.x collision)

Intel internal networks use 172.16.0.0/12. Docker allocates bridge networks from
172.17.0.0/16 upward by default, which collides. Override before installing:

```bash
sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  "insecure-registries": ["localhost:5000"],
  "bip": "192.168.200.1/24",
  "default-address-pools": [
    {"base": "192.168.0.0/16", "size": 24}
  ]
}
EOF

sudo systemctl daemon-reload
sudo systemctl restart docker
```

> **Note:** This replaces the §1a `daemon.json` — no need to apply §1a separately.

#### If using Kind instead of k3s

Save as `kind-config.yaml` and pass to `kind create cluster`:

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking:
  podSubnet: "10.244.0.0/16"
  serviceSubnet: "10.96.0.0/12"
```

```bash
kind create cluster --config kind-config.yaml
```

k3s already defaults to `10.42.0.0/16` (pods) and `10.43.0.0/16` (services),
so no extra k3s changes are needed.

---

### 1d. Run the installer

```bash
cd /home/sysc/geti_v2/platform/services/installer
sudo -E PLATFORM_REGISTRY_ADDRESS=localhost:5000 PYTHONPATH=app \
  .venv/bin/python app/platform_installer.py install
```

> `sudo -E` preserves the exported proxy env vars so the installer sees them.

### 1d. Fix pigz downgrade (installer overwrites system pigz)

The installer pulls `pigz 2.4-1` which breaks k3s. Fix immediately after install:

```bash
sudo apt-get install -y pigz          # reinstall 2.8
sudo dpkg --set-selections <<< 'pigz hold'   # pin it
```

---

## 2. Fix: Trainer Image — `COPY --link` Permission Bug

### Problem

`COPY --link` without `--chown` in the final Dockerfile stage recreates all
parent directories as root-owned. The container runs as uid 10001 (`non-root`)
and cannot write `otx-full.log` to the WORKDIR → immediate `PermissionError`
and silent crash (no `error.json` uploaded to S3).

### Fix A — `scripts/run.py`: write log/pid to `/tmp/`

File: `interactive_ai/workflows/train/trainer/scripts/run.py`

```python
# Before (line ~54)
log_file = "otx-full.log"
...
with open("primary.pid", "w") as fp:

# After
log_file = "/tmp/otx-full.log"
...
with open("/tmp/primary.pid", "w") as fp:
```

### Fix B — `gpu/Dockerfile`: add `--chown=10001` to final COPY instructions

File: `interactive_ai/workflows/train/trainer/gpu/Dockerfile`

```dockerfile
# Before (last 3 COPY lines in the runtime stage)
COPY --link scripts/ scripts
COPY --link run run
COPY --link download_pretrained_weights.py download_pretrained_weights.py

# After
COPY --link --chown=10001 scripts/ scripts
COPY --link --chown=10001 run run
COPY --link --chown=10001 download_pretrained_weights.py download_pretrained_weights.py
```

### Rebuild and reload

```bash
cd /home/sysc/geti_v2/interactive_ai/workflows/train/trainer

sudo docker build \
  --build-arg http_proxy=http://proxy-dmz.intel.com:911 \
  --build-arg https_proxy=http://proxy-dmz.intel.com:912 \
  --build-context libs=../../../../libs \
  -t localhost:5000/open-edge-platform/geti/otx2-training:2.13.1-de29b7ba \
  -f gpu/Dockerfile .

sudo docker push localhost:5000/open-edge-platform/geti/otx2-training:2.13.1-de29b7ba

# Load into k3s containerd so existing nodes pick it up
sudo docker save localhost:5000/open-edge-platform/geti/otx2-training:2.13.1-de29b7ba | \
  sudo k3s ctr images import -
```

---

## 3. Fix: `/data` Directory Permissions

### Problem

After a fresh k3s install the `/data` path is created with `750` permissions.
Training pods run as a non-root uid and cannot read dataset shards mounted
under `/data/binary_data`.

### Fix

```bash
sudo chmod 755 /data
sudo chmod -R 755 /data/binary_data /data/audit_logs /data/logs
```

Run this once after every fresh install before starting training.

---

## 4. IIS Reverse Proxy (Windows → Geti)

The Istio gateway exposes Geti on **port 80** (HTTP) at the server's IP.
Check the current LoadBalancer IP:

```powershell
# On the Linux host
sudo kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml get svc -n istio-system istio-gateway
# External-IP column → e.g. 172.25.80.195
```

### IIS `web.config` URL rewrite rule

In IIS Application Request Routing, rewrite to port 80 of the Istio gateway IP:

```xml
<rewrite>
  <rules>
    <rule name="Geti" stopProcessing="true">
      <match url="(.*)" />
      <action type="Rewrite" url="http://172.25.80.195/{R:1}" />
    </rule>
  </rules>
</rewrite>
```

Replace `172.25.80.195` with the actual external IP shown above.

### Bypass proxy for the Geti IP in IIS

IIS Application Request Routing respects the Windows WinHTTP proxy.
If IIS routes Geti traffic through the corporate proxy, the request fails.
Bypas it by adding the Geti server IP to the WinHTTP bypass list
(run in an elevated PowerShell on Windows):

```powershell
netsh winhttp set proxy proxy-server="proxy-dmz.intel.com:911" bypass-list="172.25.80.195;<local>"
```

Or add the IP to the machine's `NO_PROXY` system environment variable:

```powershell
[System.Environment]::SetEnvironmentVariable(
    'NO_PROXY',
    '172.25.80.195,localhost,127.0.0.1',
    [System.EnvironmentVariableTarget]::Machine
)
```

Restart the IIS service after the change.

---

## 5. Fix: Proxy for Pretrained Weight Downloads

Training pods download weights from external URLs when the file is not already
cached in the `pretrainedweights` S3 bucket. Pods have no proxy by default
because `impt-configuration` ConfigMap has no proxy keys.

### 4a. Patch the ConfigMap (survives pod restarts, lost on Helm upgrade)

```bash
NO_PROXY="localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.svc,.cluster.local,impt-seaweed-fs.impt.svc.cluster.local"

sudo kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml \
  patch configmap impt-configuration -n impt \
  --type merge \
  -p "{\"data\":{
    \"http_proxy\":\"http://proxy-dmz.intel.com:911\",
    \"https_proxy\":\"http://proxy-dmz.intel.com:912\",
    \"no_proxy\":\"${NO_PROXY}\"
  }}"
```

### 5b. Pre-seed weights into SeaweedFS (avoids download entirely)

Get the SeaweedFS S3 ClusterIP:

```bash
sudo kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml get svc -n impt impt-seaweed-fs
# e.g. ClusterIP 10.43.190.83
```

Download weights on the host (where proxy works) and upload:

```bash
SEAWEED="http://10.43.190.83:8333"
ACCESS_KEY="iKcWvb8xpAT4ptZ8qAoL"
SECRET_KEY="1zyiGoOgYivwAw3nmlo81fQYDbxgUzZYfmCMs5S6"

# Upload pretrained_models_v2.json
curl -s https://raw.githubusercontent.com/.../pretrained_models_v2.json \
  -o /tmp/pretrained_models_v2.json

uv run --no-project --with minio python3 - <<'EOF'
from minio import Minio
client = Minio('10.43.190.83:8333',
               access_key='iKcWvb8xpAT4ptZ8qAoL',
               secret_key='1zyiGoOgYivwAw3nmlo81fQYDbxgUzZYfmCMs5S6',
               secure=False)
client.fput_object('pretrainedweights', 'pretrained_models_v2.json',
                   '/tmp/pretrained_models_v2.json')
print("done")
EOF
```

For each weight file listed in the manifest, download via proxy on host and
upload to the `pretrainedweights` bucket with the filename as object name.

### 5c. `pretrained_weights.py` fallback fix

File: `interactive_ai/workflows/train/trainer/scripts/pretrained_weights.py`

The script is patched to try the manifest's public URL **before** the internal
`WEIGHTS_URL` (`storage.geti.intel.com`), so weights are fetched from
`openvinotoolkit.org` or `github.com` automatically when not in S3.

---

## 6. Fix: ModelMesh — etcd Authentication

### Problem

After a fresh k3s install etcd starts **without** authentication enabled.
ModelMesh requires auth and enters `CrashLoopBackOff` with:

```
authentication is not enabled
```

### Fix

```bash
# Get etcd pod name
ETCD_POD=$(sudo kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml \
  get pods -n kube-system -l component=etcd -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

# If running embedded k3s etcd, use etcdctl directly
ETCDCTL="sudo etcdctl --endpoints=https://127.0.0.1:2379 \
  --cacert=/var/lib/rancher/k3s/server/tls/etcd/server-ca.crt \
  --cert=/var/lib/rancher/k3s/server/tls/etcd/server-client.crt \
  --key=/var/lib/rancher/k3s/server/tls/etcd/server-client.key"

# Add root user and enable auth
$ETCDCTL user add root --new-user-password=""
$ETCDCTL role add root
$ETCDCTL user grant-role root root
$ETCDCTL auth enable

# Verify modelmesh recovers
sudo kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml get pods -n impt | grep modelmesh
```

Must be repeated after every fresh k3s install.

---

## Post-Install Checklist

> If §1c proxy env vars were set before install, skip step 4 below.

Run in order after every fresh install:

| Step | Command                                                                                                                                                            |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1    | Fix pigz: `sudo apt-get install -y pigz`                                                                                                                           |
| 2    | Fix `/data` perms: `sudo chmod 755 /data && sudo chmod -R 755 /data/binary_data /data/audit_logs /data/logs`                                                       |
| 3    | Enable etcd auth (modelmesh fix) — see §6                                                                                                                          |
| 4    | Patch proxy into ConfigMap (only if §1c was skipped) — see §5a-alt                                                                                                 |
| 5    | Verify all pods Running: `sudo kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml get pods -n impt`                                                                    |
| 6    | Scale down visual-prompt (SAM weights unavailable): `sudo kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml scale deployment -n impt impt-visual-prompt --replicas=0` |
