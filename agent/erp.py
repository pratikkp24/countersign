"""The buyer's own records: vendor master, purchase orders and payment history, in SQLite.

These are the trusted records the policy checks an invoice against. An invoice is untrusted text: anyone can send one.
"""
import os
import sqlite3
import time
from pathlib import Path

DB = Path(os.getenv("COUNTERSIGN_DATA", Path(__file__).resolve().parent.parent / "data")) / "erp.db"


def demo_payee():
    """The PayPal account the demo's first vendor is paid at. Set DEMO_PAYEE_EMAIL to a sandbox personal account
    (developer.paypal.com > Sandbox accounts) so its payout lands in a real sandbox wallet and shows as completed."""
    return (os.getenv("DEMO_PAYEE_EMAIL") or "lena.ortiz.design@example.com").strip()


def vendors():
    # name, PayPal email on file, category
    return [
        ("Lena Ortiz Design", demo_payee(), "Brand design"),
        ("Mara Chen Illustration", "mara.chen.art@example.com", "Illustration"),
        ("Northwind Copy Co.", "accounts@northwindcopy.example.com", "Copywriting"),
        ("Kestrel Motion", "pay@kestrelmotion.example.com", "Motion graphics"),
        ("Brightline Audio", "studio@brightlineaudio.example.com", "Sound design"),
    ]


POS = [
    # po, vendor, amount (USD), what for
    ("PO-2201", "Lena Ortiz Design", 1250.00, "Spring campaign identity, 25 hours"),
    ("PO-2203", "Mara Chen Illustration", 480.00, "Six spot illustrations"),
    ("PO-2204", "Northwind Copy Co.", 640.00, "Website copy, 8 pages"),
    ("PO-2207", "Kestrel Motion", 1800.00, "Product launch animation, 30 s"),
    ("PO-2196", "Brightline Audio", 360.00, "Podcast intro and outro"),
]

PAID = [
    # vendor, invoice_no, amount, when (already paid last month, so a resubmission is a duplicate)
    ("Brightline Audio", "INV-0977", 360.00, "2026-10-14"),
]


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    fresh = not DB.exists()
    db = sqlite3.connect(DB, check_same_thread=False)
    db.row_factory = sqlite3.Row
    if fresh:
        seed(db)
    return db


def seed(db):
    db.executescript("""
        DROP TABLE IF EXISTS vendors; DROP TABLE IF EXISTS purchase_orders; DROP TABLE IF EXISTS payments;
        CREATE TABLE vendors (name TEXT PRIMARY KEY, paypal_email TEXT, category TEXT);
        CREATE TABLE purchase_orders (po TEXT PRIMARY KEY, vendor TEXT, amount REAL, item TEXT, status TEXT);
        CREATE TABLE payments (vendor TEXT, invoice_no TEXT, amount REAL, paid_on TEXT, payout_batch_id TEXT,
                               PRIMARY KEY (vendor, invoice_no));
    """)
    db.executemany("INSERT INTO vendors VALUES (?, ?, ?)", vendors())
    db.executemany("INSERT INTO purchase_orders VALUES (?, ?, ?, ?, 'open')", POS)
    db.executemany("INSERT INTO payments VALUES (?, ?, ?, ?, NULL)", PAID)
    db.commit()


class ERP:
    """What the Shio checks call. Every method reads the buyer's own records."""

    def __init__(self, db=None):
        self.db = db or connect()

    def vendor(self, name):
        r = self.db.execute("SELECT * FROM vendors WHERE lower(name) = lower(?)", (str(name or ""),)).fetchone()
        return dict(r) if r else None

    def payee(self, name):
        v = self.vendor(name)
        return v["paypal_email"] if v else None

    def open_po(self, po, vendor):
        r = self.db.execute("SELECT * FROM purchase_orders WHERE po = ? AND lower(vendor) = lower(?) AND status = 'open'",
                            (str(po or ""), str(vendor or ""))).fetchone()
        return dict(r) if r else None

    def po_amount(self, po, vendor):
        p = self.open_po(po, vendor)
        return p["amount"] if p else 0.0

    def already_paid(self, vendor, invoice_no):
        r = self.db.execute("SELECT 1 FROM payments WHERE lower(vendor) = lower(?) AND upper(invoice_no) = upper(?)",
                            (str(vendor or ""), str(invoice_no or ""))).fetchone()
        return r is not None

    # -- writes, made only after a person approved and PayPal accepted the payout ------------------------------------

    def record_payment(self, vendor, invoice_no, amount, po, batch_id):
        self.db.execute("INSERT OR REPLACE INTO payments VALUES (?, ?, ?, ?, ?)",
                        (vendor, invoice_no, amount, time.strftime("%Y-%m-%d"), batch_id))
        self.db.execute("UPDATE purchase_orders SET status = 'paid' WHERE po = ?", (po,))
        self.db.commit()

    def snapshot(self):
        q = lambda sql: [dict(r) for r in self.db.execute(sql).fetchall()]
        return {"vendors": q("SELECT * FROM vendors ORDER BY name"),
                "purchase_orders": q("SELECT * FROM purchase_orders ORDER BY po"),
                "payments": q("SELECT * FROM payments ORDER BY paid_on DESC")}


# -- the accounting connector: CSV in, CSV out ------------------------------------------------------------------------

import csv  # noqa: E402
import io  # noqa: E402

VENDOR_COLS = ["name", "paypal_email", "category"]
PO_COLS = ["po", "vendor", "amount", "item"]


def _rows(text, cols):
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    head = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in cols if c not in head and c not in ("category", "item")]
    if missing:
        raise ValueError(f"the CSV needs the columns {', '.join(cols)} (missing {', '.join(missing)})")
    for r in reader:
        yield {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}


def import_vendors(erp, text):
    n = 0
    for r in _rows(text, VENDOR_COLS):
        if not r.get("name") or "@" not in r.get("paypal_email", ""):
            continue
        erp.db.execute("INSERT INTO vendors VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                       "paypal_email = excluded.paypal_email, category = excluded.category",
                       (r["name"], r["paypal_email"].lower(), r.get("category", "")))
        n += 1
    erp.db.commit()
    return n


def import_pos(erp, text):
    n = 0
    for r in _rows(text, PO_COLS):
        try:
            amount = float(r.get("amount", "").replace(",", ""))
        except ValueError:
            continue
        if not r.get("po") or not r.get("vendor"):
            continue
        erp.db.execute("INSERT INTO purchase_orders VALUES (?, ?, ?, ?, 'open') ON CONFLICT(po) DO UPDATE SET "
                       "vendor = excluded.vendor, amount = excluded.amount, item = excluded.item",
                       (r["po"], r["vendor"], amount, r.get("item", "")))
        n += 1
    erp.db.commit()
    return n


def payments_csv(erp):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["paid_on", "vendor", "invoice_no", "amount", "currency", "paypal_payout_batch_id"])
    for p in erp.db.execute("SELECT * FROM payments ORDER BY paid_on DESC").fetchall():
        w.writerow([p["paid_on"], p["vendor"], p["invoice_no"], f"{p['amount']:.2f}", "USD", p["payout_batch_id"] or ""])
    return out.getvalue()


def template(kind):
    if kind == "vendors":
        return "name,paypal_email,category\nAcme Print Co.,billing@acmeprint.example.com,Printing\n"
    return "po,vendor,amount,item\nPO-2300,Acme Print Co.,950.00,Spring catalogue print run\n"
