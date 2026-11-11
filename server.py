"""Countersign: an accounts-payable agent that pays suppliers through PayPal, where the model reads, code decides and
people approve.

    .venv/bin/uvicorn server:app --port 8090      # then open http://localhost:8090

Flow for one invoice:
  1. Claude reads the PDF (a Shio `read`): supplier, invoice no., PO, total, currency, the PayPal address to pay.
  2. Shio checks compare that reading with the buyer's own records. The rule decides BLOCK or PAY.
  3. A PAY is PENDING until the people its `signoff` lines name have signed it in the ledger (maker-checker enforced,
     two people above $1,000).
  4. Only then does the server create the PayPal payout, to the address in the vendor master (never the invoice's),
     with a sender_batch_id derived from the signed record, so PayPal refuses a second payout of the same decision.
  5. PayPal's batch id and status are chained into the same signed ledger. `shio verify data/records` checks it all.
"""
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
try:  # python.org builds on macOS ship without a CA bundle; urllib (used for Groq) then fails TLS
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass
import urllib.request  # noqa: E402

_opener = urllib.request.build_opener()   # Groq's edge refuses urllib's default User-Agent (Cloudflare 1010)
_opener.addheaders = [("User-Agent", "countersign/0.1 (+https://github.com/pratikkp24/countersign)")]
urllib.request.install_opener(_opener)
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "agent"))

from fastapi import FastAPI, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import shio  # noqa: E402
import shio.models  # noqa: E402
from shio import ledger  # noqa: E402
from shio.core import _signer, last_run  # noqa: E402

import erp as erp_mod  # noqa: E402
import invoices  # noqa: E402
import mailer  # noqa: E402
import settings as cfg  # noqa: E402
from paypal import PayPal, PayPalError  # noqa: E402

