"""PayPal REST, sandbox only: OAuth client credentials and the Payouts API.

Credentials come from .env.local (PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET). They are never logged or traced.
"""
import os
import time

import httpx

BASE = os.getenv("PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com")


class PayPalError(RuntimeError):
    def __init__(self, status, body):
        self.status, self.body = status, body
        name = body.get("name") if isinstance(body, dict) else None
        msg = body.get("message") if isinstance(body, dict) else str(body)[:300]
        super().__init__(f"PayPal {status}: {name or ''} {msg or ''}".strip())


class PayPal:
    def __init__(self):
        if "sandbox" not in BASE:
            raise RuntimeError("Countersign only talks to the PayPal sandbox")
        self._token, self._expires = None, 0.0
        self.http = httpx.Client(base_url=BASE, timeout=30)

    @staticmethod
    def configured():
        return bool(os.getenv("PAYPAL_CLIENT_ID") and os.getenv("PAYPAL_CLIENT_SECRET"))

    def token(self):
        if self._token and time.time() < self._expires - 60:
            return self._token
        r = self.http.post("/v1/oauth2/token", data={"grant_type": "client_credentials"},
                           auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]))
        if r.status_code != 200:
            raise PayPalError(r.status_code, _json(r))
        body = r.json()
        self._token, self._expires = body["access_token"], time.time() + int(body.get("expires_in", 3000))
        return self._token

    def _req(self, method, path, **kw):
        headers = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json",
                   **kw.pop("headers", {})}
        r = self.http.request(method, path, headers=headers, **kw)
        if r.status_code >= 400:
            raise PayPalError(r.status_code, _json(r))
        return r.json() if r.content else {}

    # -- Payouts ------------------------------------------------------------------------------------------------------

    def create_payout(self, *, batch_id, item_id, receiver, amount, currency, note, subject):
        """One payout item. batch_id is derived from the signed decision record, so PayPal itself refuses a second
        payout for the same decision (sender_batch_id must be unique)."""
        body = {
            "sender_batch_header": {"sender_batch_id": batch_id, "email_subject": subject,
                                    "email_message": note, "recipient_type": "EMAIL"},
            "items": [{"recipient_type": "EMAIL", "amount": {"value": f"{amount:.2f}", "currency": currency},
                       "receiver": receiver, "note": note, "sender_item_id": item_id,
                       "recipient_wallet": "PAYPAL"}],
        }
        return self._req("POST", "/v1/payments/payouts", json=body,
                         headers={"PayPal-Request-Id": batch_id})

    def get_payout(self, payout_batch_id):
        return self._req("GET", f"/v1/payments/payouts/{payout_batch_id}")


def _json(r):
    try:
        return r.json()
    except Exception:
        return r.text
