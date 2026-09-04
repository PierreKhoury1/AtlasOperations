# Atlas Vision Node

Continuous camera inference for an Atlas desk. Instead of the desk grabbing one frame every N seconds, the node
watches **every frame** of each camera (YOLO through OpenVINO + ByteTrack), keeps track identities, and posts only
**events** to the desk's `/hook/<token>/vision`:

| event | when | desk reaction |
|---|---|---|
| `entered` / `left` | a tracked object appeared / disappeared (after `confirm_s`, `gone_s`) | logged for "ask the cameras" |
| `count` | watched labels reach `min_count` at once (rising edge, then every `cooldown_s` while it holds) | agent run |
| `dwell` | one object stayed longer than `dwell_s` | agent run |
| `after_hours` | a watched object present outside `hours` | agent run |
| `heartbeat` | every `heartbeat_min` with current counts + keyframe | logged |

Video never leaves the node. Each event carries one annotated keyframe (boxes, track ids, timestamp) so the desk's
vision model and agents see what the node saw. The desk keeps the VLM, rule engine and RAG log exactly as before -
the node is just a much better "external detector".

## Run it anywhere with a CPU

```bash
pip install -r node/requirements.txt          # torch CPU + openvino + ultralytics
cp node/node.example.json node/node.json       # fill in hook + cameras
python node/atlas_node.py --check              # 10 s: can it reach every camera? fps?
python node/atlas_node.py --dry --seconds 60   # events printed, nothing posted
python node/atlas_node.py                      # forever; health JSON on http://127.0.0.1:8765/
```

The hook URL is on the desk's **Cameras** page ("sensor hook URL"). Weights are downloaded and exported to OpenVINO
on first start (`node/models/`, cached). `device: "cpu"` skips OpenVINO and uses torch.

Measured on a Ryzen laptop, yolov8n @ 640: OpenVINO 33 ms/frame inference; a remote 1080p RTSP stream yields ~9 fps
at `vid_stride: 2`, the decode is the limit, not the model. Oracle A1 (4 ARM cores): expect ~60-80 ms/frame,
enough for 2-3 cameras at `vid_stride: 2-3`.

## Oracle Cloud Always Free (the "free forever" host)

Oracle's free tier includes an Ampere A1 VM with up to **4 OCPU / 24 GB RAM** and 10 TB egress per month, permanently.
Inbound traffic (the camera streams) is free. That is enough for a node watching a few cameras 24/7.

1. **Sign up** at cloud.oracle.com/free (card needed for identity, not charged on Always-Free resources).
   Pick the **home region** carefully - it cannot be changed later, and A1 capacity is scarce in the busiest ones
   (Frankfurt / London / Ashburn). Anywhere works as long as the cameras are reachable from the internet.
2. **Network**: Networking -> Virtual cloud networks -> *Start VCN Wizard* -> "VCN with Internet Connectivity" -> defaults.
3. **Instance**: Compute -> Instances -> *Create instance*
   - Image: **Canonical Ubuntu 22.04** (the aarch64 build is picked automatically once the shape is A1)
   - Shape: *Ampere* -> **VM.Standard.A1.Flex**, 4 OCPU, 24 GB
   - Networking: the public subnet from step 2, *assign a public IPv4*
   - SSH: paste your public key
   - Boot volume: 50-100 GB (free tier allows 200 GB total)
   - *Create*. If it says **Out of host capacity**, either retry at odd hours or let the CLI do it:
     ```bash
     pip install oci-cli && oci setup config          # once; API key gets added to your user automatically
     python node/oracle/launch_a1.py --compartment <tenancy OCID> --subnet <subnet OCID> --ssh-key ~/.ssh/id_ed25519.pub
     ```
     It retries every availability domain every 90 s until it lands, then prints the IP.
4. **Install the node** (one command on the VM, ~10 min on first build):
   ```bash
   ssh ubuntu@<public ip>
   curl -fsSL https://raw.githubusercontent.com/PierreKhoury1/hermes-ops/master/node/oracle/setup.sh | bash -s -- \
       "https://atlas-ops.onrender.com/hook/<token>/vision"
   nano /opt/atlas-node/config/node.json     # cameras: name / rtsp source / watch_for / min_count / hours
   sudo docker restart atlas-node
   sudo docker logs -f atlas-node            # "POST counter count: 3 person at once -> 412 run r_..."
   curl -s localhost:8765 | python3 -m json.tool
   ```
   Re-run `setup.sh` to update to the latest code. The container restarts on its own and on reboot.
5. **Keep it free**: Oracle reclaims Always-Free VMs that sit idle (<20% CPU for 7 days). A node doing inference
   never idles. No inbound port needs opening - the health endpoint is loopback-only.

## Elsewhere

Same image runs on any Docker host: `docker compose -f node/docker-compose.yml up -d --build` from the repo root,
config in `/opt/atlas-node/config/node.json`. Hetzner CAX21 (4 ARM vCPU, ~EUR 6.5/mo) is the paid equivalent
when Oracle capacity never appears.

## Config reference (`node.json`)

```jsonc
{
  "hook": "https://.../hook/<token>/vision",   // or env ATLAS_HOOK
  "model": "yolov8n.pt",                       // any ultralytics detect model; yolov8s.pt if the CPU has room
  "device": "openvino",                        // or "cpu"
  "imgsz": 640, "conf": 0.35, "vid_stride": 2, // infer every 2nd frame
  "heartbeat_min": 30, "max_posts_per_min": 20,
  "cameras": [{
    "name": "counter",
    "source": "rtsp://user:pw@host:554/Streaming/Channels/101",   // rtsp / http snapshot / webcam index / file
    "watch_for": ["person"],                   // labels that count; [] = everything
    "min_count": 3, "dwell_s": 0, "hours": "08:00-20:00", "alert_outside_hours": true,
    "cooldown_s": 180, "confirm_s": 1.0, "gone_s": 3.0
  }]
}
```

Env overrides: `ATLAS_HOOK`, `ATLAS_CAMERAS` (JSON list), `ATLAS_NODE_CONFIG`, `ATLAS_MODELS`, `ATLAS_HEALTH_BIND`.