DATA = erp_mod.DB.parent
RECORDS = DATA / "records"
CASES = DATA / "cases.json"
KEYS = DATA / "keys"
AGENT = "countersign-agent"          # the requester and maker of every decision: it can never sign one off
APP_URL = os.getenv("COUNTERSIGN_URL", "http://localhost:8090")
ROLES = cfg.ROLES
READERS = [  # (id, label, which key it needs)
    ("groq:openai/gpt-oss-120b", "gpt-oss-120b on Groq", "GROQ_API_KEY"),
    ("groq:openai/gpt-oss-20b", "gpt-oss-20b on Groq (faster)", "GROQ_API_KEY"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5", "ANTHROPIC_API_KEY"),
    ("claude-opus-5-5", "Claude Opus 5.5", "ANTHROPIC_API_KEY"),
    ("replay", "Replay (recorded readings, no model)", None),
]


def readers():
    return [{"id": i, "label": l, "available": k is None or bool(os.getenv(k))} for i, l, k in READERS]


def current_model():
    """The reader chosen in Settings if its key is set, else COUNTERSIGN_MODEL, else the best one a key exists for."""
    chosen = cfg.load().get("reader")
    if chosen and any(r["id"] == chosen and r["available"] for r in readers()):
        return chosen
    return os.getenv("COUNTERSIGN_MODEL") or next(r["id"] for r in readers() if r["available"])


def check_labels(policy):
    return {
        "known_vendor": "Supplier is in the vendor master",
        "payee_on_file": "Pays the PayPal account on file",
        "open_po": "Matches an open purchase order",
        "within_po": f"Total within the PO (+{policy['tolerance']:.0%})",
        "not_paid": "Not already paid",
        "usd": f"Currency is {policy['currency']}",
    }

lock = threading.RLock()


# -- setup ------------------------------------------------------------------------------------------------------------

def ensure_keys():
    if not os.getenv("SHIO_SIGNING_KEY"):
        key = KEYS / "shio-signing.key"
        if not key.exists():
            ledger.keygen(str(KEYS))
        os.environ["SHIO_SIGNING_KEY"] = str(key)


ensure_keys()
invoices.ensure()
shio.configure(trace_dir=str(RECORDS), actor=AGENT)
PROGRAM = shio.load(ROOT / "agent" / "payables.shio")
ERP = erp_mod.ERP()
PAYPAL = PayPal()


def model():
    m = current_model()
    if m == "replay":
        return replay_model()
    if m.startswith("groq:"):
        # Groq's OpenAI-compatible endpoint. The model reads the PDF's extracted text; the id is pinned in each record.
        name = m.removeprefix("groq:")
        return shio.models.openai_compat(name, "https://api.groq.com/openai/v1", key_env="GROQ_API_KEY", pinned=name,
                                         timeout=60)
    return shio.models.anthropic(m, max_tokens=2000)


def write_text(system, prompt):
    """Free text from the reader model (supplier messages). Not a decision: nothing here moves money."""
    m = current_model()
    if m == "replay":
        return None
    if m.startswith("groq:"):
        import httpx
        r = httpx.post("https://api.groq.com/openai/v1/chat/completions", timeout=60,
                       headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}",
                                "User-Agent": "countersign/0.1"},
                       json={"model": m.removeprefix("groq:"), "temperature": 0.3,
                             "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    import anthropic
    msg = anthropic.Anthropic().messages.create(model=m, max_tokens=800, system=system,
                                               messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def replay_model():
    """No Anthropic key? `COUNTERSIGN_MODEL=replay` answers with what Claude read from each demo invoice, so the rest
    of the flow (checks, sign-off, PayPal) still runs for real. The UI says which model read each invoice."""
    from shio.models import replay
    known = {}
    for s in invoices.specs():
        total = sum(q * r for _, q, r in s["lines"])
        known[s["no"]] = {"vendor": s["vendor"], "invoice_no": s["no"], "po": s["po"], "amount": total,
                          "currency": "USD", "pay_to": s["pay_to"], "summary": s["lines"][0][0], "due_date": s["due"]}

    def answer(req):
        text = " ".join((src.text or "") for src in req.sources)
        return next((v for k, v in known.items() if k in text), None)
    return replay(answer, name="replay")


# -- cases: one per invoice in the inbox -------------------------------------------------------------------------------

def load_cases():
    if CASES.exists():
        return json.loads(CASES.read_text())
    return {}


def save_cases(cases):
    CASES.write_text(json.dumps(cases, indent=1, default=str))


def sync_inbox():
    with lock:
        cases = load_cases()
        for p in sorted(invoices.FOLDER.glob("*.pdf"), key=lambda p: p.stat().st_mtime):
            cid = re.sub(r"[^A-Za-z0-9_-]", "_", p.stem)
            if cid not in cases:
                cases[cid] = {"id": cid, "file": p.name, "received": time.time(), "status": "NEW"}
        save_cases(cases)
        return cases


def case_or_404(cid):
    cases = load_cases()
    if cid not in cases:
        raise HTTPException(404, "no such invoice")
    return cases


def signoff_view(record_path):
    req, state, entries = ledger.status(record_path)
    signed = [{"signer": e["signer"], "role": e["role"], "decision": e["decision"], "time": e["time"],
               "sha256": e.get("sha256")} for e in entries]
    needed = [r for r in (req or {}).get("roles", []) if r not in {s["role"] for s in signed}]
    return {"state": state, "roles": (req or {}).get("roles", []), "because": (req or {}).get("because", []),
            "signed": signed, "waiting_for": needed}


# -- the API ----------------------------------------------------------------------------------------------------------

app = FastAPI(title="Countersign")


@app.get("/api/state")
def state():
    cases = sync_inbox()
    s = cfg.load()
    m = current_model()
    return {
        "config": {"paypal": PayPal.configured(), "model": m,
                   "model_label": next((r["label"] for r in readers() if r["id"] == m), m),
                   "anthropic": True, "agent": AGENT, "roles": ROLES, "policy": s["policy"],
                   "dual_limit": s["policy"]["dual_limit"], "tolerance": s["policy"]["tolerance"],
                   "approvers": s["approvers"], "company": s["company"], "auto_refresh": s["auto_refresh"],
                   "email": mailer.public()},
        "cases": sorted(cases.values(), key=lambda c: c["received"]),
        "erp": ERP.snapshot(),
    }


@app.post("/api/cases/{cid}/run")
def run_case(cid: str):
    """Claude reads the invoice; Shio checks and decides. Nothing is paid here."""
    with lock:
        cases = case_or_404(cid)
        case = cases[cid]
        doc = shio.Source.from_pdf(invoices.FOLDER / case["file"], name=case["file"])
        started = time.time()
        try:
            policy = cfg.load()["policy"]
            out = PROGRAM.pay_invoice(doc, ERP, AGENT, policy, model())
        except Exception as e:
            raise HTTPException(502, f"the agent run failed: {type(e).__name__}: {e}")
        run = last_run["run"]
        record = Path(PROGRAM.pay_invoice.last_trace)
        d = run.to_dict()
        reading = d["readings"][0] if d["readings"] else {}
        decided = out.get("decided", out) if isinstance(out, dict) else {}
        reasons = dict(zip(decided.get("codes") or [], decided.get("reasons") or []))
        checks = []
        for c in run.checks:
            checks.append({"name": c["name"], "label": check_labels(policy).get(c["name"], c["name"]), "code": c.get("code"),
                           "passed": c["passed"], "skipped": bool(c.get("skipped")),
                           "detail": reasons.get(c.get("code"), "")})
        case.update({
            "status": "PENDING" if out.get("status") == "PENDING" else decided.get("action", "BLOCK"),
            "reading": reading.get("value"), "read_by": reading.get("model") or current_model(),
            "read_error": reading.get("error"), "checks": checks, "decided": decided,
            "record": record.name, "record_sha256": ledger._record_entry(ledger._entries(RECORDS), record.name)["sha256"],
            "seconds": round(time.time() - started, 1), "ran_at": time.time(),
        })
        if case["status"] == "PENDING":
            case["signoff"] = signoff_view(record)
        case.pop("draft", None)
        cases[cid] = case
        save_cases(cases)
    if case["status"] == "PENDING" and mailer.load()["email"]["auto_approval_requests"] and mailer.ready()[0]:
        email_approvers(cid)
    return load_cases()[cid]


class SignIn(BaseModel):
    name: str
    role: str
    approve: bool = True
    note: str = ""


@app.post("/api/cases/{cid}/signoff")
def sign(cid: str, body: SignIn):
    """A person signs (or rejects) a PENDING decision in the Shio ledger. When the last required role signs, the
    payout goes to PayPal."""
    with lock:
        cases = case_or_404(cid)
        case = cases[cid]
        if case.get("status") not in ("PENDING",):
            raise HTTPException(409, f"this invoice is {case.get('status')}, not waiting for sign-off")
        person = next((a for a in cfg.load()["approvers"] if a["name"].lower() == body.name.strip().lower()), None)
        if body.name.strip().lower() == AGENT.lower():
            raise HTTPException(403, plain_refusal("requested or made this decision", AGENT, body.role))
        if person is None:
            raise HTTPException(403, f"{body.name.strip()} is not in the approver list (Settings > Approvers)")
        if body.role not in person["roles"]:
            raise HTTPException(403, f"{person['name']} cannot approve as {ROLES.get(body.role, body.role)} (Settings > Approvers)")
        meaning = body.note.strip() or ("approved for payment via PayPal Payouts" if body.approve else "rejected")
        try:
            ledger.signoff(RECORDS / case["record"], body.name.strip(), body.role, meaning, _signer(),
                           reject=not body.approve)
        except ledger.SignoffRefused as e:
            raise HTTPException(403, plain_refusal(str(e), body.name.strip(), body.role))
        view = signoff_view(RECORDS / case["record"])
        case["signoff"] = view
        if view["state"] == "REJECTED":
            case["status"] = "REJECTED"
        elif view["state"] == "APPROVED":
            case["status"] = "APPROVED"
            cases[cid] = case
            save_cases(cases)
            return pay(cid)
        cases[cid] = case
        save_cases(cases)
    if view["state"] == "PENDING" and mailer.load()["email"]["auto_approval_requests"] and mailer.ready()[0]:
        email_approvers(cid)
    return load_cases()[cid]


def plain_refusal(msg, who, role):
    """The ledger's refusal, in the app's words. The ledger's own text stays in the API's `detail`."""
    if "requested or made this decision" in msg:
        return f"{who} prepared this payment, so {who} can't approve it. Maker-checker: someone else approves."
    if "has already signed" in msg:
        return f"{who} has already approved this payment. A different person must approve as {ROLES.get(role, role)}."
    return msg.replace("sign-off", "approval")


def pay(cid):
    """Create the payout from the signed record, never from anything the browser sent."""
    with lock:
        cases = load_cases()
        case = cases[cid]
        path = RECORDS / case["record"]
        req, st, _ = ledger.status(path)
        if st != "APPROVED":
            raise HTTPException(409, "the decision is not approved in the ledger")
        entry = ledger._record_entry(ledger._entries(RECORDS), path.name)
        rec = json.loads(path.read_text())
        if ledger.record_hash(rec) != entry["sha256"]:
            raise HTTPException(409, "the decision record has changed since it was signed: not paying")
        d = req["decided"]
        if d.get("action") != "PAY":
            raise HTTPException(409, "the signed decision is not a payment")
        if ERP.already_paid(d["vendor"], d["invoice"]):
            raise HTTPException(409, f"{d['invoice']} from {d['vendor']} is already paid")
        if not PayPal.configured():
            case["status"] = "APPROVED"
            case["payout_error"] = "PayPal sandbox credentials are not set in .env.local"
            cases[cid] = case
            save_cases(cases)
            return case
        batch_id = "CS-" + entry["sha256"][:24]
        try:
            res = PAYPAL.create_payout(batch_id=batch_id, item_id=f"{d['invoice']}"[:30], receiver=d["pay_to"],
                                       amount=float(d["amount"]), currency=d["currency"],
                                       note=f"{d['invoice']} / {d['po']} from Harbor & Pine Studio",
                                       subject=f"Payment for {d['invoice']}")
        except PayPalError as e:
            case["payout_error"] = str(e)
            cases[cid] = case
            save_cases(cases)
            raise HTTPException(502, str(e))
        hdr = res.get("batch_header", {})
        payout = {"payout_batch_id": hdr.get("payout_batch_id"), "batch_status": hdr.get("batch_status"),
                  "sender_batch_id": batch_id, "amount": d["amount"], "currency": d["currency"],
                  "receiver": d["pay_to"], "created": time.time()}
        ERP.record_payment(d["vendor"], d["invoice"], d["amount"], d["po"], payout["payout_batch_id"])
        PROGRAM.settle(path.name, entry["sha256"], payout)
        payout["settlement_record"] = Path(PROGRAM.settle.last_trace).name
        case.update({"status": "PAID", "payout": payout})
        case.pop("payout_error", None)
        cases[cid] = case
        save_cases(cases)
    out = refresh(cid)
    if mailer.load()["email"]["auto_remittance"] and mailer.ready()[0]:
        send_remittance(cid)
        out = load_cases()[cid]
    return out


@app.post("/api/cases/{cid}/pay")
def retry_pay(cid: str):
    return pay(cid)


@app.post("/api/cases/{cid}/refresh")
def refresh(cid: str):
    """Ask PayPal where the payout is now."""
    with lock:
        cases = case_or_404(cid)
        case = cases[cid]
        p = case.get("payout")
        if not p or not PayPal.configured():
            return case
        try:
            res = PAYPAL.get_payout(p["payout_batch_id"])
        except PayPalError as e:
            raise HTTPException(502, str(e))
        hdr = res.get("batch_header", {})
        item = (res.get("items") or [{}])[0]
        p.update({"batch_status": hdr.get("batch_status"), "item_status": item.get("transaction_status"),
                  "transaction_id": item.get("transaction_id"), "payout_item_id": item.get("payout_item_id"),
                  "fee": (item.get("payout_item_fee") or {}).get("value"), "checked": time.time()})
        case["payout"] = p
        cases[cid] = case
        save_cases(cases)
        return case


@app.post("/api/upload")
async def upload(file: UploadFile):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename or "invoice.pdf").name)
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "upload a PDF invoice")
    data = await file.read()
    if not data.startswith(b"%PDF") or len(data) > 10_000_000:
        raise HTTPException(400, "that is not a PDF under 10 MB")
    (invoices.FOLDER / name).write_bytes(data)
    sync_inbox()
    return {"ok": True}


