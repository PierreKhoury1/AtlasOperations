# What would make it better: measured on WILDTRACK

Six experiments, one per weak spot. Each one tuned on instants 0-199 and reported on 200-399, then a second agent
re-ran it independently and checked it for tuning on the test half, ground truth leaking into predictions, and
unfair baselines. Only verified numbers are below. CPU only (4 cores, no GPU), cached yolo11n detections unless stated.

| area | today | best measured | verdict | cost |
|---|---|---|---|---|
| person detection (C1, C2, C3, C6) | yolo11n, 960x540, conf 0.35: F1 0.644, recall 0.53 | yolo11s on the full 1920x1080 frame at imgsz 1280, conf 0.20, no tiles: F1 ~0.70 (+0.05 to +0.06; 0.707 and 0.695 on two separate sets of held-out instants), recall ~0.67 | confirmed | ~1.8 s/frame on this CPU, 2.6x less than the tiled detector the catalogue ships |
| the catalogue's tiled detector (PreciseDetector, 2x2 tiles + full frame) | - | no better than the baseline here (F1 change -0.01, interval includes 0): boxes cut at tile edges become false positives | confirmed | 4.6 s/frame |
| free detection gain | conf 0.35 | conf 0.15 on the same 960x540 path: +0.02 to +0.03 F1 | confirmed | none |
| identity over time, per camera | catalogue tracker IDF1 0.470 (held-out, cold start), ByteTrack 0.451 | one ground-plane tracker for all cameras (merge sightings by foot position at 120 cm, Kalman + Hungarian on the ground): IDF1 0.574, MOTA 0.362; identity switches rise 620 -> 802 | confirmed | ~1 s CPU per 200 instants x 7 cameras |
| same person, same id on every camera | 0.448 (catalogue tracks linked by foot position afterwards) | 0.560 cross-camera IDF1; on the ground plane IDF1 0.71, MODA 0.59 at 50 cm | confirmed | same |
| head count of the scene | busiest camera MAE 7.25; product's 150 cm merge 11.4 | merge feet at 150 cm, keep a person seen by 3+ cameras (or one detection with conf >= 0.7), count only inside the area: MAE 2.71, bias +0.5, 78% of instants within 20% | partially confirmed: beats every product method clearly; only +0.4 better than an 800 cm merge restricted to the area (MAE 3.14), and sensitive to the 0.7 threshold | none |
| live detection speed | PyTorch yolo11n: ~88 CPU-ms per frame (about 6-7 cameras at 5 fps on a free 4-core box) | the same model through OpenVINO, fixed 384x640 input, bf16, one thread per camera: ~25-27 CPU-ms, same P/R/F1 (0.784/0.495/0.607): ~23 cameras at 5 fps, ~11 at 10 fps | confirmed (bf16 relies on this Xeon's AMX; other CPUs gain less) | a one-time export |
| camera agents' notes | 81% of claims true, 4.5% wrong (judge: claude-sonnet-5) | prompt rules (no absence claims, no carrying people forward) and grounding did NOT lower wrong claims (6.4% on test); code-inserted people count per camera from all cameras' detections: count error 3.1 vs 7.3 for the raw detector (4.4 for a detector scaled x1.4) | partially confirmed; the prompt part was not tested on a clean split | +$0 for the count |
| cheaper agent model | gemini-3.1-flash-lite, ~$0.0022 per note | gemini-2.5-flash-lite: 5x cheaper ($0.0004), similar wrong claims per note but ~40% fewer details | partially confirmed | - |
| cheaper judge | claude-sonnet-5, $0.33 per 24 notes | no cheaper model (Haiku, GPT mini, Qwen, Gemini Flash) gave the same verdicts; Sonnet with 2 neighbouring cameras at 512 px is 26% cheaper but flags more notes than Sonnet does; 2 neighbours at 960 px (-12%) is the safe saving | partially confirmed | - |

Shipped since: the live detector runs on OpenVINO by default (`VISION_RUNTIME`, 83-89 -> 23-24 CPU-ms per frame with
ByteTrack, same P/R/F1), and the tiled detector drops every edge-cut tile box, merges at NMS 0.6, conf 0.25 (F1 0.635 ->
0.699 on C1 C2 C3 C6, 20 held-out instants). `python scripts/bench_detector.py` reproduces both.

Lessons that matter beyond the numbers:

- Telling a vision model a number does not make it use it: told "21 people", gemini-3.1-flash-lite wrote 45. A count
  has to be written into the note by code.
- The live view was CPU-bound partly because the desk server itself used about 3.6 of the 4 cores (four demo feeds
  plus two kitchen clips), so every timing here is relative, not absolute.
- Everything above is one scene. Tiling was added for very distant people in other footage; drop it per site, not
  everywhere.

Experiment scripts and logs were kept outside the repository (WILDTRACK is non-commercial research data); the
numbers above come from their verified reruns. API spend for this round: about $5.4 (notes $2.5, judge $2.9).
