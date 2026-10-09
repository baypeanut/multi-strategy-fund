#!/usr/bin/env python3
"""Bounded GKE GPU demo operations; explicit project/zone, no provisioning by import.

Requires a pre-existing least-privilege node service account and private image
repository. The foreground watcher initiates cluster deletion at the configured
<=2h deadline and on creation failure/signals. It is NOT a provider-side TTL:
closing/killing its host can defeat cleanup. A provider independently scheduled
cleanup is required if that host cannot stay available.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

MAX_BUDGET_USD = 20.0
MAX_LIFETIME_MINUTES = 120
# Deliberately conservative allowance, not an invoice or Google billing limit.
ESTIMATED_HOURLY_USD = 1.10  # one L4 G2 node + zonal cluster + storage allowance
FIXED_ALLOWANCE_USD = 5.0   # image build/storage/network allowance; measured billing remains separate


class CloudError(RuntimeError): pass
class NotFound(CloudError): pass


def utc_now(): return datetime.now(timezone.utc)

def cloud(project, *args, timeout=45):
    proc = subprocess.run(["gcloud", *args, "--project", project, "--quiet", "--format=json"],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode:
        if "not found" in proc.stderr.lower() or "not_found" in proc.stderr.lower(): raise NotFound("Named cloud resource not found")
        # stderr can include identities/endpoints; never print it in evidence.
        raise CloudError(f"Cloud operation {args[0]} {args[1] if len(args)>1 else ''} failed (exit {proc.returncode}); inspect gcloud locally")
    return json.loads(proc.stdout or "{}")


def save_private(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out: json.dump(data, out, indent=2); out.write("\n")
    os.chmod(path, 0o600)


def project_hash(project): return hashlib.sha256(project.encode()).hexdigest()[:12]

def quota_value(quotas, metric):
    aliases = {metric}
    if metric == "NVIDIA_L4_GPUS":
        aliases.add("GPU_FAMILY:NVIDIA_L4")
    item = next((q for q in quotas if q.get("metric") in aliases), None)
    if item is None: return None
    return {"metric":item["metric"],"limit":float(item.get("limit",0)),"usage":float(item.get("usage",0)),
            "remaining":float(item.get("limit",0))-float(item.get("usage",0))}


def validate_inputs(args):
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", args.project): raise ValueError("Use an explicit valid Google project ID")
    if not re.fullmatch(r"[a-z]+-[a-z]+[0-9]-[a-z]", args.zone): raise ValueError("Use an explicit single zone")
    if not re.fullmatch(r"fund-llm-demo-[a-z0-9-]{1,25}", args.name): raise ValueError("Demo cluster name must start fund-llm-demo- and be <=39 characters")
    if not 1 <= args.lifetime_minutes <= MAX_LIFETIME_MINUTES: raise ValueError("Demo lifetime must be between 1 and 120 minutes")
    if not 0 < args.budget_usd <= MAX_BUDGET_USD: raise ValueError("Approved total demo budget must be <=$20")
    estimate = FIXED_ALLOWANCE_USD + ESTIMATED_HOURLY_USD * args.lifetime_minutes/60
    if estimate > args.budget_usd: raise ValueError("Conservative demo estimate exceeds the approved budget")
    return estimate


def preflight(args):
    estimate = validate_inputs(args)
    region = args.zone.rsplit("-",1)[0]
    project = cloud(args.project, "compute", "project-info", "describe")
    regional = cloud(args.project, "compute", "regions", "describe", region)
    # G2 uses L4 GPU quota; Google explicitly lists its CPU quota as N/A.
    # Inventing a required G2_CPUS metric would reject legitimate projects.
    quotas = [quota_value(project.get("quotas",[]),"GPUS_ALL_REGIONS"),
              quota_value(regional.get("quotas",[]),"NVIDIA_L4_GPUS")]
    problems = []
    for quota,need in zip(quotas,[1,1]):
        if quota is None: problems.append("A required quota metric is unavailable; fail closed")
        elif quota["remaining"] < need: problems.append(f"Insufficient quota: {quota['metric']}")
    billing = cloud(args.project,"billing","projects","describe",args.project)
    if not billing.get("billingEnabled"): problems.append("Billing is not enabled for this project")
    enabled = cloud(args.project,"services","list","--enabled")
    names = {s.get("config",{}).get("name") for s in enabled}
    if not {"container.googleapis.com","compute.googleapis.com"} <= names: problems.append("Required pre-enabled GKE/Compute APIs are missing")
    try:
        cloud(args.project,"compute","accelerator-types","describe","nvidia-l4","--zone",args.zone)
        cloud(args.project,"compute","machine-types","describe","g2-standard-8","--zone",args.zone)
    except NotFound: problems.append("L4/G2 machine type is unavailable in the named zone")
    try:
        cloud(args.project,"container","clusters","describe",args.name,"--zone",args.zone)
        problems.append("A cluster already exists at this name; never adopt/overwrite existing resources")
    except NotFound: pass
    if not args.node_service_account:
        problems.append("Provide a dedicated least-privilege --node-service-account")
    else:
        if re.fullmatch(r"[0-9]+-compute@developer\.gserviceaccount\.com",args.node_service_account):
            problems.append("Default Compute service account refused; use a dedicated account")
        policy = cloud(args.project,"projects","get-iam-policy",args.project)
        member = "serviceAccount:"+args.node_service_account
        roles = {b.get("role") for b in policy.get("bindings",[]) if member in b.get("members",[])}
        if "roles/container.defaultNodeServiceAccount" not in roles: problems.append("Dedicated node account lacks its required direct project node role")
        if roles & {"roles/editor","roles/owner"}: problems.append("Node account has overly broad Editor/Owner role")
        if not args.artifact_repository: problems.append("Provide the private image --artifact-repository for repository-scoped reader validation")
        else:
            registry_location = args.registry_location or region
            policy = cloud(args.project,"artifacts","repositories","get-iam-policy",args.artifact_repository,"--location",registry_location)
            reader = any(b.get("role")=="roles/artifactregistry.reader" and member in b.get("members",[]) for b in policy.get("bindings",[]))
            if not reader: problems.append("Dedicated node account lacks repository-scoped Artifact Registry reader")
    if not args.authorized_cidr: problems.append("Provide --authorized-cidr for the operator control-plane access")
    else:
        cidr = ipaddress.ip_network(args.authorized_cidr,strict=True)
        if cidr.version != 4 or cidr.prefixlen != 32: problems.append("Operator authorized CIDR must be one IPv4 /32")
    return {"checked_at":utc_now().isoformat(),"project_fingerprint":project_hash(args.project),"zone":args.zone,
            "preflight_passed":not problems,"quotas":quotas,"problems":problems,
            "budget_usd":args.budget_usd,"conservative_demo_estimate_usd":estimate,"max_lifetime_minutes":args.lifetime_minutes,
            "gpu_deployment_verified":False,"cloud_created":False,
            "limitations":["Quota is not zone inventory; creation can still fail","Dollar estimate is not a hard Google billing limit",
                           "Foreground cleanup timer requires this host/process to remain alive; no provider-side TTL is claimed"]}


def create_command(args, demo_id):
    return ["container","clusters","create",args.name,"--zone",args.zone,"--node-locations",args.zone,
            "--num-nodes=1","--machine-type=g2-standard-8","--accelerator=type=nvidia-l4,count=1,gpu-driver-version=latest",
            "--image-type=COS_CONTAINERD","--disk-size=100","--disk-type=pd-balanced",
            "--enable-ip-alias","--enable-dataplane-v2","--no-enable-autoscaling","--no-enable-autoprovisioning",
            "--max-surge-upgrade=0","--max-unavailable-upgrade=1","--no-enable-autoupgrade",
            "--service-account",args.node_service_account,"--scopes=https://www.googleapis.com/auth/cloud-platform",
            "--workload-pool",args.project+".svc.id.goog","--workload-metadata=GKE_METADATA",
            "--enable-master-authorized-networks","--master-authorized-networks",args.authorized_cidr,
            "--addons=HttpLoadBalancing=DISABLED,HorizontalPodAutoscaling=DISABLED,GcePersistentDiskCsiDriver=ENABLED",
            "--no-enable-managed-prometheus","--logging=SYSTEM","--monitoring=SYSTEM",
            "--labels=fund-demo-id="+demo_id+",fund-purpose=vllm-gpu-cv-demo","--async"]


def public_command(args,demo_id):
    cmd=create_command(args,demo_id)
    for option in ("--service-account","--workload-pool","--master-authorized-networks"):
        cmd[cmd.index(option)+1]="REDACTED"
    return ["gcloud",*cmd,"--project","REDACTED","--quiet","--format=json"]


def describe_owned(args,state):
    cluster=cloud(args.project,"container","clusters","describe",args.name,"--zone",args.zone)
    if cluster.get("resourceLabels",{}).get("fund-demo-id") != state.get("demo_id"):
        raise CloudError("Cluster ownership label does not match private demo state; refusal to delete")
    return cluster


def operation_status(args, operation, name):
    """Accept only the recorded create operation for this exact zonal cluster."""
    if not isinstance(operation, dict) or operation.get("name") != name:
        return "UNKNOWN"
    target = operation.get("targetLink", "")
    suffixes = tuple(f"/{scope}/{args.zone}/clusters/{args.name}" for scope in ("zones", "locations"))
    if (operation.get("operationType") != "CREATE_CLUSTER" or
            not isinstance(target, str) or not target.endswith(suffixes)):
        return "UNKNOWN"
    status = operation.get("status")
    return status if status in ("PENDING", "RUNNING", "ABORTING", "DONE") else "UNKNOWN"


def creation_status(args, state):
    """A missing cluster is conclusive only after its create operation is terminal."""
    if state.get("create_attempted") is False:
        return "NOT_ATTEMPTED"
    name = state.get("create_operation")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        return "UNKNOWN"  # Includes a CLI timeout with no accepted operation ID.
    if state.get("create_operation_status") == "DONE":
        return "DONE"  # A previously verified terminal operation cannot resume.
    try:
        operation = cloud(args.project, "container", "operations", "describe", name, "--zone", args.zone)
        status = operation_status(args, operation, name)
    except (CloudError, subprocess.TimeoutExpired):
        status = "UNKNOWN"
    state["create_operation_status"] = status
    return status


def pending_cleanup(status):
    return {"cluster_deleted": False, "cleanup_pending": True, "cleanup_confirmed": False,
            "gpu_deployment_verified": False, "create_operation_status": status,
            "note": "Creation is pending or unconfirmed; cluster absence does not prove cleanup. "
                    "Inspect the create operation and exact cluster in the named project/zone, "
                    "recover an unknown operation outcome, then rerun cleanup. Operator attention required."}


def confirm_absence(args, state):
    state.update({"cluster_deleted": True, "cleanup_pending": False, "cleanup_confirmed": True,
                  "cleanup_confirmed_at": utc_now().isoformat()})
    save_private(args.state, state)
    return {"cluster_deleted": True, "cleanup_pending": False, "cleanup_confirmed": True,
            "cleanup_confirmed_at": state["cleanup_confirmed_at"],
            "note": "Create operation is terminal and cluster absence is confirmed; "
                    "final charges/residual PVC disks/registry require separate provider checks"}


def cleanup(args,state):
    # Deletes only the exact owned cluster, never all project clusters/VMs/disks.
    state.update({"cluster_deleted": False, "cleanup_pending": True, "cleanup_confirmed": False})
    save_private(args.state, state)
    status = creation_status(args, state)
    save_private(args.state, state)
    if status not in ("DONE", "NOT_ATTEMPTED"):
        # Bounded fail-closed recovery: do not wait forever or claim a pending create was deleted.
        return pending_cleanup(status)
    # This read must follow terminal-operation verification, not precede it.
    try: describe_owned(args,state)
    except NotFound:
        return confirm_absence(args, state)
    cloud(args.project,"container","clusters","delete",args.name,"--zone",args.zone,"--async")
    deadline=time.monotonic()+600
    while time.monotonic()<deadline:
        time.sleep(10)
        try: describe_owned(args,state)
        except NotFound:
            return confirm_absence(args, state)
    state["cleanup_confirmed"]=False
    save_private(args.state,state)
    raise CloudError("Cleanup requested but not confirmed within 10 minutes; immediate operator attention required")


def read_state(args):
    state=json.loads(Path(args.state).read_text())
    if state.get("project") != args.project or state.get("zone") != args.zone or state.get("name") != args.name:
        raise ValueError("Private state does not match the explicit project/zone/name")
    return state


def emit(data,output=None):
    if output: save_private(output,data)
    print(json.dumps(data,indent=2),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("action",choices=["preflight","create","status","cleanup"])
    p.add_argument("--project",required=True);p.add_argument("--zone",required=True)
    p.add_argument("--name",default="fund-llm-demo-20261009")
    p.add_argument("--node-service-account");p.add_argument("--artifact-repository");p.add_argument("--registry-location")
    p.add_argument("--authorized-cidr");p.add_argument("--budget-usd",type=float,default=20)
    p.add_argument("--lifetime-minutes",type=int,default=120)
    p.add_argument("--state",default="/tmp/fund-gke-demo-private-state.json")
    p.add_argument("--output");p.add_argument("--execute",action="store_true")
    p.add_argument("--watch",action="store_true",help="Required for execute-create: foreground automatic cleanup timer")
    args=p.parse_args()
    state=None
    try:
        validate_inputs(args)
        if args.action=="preflight":
            result=preflight(args);emit(result,args.output);return 0 if result["preflight_passed"] else 2
        if args.action=="status":
            state=read_state(args)
            creation = creation_status(args, state)
            try:
                c=describe_owned(args,state)
                emit({"checked_at":utc_now().isoformat(),"project_fingerprint":project_hash(args.project),"zone":args.zone,
                      "status":c.get("status"),"datapath_provider":c.get("networkConfig",{}).get("datapathProvider"),
                      "node_pools":[{"name":n.get("name"),"initial_node_count":n.get("initialNodeCount"),"machine_type":n.get("config",{}).get("machineType"),
                                     "accelerators":n.get("config",{}).get("accelerators"),"autoscaling_enabled":n.get("autoscaling",{}).get("enabled",False)} for n in c.get("nodePools",[])],
                      "cleanup_due":state["cleanup_due"],"gpu_deployment_verified":False},args.output)
            except NotFound:
                confirmed = creation in ("DONE", "NOT_ATTEMPTED")
                emit({"cluster_deleted": True, "cleanup_pending": False, "cleanup_confirmed": True}
                     if confirmed else pending_cleanup(creation), args.output)
                return 0 if confirmed else 2
            return 0
        if not args.execute:
            emit({"cloud_created":False,"execute_required":True,"create_command":public_command(args,"PLAN") if args.action=="create" else None},args.output)
            return 0
        if args.action=="cleanup":
            state=read_state(args)
            result=cleanup(args,state)
            emit(result,args.output);return 0 if result["cluster_deleted"] else 2
        if not args.watch: raise ValueError("Execute-create requires --watch to keep the cleanup timer in the foreground")
        result=preflight(args)
        if not result["preflight_passed"]: emit(result,args.output);return 2
        started=utc_now();due=started+timedelta(minutes=args.lifetime_minutes)
        state={"project":args.project,"zone":args.zone,"name":args.name,"demo_id":uuid.uuid4().hex[:12],
               "started_at":started.isoformat(),"cleanup_due":due.isoformat(),"budget_usd":args.budget_usd,
               "node_service_account":args.node_service_account,"cluster_deleted":False,"create_attempted":False}
        save_private(args.state,state)
        def interrupted(signum,frame): raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM,interrupted)
        try:
            state["create_attempted"]=True;save_private(args.state,state)
            operation=cloud(args.project,*create_command(args,state["demo_id"]),timeout=120)
            # Persist the identity before output/watching, so interruption can reconcile acceptance.
            name=operation.get("name") if isinstance(operation,dict) else None
            state["create_operation"]=name
            state["create_operation_status"]=operation_status(args,operation,name) if name else "UNKNOWN"
            save_private(args.state,state)
            emit({"cloud_create_requested":True,"operation_status":state["create_operation_status"],"cleanup_due":due.isoformat(),
                  "gpu_deployment_verified":False,"project_fingerprint":project_hash(args.project)},args.output)
            while utc_now()<due:
                time.sleep(min(20,max(0.1,(due-utc_now()).total_seconds())))
                try:
                    c=describe_owned(args,state)
                    if c.get("status")=="ERROR": raise CloudError("Cloud cluster entered ERROR; cleaning up")
                except NotFound:
                    # Async creation can be briefly absent; absence after initially visible means root cleaned it up.
                    if state.get("observed_cluster"): break
                    continue
                state["observed_cluster"]=True;save_private(args.state,state)
        finally:
            if state.get("create_attempted"):
                result=cleanup(args,state)
                emit(result,args.output)
        return 0 if result["cluster_deleted"] else 2
    except (CloudError,ValueError,FileNotFoundError,subprocess.TimeoutExpired,KeyboardInterrupt) as exc:
        pending=bool(state and not state.get("cluster_deleted"))
        emit({"operation_failed":True,"gpu_deployment_verified":False,"error":str(exc) or "Interrupted",
              "cleanup_pending":pending,"cleanup_must_be_confirmed":pending},args.output)
        return 2


if __name__=="__main__": sys.exit(main())