@app.get("/api/ledger")
def ledger_view():
    entries = ledger._entries(RECORDS)
    pub = KEYS / "shio-signing.pub"
    ok, lines, head = ledger.verify(RECORDS, ledger.load_public_key(pub) if pub.exists() else None) if entries else (
        None, ["no decisions yet"], None)
    rows = []
    for e in entries:
        row = {"kind": e.get("kind", "record"), "file": e.get("file"), "seq": e.get("seq"),
               "sha256": e.get("sha256"), "prev": e.get("prev"), "time": e.get("time")}
        if row["kind"] == "signoff":
            row.update({"signer": e.get("signer"), "role": e.get("role"), "decision": e.get("decision")})
        else:
            try:
                rec = json.loads((RECORDS / e["file"]).read_text())
                row.update({"agent": rec.get("agent"), "outcome": rec.get("outcome"), "time": rec.get("started")})
            except Exception:
                pass
        rows.append(row)
    return {"verified": ok, "report": lines, "head": head, "entries": rows}


@app.post("/api/reset")
def reset():
    """Start the demo over: a fresh inbox and ERP. The old ledger is kept, moved aside, never edited."""
    with lock:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if RECORDS.exists() and any(RECORDS.iterdir()):
            shutil.move(str(RECORDS), str(DATA / f"records-archive-{stamp}"))
        CASES.unlink(missing_ok=True)
        ERP.db.close()
        erp_mod.DB.unlink(missing_ok=True)
        globals()["ERP"] = erp_mod.ERP()
        for p in invoices.FOLDER.glob("*.pdf"):
            p.unlink()
        invoices.ensure(force=True)
        sync_inbox()
        st = cfg.load()                  # the policy goes back to its defaults; people, reader and email stay
        st["policy"], st["history"] = dict(cfg.DEFAULT["policy"]), []
        cfg.save(st)
    return {"ok": True}


