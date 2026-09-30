"""The multi-camera scorer's building blocks on synthetic data (no dataset needed), and the Review page's endpoints."""
import json

import numpy as np
import pytest

from atlas import mcam_eval as M


def test_iou_and_one_to_one_matching():
    a = [[0, 0, 10, 10], [20, 20, 30, 30]]
    b = [[0, 0, 10, 10], [5, 0, 15, 10], [100, 100, 110, 110]]
    I = M.iou_matrix(a, b)
    assert I.shape == (2, 3) and I[0, 0] == pytest.approx(1.0) and I[0, 1] == pytest.approx(50 / 150) and I[1].max() == 0
    assert M.match(a, b) == [(0, 0)]                           # one label, one prediction: the duplicate is not a second hit
    assert M.match(a, b, thr=0.3) == [(0, 0)]
    assert M.match([], b) == [] and M.match(a, []) == []


def test_roi_filter_is_the_labelled_ground_area():
    xy = np.array([[0.0, 0.0], [899.0, 2699.0], [950.0, 0.0], [-370.0, 0.0]])
    assert M.in_roi(xy).tolist() == [True, True, False, False]
    assert M.in_roi(xy, margin=60).tolist() == [True, True, True, False]


def _toy(n=4):
    """Two cameras (padded to seven), one person seen by both, one only by the first."""
    gt = []
    for i in range(n):
        per = [[] for _ in M.CAMS]
        per[0] = [(1, [10 + i, 10, 40 + i, 90]), (2, [100, 10, 130, 90])]
        per[1] = [(1, [200, 20, 230, 100])]
        gt.append(per)
    return {"frames": [f"{i * 5:08d}" for i in range(n)], "gt": gt, "scene": [2] * n}


def test_detection_tracking_and_counts_scores():
    D = _toy()
    preds = [[[] for _ in M.CAMS] for _ in D["frames"]]
    for i in range(len(D["frames"])):
        preds[i][0] = [{"box": [10 + i, 10, 40 + i, 90], "conf": 0.9, "id": 7},     # person 1, tracked throughout
                       {"box": [500, 10, 530, 90], "conf": 0.8, "id": 8}]           # a false positive
        preds[i][1] = [{"box": [200, 20, 230, 100], "conf": 0.9, "id": 3 if i < 2 else 4}]   # an identity switch halfway
    d = M.score_detection(D, preds)
    c1 = d["per_camera"][0]
    assert (c1["tp"], c1["fp"], c1["fn"]) == (4, 4, 4) and c1["precision"] == 0.5 and c1["recall"] == 0.5
    assert d["per_camera"][1]["recall"] == 1.0 and d["precision"] == round(8 / 12, 3)
    t = M.score_tracking(D, preds)
    c2 = next(r for r in t["per_camera"] if r["camera"] == "C2")
    assert c2["id_switches"] == 1 and c2["recall"] == 1.0
    assert next(r for r in t["per_camera"] if r["camera"] == "C1")["id_switches"] == 0
    c = M.score_counts(D, preds)
    assert c["truth_mean"] == 2 and c["sum_of_cameras"]["bias"] == 1.0 and c["largest_camera"]["mae"] == 0.0


def test_review_json_marks_matches_and_misses():
    D = _toy(2)
    preds = [[[] for _ in M.CAMS] for _ in D["frames"]]
    tracks = [[[] for _ in M.CAMS] for _ in D["frames"]]
    for i in range(2):
        tracks[i][0] = [{"box": [10 + i, 10, 40 + i, 90], "conf": 0.9, "id": 5}, {"box": [600, 10, 630, 90], "conf": 0.6, "id": 6}]
        preds[i][0] = tracks[i][0]
    R = M.review_json(D, preds, tracks)
    assert R["cameras"] == M.CAMS and R["fps"] == 2 and len(R["instants"]) == 2
    c = R["instants"][1]["cams"][0]
    assert [g[0] for g in c["gt"]] == [1, 2] and c["det"] == 2
    assert c["pred"][0][0] == 5 and c["pred"][0][5] == 1            # track 5 matched person 1
    assert c["pred"][1][5] == -1                                    # track 6 matched no label
    assert json.loads(json.dumps(R)) == R                           # plain JSON, as the page reads it


def test_review_endpoints(app_client, tmp_path, monkeypatch):
    import atlas.desk.app as A
    c = app_client
    c.post("/signup", json={"name": "R", "company": "Review Co", "email": "review@example.com", "password": "password1"})
    c.post("/login", json={"email": "review@example.com", "password": "password1"})
    monkeypatch.setattr(A, "REVIEW_OUT", tmp_path / "out")
    monkeypatch.setattr(A, "REVIEW_DATA", tmp_path / "data")
    assert c.get("/desk/review").status_code == 200
    r = c.get("/api/review/data")
    assert r.status_code == 404 and "mcam-eval" in r.get_json()["error"]
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "review.json").write_text(json.dumps({"cameras": ["C1"], "instants": []}))
    j = c.get("/api/review/data").get_json()
    assert j["review"]["cameras"] == ["C1"] and j["results"] is None
    (tmp_path / "data" / "frames" / "C3").mkdir(parents=True)
    (tmp_path / "data" / "frames" / "C3" / "00000005.jpg").write_bytes(b"\xff\xd8jpeg")
    assert c.get("/api/review/frame/C3/00000005.jpg").data == b"\xff\xd8jpeg"
    for bad in ("/api/review/frame/C8/00000005.jpg", "/api/review/frame/C3/5.jpg", "/api/review/frame/C3/..%2F..%2Fx.jpg"):
        assert c.get(bad).status_code == 404
