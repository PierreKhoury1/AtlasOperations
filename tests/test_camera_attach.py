"""Attaching a feed to a camera from the workspace: upload a recording or image, probe a source, attach it, swap it,
and remove the camera together with its watch job."""
import io
import json

from PIL import Image


def J(r):
    return json.loads(r.data)


def _png() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (320, 180), (40, 90, 160)).save(b, "PNG")
    return b.getvalue()


def _client(app_client):
    import atlas.desk.app as A
    c = A.app.test_client()                                  # its own cookie jar: signed in only once it signs up
    return A, c


def test_camera_feed_endpoints_need_a_session(app_client):
    _, c = _client(app_client)
    assert c.post("/api/cameras/upload", data={"file": (io.BytesIO(_png()), "x.png")}).status_code == 401
    assert c.post("/api/cameras/probe", json={"source": "0"}).status_code == 401


def test_upload_probe_attach_swap_delete(app_client):
    A, c = _client(app_client)
    r = c.post("/signup", json={"name": "Cam", "company": "Cam Shop", "email": "cam-attach@example.com", "password": "password1"})
    assert r.status_code in (200, 302)
    assert c.post("/api/cameras/attach", json={"name": "door", "source": "0"}).status_code == 409   # no desk built yet
    c.post("/api/desks", json={"name": "Cam Shop", "template": "site_watch", "tier": "free"})

    bad = c.post("/api/cameras/upload", data={"file": (io.BytesIO(b"hello"), "notes.txt")}, content_type="multipart/form-data")
    assert bad.status_code == 400 and "not supported" in J(bad)["error"]

    up = J(c.post("/api/cameras/upload", data={"file": (io.BytesIO(_png()), "front door.png")}, content_type="multipart/form-data"))
    assert up["kind"] == "file" and up["source"].endswith(".png") and "front-door" in up["source"]

    pr = J(c.post("/api/cameras/probe", json={"source": up["source"]}))
    assert pr["ok"] and pr["preview"].startswith("data:image/jpeg;base64,")
    miss = J(c.post("/api/cameras/probe", json={"source": "C:/nowhere/missing.mp4"}))
    assert miss["ok"] is False and miss["error"]

    at = J(c.post("/api/cameras/attach", json={"name": "front door", "source": up["source"]}))
    assert at["ok"] and at["camera"]["name"] == "front-door" and at["camera"]["source_kind"] == "file"
    cid = at["camera"]["id"]
    cams = J(c.get("/api/cameras"))["cameras"]
    assert [x["name"] for x in cams] == ["front-door"]
    desk = A.store.desk(cams[0]["desk_id"])

    def watch_jobs():
        return [j for j in A.store.jobs(desk["id"]) if j["kind"] == "camera_watch" and '"connector": "front-door"' in (j["task"] or "")]
    assert len(watch_jobs()) == 1

    up2 = J(c.post("/api/cameras/upload", data={"file": (io.BytesIO(_png()), "door2.jpg")}, content_type="multipart/form-data"))
    sw = J(c.post("/api/cameras/attach", json={"name": "front-door", "cid": cid, "source": up2["source"]}))
    assert sw["ok"] and sw["camera"]["id"] == cid
    assert A.store.connector(cid)["config"]["source"] == up2["source"]
    assert A.store.connector(cid)["config"].get("journal") == "1"          # journal settings kept on a swap
    assert len(watch_jobs()) == 1                                          # no second watch job

    assert J(c.post("/api/cameras/attach", json={"name": "x", "source": ""})).get("error")
    assert J(c.delete(f"/api/connectors/{cid}"))["ok"]
    assert watch_jobs() == []                                              # the watch job goes with the camera


def _owner(A, email):
    c = A.app.test_client()
    A._RL_AUTH._hits.clear()
    assert c.post("/signup", json={"name": "Cam", "company": "Cam Co", "email": email, "password": "password1"}).status_code in (200, 302)
    c.post("/api/desks", json={"name": "Cam Co", "template": "site_watch", "tier": "free"})
    return c


