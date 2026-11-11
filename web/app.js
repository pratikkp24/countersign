const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (n) => (n == null || isNaN(n) ? "" : "USD " + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const short = (h) => (h ? h.slice(0, 12) : "");
const when = (t) => (t ? new Date(t * 1000).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");

let S = { cases: [], config: {}, erp: {} };
let selected = null;
let view = "inbox";
const running = new Set();
let pollTimer = null;

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}
const post = (p, b) => api(p, { method: "POST", body: JSON.stringify(b || {}) });
const put = (p, b) => api(p, { method: "PUT", body: JSON.stringify(b || {}) });

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("on");
  clearTimeout(toast.h);
  toast.h = setTimeout(() => t.classList.remove("on"), 4200);
}

const STATUS = {
  NEW: ["New", ""],
  BLOCK: ["Blocked", "neg"],
  PENDING: ["Awaiting approval", "warn"],
  REJECTED: ["Rejected", "neg"],
  APPROVED: ["Approved", "acc"],
  PAID: ["Paid", "pos"],
};
const roleName = (k) => S.config.roles?.[k] || k;

function chip(status, c) {
  if (running.has(c?.id)) return `<span class="chip">Reading</span>`;
  const [label, cls] = STATUS[status] || [status, ""];
  let extra = "";
  if (status === "PENDING" && c?.signoff) extra = ` ${c.signoff.signed.length}/${c.signoff.roles.length}`;
  return `<span class="chip ${cls}">${label}${extra}</span>`;
}
const vendorOf = (c) => c.reading?.vendor || c.file.replace(/^INV-\d+_/, "").replace(/\.pdf$/i, "").replace(/_/g, " ");
const invOf = (c) => c.reading?.invoice_no || (c.file.match(/^(INV-\d+)/) || [])[1] || c.file;

function dueText(d) {
  if (!d || !/^\d{4}-\d{2}-\d{2}$/.test(d)) return "";
  const days = Math.round((new Date(d + "T00:00:00") - new Date(new Date().toDateString())) / 86400000);
  const label = days < 0 ? `overdue ${-days}d` : days === 0 ? "due today" : `due in ${days}d`;
  return `<span class="due ${days <= 3 ? "soon" : ""}">${label}</span>`;
}

async function load() {
  S = await api("/api/state");
  renderEnv();
  renderList();
  if (selected) renderDetail();
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (!S.config.auto_refresh) return;
  const live = S.cases.filter((c) => c.payout && /PENDING|PROCESSING|NEW/.test(c.payout.item_status || c.payout.batch_status || ""));
  if (!live.length) return;
  pollTimer = setTimeout(async () => {
    for (const c of live) await post(`/api/cases/${c.id}/refresh`).catch(() => {});
    await load();
  }, 4000);
}

function renderEnv() {
  const c = S.config;
  $("#env").innerHTML =
    `<span class="chip ${c.paypal ? "ok" : "bad"}"><span class="dot"></span>PayPal sandbox ${c.paypal ? "connected" : "not set"}</span>` +
    `<span class="chip ok"><span class="dot"></span>${esc(c.model_label)}</span>` +
    `<span class="chip ${c.email?.configured ? "ok" : ""}"><span class="dot"></span>Email ${c.email?.configured ? "on" : "off"}</span>` +
    `<span class="chip"><span class="dot"></span>Policy v${c.policy?.version} in Shio</span>`;
}

function renderList() {
  const waiting = S.cases.filter((c) => c.status === "PENDING").length;
  const blocked = S.cases.filter((c) => c.status === "BLOCK").length;
  const due = S.cases.filter((c) => c.status === "PENDING").reduce((a, c) => a + (c.decided?.amount || 0), 0);
  $("#count").textContent = `${S.cases.length} in inbox · ${waiting} awaiting approval${waiting ? ` (${usd(due)})` : ""} · ${blocked} blocked`;
  $("#cases").innerHTML = S.cases
    .map((c) => {
      const amt = c.reading?.amount;
      return `<li role="option" data-id="${esc(c.id)}" class="${c.id === selected ? "on" : ""}" tabindex="0" aria-selected="${c.id === selected}">
        <div class="v">${esc(vendorOf(c))}</div><div class="amt num">${amt != null ? usd(amt) : ""}</div>
        <div class="m">${esc(invOf(c))} · ${c.reading?.due_date && !["PAID", "BLOCK", "REJECTED"].includes(c.status) ? dueText(c.reading.due_date) : `received ${when(c.received)}`}</div><div class="st">${chip(c.status, c)}</div></li>`;
    })
    .join("");
}

function stepHead(n, title, sub, done = true) {
  return `<div class="step-h"><span class="n ${done ? "" : "idle"}">${n}</span><h3>${title}</h3>${sub ? `<span class="sub">${sub}</span>` : ""}</div>`;
}

