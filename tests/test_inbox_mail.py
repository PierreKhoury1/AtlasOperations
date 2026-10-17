"""Inbound mail: machine mail is skipped, attachments are read, replies thread (atlas/integrations.py, inbox_watch)."""
import io
import smtplib
from email.message import EmailMessage

import pytest

import atlas.integrations as I


def _mail(frm="Jane <jane@example.com>", subject="Boiler", body="The boiler is broken.", **headers):
    m = EmailMessage()
    m["From"] = frm; m["To"] = "desk@agency.test"; m["Subject"] = subject
    m["Message-ID"] = headers.pop("message_id", "<abc@example.com>")
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return m


def test_bulk_detection():
    assert I.is_bulk(_mail(), "jane@example.com") == ""
    assert I.is_bulk(_mail(Auto_Submitted="auto-replied"), "jane@example.com") == "auto-submitted"
    assert I.is_bulk(_mail(List_Unsubscribe="<mailto:x>"), "jane@example.com") == "mailing list"
    assert I.is_bulk(_mail(Precedence="bulk"), "jane@example.com") == "bulk precedence"
    assert I.is_bulk(_mail(), "noreply@rightmove.co.uk") == "no-reply sender"
    assert I.is_bulk(_mail(), "no-reply+alerts@zoopla.co.uk") == "no-reply sender"
    assert I.is_bulk(_mail(), "notifications@github.com") == "no-reply sender"
    assert I.is_bulk(_mail(), "info@example.com") == ""          # a real shared inbox is a person


def test_attachments_become_text():
    from pypdf import PdfWriter
    m = _mail()
    m.add_attachment(b"meter reading 1234", maintype="text", subtype="plain", filename="reading.txt")
    w = PdfWriter(); w.add_blank_page(width=200, height=200); buf = io.BytesIO(); w.write(buf)
    m.add_attachment(buf.getvalue(), maintype="application", subtype="pdf", filename="invoice.pdf")
    m.add_attachment(b"\x89PNG....", maintype="image", subtype="png", filename="photo.png")
    atts = I.attachments_of(m)
    assert [a["name"] for a in atts] == ["reading.txt", "invoice.pdf", "photo.png"]
    assert atts[0]["text"] == "meter reading 1234" and atts[2]["text"] == ""
    body = I._with_attachments("The boiler is broken.", atts)
    assert "[3 attachments: reading.txt (text/plain" in body and "--- reading.txt ---\nmeter reading 1234" in body
    assert "photo.png (image/png" in body and "--- photo.png ---" not in body


def test_attachment_text_is_capped(monkeypatch):
    monkeypatch.setattr(I, "ATTACH_TEXT_MAX", 50)
    atts = [{"name": "big.txt", "type": "text/plain", "size": 9000, "text": "x" * 9000}, {"name": "b.txt", "type": "text/plain", "size": 5, "text": "hello"}]
    body = I._with_attachments("hi", atts)
    assert ("x" * 50 + "\n...[truncated]") in body and ("x" * 51) not in body and "--- b.txt ---" not in body


class _SMTP:
    sent = []
    def __init__(self, host, port, timeout=30): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def ehlo(self): pass
    def starttls(self, context=None): pass
    def login(self, u, p): pass
    def send_message(self, msg): _SMTP.sent.append(msg)


def test_reply_threads_under_the_customers_mail(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _SMTP)
    _SMTP.sent.clear()
    cfg = {"host": "smtp.test", "port": 587, "user": "u", "password": "p", "from_email": "desk@agency.test"}
    I.send_email(cfg, "jane@example.com", "Re: Boiler", "On it.", in_reply_to="<abc@example.com>")
    m = _SMTP.sent[-1]
    assert m["In-Reply-To"] == "<abc@example.com>" and m["References"] == "<abc@example.com>" and m["Message-ID"].endswith("@agency.test>")
    I.send_email(cfg, "jane@example.com", "Hello", "Fresh mail")
    assert _SMTP.sent[-1]["In-Reply-To"] is None
    meta = {"in_reply_to": "<abc@example.com>"}
    I.deliver({"kind": "smtp", "config": cfg}, "email", "jane@example.com", "Re: Boiler", "Still on it.", meta=meta)
    assert _SMTP.sent[-1]["In-Reply-To"] == "<abc@example.com>"


class _IMAP:
    boxes = {}
    def __init__(self, host, port): pass
    def login(self, u, p): pass
    def select(self, folder, readonly=False): return "OK", [b"2"]
    def search(self, cs, crit): return "OK", [b" ".join(k.encode() for k in self.boxes)]
    def fetch(self, uid, what): return "OK", [(b"", self.boxes[uid.decode()].as_bytes())]
    def store(self, uid, op, flags): pass
    def logout(self): pass


def test_fetch_unseen_marks_bulk_and_keeps_message_id(monkeypatch):
    import imaplib
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _IMAP)
    _IMAP.boxes = {"1": _mail(message_id="<m1@example.com>"), "2": _mail("Rightmove <noreply@rightmove.co.uk>", "New enquiry", "Someone asked", message_id="<m2@rm>")}
    mails = I.fetch_unseen({"host": "imap.test", "user": "u", "password": "p"})
    assert [m["message_id"] for m in mails] == ["<m1@example.com>", "<m2@rm>"]
    assert mails[0]["bulk"] == "" and mails[1]["bulk"] == "no-reply sender" and mails[0]["attachments"] == []


def test_inbox_watch_skips_machine_mail_and_threads(app_client, monkeypatch):
    import atlas.desk.app as A
    import atlas.desk.scheduler as S
    c = A.app.test_client()
    c.post("/signup", json={"name": "P", "company": "Co", "email": "inbox@example.com", "password": "password1"})
    did = c.post("/api/desks", json={"name": "Mail desk", "template": "lettings"}).get_json()["id"]
    desk = A.store.desk(did)
    A.store.add_connector(did, "imap", "mail", {"host": "imap.test", "user": "u", "password": "p"})
    monkeypatch.setattr(I, "fetch_unseen", lambda cfg, limit=10, mark_seen=True: [
        {"uid": "1", "from_name": "Jane", "from_email": "jane@example.com", "subject": "Boiler", "body": "Broken", "date": "",
         "message_id": "<m1@example.com>", "in_reply_to": "", "bulk": "", "attachments": []},
        {"uid": "2", "from_name": "Rightmove", "from_email": "noreply@rightmove.co.uk", "subject": "Digest", "body": "...", "date": "",
         "message_id": "<m2@rm>", "in_reply_to": "", "bulk": "no-reply sender", "attachments": []}])
    runs = []
    job = {"id": 0, "kind": "inbox_watch", "desk_id": did, "task": "", "name": "Inbox"}
    out = S._run_job(A.store, job, lambda desk, task, lane, lid=None: runs.append(task) or "run-1", lambda d: A.store.desk(d))
    assert "2 new email(s)" in out and "skipped 1 machine mail" in out and "noreply@rightmove.co.uk" in out
    assert len(runs) == 1 and "jane@example.com" in runs[0]
    leads = [l for l in A.store.leads(did) if l.get("email") == "noreply@rightmove.co.uk"]
    assert leads == []
    assert A.store.last_inbound_msg_id(did, "jane@example.com") == "<m1@example.com>"
    assert A.store.last_inbound_msg_id(did, "nobody@example.com") == ""
