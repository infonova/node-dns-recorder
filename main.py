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

NDR_HOSTS_SNIPPET_FILE = "ndr-hosts-snippet"

NDR_HOSTS_SNIPPET = f"""hosts /etc/coredns/{NDR_HOSTS_FILE} {' '.join(NDR_DOMAINS)} {{
    fallthrough
}}
"""

NDR_COREFILE_ANCHOR = "forward"
"""NDR_COREFILE_ANCHOR is the anchor the operator uses to patch in the hosts plugin to the Corefile of CoreDNS"""

NDR_GUARD_BEGIN = "# BEGIN managed by NDR"
NDR_GUARD_END = "# END managed by NDR"

NDR_COREFILE_PATCH = f"""{NDR_GUARD_BEGIN}
    import /etc/coredns/{NDR_HOSTS_SNIPPET_FILE}
    {NDR_GUARD_END}
    """
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

    anchor_index = corefile.index(NDR_COREFILE_ANCHOR)
    begin_index = (
        corefile.find(NDR_GUARD_BEGIN)
        if corefile.find(NDR_GUARD_BEGIN) > 0
        else anchor_index
    )
    patched_corefile = (
        corefile[:begin_index] + NDR_COREFILE_PATCH + corefile[anchor_index:]
    )

    core_v1 = client.CoreV1Api()
    core_v1.patch_namespaced_config_map(
        name=name,
        namespace=namespace,
        body={
            "metadata": {"annotations": {NDR_ANNOTATION_KEY: "true"}},
            "data": {
                "Corefile": patched_corefile,
                NDR_HOSTS_SNIPPET_FILE: NDR_HOSTS_SNIPPET,
            },
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
