# Multi-camera accuracy: WILDTRACK

7 synchronised cameras, 400 instants at 2 per second (200 s), 13-40 people in the scene at once (mean 23.8), 313 labelled identities. Every number compares with the human labels.

Calibration check: 100.0% of labelled feet project inside the labelled area; a labelled box's foot point lands 8.9 cm from that person's labelled ground position at the median (19.8 cm at the 90th percentile).

## Detection (people, IoU >= 0.5, inside the labelled area)

| detector | precision | recall | F1 | per-camera count error (people/frame) |
|---|---|---|---|---|
| yolo11n | 0.696 | 0.55 | 0.615 | 5.68 |
| yolo11n @ IoU 0.3 | 0.759 | 0.6 | 0.67 | 5.68 |
| yolo11s | 0.714 | 0.531 | 0.609 | 6.11 |
| yolo11s @ IoU 0.3 | 0.776 | 0.578 | 0.662 | 6.11 |

| camera | yolo11n P / R | yolo11n @ IoU 0.3 P / R | yolo11s P / R | yolo11s @ IoU 0.3 P / R |
|---|---|---|---|---|
| C1 | 0.762 / 0.508 | 0.805 / 0.536 | 0.794 / 0.435 | 0.832 / 0.456 |
| C2 | 0.852 / 0.558 | 0.906 / 0.594 | 0.864 / 0.571 | 0.915 / 0.604 |
| C3 | 0.67 / 0.61 | 0.748 / 0.681 | 0.699 / 0.581 | 0.767 / 0.638 |
| C4 | 0.343 / 0.57 | 0.416 / 0.691 | 0.365 / 0.574 | 0.435 / 0.685 |
| C5 | 0.862 / 0.691 | 0.945 / 0.757 | 0.858 / 0.691 | 0.949 / 0.764 |
| C6 | 0.834 / 0.386 | 0.898 / 0.416 | 0.838 / 0.378 | 0.904 / 0.407 |
| C7 | 0.542 / 0.775 | 0.601 / 0.858 | 0.557 / 0.774 | 0.621 / 0.864 |

## Tracking (identity kept over time, per camera, 2 frames a second)

| tracker | MOTA | IDF1 | identity switches | recall | mostly tracked |
|---|---|---|---|---|---|
| ByteTrack (yolo11n, live view) | 0.212 | 0.357 | 2633 | 0.453 | 169 of 1611 |
| catalogue tracker (yolo11n) | 0.278 | 0.428 | 1256 | 0.498 | 208 of 1611 |
| ground tracker (all cameras) | 0.308 | 0.538 | 1482 | 0.524 | 293 of 1611 |

The ground tracker merges every camera's sightings by where the feet land (120 cm), follows each person on the ground (constant-velocity Kalman, Hungarian assignment within 120 cm, 3 missed instants allowed), starts a person only when 2 cameras see them, and gives every sighting of that person the same id on every camera.

| one id per person on every camera | IDF1 across cameras | ID precision | ID recall |
|---|---|---|---|
| ground tracker (all cameras) | 0.523 | 0.633 | 0.445 |
| catalogue tracker, linked across cameras by foot position | 0.402 | 0.494 | 0.339 |
| catalogue tracker, one id per camera | 0.168 | 0.206 | 0.142 |

On the ground plane (ground tracker (all cameras) vs the labelled positions, a hit within 50 cm): MODA 0.501, MOTA 0.477, IDF1 0.645, 226 identity switches, precision 0.791, recall 0.681, position error 17.2 cm (people seen by two cameras or more).

## Re-identification across cameras (CLIP appearance, same instant)

Pick a person on one camera; among everyone visible on another camera at that instant, the most similar crop is the same person **11%** of the time (chance 1%, 3728 queries). ROC AUC 0.52 pooled, 0.503 per query (same-person similarity 0.741 vs different 0.733). Control, the easiest case: the same camera a few seconds later, rank-1 42% (2483 queries).

| threshold | precision | recall | false matches per true one |
|---|---|---|---|
| 0.86 | 0.051 | 0.138 | 18.7 |
| 0.92 | 0.059 | 0.017 | 16.0 |

## Geometry: the cameras are calibrated

Matching a person across cameras by where their feet land on the ground (same instant, labelled boxes): the nearest foot point on the other camera is the same person **100%** of the time (160054 queries), against 11% for CLIP appearance. The same person's two foot points are 11.8 cm apart at the median (21.3 cm at the 90th percentile).

## How many people are in the scene?

| estimate | mean absolute error (people) | bias | within 20% of the truth |
|---|---|---|---|
| sum of per-camera detections | 58.2 | +58.2 | 0% |
| largest single camera | 7.3 | -7.1 | 30% |
| detections' feet clustered on the ground, 50 cm | 22.7 | +22.7 | 0% |
| detections' feet clustered on the ground, 75 cm | 16.8 | +16.8 | 0% |
| detections' feet clustered on the ground, 100 cm | 14.4 | +14.4 | 1% |
| detections' feet clustered on the ground, 150 cm | 11.4 | +11.3 | 7% |
| multi-camera vote: feet merged at 150 cm, kept when 3 cameras agree or one detection is >= 0.7 sure, inside the labelled area | 2.7 | -0.2 | 83% |
| google/gemini-3.1-flash-lite from all 7 frames | 75.9 | +75.9 | 0% (8 instants) |
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free from all 7 frames | 82.3 | +82.3 | 0% (6 instants) |

The vision models are not held to the same standard: they see everyone in the seven frames, including people outside the labelled area (the stairs, the far side), which the truth does not count. Their round answers (85, 115, 120) still say they cannot count a crowd across seven overlapping views.

## Held out: instants 200-399

The ground tracker's parameters (merge radius, gate, misses, confirmations, Kalman noise) and the vote's (radius, cameras, confidence) were chosen on instants 0-199, the cross-camera linking of the catalogue tracks too; the full-run numbers above include those instants. On instants 200-399 alone (the catalogue and ground trackers start fresh at 200; ByteTrack keeps its ids from the full run):

| tracker | MOTA | IDF1 | identity switches | recall |
|---|---|---|---|---|
| ByteTrack (live view) | 0.292 | 0.451 | 1079 | 0.498 |
| catalogue tracker | 0.339 | 0.47 | 620 | 0.528 |
| ground tracker (all cameras) | 0.362 | 0.574 | 802 | 0.551 |

| one id per person on every camera | IDF1 across cameras |
|---|---|
| ground tracker (all cameras) | 0.56 |
| catalogue tracker, linked across cameras by foot position | 0.447 |
| catalogue tracker, one id per camera | 0.172 |

Ground plane, ground tracker (all cameras), within 50 cm: MODA 0.589, MOTA 0.568, IDF1 0.712.

Head count (truth mean 23.7): the vote errs by 2.7 people (+0.5), the busiest camera by 7.3 (-6.9). In the experiment that chose these settings on WILDTRACK (tuned on instants 0-199, re-run on 200-399), the vote's 0.7 confidence bar was sensitive (0.6 or 0.8 cost about one person of error), and plain clustering with a very wide radius (800 cm) counted inside the area came within 0.4 people of it.

_Generated 2026-10-02 21:16. Data: WILDTRACK (Chavdarova et al., CVPR 2018), non-commercial research use._