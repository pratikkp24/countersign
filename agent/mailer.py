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


STATUS = {  # kind -> (pill text, pill colour, pill background)
    "approval_request": ("Awaiting your approval", "#8A5A12", "#FBF1DF"),
    "supplier_note": ("On hold", "#9A2A1F", "#FBEAE7"),
    "remittance": ("Paid", "#1F6A4B", "#E6F2EC"),
    "test": ("Connected", "#3F63B8", "#EEF2FB"),
}


def _page(title, intro, rows, body="", button=None, meant_for=None, kind="email", amount=None, eyebrow=""):
    """A table-based HTML email (Gmail, Outlook, Apple Mail), inline styles only, light background, 600 px."""
    esc = html.escape
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
    pill_text, pill_fg, pill_bg = STATUS.get(kind, ("", "#555", "#f1f1f1"))
    pill = (f'<span style="display:inline-block;padding:4px 10px;border-radius:999px;background:{pill_bg};color:{pill_fg};'
            f'font:600 11px {font};letter-spacing:1.2px;text-transform:uppercase">{esc(pill_text)}</span>') if pill_text else ""
    amount_html = (f'<div style="font:300 34px/1.1 {font};letter-spacing:-1px;color:#111;margin:14px 0 2px">'
                   f'{esc(amount)}</div>') if amount else ""
    eyebrow_html = (f'<div style="font:500 11px {font};letter-spacing:2px;text-transform:uppercase;color:#8a8a8a;'
                    f'margin-bottom:8px">{esc(eyebrow)}</div>') if eyebrow else ""
    banner = (f'<tr><td style="padding:0 0 14px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0">'
              f'<tr><td style="background:#F1EFEA;border-radius:10px;padding:10px 14px;font:12px/1.5 {font};color:#555">'
              f'Safe delivery: this email was meant for <b style="color:#111">{esc(meant_for)}</b>.</td></tr></table></td></tr>'
              ) if meant_for else ""
    rows_html = "".join(
        f'<tr><td style="padding:10px 0;border-top:1px solid #EEECE7;color:#8a8a8a;font:13px/1.4 {font};width:38%;'
        f'vertical-align:top">{esc(k)}</td><td style="padding:10px 0;border-top:1px solid #EEECE7;color:#111;'
        f'font:14px/1.4 {font};vertical-align:top;word-break:break-word">{esc(str(v))}</td></tr>' for k, v in rows if v)
    body_html = "".join(f'<p style="margin:0 0 14px;font:15px/1.65 {font};color:#222">{esc(p).replace(chr(10), "<br>")}</p>'
                        for p in body.split("\n\n") if p.strip())
    buttons = [button] if isinstance(button, tuple) else list(button or [])
    styles = {"primary": ("#111", "#ffffff", "#111"), "positive": ("#1F6A4B", "#ffffff", "#1F6A4B"),
              "danger": ("#ffffff", "#9A2A1F", "#E3B8B2"), "ghost": ("#ffffff", "#111", "#D9D6CF")}
    cells = []
    for b in buttons:
        bg, fg, border = styles.get(b[2] if len(b) > 2 else "primary", styles["primary"])
        cells.append(f'<td style="padding:0 8px 8px 0"><table role="presentation" cellspacing="0" cellpadding="0"><tr>'
                     f'<td style="background:{bg};border:1px solid {border};border-radius:999px"><a href="{esc(b[1])}" '
                     f'style="display:inline-block;padding:12px 22px;font:500 14px {font};color:{fg};text-decoration:none">'
                     f'{esc(b[0])}</a></td></tr></table></td>')
    btn = (f'<table role="presentation" cellspacing="0" cellpadding="0" style="margin:24px 0 0"><tr>{"".join(cells)}'
           f'</tr></table>') if cells else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><title>{esc(title)}</title></head>
<body style="margin:0;padding:0;background:#F6F5F2;-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{esc(intro)}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#F6F5F2">
<tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="width:100%;max-width:600px">
<tr><td style="padding:0 4px 20px;font:15px {font};color:#111">
  <span style="display:inline-block;width:14px;height:14px;border:1.5px solid #111;border-radius:50%;vertical-align:-2px;
  text-align:center;line-height:14px"><span style="display:inline-block;width:6px;height:6px;border-radius:50%;
  background:#6487D7;vertical-align:middle"></span></span>&nbsp; Countersign</td></tr>
{banner}
<tr><td style="background:#ffffff;border:1px solid #E8E6E1;border-radius:16px;padding:32px 32px 28px">
  {eyebrow_html}{pill}
  <h1 style="margin:16px 0 8px;font:300 26px/1.2 {font};letter-spacing:-0.5px;color:#111">{esc(title)}</h1>
  <p style="margin:0 0 6px;font:15px/1.6 {font};color:#555">{esc(intro)}</p>
  {amount_html}
  <div style="height:18px"></div>{body_html}
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0">{rows_html}</table>
  {btn}
</td></tr>
<tr><td style="padding:18px 6px 0;font:12px/1.6 {font};color:#9a9a9a">
  Sent by Countersign for Harbor &amp; Pine Studio. The model reads, a policy decides, people approve.<br>
  Payments run on the PayPal sandbox; no real money moves.</td></tr>
</table></td></tr></table></body></html>"""


def send(to, subject, title, intro, rows, body="", button=None, kind="email", amount=None, eyebrow=""):
    """-> a log entry: {time, kind, to, delivered_to, subject, id | error}. Never raises for a provider error."""
    e = load()["email"]
    ok, why = ready()
    entry = {"time": time.time(), "kind": kind, "to": to, "subject": subject}
    if not ok:
        return {**entry, "error": why}
    real_to = e["deliver_to"] if e["safe_delivery"] else to
    entry["delivered_to"] = real_to
    meant = to if e["safe_delivery"] and real_to.lower() != str(to).lower() else None
    links = [button] if isinstance(button, tuple) else list(button or [])
    text = f"{title}\n\n{intro}\n\n{body}\n\n" + "\n".join(f"{k}: {v}" for k, v in rows) + "".join(
        f"\n\n{b[0]}: {b[1]}" for b in links)
    try:
        r = httpx.post(API, timeout=20, headers={"Authorization": f"Bearer {e['api_key']}"},
                       json={"from": e["from"], "to": [real_to], "subject": subject, "text": text,
                             "html": _page(title, intro, rows, body, button, meant, kind, amount, eyebrow)})
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            return {**entry, "error": f"Resend {r.status_code}: {data.get('message') or data}"}
        return {**entry, "id": data.get("id")}
    except Exception as ex:
        return {**entry, "error": f"{type(ex).__name__}: {ex}"}