# -- email ------------------------------------------------------------------------------------------------------------

def log_email(cid, entry):
    with lock:
        cases = load_cases()
        cases[cid].setdefault("emails", []).insert(0, entry)
        save_cases(cases)
    return entry


def usd(n):
    return f"USD {float(n):,.2f}"


def email_approvers(cid):
    """Ask each person who can approve a waiting role. -> the log entries."""
    case = load_cases()[cid]
    d, so = case.get("decided") or {}, case.get("signoff") or {}
    waiting = so.get("waiting_for", [])
    signed = {s["signer"].lower() for s in so.get("signed", [])}
    out = []
    for a in cfg.load()["approvers"]:
        roles = [r for r in a["roles"] if r in waiting]
        if not roles or a["name"].lower() in signed or not a.get("email"):
            continue
        role = ROLES[roles[0]]
        rows = [("Supplier", d.get("vendor")), ("Invoice", d.get("invoice")), ("PO", d.get("po")),
                ("Amount", usd(d.get("amount", 0))), ("Pays", f"{d.get('pay_to')} (PayPal account on file)"),
                ("Checks", f"{sum(1 for c in case.get('checks', []) if c['passed'])} of {len(case.get('checks', []))} passed"),
                ("You approve as", role)]
        if so.get("signed"):
            rows.append(("Already approved by", ", ".join(f"{s['signer']} ({ROLES.get(s['role'], s['role'])})" for s in so["signed"])))
        out.append(log_email(cid, mailer.send(
            a["email"], f"Approve: {d.get('invoice')} · {d.get('vendor')} · {usd(d.get('amount', 0))}",
            f"{a['name'].split()[0]}, a payment needs your approval",
            f"The agent read {d.get('invoice')} and every policy check passed. Nothing is paid until the people "
            f"listed approve it.", rows, button=("Review and approve", f"{APP_URL}/#case={cid}"),
            kind="approval_request", amount=usd(d.get("amount", 0)),
            eyebrow=f"{d.get('invoice')} · {d.get('vendor')}")))
    return out