function approveForm(c) {
  const so = c.signoff;
  const people = (S.config.approvers || []).filter((a) => a.roles.some((r) => so.waiting_for.includes(r)));
  if (!people.length) return `<p class="err">No one in Settings > Approvers can approve the remaining role.</p>`;
  const saved = localStorage.getItem("cs-person");
  const first = people.find((p) => p.name === saved) || people[0];
  return `<p class="note">Approving sends <b class="num">${usd(c.decided.amount)}</b> to <b>${esc(c.decided.pay_to)}</b> through PayPal Payouts once every role above has approved. The agent that prepared it cannot approve it, and one person cannot approve twice.</p>
    <div class="form">
      <select id="who" aria-label="Approving person">${people.map((p) => `<option ${p === first ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>
      <select id="role" aria-label="Approving as"></select>
      <div class="row"><button class="btn" data-act="approve">Approve</button><button class="btn neg small" data-act="reject">Reject</button>
      ${S.config.email?.configured ? `<button class="linkish" data-act="email-approvers">Email the approvers</button>` : ""}</div>
    </div><div class="err" id="sign-err"></div>`;
}

function fillRoles() {
  const c = S.cases.find((x) => x.id === selected);
  const who = $("#who")?.value;
  if (!c?.signoff || !who) return;
  const p = (S.config.approvers || []).find((a) => a.name === who);
  const roles = (p?.roles || []).filter((r) => c.signoff.waiting_for.includes(r));
  $("#role").innerHTML = roles.map((r) => `<option value="${esc(r)}">${esc(roleName(r))}</option>`).join("");
}

function emailsCard(c) {
  const list = c.emails || [];
  if (!list.length) return "";
  const kind = { approval_request: "Approval request", supplier_note: "Note to supplier", remittance: "Remittance advice", test: "Test" };
  return `<div class="card">${stepHead("@", "Emails", `${list.length} sent`)}<ul class="mail">${list
    .map(
      (e) => `<li>${e.error ? `<span class="chip neg">Not sent</span>` : `<span class="chip pos">Sent</span>`} ${esc(kind[e.kind] || e.kind)} to <b>${esc(e.to)}</b>
      <div class="sub">${esc(e.subject)} · ${when(e.time)}${e.delivered_to && e.delivered_to !== e.to ? ` · safe delivery to ${esc(e.delivered_to)}` : ""}${e.error ? ` · ${esc(e.error)}` : ""}</div></li>`
    )
    .join("")}</ul></div>`;
}

function supplierCard(c) {
  if (!["BLOCK", "REJECTED"].includes(c.status)) return "";
  const d = c.draft;
  let inner = `<p class="note">Let the supplier know why it is on hold. The note goes to the address in your vendor master, never to one printed on the invoice.</p><button class="btn ghost small" data-act="draft">Draft a note with AI</button>`;
  if (d) {
    inner = d.to
      ? `<div class="field">To<input value="${esc(d.to)}" disabled aria-label="To"></div>
         <div class="field">Subject<input id="d-subj" value="${esc(d.subject)}"></div>
         <div class="field">Message<textarea id="d-body">${esc(d.body)}</textarea></div>
         <div class="btns">${S.config.email?.configured ? `<button class="btn small" data-act="send-note">Send to supplier</button>` : `<span class="muted small">Connect email in Connectors to send it.</span>`}
         <button class="btn ghost small" data-act="copy-note">Copy</button><button class="linkish" data-act="draft">Draft again</button></div>`
      : `<p class="err">${esc(d.note)}</p><div class="field">Message<textarea id="d-body">${esc(d.body)}</textarea></div><button class="btn ghost small" data-act="copy-note">Copy</button>`;
  }
  return `<div class="card">${stepHead("✉", "Tell the supplier", "", !!d)}${inner}</div>`;
}

function renderDetail() {
  const c = S.cases.find((x) => x.id === selected);
  if (!c) return;
  const r = c.reading || {};
  const isRunning = running.has(c.id);
  const payeeBad = c.checks?.some((k) => k.name === "payee_on_file" && !k.passed && !k.skipped);

  let read = `<div class="card">${stepHead(1, "Agent reads the invoice", c.reading ? `${esc(c.read_by)} · ${c.seconds}s` : "", !!c.reading)}`;
  if (isRunning) read += `<div class="busy">The model is reading the PDF…</div>`;
  else if (!c.ran_at) read += `<p class="note">The model reads the PDF and reports what it asks for. It proposes; it decides nothing.</p><button class="btn" data-act="run">Read with agent</button>`;
  else if (!c.reading) read += `<div class="err">The model's answer could not be read${c.read_error ? `: ${esc(c.read_error)}` : ""}. Nothing can be paid from it.</div><div class="btns"><button class="btn small" data-act="run">Try again</button></div>`;
  else
    read += `<dl class="kv">
      <dt>Supplier</dt><dd>${esc(r.vendor)}</dd>
      <dt>Invoice</dt><dd>${esc(r.invoice_no)}</dd>
      <dt>PO</dt><dd>${esc(r.po)}</dd>
      <dt>Total</dt><dd class="num">${esc(r.currency)} ${Number(r.amount).toLocaleString("en-US", { minimumFractionDigits: 2 })}</dd>
      <dt>Due</dt><dd>${esc(r.due_date || "not stated")} ${dueText(r.due_date)}</dd>
      <dt>Pay to</dt><dd class="${payeeBad ? "flag" : ""}">${esc(r.pay_to)}</dd>
      <dt>For</dt><dd>${esc(r.summary)}</dd></dl>`;
  read += `</div>`;

  let checks = "";
  if (c.checks) {
    const rows = c.checks
      .map((k) => {
        const icon = k.skipped ? `<span class="skip" title="Not run: an earlier check failed"></span>` : k.passed ? `<span class="tick">✓</span>` : `<span class="cross">✕</span>`;
        return `<li>${icon}<span>${esc(k.label)}${k.skipped ? ` <span class="muted small">not run</span>` : ""}</span><span class="code">${esc(k.code)}</span>${!k.passed && !k.skipped && k.detail ? `<span class="why">${esc(k.detail)}</span>` : ""}</li>`;
      })
      .join("");
    const d = c.decided || {};
    const verdict =
      c.status === "BLOCK"
        ? `<div class="verdict block">Blocked. Nothing will be paid. The reason codes above are what the supplier and the AP team see.</div>`
        : `<div class="verdict pay">Decision: pay <b class="num">${usd(d.amount)}</b> to <b>${esc(d.pay_to)}</b>, the PayPal account in the vendor master. ${c.status === "PENDING" ? "It waits for people to approve." : c.status === "REJECTED" ? "People rejected it, so nothing was paid." : "People approved it."}</div>`;
    checks = `<div class="card">${stepHead(2, "Policy checks it against your records", `Shio · policy v${esc(d.policy_version || S.config.policy?.version)}`)}<ul class="checks">${rows}</ul>${verdict}</div>`;
  }

  let appr = "";
  if (c.signoff) {
    const so = c.signoff;
    const lines = so.roles
      .map((role) => {
        const s = so.signed.find((x) => x.role === role);
        return `<li>${s ? (s.decision === "approved" ? `<span class="tick">✓</span>` : `<span class="cross">✕</span>`) : `<span class="skip"></span>`}<span>${esc(roleName(role))}</span><span class="who">${s ? `${esc(s.signer)} · ${s.decision} · ${when(s.time)}` : "waiting"}</span></li>`;
      })
      .join("");
    const why = so.because.map((b) => (b.startsWith("amount >") ? `over ${usd(c.decided.dual_limit ?? S.config.dual_limit)}, so a second person approves` : "every payment needs a person")).join("; ");
    appr = `<div class="card">${stepHead(3, "People approve", why, so.state !== "PENDING")}<ul class="approvals">${lines}</ul>${c.status === "PENDING" ? approveForm(c) : ""}</div>`;
  }

  let pp = "";
  if (c.status === "PAID" || c.status === "APPROVED" || c.payout_error) {
    const p = c.payout;
    if (p) {
      const st = p.item_status || p.batch_status;
      const cls = /SUCCESS/.test(st) ? "pos" : /FAILED|DENIED|BLOCKED|RETURNED/.test(st) ? "neg" : /UNCLAIMED|PENDING|PROCESSING/.test(st) ? "warn" : "";
      const live = /PENDING|PROCESSING/.test(st) && S.config.auto_refresh;
      const sentRemit = (c.emails || []).some((e) => e.kind === "remittance" && !e.error);
      pp = `<div class="card">${stepHead(4, "PayPal moves the money", "Payouts API · sandbox")}
        <dl class="kv">
          <dt>Status</dt><dd><span class="chip ${cls}">${esc(st)}</span>${live ? ` <span class="muted small">checking PayPal every few seconds</span>` : ""}</dd>
          <dt>Amount</dt><dd class="num">${usd(p.amount)}${p.fee ? ` <span class="muted">(PayPal fee ${esc(p.fee)})</span>` : ""}</dd>
          <dt>To</dt><dd>${esc(p.receiver)}</dd>
          <dt>Batch</dt><dd class="mono">${esc(p.payout_batch_id)}</dd>
          ${p.transaction_id ? `<dt>Transaction</dt><dd class="mono">${esc(p.transaction_id)}</dd>` : ""}
          <dt>Your batch id</dt><dd class="mono hash" title="Derived from the approved record, so PayPal refuses a second payout of the same decision">${esc(p.sender_batch_id)}</dd>
        </dl>
        ${st === "UNCLAIMED" ? `<p class="note" style="margin-top:10px">UNCLAIMED: no sandbox PayPal account uses this address yet, so PayPal holds the money until the supplier opens one (or the payout is returned).</p>` : ""}
        <div class="btns" style="margin-top:12px"><button class="btn ghost small" data-act="refresh">Check PayPal status</button>
        ${S.config.email?.configured && !sentRemit ? `<button class="btn ghost small" data-act="remit">Email remittance to supplier</button>` : ""}</div></div>`;
    } else if (c.payout_error) {
      pp = `<div class="card">${stepHead(4, "PayPal moves the money", "", false)}<div class="err">${esc(c.payout_error)}</div><div class="btns"><button class="btn small" data-act="pay">Try the payout again</button></div></div>`;
    }
  }

  const audit = c.record
    ? `<p class="muted small" style="margin:4px 2px 0">Decision record <span class="mono">${esc(c.record)}</span> · sha256 <span class="mono">${short(c.record_sha256)}</span> · signed into the ledger${c.payout?.settlement_record ? ", with the payout chained after it" : ""}.</p>`
    : "";
  const actions = c.ran_at && !isRunning && ["BLOCK", "REJECTED"].includes(c.status) ? `<button class="btn ghost small" data-act="run">Read again</button>` : "";

  $("#detail").innerHTML = `
    <div class="d-head">
      <div><div class="eyebrow">${esc(invOf(c))} · ${esc(c.file)}</div><h2>${esc(vendorOf(c))}</h2></div>
      <div style="text-align:right"><div class="big num">${r.amount != null ? usd(r.amount) : ""}</div><div style="margin-top:6px;display:flex;gap:8px;justify-content:flex-end;align-items:center">${actions}${chip(c.status, c)}</div></div>
    </div>
    <div class="d-grid">
      <div class="pdf" id="pdf" role="img" aria-label="Invoice ${esc(invOf(c))} from ${esc(vendorOf(c))}"><a class="pdf-open" href="/invoices/${encodeURIComponent(c.file)}" target="_blank" rel="noopener">Open PDF</a></div>
      <div class="steps">${read}${checks}${supplierCard(c)}${appr}${pp}${emailsCard(c)}${audit}</div>
    </div>`;
  fillRoles();
  drawPdf(c.file);
}

const pdfCache = {};
async function drawPdf(file) {
  const box = $("#pdf");
  if (!box || !window.pdfjsLib) return;
  try {
    pdfCache[file] ||= pdfjsLib.getDocument(`/invoices/${encodeURIComponent(file)}`).promise;
    const page = await (await pdfCache[file]).getPage(1);
    const scale = (box.clientWidth / page.getViewport({ scale: 1 }).width) * (window.devicePixelRatio || 1);
    const vp = page.getViewport({ scale });
    const cv = document.createElement("canvas");
    cv.width = vp.width;
    cv.height = vp.height;
    await page.render({ canvasContext: cv.getContext("2d"), viewport: vp }).promise;
    if ($("#pdf") !== box) return;
    box.querySelector("canvas")?.remove();
    box.prepend(cv);
  } catch (e) {
    box.insertAdjacentHTML("afterbegin", `<p class="muted small" style="padding:16px">The PDF preview could not be drawn.</p>`);
  }
}

async function runCase(id) {
  running.add(id);
  renderList();
  if (selected === id) renderDetail();
  try {
    const c = await post(`/api/cases/${id}/run`);
    const i = S.cases.findIndex((x) => x.id === id);
    S.cases[i] = c;
    const mailed = (c.emails || []).filter((e) => e.kind === "approval_request" && !e.error).length;
    toast(c.status === "BLOCK" ? `${c.reading?.invoice_no || "Invoice"} blocked: ${c.checks.filter((k) => !k.passed && !k.skipped).map((k) => k.code).join(", ")}` : c.status === "PENDING" ? `${c.reading?.invoice_no} is waiting for approval${mailed ? ` · ${mailed} approver${mailed > 1 ? "s" : ""} emailed` : ""}` : "Read");
  } catch (e) {
    toast(e.message);
  } finally {
    running.delete(id);
    await load();
  }
}

async function sign(approve) {
  const who = $("#who").value;
  const role = $("#role").value;
  $("#sign-err").textContent = "";
  if (!role) {
    $("#sign-err").textContent = `${who} cannot approve the remaining role.`;
    return;
  }
  localStorage.setItem("cs-person", who);
  const btns = document.querySelectorAll("[data-act=approve],[data-act=reject]");
  btns.forEach((b) => (b.disabled = true));
  try {
    const c = await post(`/api/cases/${selected}/signoff`, { name: who, role, approve });
    toast(c.status === "PAID" ? `Paid: PayPal batch ${c.payout.payout_batch_id}` : c.status === "REJECTED" ? "Rejected. Nothing will be paid." : `Approved by ${who}. Waiting for the next approver.`);
  } catch (e) {
    $("#sign-err").textContent = e.message;
    btns.forEach((b) => (b.disabled = false));
    return;
  }
  await load();
}

async function act(name, el) {
  const id = selected;
  try {
    if (name === "run") return runCase(id);
    if (name === "approve") return sign(true);
    if (name === "reject") return sign(false);
    if (name === "refresh" || name === "pay") {
      await post(`/api/cases/${id}/${name}`);
      toast(name === "pay" ? "Payout sent to PayPal" : "Status updated from PayPal");
    }
    if (name === "draft") {
      el.disabled = true;
      el.textContent = "Drafting…";
      await post(`/api/cases/${id}/draft`);
    }
    if (name === "send-note") {
      el.disabled = true;
      await post(`/api/cases/${id}/email`, { kind: "supplier_note", subject: $("#d-subj").value, body: $("#d-body").value });
      toast("Note sent to the supplier's address on file");
    }
    if (name === "copy-note") {
      await navigator.clipboard.writeText($("#d-body").value);
      return toast("Copied");
    }
    if (name === "email-approvers") {
      await post(`/api/cases/${id}/email`, { kind: "approval_request" });
      toast("Approvers emailed");
    }
    if (name === "remit") {
      await post(`/api/cases/${id}/email`, { kind: "remittance" });
      toast("Remittance advice sent");
    }
  } catch (e) {
    toast(e.message);
  }
  await load();
}

document.addEventListener("click", async (e) => {
  const li = e.target.closest("#cases li");
  if (li) return select(li.dataset.id);
  const a = e.target.closest("[data-act]");
  if (a) act(a.dataset.act, a);
});
document.addEventListener("change", (e) => {
  if (e.target.id === "who") fillRoles();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.matches("#cases li")) e.target.click();
});

