"""The ground-plane tracker and the multi-camera head count (atlas.ground) on synthetic people, and the Review data it feeds."""
import json

import numpy as np
import pytest

from atlas import ground as G
from atlas import mcam_eval as M


def test_cluster_memberships_hold_one_sighting_per_camera():
    # A seen by cameras 0, 1, 2; B by cameras 0 and 1, 3 m away; camera 0 also sees C 20 cm from A
    pts = np.array([[0, 0], [20, 10], [-10, 25], [300, 0], [310, 15], [15, 5]], float)
    cams = [0, 1, 2, 0, 1, 0]
    cl = G.cluster(pts, cams, 100)
    assert sorted(map(sorted, cl)) == [[0, 2], [1, 5], [3, 4]]       # C pairs with A's camera-1 sighting, not with A
    for m in cl:
        assert len({cams[i] for i in m}) == len(m)                # never two sightings from one camera in a person
    assert sorted(i for m in cl for i in m) == list(range(len(pts)))
    assert len(cl) == 3 == M.cluster_count(pts, cams, 100)        # the scorer's count is the same clustering
    assert G.cluster(np.zeros((0, 2)), [], 50) == []


def _walk(people, t, cams=(0, 1, 2), jitter=8.0, rng=None):
    """Feet of each person at instant t on every camera in cams (a few cm of calibration noise per camera)."""
    rng = rng or np.random.default_rng(0)
    return [np.array([p(t) + rng.normal(0, jitter, 2) for p in people]) for _ in cams]


def test_tracker_keeps_an_id_through_a_two_instant_miss():
    rng = np.random.default_rng(1)
    walker = lambda t: np.array([100.0 * t, 0.0])                # 1 m/s along x, 2 instants a second
    trk = G.GroundTracker()
    seen = []
    for k in range(12):
        t = k * 0.5
        feet = _walk([walker], t, rng=rng) if k not in (6, 7) else [np.zeros((0, 2))] * 3
        ids = trk.update(feet, [[0.9] * len(f) for f in feet], t)
        if k not in (6, 7):
            seen.append({i for c in ids for i in c})
    assert seen[0] == {None} and seen[1] == {None}                # online: confirmed at the third instant
    assert all(s == {1} for s in seen[2:])                         # one id, on every camera, across the gap


def test_tracker_does_not_swap_two_people_walking_past_each_other():
    rng = np.random.default_rng(2)
    a = lambda t: np.array([-400.0 + 120.0 * t, 0.0])            # A walks +x, B walks -x, 2 m apart in y
    b = lambda t: np.array([400.0 - 120.0 * t, 200.0])
    ids_a, ids_b = set(), set()
    feet = [_walk([a, b], k * 0.5, rng=rng) for k in range(14)]
    ids, ground = G.track(feet, None, [k * 0.5 for k in range(14)])
    for k in range(14):
        for ci in range(3):
            ids_a.add(ids[k][ci][0]); ids_b.add(ids[k][ci][1])
    assert len(ids_a) == len(ids_b) == 1 and ids_a != ids_b and None not in ids_a   # retro-labelled from the first instant
    assert all(len(g) == 2 for g in ground)
    # online, the same people keep the same ids once confirmed
    trk = G.GroundTracker(retro=False)
    out = [trk.update(f, None, k * 0.5) for k, f in enumerate(feet)]
    assert {c[0] for o in out[2:] for c in o} == ids_a and {c[1] for o in out[2:] for c in o} == ids_b


def test_tracker_needs_two_cameras_to_start_a_person():
    trk = G.GroundTracker(min_hits=1)
    ids = trk.update([np.array([[0.0, 0.0]]), np.zeros((0, 2))], [[0.99], []], 0.0)
    assert ids == [[None], []] and trk.ground == []
    ids = trk.update([np.array([[0.0, 0.0]]), np.array([[30.0, 0.0]])], [[0.9], [0.9]], 0.5)
    assert ids[0][0] == ids[1][0] is not None and len(trk.ground) == 1


def test_scene_count_vote():
    three = [np.array([[0.0, 0.0]]), np.array([[40.0, 10.0]]), np.array([[-20.0, 30.0]])]
    feet = [three[0], three[1], np.vstack([three[2], [[500.0, 500.0]]])]      # a 1-camera blob far from anyone
    conf = [[0.5], [0.5], [0.5, 0.4]]
    P = G.scene_count(feet, conf)
    assert len(P) == 1 and np.linalg.norm(P[0] - [6.7, 13.3]) < 1        # the 3-camera person, not the weak blob
    conf[2][1] = 0.8                                                     # one sure detection is enough on its own
    assert len(G.scene_count(feet, conf)) == 2
    assert len(G.scene_count(feet, conf, roi=(-100, 100, -100, 100))) == 1    # counted only inside the area
    assert len(G.scene_count([np.zeros((0, 2))] * 3, [[], [], []])) == 0


