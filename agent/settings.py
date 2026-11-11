"""Workspace settings: the payment policy, the people who approve, and which model reads invoices.

Stored in data/settings.json. Policy changes are also written to the Shio ledger (agent `set_policy`), so an auditor sees
which limits each decision was made under and who changed them.
"""
import json
import time

from erp import DB

PATH = DB.parent / "settings.json"
ROLES = {"ap_approver": "AP approver", "controller": "Finance controller"}
CURRENCIES = ["USD"]

DEFAULT = {
    "company": "Harbor & Pine Studio",
    "policy": {"tolerance": 0.02, "dual_limit": 1000, "currency": "USD", "version": 1},
    "approvers": [
        {"name": "Priya Nair", "email": "priya@harborpine.example.com", "roles": ["ap_approver", "controller"]},
        {"name": "Dana Whitfield", "email": "dana@harborpine.example.com", "roles": ["ap_approver"]},
        {"name": "Sam Ortega", "email": "sam@harborpine.example.com", "roles": ["controller"]},
    ],
    "reader": None,           # None: the best model a key is set for
    "auto_refresh": True,     # poll PayPal while a payout is pending
    "history": [],            # policy changes: {time, by, before, after}
}


def load():
    if PATH.exists():
        s = json.loads(PATH.read_text())
        return {**DEFAULT, **s, "policy": {**DEFAULT["policy"], **s.get("policy", {})}}
    return json.loads(json.dumps(DEFAULT))


def save(s):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(s, indent=1))


def validate_policy(p):
    out = {}
    try:
        out["tolerance"] = round(float(p["tolerance"]), 4)
        out["dual_limit"] = round(float(p["dual_limit"]), 2)
    except (KeyError, TypeError, ValueError):
        raise ValueError("tolerance and the second-approval limit must be numbers")
    if not 0 <= out["tolerance"] <= 0.25:
        raise ValueError("PO tolerance must be between 0% and 25%")
    if not 0 <= out["dual_limit"] <= 1_000_000:
        raise ValueError("the second-approval limit must be between 0 and 1,000,000")
    out["currency"] = str(p.get("currency", "USD")).upper()
    if out["currency"] not in CURRENCIES:
        raise ValueError(f"currency must be one of {', '.join(CURRENCIES)}")
    return out


def validate_approvers(people, agent):
    clean, seen = [], set()
    for a in people or []:
        name = " ".join(str(a.get("name", "")).split())
        roles = [r for r in a.get("roles", []) if r in ROLES]
        if not name:
            continue
        if name.lower() == agent.lower():
            raise ValueError(f"{agent} prepares payments, so it cannot be an approver")
        if name.lower() in seen:
            raise ValueError(f"{name} is listed twice")
        if not roles:
            raise ValueError(f"{name} needs at least one role")
        seen.add(name.lower())
        clean.append({"name": name, "email": str(a.get("email", "")).strip(), "roles": roles})
    for r, label in ROLES.items():
        if not any(r in a["roles"] for a in clean):
            raise ValueError(f"at least one person must be able to approve as {label}")
    if not any("controller" in a["roles"] for a in clean) or len(clean) < 2:
        raise ValueError("payments above the limit need two different people, so list at least two approvers")
    return clean


def record_change(s, by, before, after):
    s["history"] = ([{"time": time.time(), "by": by, "before": before, "after": after}] + s.get("history", []))[:50]
