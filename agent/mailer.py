"""Email through Resend: approval requests, notices to suppliers about blocked invoices, and remittance advice.

Safe delivery (on by default): every email goes to one inbox you choose, marked with who it was meant for, so a demo
never mails a supplier. Turn it off in Connectors when the vendor master holds real addresses.
"""
import html
import json
import os
import time

import httpx

from erp import DB

PATH = DB.parent / "connectors.json"
API = "https://api.resend.com/emails"


def load():
    c = json.loads(PATH.read_text()) if PATH.exists() else {}
    email = {"api_key": "", "from": "Countersign <onboarding@resend.dev>", "safe_delivery": True, "deliver_to": "",
             "auto_approval_requests": True, "auto_remittance": True, **c.get("email", {})}
    if not email["api_key"] and os.getenv("RESEND_API_KEY"):
        email["api_key"] = os.environ["RESEND_API_KEY"]
        email["key_from_env"] = True
    if not email["deliver_to"] and os.getenv("COUNTERSIGN_DELIVER_TO"):
        email["deliver_to"] = os.environ["COUNTERSIGN_DELIVER_TO"]
    return {"email": email}


def save_email(patch):
    c = json.loads(PATH.read_text()) if PATH.exists() else {}
    cur = c.get("email", {})
    for k in ("api_key", "from", "deliver_to"):
        if k in patch and patch[k] is not None:
            v = str(patch[k]).strip()
            if k == "api_key" and (not v or "…" in v):
                continue                      # a masked key sent back unchanged
            cur[k] = v
    for k in ("safe_delivery", "auto_approval_requests", "auto_remittance"):
        if k in patch:
            cur[k] = bool(patch[k])
    c["email"] = cur
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(c, indent=1))
    os.chmod(PATH, 0o600)


def public():
    e = load()["email"]
    k = e["api_key"]
    return {**{x: e[x] for x in ("from", "safe_delivery", "deliver_to", "auto_approval_requests", "auto_remittance")},
            "configured": bool(k), "api_key": (k[:5] + "…" + k[-3:]) if k else "", "key_from_env": e.get("key_from_env", False)}


def ready():
    e = load()["email"]
    if not e["api_key"]:
        return False, "Add a Resend API key in Connectors"
    if e["safe_delivery"] and not e["deliver_to"]:
        return False, "Safe delivery is on: set the inbox that receives every email in Connectors"
    return True, ""


def _page(title, intro, rows, body="", button=None, meant_for=None):
    esc = html.escape
    rows_html = "".join(
        f'<tr><td style="padding:6px 0;color:#6b6b6b;width:150px;vertical-align:top">{esc(k)}</td>'
        f'<td style="padding:6px 0;color:#111">{esc(str(v))}</td></tr>' for k, v in rows)
    banner = (f'<div style="background:#F3F1EC;border-radius:8px;padding:10px 14px;font-size:12px;color:#555;'
              f'margin-bottom:18px">Safe delivery: this email was meant for <b>{esc(meant_for)}</b>.</div>') if meant_for else ""
    btn = (f'<p style="margin:22px 0 4px"><a href="{esc(button[1])}" style="background:#111;color:#fff;'
           f'text-decoration:none;padding:10px 18px;border-radius:999px;font-size:14px">{esc(button[0])}</a></p>') if button else ""
    body_html = "".join(f'<p style="margin:0 0 12px;line-height:1.6">{esc(p)}</p>' for p in body.split("\n\n") if p.strip())
    return f"""<!doctype html><html><body style="margin:0;background:#FAF9F7;font-family:Helvetica,Arial,sans-serif">
<div style="max-width:560px;margin:0 auto;padding:32px 24px">
<div style="font-size:15px;color:#111;margin-bottom:22px">&#9673; Countersign</div>{banner}
<div style="background:#fff;border:1px solid #e7e5e0;border-radius:12px;padding:24px">
<h1 style="font-weight:300;font-size:24px;letter-spacing:-0.4px;margin:0 0 8px;color:#111">{esc(title)}</h1>
<p style="margin:0 0 16px;color:#444;line-height:1.6">{esc(intro)}</p>{body_html}
<table style="width:100%;border-collapse:collapse;font-size:14px">{rows_html}</table>{btn}</div>
<p style="font-size:12px;color:#888;margin-top:18px">Sent by Countersign for Harbor &amp; Pine Studio. Payments run on the PayPal sandbox.</p>
</div></body></html>"""


def send(to, subject, title, intro, rows, body="", button=None, kind="email"):
    """-> a log entry: {time, kind, to, delivered_to, subject, id | error}. Never raises for a provider error."""
    e = load()["email"]
    ok, why = ready()
    entry = {"time": time.time(), "kind": kind, "to": to, "subject": subject}
    if not ok:
        return {**entry, "error": why}
    real_to = e["deliver_to"] if e["safe_delivery"] else to
    entry["delivered_to"] = real_to
    meant = to if e["safe_delivery"] and real_to.lower() != str(to).lower() else None
    text = f"{title}\n\n{intro}\n\n{body}\n\n" + "\n".join(f"{k}: {v}" for k, v in rows) + (
        f"\n\n{button[0]}: {button[1]}" if button else "")
    try:
        r = httpx.post(API, timeout=20, headers={"Authorization": f"Bearer {e['api_key']}"},
                       json={"from": e["from"], "to": [real_to], "subject": subject, "text": text,
                             "html": _page(title, intro, rows, body, button, meant)})
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            return {**entry, "error": f"Resend {r.status_code}: {data.get('message') or data}"}
        return {**entry, "id": data.get("id")}
    except Exception as ex:
        return {**entry, "error": f"{type(ex).__name__}: {ex}"}