def test_review_json_carries_cross_camera_ids():
    D = {"frames": ["00000000", "00000005"], "scene": [1, 1],
         "gt": [[[(4, [10, 10, 40, 90])], [(4, [200, 20, 230, 100])], *[[] for _ in M.CAMS[2:]]] for _ in range(2)]}
    preds = [[[{"box": [10, 10, 40, 90], "conf": 0.9}], [{"box": [200, 20, 230, 100], "conf": 0.8}], *[[] for _ in M.CAMS[2:]]] for _ in range(2)]
    tracks = [[[dict(c[0], id=7)] if c else [] for c in f] for f in preds]   # the ground tracker: one id on both cameras
    R = M.review_json(D, preds, tracks, [1, 1], "multi-camera count (vote)", shared_ids=True)
    assert R["shared_ids"] is True and R["geo_label"] == "multi-camera count (vote)"
    for inst in R["instants"]:
        assert inst["geo"] == 1
        assert [c["pred"][0][0] for c in inst["cams"][:2]] == [7, 7]          # same id on C1 and C2
        assert [c["pred"][0][5] for c in inst["cams"][:2]] == [4, 4]          # and both matched to person 4
    assert json.loads(json.dumps(R)) == R
    assert "geo_label" not in M.review_json(D, preds, tracks)


def test_cross_camera_identity_and_linking():
    # person 1 on cameras 0 and 1 over 4 instants: one shared id scores 1; per-camera ids score half at best
    gt = [[[(1, [10, 10, 40, 90])], [(1, [200, 20, 230, 100])], *[[] for _ in M.CAMS[2:]]] for _ in range(4)]
    D = {"frames": [str(i) for i in range(4)], "gt": gt}
    shared = [[[{"box": b, "id": 5}] for _, b in (c[0] for c in f[:2])] + [[] for _ in M.CAMS[2:]] for f in gt]
    assert M.score_cross_camera(D, shared)["idf1"] == 1.0
    per_cam = [[[{"box": b, "id": 3}] for _, b in (c[0] for c in f[:2])] + [[] for _ in M.CAMS[2:]] for f in gt]
    assert M.score_cross_camera(D, per_cam, per_camera_ids=True)["idf1"] == 0.5


def test_link_by_feet_joins_cameras_but_never_one_camera_with_itself(monkeypatch):
    # feet: the box's top-left corner stands in for the ground point, so the test does not need a calibration
    monkeypatch.setattr(M, "feet_on_ground", lambda cal, boxes: np.array([[b[0], b[1]] for b in boxes], float).reshape(-1, 2))
    n = 4
    gt = [[[(1, [0, 0, 30, 90])], [(1, [10, 0, 40, 90])], *[[] for _ in M.CAMS[2:]]] for _ in range(n)]
    D = {"frames": [str(i) for i in range(n)], "gt": gt, "calib": [None] * len(M.CAMS)}
    tracks = [[[{"box": [0, 0, 30, 90], "id": 3}], [{"box": [10, 0, 40, 90], "id": 9}], *[[] for _ in M.CAMS[2:]]] for _ in range(n)]
    linked = M.link_by_feet(D, tracks, kmin=2)
    assert linked[0][0][0]["id"] == linked[0][1][0]["id"]                    # one person, one id on both cameras
    assert M.score_cross_camera(D, linked)["idf1"] == 1.0
    # two tracks on camera 0 alive at the same time, both near camera 1's track: they stay two people
    both = [[[{"box": [0, 0, 30, 90], "id": 4}, {"box": [20, 0, 50, 90], "id": 5}], [{"box": [10, 0, 40, 90], "id": 9}],
             *[[] for _ in M.CAMS[2:]]] for _ in range(n)]
    out = M.link_by_feet(D, both, kmin=2)
    assert out[0][0][0]["id"] != out[0][0][1]["id"]


def test_gather_checks_confidences_and_survives_zero_weights():
    with pytest.raises(ValueError):
        G.gather([np.zeros((1, 2)), np.zeros((0, 2))], [[0.9, 0.8], []])
    t = G.GroundTracker()
    t.update([np.array([[0.0, 0.0]]), np.array([[10.0, 0.0]])], [[0.0], [0.0]], 0.0)   # all-zero confidence: no crash