function select(id) {
  selected = id;
  history.replaceState(null, "", `#case=${encodeURIComponent(id)}`);
  renderList();
  renderDetail();
}

$("#run-all").addEventListener("click", async () => {
  const todo = S.cases.filter((c) => !c.ran_at);
  if (!todo.length) return toast("Every invoice has been read");
  for (const c of todo) {
    if (!selected) selected = c.id;
    await runCase(c.id);
  }
});

$("#upload").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  const r = await fetch("/api/upload", { method: "POST", body: fd });
  const b = await r.json();
  toast(r.ok ? `${f.name} added to the inbox` : b.error);
  e.target.value = "";
  await load();
});

function show(v) {
  view = v;
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x.dataset.view === v));
  document.querySelectorAll(".view").forEach((s) => s.classList.toggle("on", s.id === `view-${v}`));
  if (v === "ledger") loadLedger();
  if (v === "reconcile") loadReconcile();
  if (v === "records") renderRecords();
  if (v === "connectors") loadConnectors();
  if (v === "settings") loadSettings();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

// -- ledger -----------------------------------------------------------------------------------------------------------
async function loadLedger() {
  $("#verify-out").innerHTML = `<div class="busy">Verifying the chain…</div>`;
  const L = await api("/api/ledger");
  const ok = L.verified;
  $("#verify-out").innerHTML = `<div class="banner">${ok ? `<span class="tick" style="width:26px;height:26px;font-size:14px">✓</span>` : ok === false ? `<span class="cross" style="width:26px;height:26px">✕</span>` : ""}
    <div><div class="big">${ok ? "Verified" : ok === false ? "Problems found" : "No decisions yet"}</div>
    <div class="muted small">${ok != null ? `${esc((L.report.find((l) => /ledger entries/.test(l)) || "").replace("sign-offs", "approvals").replace(", 0 erasures", ""))} · head <span class="mono">${short(L.head)}</span>` : "Read an invoice to start the ledger."}</div></div>
    <a class="btn ghost small" style="margin-left:auto" href="/api/ledger/download">Download for audit</a></div>
    ${ok === false ? `<pre class="mono">${esc(L.report.join("\n"))}</pre>` : ""}`;
  const rows = L.entries
    .map((e) => {
      let what = "";
      if (e.kind === "signoff") what = `<span class="chip ${e.decision === "approved" ? "pos" : "neg"}">${esc(e.decision)}</span> by ${esc(e.signer)} as ${esc(roleName(e.role))}`;
      else if (e.agent === "settle") what = `<span class="chip acc">Sent to PayPal</span> batch <span class="mono">${esc(e.outcome?.payout_batch_id)}</span> · ${usd(e.outcome?.amount)}`;
      else if (e.agent === "set_policy") {
        const o = e.outcome || {};
        what = `<span class="chip">Policy v${esc(o.after?.version)}</span> by ${esc(o.changed_by)}: tolerance ${Math.round((o.after?.tolerance || 0) * 100)}%, second approval over ${usd(o.after?.dual_limit)}`;
      } else {
        const o = e.outcome || {};
        const d = o.decided || o;
        what = o.status === "PENDING" ? `<span class="chip warn">Pay, needs approval</span> ${esc(d.invoice)} · ${usd(d.amount)}` : `<span class="chip neg">Blocked</span> ${esc(d.invoice || "")} · ${(d.codes || []).join(", ")}`;
      }
      return `<tr><td class="r num">${e.seq}</td><td>${when(e.time)}</td><td>${what}</td><td class="mono hash">${short(e.sha256)}</td><td class="mono hash">${short(e.prev)}</td></tr>`;
    })
    .join("");
  $("#ledger-tbl").innerHTML = `<thead><tr><th class="r">#</th><th>When</th><th>Entry</th><th>sha256</th><th>Previous</th></tr></thead><tbody>${rows}</tbody>`;
}
$("#verify").addEventListener("click", loadLedger);

