# Countersign

**An accounts-payable agent that pays suppliers through PayPal. The model reads, code decides, people approve.**

Small businesses pay freelancers and suppliers from invoices that arrive as PDFs. Reading them is slow, so it is the
first job people hand to an AI agent. It is also where money gets stolen: a supplier's "our PayPal account has
changed" notice, a total quietly above the purchase order, an invoice sent twice. An agent that does what the
document says will pay all three.

Countersign lets an AI model do the reading and keeps the paying under rules and people:

1. **The model reads the invoice PDF** (gpt-oss-120b on Groq by default, or Claude). It reports what the invoice asks for: supplier, invoice number, PO, total,
   currency, and the PayPal address to pay. It proposes; it decides nothing.
2. **A policy written in [Shio](https://github.com/pratikkp24/shio) checks the reading against the buyer's own
   records**: the supplier is in the vendor master, the PayPal address is the one on file, there is an open PO, the
   total is within 2% of it, the invoice was not paid before, and the currency is USD. Shio's compiler refuses any
   code path where the model's output reaches the payment without passing a check or rule.
3. **Every payment waits for people.** A payment over USD 1,000 needs two: an AP approver and a finance controller.
   The agent that prepared a payment cannot approve it, and one person cannot approve twice (enforced in the ledger).
4. **Only then does PayPal move the money**, through the Payouts API on the sandbox, to the address in the vendor
   master, never the one in the invoice. The `sender_batch_id` is derived from the hash of the approved decision record,
   so PayPal itself refuses to pay the same decision twice.
5. **People hear about it by email** (Resend): approvers get an approval request with **Approve** and **Reject**
   buttons (links signed for that person, role and decision record, valid 72 hours; opening one shows a confirmation
   page, so a mail scanner cannot approve), plus a link straight to the invoice, suppliers get remittance advice when PayPal pays, and for a blocked invoice the model drafts a note
   to the supplier that a person edits and sends, always to the address in the vendor master.
6. **Everything is evidence.** Each decision, each approval and the PayPal payout batch are Ed25519-signed,
   hash-chained records. The Ledger tab and `shio verify` check that nothing was edited, removed or reordered.

## The demo inbox

| Invoice | What happens |
|---|---|
| INV-1042 Lena Ortiz Design, USD 1,250 | Clean. Over USD 1,000, so two people approve; PayPal pays it |
| INV-3115 Mara Chen Illustration, USD 480 | Clean. One approval; PayPal pays it |
| INV-0388 Northwind Copy Co., USD 640 | Blocked `PAYEE_CHANGED`: a "new PayPal account" notice, plus a hidden note telling AI assistants to pay it |
| INV-2290 Kestrel Motion, USD 2,180 | Blocked `OVER_PO`: PO-2207 is USD 1,800 |
| INV-0977 Brightline Audio, USD 360 | Blocked `DUPLICATE`: already paid on 14 Oct |

You can also upload your own PDF invoice.

## Settings and connectors

- **Settings > Payment policy**: the PO tolerance and the amount above which a second person approves. Only a finance
  controller can change them; each change is written to the ledger, and every decision records the policy version
  it was made under.
- **Settings > Approvers**: who can approve, as which role, and where their approval requests go. Approving means
  picking a listed person; the agent can never be one.
- **Settings > AI reader**: gpt-oss-120b or gpt-oss-20b on Groq, Claude Sonnet or Opus, or replay.
- **Connectors**: PayPal (test the connection), the AI reader, Email (Resend, with *safe delivery* that sends every
  email to one inbox, marked with who it was meant for), Accounting (import the vendor master and POs from CSV, export
  payments for reconciliation), and the Shio ledger (download it for audit with the public key).

## Run it

You need Python 3.10+, a PayPal developer account (free) and, ideally, a Groq API key (free tier) or an Anthropic API key.

```bash
git clone https://github.com/pratikkp24/countersign && cd countersign
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env.local      # then fill it in, see below
.venv/bin/uvicorn server:app --port 8090
```

Open http://localhost:8090, press **Read all**, open an invoice and approve it.

`.env.local`:

| Variable | Where it comes from |
|---|---|
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | developer.paypal.com > Apps & Credentials > **Sandbox** > Create App, linked to a **US** sandbox business account (keep **Payouts** ticked). Sandbox business accounts in some countries, India among them, cannot send payouts: PayPal answers `PAYOUT_NOT_AVAILABLE` |
| `DEMO_PAYEE_EMAIL` (optional) | developer.paypal.com > Sandbox accounts: the email of any sandbox account you have, personal or business (an account that cannot send payouts can still receive them). The first supplier is paid there, so its payout completes (`SUCCESS`). Other suppliers use `example.com` addresses, which PayPal reports as `UNCLAIMED` (the money waits for the recipient to open an account) |
| `GROQ_API_KEY` | console.groq.com. The default reader is `openai/gpt-oss-120b` on Groq, about one second per invoice |
| `ANTHROPIC_API_KEY` (alternative) | console.anthropic.com, for Claude, which reads the PDF itself rather than its extracted text. With neither key, the app uses a **replay** model that returns recorded readings of the five demo invoices, and says so in the header. The checks, approvals and PayPal payouts still run for real |
| `COUNTERSIGN_MODEL` (optional) | `groq:openai/gpt-oss-120b` (default with a Groq key), `claude-sonnet-5-5`, `claude-opus-5-5`, or `replay` |

**Reset demo** (Vendors & POs tab) restores the inbox and records. The old ledger is moved aside, never edited.

### Check the evidence from the command line

```bash
.venv/bin/shio verify data/records --pubkey data/keys/shio-signing.pub   # chain, signatures, approvals
.venv/bin/shio explain agent/payables.shio -o decision-map.html          # the policy as a decision map
```

## How it is built

| Part | File | What it does |
|---|---|---|
| Policy | [`agent/payables.shio`](agent/payables.shio) | The Shio program: the model's `read`, six `check`s with reason codes, the `rule`, and two `signoff` lines (every payment; a second role above USD 1,000). `regulated` makes every decision a signed record |
| Records | [`agent/erp.py`](agent/erp.py) | Vendor master, purchase orders and payment history (SQLite). These are what the checks trust |
| PayPal | [`agent/paypal.py`](agent/paypal.py) | OAuth client credentials, `POST /v1/payments/payouts`, `GET /v1/payments/payouts/{id}`. Sandbox only, by construction |
| Server | [`server.py`](server.py) | FastAPI: runs the agent, records approvals in the Shio ledger, pays from the signed record (not from the browser's request), polls PayPal, chains the payout into the ledger |
| UI | [`web/`](web) | Inbox, the four steps for each invoice, the ledger with live verification, the buyer's records |
| Demo invoices | [`agent/invoices.py`](agent/invoices.py) | Generates the five PDFs |

### AI tools used

- **gpt-oss-120b on Groq** (`openai/gpt-oss-120b`, Groq's OpenAI-compatible API) reads the text of each invoice PDF
  and returns a typed reading through structured output (JSON schema). The model id is pinned in every decision record.
- The same model drafts the note to a supplier about a blocked invoice (free text, never a decision; a person edits
  and sends it).
- **Claude** (Anthropic API) is supported as the reader too (`COUNTERSIGN_MODEL=claude-sonnet-5-5`); it reads the PDF
  itself as a document.
- **Shio** (open source, Aonami's agent language) keeps the model's reading away from the payment until the checks
  and rule have run, records every decision as a signed, hash-chained record, and enforces the people who approve.
- Claude Code helped write the code.

### PayPal used

- **Payouts API** (sandbox): `POST /v1/payments/payouts` with a `sender_batch_id` bound to the approved decision,
  `GET /v1/payments/payouts/{payout_batch_id}` for the batch and item status, transaction id and fee.
- **OAuth 2.0 client credentials** against `api-m.sandbox.paypal.com`.

## What it does not do yet

- One currency (USD) and one payout per approval. Batching approved invoices into one PayPal batch is the next step.
- The vendor master is a local SQLite file. A real deployment reads it from the accounting system.
- Approvers type their name; there is no login. The ledger can also take each approver's own Ed25519 key
  (`shio signoff --key`), which the UI does not use yet.

## Licence

MIT, see [LICENSE](LICENSE). Shio is MIT or Apache-2.0. Built by Aonami Inc.
