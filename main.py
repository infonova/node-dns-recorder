"""NDR - Node DNS Recorder Operator"""
import hashlib
import os
import re

import kopf
from kubernetes import client

NDR_ANNOTATION_KEY = os.getenv("NDR_ANNOTATION_KEY", "node-dns-recorder/patched")
"""NDR_ANNOTATION_KEY is the key which the operator writes to certain resources to mark them as completed"""

NDR_DOMAINS = os.getenv("NDR_DOMAINS", ".cluster.local,k8s.local").split(",")
"""NDR_DOMAINS are the domains which the operator will append to the found nodes in the hosts file"""

NDR_HOSTS_FILE = "ndr-hosts"
"""NDR_HOSTS_FILE is the file name of the hosts file which the operator writes to the CoreDNS configmap"""

NDR_COREFILE_ANCHOR = "forward"
"""NDR_COREFILE_ANCHOR is the anchor the operator uses to patch in the hosts plugin to the Corefile of CoreDNS"""

NDR_COREFILE_PATCH = fr"\g<1>hosts /etc/coredns/{NDR_HOSTS_FILE} {{\g<1>  fallthrough\g<1>}}\g<1>{NDR_COREFILE_ANCHOR}\g<2>"
"""NDR_COREFILE_PATCH defines the patch configuration for patching in the hosts file to the Corefile"""

NDR_STATE = {"hosts": {}, "hash": ""}
"""NDR_STATE represents the current state which the operator needs to write to the config map"""


@kopf.on.event(
    "apps",
    "v1",
    "deployment",
    labels={"kubernetes.io/name": "CoreDNS", "kubernetes.io/cluster-service": "true"},
    annotations={NDR_ANNOTATION_KEY: kopf.ABSENT},
)
async def patch_coredns_deployment(namespace, name, **_):
    apps_v1 = client.AppsV1Api()
    apps_v1.patch_namespaced_deployment(
        name=name,
        namespace=namespace,
        body={
            "metadata": {"annotations": {NDR_ANNOTATION_KEY: "true"}},
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [
                            {"name": "config-volume", "configMap": {"items": None}}
                        ]
                    }
                }
            },
        },
    )


@kopf.on.event(
    "",
    "v1",
    "configmap",
    field="metadata.name",
    value="coredns",
    annotations={NDR_ANNOTATION_KEY: kopf.ABSENT},
)
async def patch_coredns_configmap(namespace, name, body, **_):
    corefile = body.get("data").get("Corefile")
    if "hosts" not in corefile:
        patched_corefile = re.sub(
            fr"(\s.*){NDR_COREFILE_ANCHOR}(.*)", NDR_COREFILE_PATCH, corefile
        )

        core_v1 = client.CoreV1Api()
        core_v1.patch_namespaced_config_map(
            name=name,
            namespace=namespace,
            body={
                "metadata": {"annotations": {NDR_ANNOTATION_KEY: "true"}},
                "data": {"Corefile": patched_corefile},
            },
        )


@kopf.on.event("", "v1", "node")
async def write_hosts_file(name, body, type, **_):
    ndr_hosts = NDR_STATE["hosts"]

    for address in body["status"]["addresses"]:
        if address["type"] in ["InternalIP"]:
            if type in [None, "MODIFIED"]:
                ndr_hosts[address["address"]] = [
                    f"{name}.{domain}" for domain in NDR_DOMAINS
                ]
            elif type == "DELETED":
                ndr_hosts.pop(address["address"])

    ndr_hosts = "\n".join([f'{k} {" ".join(v)}' for k, v in ndr_hosts.items()])
    new_ndr_hosts_hash = hashlib.sha512(ndr_hosts.encode()).hexdigest()

    if not new_ndr_hosts_hash == NDR_STATE["hash"]:
        core_v1 = client.CoreV1Api()
        core_v1.patch_namespaced_config_map(
            name="coredns",
            namespace="kube-system",
            body={"data": {NDR_HOSTS_FILE: ndr_hosts}},
        )
        NDR_STATE["hash"] = new_ndr_hosts_hash