// -- reconcile ----------------------------------------------------------------------------------------------------------
async function loadReconcile() {
  const el = $("#reconcile");
  el.innerHTML = `<div class="busy">Reading the studio's PayPal transactions…</div>`;
  let R;
  try {
    R = await api("/api/reconcile");
  } catch (e) {
    R = { ok: false, error: String(e) };
  }
  if (!R.ok) {
    el.innerHTML = `<div class="banner"><span class="cross" style="width:26px;height:26px">!</span><div><div class="big">PayPal did not answer</div><div class="muted small">${esc(R.error)}</div></div></div>`;
    return;
  }
  const n = (s) => R.ours.filter((r) => r.state === s).length;
  const refreshed = R.refreshed ? new Date(R.refreshed).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "unknown";
  const STATE = {
    MATCHED: `<span class="chip pos">Matched</span>`,
    AMOUNT_DIFFERS: `<span class="chip neg">Amount differs</span>`,
    WAITING: `<span class="chip warn">Not in PayPal's report yet</span>`,
    MISSING: `<span class="chip neg">Missing from PayPal</span>`,
  };
  const clean = !n("AMOUNT_DIFFERS") && !n("MISSING");
  const ours = R.ours.length
    ? R.ours.map((r) => `<tr><td>${STATE[r.state]}</td><td>${esc(r.invoice)}<div class="muted small">${esc(r.vendor)}</div></td><td class="r num">${usd(r.amount)}</td><td class="r num">${r.paypal_amount != null ? usd(r.paypal_amount) : "–"}</td><td class="r num">${r.paypal_fee != null ? usd(r.paypal_fee) : r.fee ? usd(r.fee) : "–"}</td><td class="mono hash">${esc(r.transaction_id || "–")}</td></tr>`).join("")
    : `<tr><td colspan="6" class="muted">No payouts in this workspace yet. Approve Lena Ortiz Design to make one.</td></tr>`;
  const outside = R.outside.length
    ? R.outside.map((r) => `<tr><td>${when(Date.parse(r.time) / 1000)}</td><td class="r num">${usd(r.amount)}</td><td class="r num">${r.fee ? usd(r.fee) : "–"}</td><td>${esc(r.counterparty || "")}<div class="muted small">${esc(r.note || "")}</div></td><td class="mono hash">${esc(r.transaction_id)}</td></tr>`).join("")
    : `<tr><td colspan="5" class="muted">None. Every payment out of the account was approved in Countersign.</td></tr>`;
  el.innerHTML = `<div class="banner"><span class="${clean ? "tick" : "cross"}" style="width:26px;height:26px;font-size:14px">${clean ? "✓" : "!"}</span>
      <div><div class="big">${n("MATCHED")} of ${R.ours.length} payouts matched in PayPal</div>
      <div class="muted small">${n("WAITING") ? `${n("WAITING")} not in PayPal's report yet (it can trail payouts by up to a few hours) · ` : ""}${R.outside.length} ${R.outside.length === 1 ? "payment" : "payments"} out without a Countersign approval · ${R.paypal_count} PayPal transactions read, last ${R.window_days} days · PayPal report refreshed ${esc(refreshed)}</div></div></div>
    <div class="section"><h3>Countersign payouts, checked against PayPal</h3>
      <table class="tbl"><thead><tr><th>PayPal says</th><th>Invoice</th><th class="r">Approved</th><th class="r">In PayPal</th><th class="r">Fee</th><th>Transaction</th></tr></thead><tbody>${ours}</tbody></table></div>
    <div class="section"><h3>Money out of PayPal without a Countersign approval</h3>
      <p class="muted small" style="margin-bottom:8px">In this demo these are payouts from earlier demo runs, whose records were reset. In a real business, this list is what an AP team investigates.</p>
      <table class="tbl"><thead><tr><th>When</th><th class="r">Amount</th><th class="r">Fee</th><th>To</th><th>Transaction</th></tr></thead><tbody>${outside}</tbody></table></div>`;
}
$("#reconcile-again").addEventListener("click", loadReconcile);

// -- records ----------------------------------------------------------------------------------------------------------
function renderRecords() {
  const E = S.erp;
  const t = (head, rows) => `<table class="tbl"><thead><tr>${head.map((h) => `<th class="${h.r ? "r" : ""}">${h.t}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
  $("#records").innerHTML = `
    <div class="section"><h3>Vendor master</h3>${t([{ t: "Supplier" }, { t: "PayPal account on file" }, { t: "Work" }], E.vendors.map((v) => `<tr><td>${esc(v.name)}</td><td>${esc(v.paypal_email)}</td><td>${esc(v.category)}</td></tr>`))}</div>
    <div class="grid2">
      <div class="section"><h3>Purchase orders</h3>${t([{ t: "PO" }, { t: "Supplier" }, { t: "Amount", r: 1 }, { t: "Status" }], E.purchase_orders.map((p) => `<tr><td>${esc(p.po)}</td><td>${esc(p.vendor)}<div class="muted small">${esc(p.item)}</div></td><td class="r num">${usd(p.amount)}</td><td><span class="chip ${p.status === "paid" ? "pos" : ""}">${esc(p.status)}</span></td></tr>`))}</div>
      <div class="section"><h3>Payments made</h3>${t([{ t: "Invoice" }, { t: "Supplier" }, { t: "Amount", r: 1 }, { t: "Paid" }], E.payments.map((p) => `<tr><td>${esc(p.invoice_no)}</td><td>${esc(p.vendor)}${p.payout_batch_id ? `<div class="mono hash">${esc(p.payout_batch_id)}</div>` : ""}</td><td class="r num">${usd(p.amount)}</td><td>${esc(p.paid_on)}</td></tr>`))}</div>
    </div>
    <p class="muted small">Import or export these from Connectors > Accounting.</p>`;
}

$("#reset").addEventListener("click", async () => {
  if (!confirm("Start the demo over? The inbox and records are restored. The policy goes back to its defaults. The current ledger is moved aside, not deleted. Approvers and connectors stay.")) return;
  await post("/api/reset");
  selected = null;
  history.replaceState(null, "", "#");
  $("#detail").innerHTML = `<div class="empty">Choose an invoice. The agent reads it, the policy checks it, and nothing is paid until people approve.</div>`;
  await load();
  renderRecords();
  toast("Fresh inbox");
});

// -- connectors -------------------------------------------------------------------------------------------------------
async function loadConnectors() {
  const C = await api("/api/connectors");
  const e = C.email;
  const status = (ok, on = "Connected", off = "Not set") => `<span class="chip ${ok ? "pos" : ""}">${ok ? on : off}</span>`;
  const reader = C.reader.readers.find((r) => r.id === C.reader.model);
  $("#connectors").innerHTML = `<div class="cards">
    <div class="card"><div class="row-h"><h3>PayPal</h3>${status(C.paypal.configured)}</div>
      <p>Pays approved invoices with the Payouts API and reads back each payout's status, transaction and fee.</p>
      <dl class="kv"><dt>Environment</dt><dd>Sandbox (${esc(C.paypal.base)})</dd><dt>App</dt><dd class="mono">${esc(C.paypal.client_id || "not set")}</dd><dt>APIs</dt><dd>${C.paypal.apis.join(", ")}</dd></dl>
      <div class="btns"><button class="btn ghost small" data-test="paypal">Test connection</button><span class="small" id="t-paypal"></span></div>
      <p class="muted small" style="margin-top:8px">Credentials live in .env.local on the server, never in the browser.</p></div>

    <div class="card"><div class="row-h"><h3>AI reader</h3>${status(true, "Ready")}</div>
      <p>Reads each invoice PDF and proposes the payment. It never decides: the Shio policy does.</p>
      <dl class="kv"><dt>Model</dt><dd>${esc(reader?.label || C.reader.model)}</dd></dl>
      <div class="btns"><button class="btn ghost small" data-test="reader">Test</button><button class="linkish" data-goto="settings">Change model</button><span class="small" id="t-reader"></span></div></div>

    <div class="card"><div class="row-h"><h3>Email (Resend)</h3>${status(e.configured)}</div>
      <p>Approval requests to approvers, notes to suppliers about blocked invoices, and remittance advice after PayPal pays.</p>
      <div class="field">Resend API key<input id="e-key" type="password" placeholder="${esc(e.api_key || "re_…")}" autocomplete="off"></div>
      <div class="field">From<input id="e-from" value="${esc(e.from)}"></div>
      <label class="check"><input type="checkbox" id="e-safe" ${e.safe_delivery ? "checked" : ""}> Safe delivery: send every email to one inbox</label>
      <div class="field">That inbox<input id="e-to" type="email" value="${esc(e.deliver_to)}" placeholder="you@company.com"></div>
      <label class="check"><input type="checkbox" id="e-auto1" ${e.auto_approval_requests ? "checked" : ""}> Email approvers when a payment needs them</label>
      <label class="check"><input type="checkbox" id="e-auto2" ${e.auto_remittance ? "checked" : ""}> Email remittance advice when PayPal pays</label>
      <div class="btns"><button class="btn small" id="e-save">Save</button><button class="btn ghost small" data-test="email" ${e.configured ? "" : "disabled"}>Send a test email</button><span class="small" id="t-email"></span></div>
      ${e.key_from_env ? `<p class="muted small" style="margin-top:8px">Using RESEND_API_KEY from .env.local.</p>` : ""}</div>

    <div class="card"><div class="row-h"><h3>Accounting (CSV)</h3>${status(true, "Ready")}</div>
      <p>Bring in your vendor master and purchase orders from any accounting system, and take payments back out for reconciliation.</p>
      <dl class="kv"><dt>Vendors</dt><dd>${C.accounting.vendors}</dd><dt>Purchase orders</dt><dd>${C.accounting.purchase_orders}</dd><dt>Payments</dt><dd>${C.accounting.payments}</dd></dl>
      <div class="btns">
        <label class="btn ghost small">Import vendors<input type="file" accept=".csv,text/csv" data-import="vendors" hidden></label>
        <label class="btn ghost small">Import POs<input type="file" accept=".csv,text/csv" data-import="pos" hidden></label>
        <a class="btn ghost small" href="/api/export/payments.csv">Export payments</a></div>
      <p class="small" style="margin-top:8px">Templates: <a href="/api/templates/vendors.csv">vendors.csv</a> · <a href="/api/templates/pos.csv">pos.csv</a></p></div>

    <div class="card"><div class="row-h"><h3>Shio ledger</h3>${status(true, "Recording")}</div>
      <p>Every decision, approval, payout and policy change, signed with Ed25519 and hash-chained.</p>
      <dl class="kv"><dt>Entries</dt><dd>${C.ledger.entries}</dd><dt>Signing key</dt><dd class="mono">${esc(C.ledger.key || "")}</dd></dl>
      <div class="btns"><a class="btn ghost small" href="/api/ledger/download">Download for audit</a><button class="linkish" data-goto="ledger">Open ledger</button></div></div>

    <div class="card"><div class="row-h"><h3>Next</h3><span class="chip">Planned</span></div>
      <p>Connectors we would add for a pilot. None of these is wired up in this build.</p>
      <ul class="planned"><li><span>QuickBooks Online, Xero</span><span class="muted small">vendor master and POs</span></li>
      <li><span>Gmail or Outlook inbox</span><span class="muted small">invoices arrive by email</span></li>
      <li><span>Slack</span><span class="muted small">approve from a message</span></li>
      <li><span>PayPal webhooks</span><span class="muted small">payout status without polling</span></li></ul></div>
  </div>`;
}

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("[data-test]");
  if (t) {
    const out = $(`#t-${t.dataset.test}`);
    out.className = "small muted";
    out.textContent = "Testing…";
    try {
      const r = await post(`/api/connectors/${t.dataset.test}/test`);
      out.className = "ok-msg";
      out.textContent = `${r.message} (${r.ms} ms)`;
    } catch (e) {
      out.className = "err";
      out.textContent = e.message;
    }
  }
  const g = ev.target.closest("[data-goto]");
  if (g) show(g.dataset.goto);
  if (ev.target.id === "e-save") {
    try {
      await put("/api/connectors/email", {
        api_key: $("#e-key").value || null,
        from_: $("#e-from").value,
        deliver_to: $("#e-to").value,
        safe_delivery: $("#e-safe").checked,
        auto_approval_requests: $("#e-auto1").checked,
        auto_remittance: $("#e-auto2").checked,
      });
      toast("Email settings saved");
      await load();
      loadConnectors();
    } catch (e) {
      toast(e.message);
    }
  }
});
document.addEventListener("change", async (ev) => {
  const inp = ev.target.closest("[data-import]");
  if (!inp || !inp.files[0]) return;
  const fd = new FormData();
  fd.append("file", inp.files[0]);
  const r = await fetch(`/api/import/${inp.dataset.import}`, { method: "POST", body: fd });
  const b = await r.json();
  toast(r.ok ? `Imported ${b.imported} ${inp.dataset.import === "pos" ? "purchase orders" : "vendors"}` : b.error);
  inp.value = "";
  await load();
  loadConnectors();
});

