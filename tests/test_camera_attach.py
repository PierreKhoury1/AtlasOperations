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
