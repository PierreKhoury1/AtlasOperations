"""Keep trying to launch an Oracle Always-Free Ampere A1 instance until capacity appears.

Oracle's free ARM shape (VM.Standard.A1.Flex, up to 4 OCPU / 24 GB per tenancy) is popular; the console often says
"Out of host capacity". This script retries every availability domain in your home region on a timer, using the
OCI CLI (`pip install oci-cli`, then `oci setup config`). Stops at the first success and prints the public IP.

    python launch_a1.py --compartment ocid1.tenancy.oc1..xxx --subnet ocid1.subnet.oc1..xxx \
        --ssh-key ~/.ssh/id_ed25519.pub [--ocpus 4 --mem 24 --every 90 --name atlas-node]

The subnet OCID comes from Networking -> Virtual cloud networks -> your VCN -> Subnets (create the default VCN once
from the console "Create VCN with Internet connectivity" wizard). The compartment can be the tenancy OCID.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def oci(*args: str) -> tuple[int, str, str]:
    p = subprocess.run(["oci", *args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def jq(*args: str) -> dict:
    rc, out, err = oci(*args)
    if rc != 0:
        sys.exit(f"oci {' '.join(args[:3])} failed: {err.strip()[:400]}")
    return json.loads(out or "{}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compartment", required=True)
    ap.add_argument("--subnet", required=True)
    ap.add_argument("--ssh-key", required=True, help="path to public key")
    ap.add_argument("--ocpus", type=int, default=4)
    ap.add_argument("--mem", type=int, default=24)
    ap.add_argument("--every", type=int, default=90, help="seconds between rounds")
    ap.add_argument("--name", default="atlas-node")
    ap.add_argument("--boot-gb", type=int, default=60, help="free tier allows 200 GB total block storage")
    ap.add_argument("--image", default="", help="image OCID; default = newest Canonical Ubuntu 22.04 aarch64")
    a = ap.parse_args()

    key = Path(a.ssh_key).expanduser()
    if not key.exists():
        sys.exit(f"ssh key not found: {key}")
    ads = [d["name"] for d in jq("iam", "availability-domain", "list", "--compartment-id", a.compartment)["data"]]
    if not ads:
        sys.exit("no availability domains - is the CLI configured for your home region?")
    image = a.image
    if not image:
        imgs = jq("compute", "image", "list", "--compartment-id", a.compartment, "--operating-system", "Canonical Ubuntu",
                  "--operating-system-version", "22.04", "--shape", "VM.Standard.A1.Flex", "--sort-by", "TIMECREATED", "--sort-order", "DESC")["data"]
        imgs = [i for i in imgs if "aarch64" in i.get("display-name", "")] or imgs
        if not imgs:
            sys.exit("no Ubuntu 22.04 A1 image found; pass --image")
        image = imgs[0]["id"]
        print("image:", imgs[0]["display-name"])
    print(f"trying {a.ocpus} OCPU / {a.mem} GB in {ads}, every {a.every}s. Ctrl+C to stop.")
    rnd = 0
    while True:
        rnd += 1
        for ad in ads:
            rc, out, err = oci("compute", "instance", "launch", "--compartment-id", a.compartment, "--availability-domain", ad,
                               "--shape", "VM.Standard.A1.Flex", "--shape-config", json.dumps({"ocpus": a.ocpus, "memoryInGBs": a.mem}),
                               "--image-id", image, "--subnet-id", a.subnet, "--assign-public-ip", "true",
                               "--display-name", a.name, "--ssh-authorized-keys-file", str(key),
                               "--boot-volume-size-in-gbs", str(a.boot_gb))
            if rc == 0:
                inst = json.loads(out)["data"]
                print("\nLAUNCHED", inst["id"], "in", ad)
                print("waiting for RUNNING...")
                for _ in range(40):
                    time.sleep(15)
                    st = jq("compute", "instance", "get", "--instance-id", inst["id"])["data"]["lifecycle-state"]
                    if st == "RUNNING":
                        break
                vnics = jq("compute", "instance", "list-vnics", "--instance-id", inst["id"])["data"]
                ip = next((v.get("public-ip") for v in vnics if v.get("public-ip")), "?")
                print(f"public ip: {ip}\n  ssh ubuntu@{ip}")
                print("then run node/oracle/setup.sh on it (see the README).")
                return 0
            msg = err.strip().splitlines()[-1][:160] if err.strip() else out[:160]
            tag = "capacity" if "capacity" in err.lower() else ("limit" if "limit" in err.lower() else "error")
            print(f"round {rnd} {ad}: {tag}: {msg}")
            if tag == "error" and any(k in err for k in ("NotAuthenticated", "NotAuthorizedOrNotFound", "InvalidParameter", "Missing")):
                return 1                                  # auth / argument errors will not fix themselves
        time.sleep(a.every)


if __name__ == "__main__":
    sys.exit(main())