// -- settings ---------------------------------------------------------------------------------------------------------
let ST = null;
async function loadSettings() {
  ST = await api("/api/settings");
  const p = ST.policy;
  const controllers = ST.approvers.filter((a) => a.roles.includes("controller"));
  $("#settings").innerHTML = `<div class="grid-2">
    <div class="card"><div class="row-h"><h3>Payment policy</h3><span class="chip">Version ${p.version}</span></div>
      <p>The Shio checks and approvals read these. Each decision records the version it was made under, and every change is written to the ledger.</p>
      <div class="field">Allow invoices over their PO by (%)<input id="p-tol" type="number" min="0" max="25" step="0.5" value="${+(p.tolerance * 100).toFixed(2)}"></div>
      <div class="field">A second person (finance controller) approves payments over (USD)<input id="p-dual" type="number" min="0" step="50" value="${p.dual_limit}"></div>
      <div class="field">Pay in<select id="p-cur"><option>USD</option></select></div>
      <div class="field">Changed by (a finance controller)<select id="p-by">${controllers.map((a) => `<option>${esc(a.name)}</option>`).join("")}</select></div>
      <div class="btns"><button class="btn small" id="p-save">Save policy</button></div>
      ${ST.history.length ? `<ul class="mail" style="margin-top:12px">${ST.history.slice(0, 5).map((h) => `<li>${esc(h.by)} · ${when(h.time)}<div class="sub">tolerance ${Math.round(h.before.tolerance * 100)}% → ${Math.round(h.after.tolerance * 100)}% · second approval over ${usd(h.before.dual_limit)} → ${usd(h.after.dual_limit)}</div></li>`).join("")}</ul>` : ""}
    </div>

    <div class="card"><div class="row-h"><h3>AI reader</h3></div>
      <p>The model that reads invoices. Models whose API key is not set are greyed out.</p>
      <div class="field">Model<select id="s-reader">${ST.readers.map((r) => `<option value="${esc(r.id)}" ${r.id === ST.current_reader ? "selected" : ""} ${r.available ? "" : "disabled"}>${esc(r.label)}${r.available ? "" : " (no key)"}</option>`).join("")}</select></div>
      <label class="check"><input type="checkbox" id="s-auto" ${ST.auto_refresh ? "checked" : ""}> Check PayPal for payout status automatically</label>
      <div class="field">Company name (in emails)<input id="s-company" value="${esc(ST.company)}"></div>
      <div class="btns"><button class="btn small" id="s-save">Save</button></div>
    </div>
  </div>

  <div class="card" style="margin-top:16px"><div class="row-h"><h3>Approvers</h3><span class="muted small">${esc(ST.agent)} prepares payments and can never approve one</span></div>
    <p>Who can approve, as which role. Approval requests go to these addresses.</p>
    <table class="people"><thead><tr><th>Name</th><th>Email</th><th>AP approver</th><th>Finance controller</th><th></th></tr></thead>
    <tbody id="ppl">${ST.approvers.map(personRow).join("")}</tbody></table>
    <div class="btns" style="margin-top:10px"><button class="btn ghost small" id="a-add">Add a person</button><button class="btn small" id="a-save">Save approvers</button></div>
  </div>`;
}
const personRow = (a) => `<tr><td><input type="text" value="${esc(a.name)}" aria-label="Name"></td><td><input type="email" value="${esc(a.email)}" aria-label="Email"></td>
  <td><input type="checkbox" data-role="ap_approver" ${a.roles.includes("ap_approver") ? "checked" : ""} aria-label="AP approver"></td>
  <td><input type="checkbox" data-role="controller" ${a.roles.includes("controller") ? "checked" : ""} aria-label="Finance controller"></td>
  <td><button class="linkish" data-del>Remove</button></td></tr>`;