def send_remittance(cid):
    case = load_cases()[cid]
    d, p = case.get("decided") or {}, case.get("payout") or {}
    to = ERP.payee(d.get("vendor"))
    rows = [("Invoice", d.get("invoice")), ("Your PO", d.get("po")), ("Amount", usd(p.get("amount", 0))),
            ("Paid to", f"{to} (PayPal)"), ("PayPal batch", p.get("payout_batch_id"))]
    if p.get("transaction_id"):
        rows.append(("PayPal transaction", p["transaction_id"]))
    return log_email(cid, mailer.send(
        to, f"Payment sent: {d.get('invoice')} ({usd(p.get('amount', 0))})", "Your invoice has been paid",
        f"{cfg.load()['company']} has paid {d.get('invoice')} through PayPal.", rows, kind="remittance",
        amount=usd(p.get("amount", 0)), eyebrow=f"Remittance advice · {d.get('invoice')}"))


ADVICE = {
    "PAYEE_CHANGED": "We never change payment details because an invoice or email asks us to. We will call the "
                     "contact we have on file to confirm any change before paying.",
    "OVER_PO": "Please send a corrected invoice within the purchase order, or ask your contact to raise a change order.",
    "DUPLICATE": "Our records show this invoice was already paid; the remittance went to the PayPal account on file.",
    "NO_OPEN_PO": "Please quote an open purchase order number from us.",
    "UNKNOWN_VENDOR": "You are not set up as a supplier yet; please contact your project lead to be added.",
    "NOT_USD": "Please invoice in the currency on your purchase order.",
}