def test_file_sources_are_the_owners_own_uploads(app_client):
    """A file source is the signed-in owner's own upload (or sample footage), never a path someone typed: the probe
    returns a frame of whatever it reads, so a path would read another tenant's upload or crop, or any server file."""
    import atlas.desk.app as A
    a, b = _owner(A, "cam-tenant-a@example.com"), _owner(A, "cam-tenant-b@example.com")
    mine = J(a.post("/api/cameras/upload", data={"file": (io.BytesIO(_png()), "private.png")}, content_type="multipart/form-data"))
    uid_a = A.store.user_by_email("cam-tenant-a@example.com")["id"]
    assert f"u{uid_a}" in mine["source"]                                  # per-account folder
    assert J(a.post("/api/cameras/probe", json={"source": mine["source"]}))["ok"]
    # tenant B: A's upload, a server file, a missing path - one answer for all, and no frame
    for src in (mine["source"], str(A.cfg.DATA_DIR / "desk.db"), str(A.cfg.DATA_DIR / "nope.jpg"),
                str(A.cfg.DATA_DIR / "objects" / "desk1" / "1.jpg")):
        r = b.post("/api/cameras/probe", json={"source": src})
        assert r.status_code == 400 and "refused" in J(r)["error"] and "preview" not in J(r), src
        assert b.post("/api/cameras/attach", json={"name": "peek", "source": src}).status_code == 400
        assert b.post("/api/connectors", json={"kind": "camera", "name": "peek", "config": {"source": src}}).status_code == 400
    assert not any(x["name"] == "peek" for x in J(b.get("/api/cameras"))["cameras"])
    # streams may sit on the LAN, never on this server's loopback or the metadata service
    for src in ("rtsp://127.0.0.1:8114/x", "rtsp://user:pw@localhost/stream", "http://127.0.0.1:9/snap.jpg",
                "rtsp://169.254.169.254/latest", "http://[::1]/x.jpg"):
        r = b.post("/api/cameras/probe", json={"source": src})
        assert r.status_code == 400 and "refused" in J(r)["error"], src
    # B's own upload is fine
    theirs = J(b.post("/api/cameras/upload", data={"file": (io.BytesIO(_png()), "door.png")}, content_type="multipart/form-data"))
    assert J(b.post("/api/cameras/probe", json={"source": theirs["source"]}))["ok"]
    assert J(b.post("/api/cameras/attach", json={"name": "door", "source": theirs["source"]}))["ok"]


def test_upload_size_is_checked_before_the_body_is_read(app_client, monkeypatch):
    import atlas.desk.app as A
    c = _owner(A, "cam-big@example.com")
    monkeypatch.setattr(A, "CAMERA_UPLOAD_MAX", 1024)
    r = c.post("/api/cameras/upload", data={"file": (io.BytesIO(b"\0" * 4096), "big.mp4")}, content_type="multipart/form-data")
    assert r.status_code == 413
    assert A.app.config["MAX_CONTENT_LENGTH"] and A.app.config["MAX_CONTENT_LENGTH"] <= 1024 ** 3   # every body is bounded


def test_stream_rule_also_guards_grabs_and_live_feeds():
    from atlas import vision as V
    assert V.stream_reason("rtsp://127.0.0.1:554/x").startswith("camera source refused")
    assert V.stream_reason("rtsp://192.168.1.20:554/stream1") is None      # a LAN camera is fine
    assert V.stream_reason("C:/videos/x.mp4") is None                       # not a stream: the portal decides files
    try:
        V._grab_rtsp("rtsp://127.0.0.1:9/x")
        raise AssertionError("grab must refuse loopback")
    except RuntimeError as exc:
        assert "refused" in str(exc)


def test_sample_cameras_reopen_on_their_clip(app_client):
    """A camera made from sample footage stores the resolved file; the cameras API names the clip again, so the Feed
    dialog opens on the Sample tab with that clip selected instead of an empty stream field."""
    import pytest

    import atlas.desk.app as A
    from atlas import designer as DS
    clips = DS.sample_clips()
    if not clips:
        pytest.skip("no sample footage installed")
    name = sorted(clips)[0]
    c = _owner(A, "cam-sample@example.com")
    made = J(c.post("/api/connectors", json={"kind": "camera", "name": "demo-cam", "config": {"source": "sample:" + name}}))
    assert made["config"]["source"] == clips[name]                         # stored resolved, as before
    cam = next(x for x in J(c.get("/api/cameras"))["cameras"] if x["name"] == "demo-cam")
    assert cam["sample"] == name
    assert DS.sample_name("sample:" + name) == name and DS.sample_name(clips[name]) == name
    assert DS.sample_name("rtsp://192.168.1.20/x") == "" and DS.sample_name(str(A.cfg.DATA_DIR / (name + ".mp4"))) == ""
