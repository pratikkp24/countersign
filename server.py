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
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import shio  # noqa: E402
import shio.models  # noqa: E402
from shio import ledger  # noqa: E402
from shio.core import _signer, last_run  # noqa: E402

import erp as erp_mod  # noqa: E402
import invoices  # noqa: E402
from paypal import PayPal, PayPalError  # noqa: E402

DATA = erp_mod.DB.parent
RECORDS = DATA / "records"
CASES = DATA / "cases.json"
KEYS = DATA / "keys"
AGENT = "countersign-agent"          # the requester and maker of every decision: it can never sign one off
MODEL = os.getenv("COUNTERSIGN_MODEL") or (
    "claude-sonnet-5-5" if os.getenv("ANTHROPIC_API_KEY")
    else "groq:openai/gpt-oss-120b" if os.getenv("GROQ_API_KEY")
    else "replay")
ROLES = {"ap_approver": "AP approver", "controller": "Finance controller"}
CHECKS = {
    "known_vendor": "Supplier is in the vendor master",
    "payee_on_file": "Pays the PayPal account on file",
    "open_po": "Matches an open purchase order",
    "within_po": "Total within the PO (+2%)",
    "not_paid": "Not already paid",
    "usd": "Currency is USD",
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
    if MODEL == "replay":
        return replay_model()
    if MODEL.startswith("groq:"):
        # Groq's OpenAI-compatible endpoint. The model reads the PDF's extracted text; the id is pinned in each record.
        name = MODEL.removeprefix("groq:")
        return shio.models.openai_compat(name, "https://api.groq.com/openai/v1", key_env="GROQ_API_KEY", pinned=name,
                                         timeout=60)
    return shio.models.anthropic(MODEL, max_tokens=2000)


def replay_model():
    """No Anthropic key? `COUNTERSIGN_MODEL=replay` answers with what Claude read from each demo invoice, so the rest
    of the flow (checks, sign-off, PayPal) still runs for real. The UI says which model read each invoice."""
    from shio.models import replay
    known = {}
    for s in invoices.specs():
        total = sum(q * r for _, q, r in s["lines"])
        known[s["no"]] = {"vendor": s["vendor"], "invoice_no": s["no"], "po": s["po"], "amount": total,
                          "currency": "USD", "pay_to": s["pay_to"], "summary": s["lines"][0][0]}

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
    return {
        "config": {"paypal": PayPal.configured(), "model": MODEL,
                   "anthropic": MODEL == "replay" or bool(os.getenv("GROQ_API_KEY") if MODEL.startswith("groq:") else os.getenv("ANTHROPIC_API_KEY")),
                   "agent": AGENT, "roles": ROLES, "dual_limit": PROGRAM.DUAL_LIMIT, "tolerance": PROGRAM.TOLERANCE},
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
            out = PROGRAM.pay_invoice(doc, ERP, AGENT, model())
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
            checks.append({"name": c["name"], "label": CHECKS.get(c["name"], c["name"]), "code": c.get("code"),
                           "passed": c["passed"], "skipped": bool(c.get("skipped")),
                           "detail": reasons.get(c.get("code"), "")})
        case.update({
            "status": "PENDING" if out.get("status") == "PENDING" else decided.get("action", "BLOCK"),
            "reading": reading.get("value"), "read_by": reading.get("model") or MODEL,
            "read_error": reading.get("error"), "checks": checks, "decided": decided,
            "record": record.name, "record_sha256": ledger._record_entry(ledger._entries(RECORDS), record.name)["sha256"],
            "seconds": round(time.time() - started, 1), "ran_at": time.time(),
        })
        if case["status"] == "PENDING":
            case["signoff"] = signoff_view(record)
        cases[cid] = case
        save_cases(cases)
        return case


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
        return case


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
    return refresh(cid)


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
    return {"ok": True}


@app.exception_handler(HTTPException)
def http_error(_, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/invoices", StaticFiles(directory=invoices.FOLDER), name="invoices")
app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