@app.post("/api/cases/{cid}/draft")
def draft(cid: str):
    """The model drafts a short note to the supplier about why an invoice is on hold. A person edits and sends it."""
    case = case_or_404(cid)[cid]
    if case.get("status") not in ("BLOCK", "REJECTED"):
        raise HTTPException(409, "only blocked or rejected invoices get a supplier note")
    d, r = case.get("decided") or {}, case.get("reading") or {}
    vendor = d.get("vendor") or r.get("vendor")
    to = ERP.payee(vendor)
    reasons = [f"{c['code']}: {c['detail'] or c['label']}" for c in case.get("checks", []) if not c["passed"] and not c["skipped"]]
    codes = [c["code"] for c in case.get("checks", []) if not c["passed"] and not c["skipped"]]
    facts = (f"Supplier: {vendor}\nInvoice: {r.get('invoice_no')}\nPO: {r.get('po')}\nAmount: {r.get('amount')}\n"
             f"Why it is on hold:\n- " + "\n- ".join(reasons) + "\nWhat we need:\n- " +
             "\n- ".join(ADVICE.get(c, "") for c in codes if c in ADVICE))
    body = None
    try:
        body = write_text(
            "You write short, polite emails from an accounts-payable team to a supplier. Plain text, no subject line, "
            "no markdown, under 120 words, signed 'Accounts Payable, " + cfg.load()["company"] + "'. State the facts "
            "given and what the supplier should do. Never ask for bank or PayPal details by email, never accuse, and "
            "never include an address or link that is not in the facts.", facts)
    except Exception:
        body = None
    if not body:
        body = (f"Hello,\n\nThank you for invoice {r.get('invoice_no')}. We have put it on hold:\n- " +
                "\n- ".join(reasons) + "\n\n" + " ".join(ADVICE.get(c, "") for c in codes) +
                f"\n\nAccounts Payable, {cfg.load()['company']}")
    out = {"to": to, "subject": f"{r.get('invoice_no')}: on hold", "body": body,
           "note": None if to else f"{vendor} is not in the vendor master, so there is no address on file to write to."}
    with lock:
        cases = load_cases()
        cases[cid]["draft"] = out
        save_cases(cases)
    return out