document.addEventListener("click", async (ev) => {
  const id = ev.target.id;
  try {
    if (id === "p-save") {
      await put("/api/settings", { by: $("#p-by").value, policy: { tolerance: $("#p-tol").value / 100, dual_limit: +$("#p-dual").value, currency: $("#p-cur").value } });
      toast("Policy saved and written to the ledger. New decisions use it.");
    } else if (id === "s-save") {
      await put("/api/settings", { reader: $("#s-reader").value, auto_refresh: $("#s-auto").checked, company: $("#s-company").value });
      toast("Settings saved");
    } else if (id === "a-add") {
      $("#ppl").insertAdjacentHTML("beforeend", personRow({ name: "", email: "", roles: ["ap_approver"] }));
      return;
    } else if (id === "a-save") {
      const people = [...document.querySelectorAll("#ppl tr")].map((tr) => ({
        name: tr.querySelector("input[type=text]").value,
        email: tr.querySelector("input[type=email]").value,
        roles: [...tr.querySelectorAll("[data-role]")].filter((x) => x.checked).map((x) => x.dataset.role),
      }));
      await put("/api/settings", { approvers: people });
      toast("Approvers saved");
    } else if (ev.target.matches("[data-del]")) {
      ev.target.closest("tr").remove();
      return;
    } else return;
    await load();
    loadSettings();
  } catch (e) {
    toast(e.message);
  }
});

// -- start ------------------------------------------------------------------------------------------------------------
(async () => {
  await load();
  const m = location.hash.match(/case=([^&]+)/);
  if (m && S.cases.some((c) => c.id === decodeURIComponent(m[1]))) select(decodeURIComponent(m[1]));
})();