class EmailIn(BaseModel):
    kind: str
    subject: str = ""
    body: str = ""


@app.post("/api/cases/{cid}/email")
def email(cid: str, body: EmailIn):
    case = case_or_404(cid)[cid]
    ok, why = mailer.ready()
    if not ok:
        raise HTTPException(409, why)
    if body.kind == "approval_request":
        if case.get("status") != "PENDING":
            raise HTTPException(409, "this invoice is not waiting for approval")
        sent = email_approvers(cid)
        if not sent:
            raise HTTPException(409, "no approver with an email address is waiting on this invoice")
    elif body.kind == "remittance":
        if case.get("status") != "PAID":
            raise HTTPException(409, "this invoice has not been paid")
        sent = [send_remittance(cid)]
    elif body.kind == "supplier_note":
        r = case.get("reading") or {}
        to = ERP.payee((case.get("decided") or {}).get("vendor") or r.get("vendor"))
        if not to:                      # the address on the invoice is never used
            raise HTTPException(409, "this supplier is not in the vendor master, so there is no address on file")
        if not body.body.strip():
            raise HTTPException(400, "the message is empty")
        sent = [log_email(cid, mailer.send(to, body.subject or f"{r.get('invoice_no')}: on hold", "Your invoice is on hold",
                                           f"About {r.get('invoice_no')}", [("Invoice", r.get("invoice_no")),
                                                                            ("Your PO", r.get("po"))],
                                           body=body.body, kind="supplier_note",
                                           eyebrow=f"{cfg.load()['company']} · Accounts payable"))]
    else:
        raise HTTPException(400, "unknown email")
    errors = [s["error"] for s in sent if s.get("error")]
    if errors:
        raise HTTPException(502, errors[0])
    return load_cases()[cid]


# -- settings ---------------------------------------------------------------------------------------------------------

class SettingsIn(BaseModel):
    by: str = ""
    policy: dict | None = None
    approvers: list | None = None
    reader: str | None = None
    auto_refresh: bool | None = None
    company: str | None = None


@app.get("/api/settings")
def get_settings():
    s = cfg.load()
    return {**s, "readers": readers(), "current_reader": current_model(), "roles": ROLES, "agent": AGENT}


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    with lock:
        s = cfg.load()
        try:
            if body.policy is not None:
                new = cfg.validate_policy(body.policy)
                before = {k: s["policy"][k] for k in ("tolerance", "dual_limit", "currency")}
                if new != before:
                    who = body.by.strip()
                    if not any(a["name"].lower() == who.lower() and "controller" in a["roles"] for a in s["approvers"]):
                        raise ValueError("a finance controller from the approver list must make policy changes")
                    after = {**new, "version": s["policy"]["version"] + 1}
                    PROGRAM.set_policy({**before, "version": s["policy"]["version"]}, after, who)
                    cfg.record_change(s, who, before, new)
                    s["policy"] = after
            if body.approvers is not None:
                s["approvers"] = cfg.validate_approvers(body.approvers, AGENT)
            if body.reader is not None:
                if not any(r["id"] == body.reader and r["available"] for r in readers()):
                    raise ValueError("that model is not available: its API key is not set")
                s["reader"] = body.reader
            if body.auto_refresh is not None:
                s["auto_refresh"] = body.auto_refresh
            if body.company:
                s["company"] = body.company.strip()[:80]
        except ValueError as e:
            raise HTTPException(400, str(e))
        cfg.save(s)
    return get_settings()


# -- connectors -------------------------------------------------------------------------------------------------------

@app.get("/api/connectors")
def connectors():
    cid_ = os.getenv("PAYPAL_CLIENT_ID", "")
    entries = ledger._entries(RECORDS)
    pub = KEYS / "shio-signing.pub"
    return {
        "paypal": {"configured": PayPal.configured(), "environment": "sandbox", "base": PAYPAL.http.base_url.host,
                   "client_id": (cid_[:6] + "…" + cid_[-4:]) if cid_ else "", "apis": ["OAuth 2.0", "Payouts"]},
        "reader": {"model": current_model(), "readers": readers()},
        "email": mailer.public(),
        "accounting": {"vendors": len(ERP.snapshot()["vendors"]), "purchase_orders": len(ERP.snapshot()["purchase_orders"]),
                       "payments": len(ERP.snapshot()["payments"])},
        "ledger": {"entries": len(entries), "key": ledger.key_id(ledger.load_public_key(pub)) if pub.exists() else None},
    }


class EmailConn(BaseModel):
    api_key: str | None = None
    from_: str | None = None
    deliver_to: str | None = None
    safe_delivery: bool | None = None
    auto_approval_requests: bool | None = None
    auto_remittance: bool | None = None


@app.put("/api/connectors/email")
def put_email(body: EmailConn):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if "from_" in patch:
        patch["from"] = patch.pop("from_")
    if patch.get("deliver_to") and "@" not in patch["deliver_to"]:
        raise HTTPException(400, "the safe-delivery inbox must be an email address")
    mailer.save_email(patch)
    return connectors()


@app.post("/api/connectors/{name}/test")
def test_connector(name: str):
    t = time.time()
    try:
        if name == "paypal":
            if not PayPal.configured():
                raise RuntimeError("set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in .env.local")
            PAYPAL._token = None
            PAYPAL.token()
            msg = "Signed in to the PayPal sandbox (OAuth client credentials)"
        elif name == "reader":
            m = current_model()
            if m == "replay":
                msg = "Replay model: no API call"
            else:
                out = write_text("Answer with one word.", "Say ready.")
                msg = f"{m} answered: {out[:40]}"
        elif name == "email":
            e = mailer.send(mailer.load()["email"]["deliver_to"] or "", "Countersign: email is connected",
                            "Email is connected", "Countersign can now send approval requests, supplier notes and "
                            "remittance advice.", [("Sent from", "Connectors > Email (Resend)"),
                                                   ("Safe delivery", "On" if mailer.load()["email"]["safe_delivery"] else "Off")],
                            kind="test", eyebrow="Connectors")
            if e.get("error"):
                raise RuntimeError(e["error"])
            msg = f"Test email sent to {e['delivered_to']}"
        else:
            raise HTTPException(404, "no such connector")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")
    return {"ok": True, "message": msg, "ms": int((time.time() - t) * 1000)}


@app.post("/api/import/{kind}")
async def import_csv(kind: str, file: UploadFile):
    text = (await file.read()).decode("utf-8", errors="replace")
    try:
        n = erp_mod.import_vendors(ERP, text) if kind == "vendors" else erp_mod.import_pos(ERP, text) if kind == "pos" else None
    except ValueError as e:
        raise HTTPException(400, str(e))
    if n is None:
        raise HTTPException(404, "import vendors or pos")
    return {"ok": True, "imported": n}


@app.get("/api/export/payments.csv")
def export_payments():
    return Response(erp_mod.payments_csv(ERP), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=countersign-payments.csv"})


@app.get("/api/templates/{kind}.csv")
def csv_template(kind: str):
    return Response(erp_mod.template(kind), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={kind}-template.csv"})


@app.get("/api/ledger/download")
def download_ledger():
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(RECORDS.glob("*")) if RECORDS.exists() else []:
            z.write(f, f"records/{f.name}")
        pub = KEYS / "shio-signing.pub"
        if pub.exists():
            z.write(pub, "shio-signing.pub")
        z.writestr("VERIFY.txt", "pip install shio-lang\nshio verify records --pubkey shio-signing.pub\n")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=countersign-ledger.zip"})


@app.exception_handler(HTTPException)
def http_error(_, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/invoices", StaticFiles(directory=invoices.FOLDER), name="invoices")
app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
