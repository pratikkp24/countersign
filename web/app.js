const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (n) => (n == null || isNaN(n) ? "" : "USD " + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const short = (h) => (h ? h.slice(0, 12) : "");
const when = (t) => (t ? new Date(t * 1000).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "");

let S = { cases: [], config: {}, erp: {} };
let selected = null;
const running = new Set();

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("on");
  clearTimeout(toast.h);
  toast.h = setTimeout(() => t.classList.remove("on"), 3800);
}

const STATUS = {
  NEW: ["New", ""],
  BLOCK: ["Blocked", "neg"],
  PENDING: ["Awaiting approval", "warn"],
  REJECTED: ["Rejected", "neg"],
  APPROVED: ["Approved", "acc"],
  PAID: ["Paid", "pos"],
};

function chip(status, c) {
  if (running.has(c?.id)) return `<span class="chip">Reading</span>`;
  const [label, cls] = STATUS[status] || [status, ""];
  let extra = "";
  if (status === "PENDING" && c?.signoff) extra = ` ${c.signoff.signed.length}/${c.signoff.roles.length}`;
  return `<span class="chip ${cls}">${label}${extra}</span>`;
}

function vendorOf(c) {
  return c.reading?.vendor || c.file.replace(/^INV-\d+_/, "").replace(/\.pdf$/i, "").replace(/_/g, " ");
}
function invOf(c) {
  return c.reading?.invoice_no || (c.file.match(/^(INV-\d+)/) || [])[1] || c.file;
}

async function load() {
  S = await api("/api/state");
  renderEnv();
  renderList();
  if (selected) renderDetail();
}

function renderEnv() {
  const c = S.config;
  const model = c.model === "replay" ? "Replay model" : c.model.startsWith("groq:") ? c.model.slice(5).replace("openai/", "").toUpperCase().replace("GPT-OSS", "gpt-oss") + " on Groq" : c.model.replace("claude-", "Claude ").replace(/-(\d)-(\d)$/, " $1.$2").replace(/(^|\s)([a-z])/g, (m) => m.toUpperCase());
  $("#env").innerHTML =
    `<span class="chip ${c.paypal ? "ok" : "bad"}"><span class="dot"></span>PayPal sandbox ${c.paypal ? "connected" : "not set"}</span>` +
    `<span class="chip ${c.anthropic ? "ok" : "bad"}"><span class="dot"></span>${esc(model)}</span>` +
    `<span class="chip"><span class="dot"></span>Policy in Shio</span>`;
}

function renderList() {
  const waiting = S.cases.filter((c) => c.status === "PENDING").length;
  const blocked = S.cases.filter((c) => c.status === "BLOCK").length;
  $("#count").textContent = `${S.cases.length} in inbox · ${waiting} awaiting approval · ${blocked} blocked`;
  $("#cases").innerHTML = S.cases
    .map((c) => {
      const amt = c.reading?.amount;
      return `<li role="option" data-id="${esc(c.id)}" class="${c.id === selected ? "on" : ""}" tabindex="0" aria-selected="${c.id === selected}">
        <div class="v">${esc(vendorOf(c))}</div><div class="amt num">${amt != null ? usd(amt) : ""}</div>
        <div class="m">${esc(invOf(c))} · received ${when(c.received)}</div><div class="st">${chip(c.status, c)}</div></li>`;
    })
    .join("");
}

function stepHead(n, title, sub, done = true) {
  return `<div class="step-h"><span class="n ${done ? "" : "idle"}">${n}</span><h3>${title}</h3>${sub ? `<span class="sub">${sub}</span>` : ""}</div>`;
}

function renderDetail() {
  const c = S.cases.find((x) => x.id === selected);
  if (!c) return;
  const r = c.reading || {};
  const isRunning = running.has(c.id);
  const payeeBad = c.checks?.some((k) => k.name === "payee_on_file" && !k.passed && !k.skipped);

  let read = `<div class="card">${stepHead(1, "Agent reads the invoice", c.reading ? `${esc(c.read_by)} · ${c.seconds}s` : "", !!c.reading)}`;
  if (isRunning) read += `<div class="busy">Claude is reading the PDF…</div>`;
  else if (!c.ran_at) read += `<p class="note">Claude reads the PDF and reports what it asks for. It proposes; it decides nothing.</p><button class="btn" data-act="run">Read with agent</button>`;
  else if (!c.reading) read += `<div class="err">The model's answer could not be read${c.read_error ? `: ${esc(c.read_error)}` : ""}. Nothing can be paid from it.</div>`;
  else
    read += `<dl class="kv">
      <dt>Supplier</dt><dd>${esc(r.vendor)}</dd>
      <dt>Invoice</dt><dd>${esc(r.invoice_no)}</dd>
      <dt>PO</dt><dd>${esc(r.po)}</dd>
      <dt>Total</dt><dd class="num">${esc(r.currency)} ${Number(r.amount).toLocaleString("en-US", { minimumFractionDigits: 2 })}</dd>
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
        ? `<div class="verdict block">Blocked. Nothing will be paid. The supplier and the AP team see the reason codes above.</div>`
        : `<div class="verdict pay">Decision: pay <b class="num">${usd(d.amount)}</b> to <b>${esc(d.pay_to)}</b>, the PayPal account in the vendor master. ${c.status === "PENDING" ? "It waits for people to approve." : c.status === "REJECTED" ? "People rejected it, so nothing was paid." : "People approved it."}</div>`;
    checks = `<div class="card">${stepHead(2, "Policy checks it against your records", "Shio rule")}<ul class="checks">${rows}</ul>${verdict}</div>`;
  }

  let appr = "";
  if (c.signoff) {
    const so = c.signoff;
    const roleName = (k) => S.config.roles?.[k] || k;
    const lines = so.roles
      .map((role) => {
        const s = so.signed.find((x) => x.role === role);
        return `<li>${s ? (s.decision === "approved" ? `<span class="tick">✓</span>` : `<span class="cross">✕</span>`) : `<span class="skip"></span>`}<span>${esc(roleName(role))}</span><span class="who">${s ? `${esc(s.signer)} · ${s.decision} · ${when(s.time)}` : "waiting"}</span></li>`;
      })
      .join("");
    const why = so.because.map((b) => (b.startsWith("amount >") ? `over USD ${Number(S.config.dual_limit).toLocaleString()}, so a second person approves` : "every payment needs a person")).join("; ");
    let form = "";
    if (c.status === "PENDING") {
      const opts = so.waiting_for.map((r) => `<option value="${esc(r)}">${esc(roleName(r))}</option>`).join("");
      form = `<p class="note">Approving sends <b class="num">${usd(c.decided.amount)}</b> to <b>${esc(c.decided.pay_to)}</b> through PayPal Payouts once every role above has approved. The agent that prepared it cannot approve it, and one person cannot approve twice.</p>
        <div class="form">
          <input id="who" placeholder="Your name" autocomplete="name" aria-label="Your name" value="${esc(localStorage.getItem("cs-name") || "")}">
          <select id="role" aria-label="Approving as">${opts}</select>
          <div class="row"><button class="btn" data-act="approve">Approve</button><button class="btn neg small" data-act="reject">Reject</button><span class="muted small" id="as-who"></span></div>
        </div><div class="err" id="sign-err"></div>`;
    }
    appr = `<div class="card">${stepHead(3, "People approve", why, so.state !== "PENDING")}<ul class="approvals">${lines}</ul>${form}</div>`;
  }

  let pp = "";
  if (c.status === "PAID" || c.status === "APPROVED" || c.payout_error) {
    const p = c.payout;
    if (p) {
      const st = p.item_status || p.batch_status;
      const cls = /SUCCESS/.test(st) ? "pos" : /FAILED|DENIED|BLOCKED|RETURNED/.test(st) ? "neg" : /UNCLAIMED|PENDING|PROCESSING/.test(st) ? "warn" : "";
      pp = `<div class="card">${stepHead(4, "PayPal moves the money", "Payouts API · sandbox")}
        <dl class="kv">
          <dt>Status</dt><dd><span class="chip ${cls}">${esc(st)}</span></dd>
          <dt>Amount</dt><dd class="num">${usd(p.amount)}${p.fee ? ` <span class="muted">(fee ${esc(p.fee)})</span>` : ""}</dd>
          <dt>To</dt><dd>${esc(p.receiver)}</dd>
          <dt>Batch</dt><dd class="mono">${esc(p.payout_batch_id)}</dd>
          ${p.transaction_id ? `<dt>Transaction</dt><dd class="mono">${esc(p.transaction_id)}</dd>` : ""}
          <dt>Your batch id</dt><dd class="mono hash" title="Derived from the approved record, so PayPal refuses a second payout of the same decision">${esc(p.sender_batch_id)}</dd>
        </dl>
        ${st === "UNCLAIMED" ? `<p class="note" style="margin-top:10px">UNCLAIMED: no sandbox PayPal account uses this address yet, so PayPal holds the money until the supplier opens one (or the payout is returned).</p>` : ""}
        <div style="margin-top:12px;display:flex;gap:8px"><button class="btn ghost small" data-act="refresh">Check PayPal status</button></div></div>`;
    } else if (c.payout_error) {
      pp = `<div class="card">${stepHead(4, "PayPal moves the money", "", false)}<div class="err">${esc(c.payout_error)}</div><div style="margin-top:10px"><button class="btn small" data-act="pay">Try the payout again</button></div></div>`;
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
      <div class="steps">${read}${checks}${appr}${pp}${audit}</div>
    </div>`;
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
    box.insertAdjacentHTML("afterbegin", `<p class=\"muted small\" style=\"padding:16px\">The PDF preview could not be drawn.</p>`);
  }
}

async function runCase(id) {
  running.add(id);
  renderList();
  if (selected === id) renderDetail();
  try {
    const c = await api(`/api/cases/${id}/run`, { method: "POST" });
    const i = S.cases.findIndex((x) => x.id === id);
    S.cases[i] = c;
    toast(c.status === "BLOCK" ? `${c.reading?.invoice_no || "Invoice"} blocked: ${c.checks.filter((k) => !k.passed && !k.skipped).map((k) => k.code).join(", ")}` : `${c.reading?.invoice_no} is waiting for approval`);
  } catch (e) {
    toast(e.message);
  } finally {
    running.delete(id);
    await load();
  }
}

async function sign(approve) {
  const who = $("#who").value.trim();
  const role = $("#role").value;
  $("#sign-err").textContent = "";
  if (!who) {
    $("#sign-err").textContent = "Enter your name to approve.";
    return;
  }
  localStorage.setItem("cs-name", who);
  const btns = document.querySelectorAll("[data-act=approve],[data-act=reject]");
  btns.forEach((b) => (b.disabled = true));
  try {
    const c = await api(`/api/cases/${selected}/signoff`, { method: "POST", body: JSON.stringify({ name: who, role, approve }) });
    toast(c.status === "PAID" ? `Paid: PayPal batch ${c.payout.payout_batch_id}` : c.status === "REJECTED" ? "Rejected. Nothing will be paid." : `Approved by ${who}. Waiting for the next approver.`);
    if (c.status === "PENDING") localStorage.removeItem("cs-name");
  } catch (e) {
    $("#sign-err").textContent = e.message;
    btns.forEach((b) => (b.disabled = false));
    return;
  }
  await load();
}

document.addEventListener("click", async (e) => {
  const li = e.target.closest("#cases li");
  if (li) {
    selected = li.dataset.id;
    renderList();
    renderDetail();
    return;
  }
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (!act) return;
  if (act === "run") runCase(selected);
  if (act === "approve") sign(true);
  if (act === "reject") sign(false);
  if (act === "refresh" || act === "pay") {
    try {
      await api(`/api/cases/${selected}/${act}`, { method: "POST" });
      toast(act === "pay" ? "Payout sent to PayPal" : "Status updated from PayPal");
    } catch (err) {
      toast(err.message);
    }
    await load();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.matches("#cases li")) e.target.click();
});

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

document.querySelectorAll(".tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("on", v.id === `view-${b.dataset.view}`));
    if (b.dataset.view === "ledger") loadLedger();
    if (b.dataset.view === "records") renderRecords();
  })
);

async function loadLedger() {
  $("#verify-out").innerHTML = `<div class="busy">Verifying the chain…</div>`;
  const L = await api("/api/ledger");
  const ok = L.verified;
  $("#verify-out").innerHTML = `<div class="banner">${ok ? `<span class="tick" style="width:26px;height:26px;font-size:14px">✓</span>` : ok === false ? `<span class="cross" style="width:26px;height:26px">✕</span>` : ""}
    <div><div class="big">${ok ? "Verified" : ok === false ? "Problems found" : "No decisions yet"}</div>
    <div class="muted small">${ok != null ? `${esc((L.report.find((l) => /ledger entries/.test(l)) || "").replace("sign-offs", "approvals").replace(", 0 erasures", ""))} · head <span class="mono">${short(L.head)}</span>` : "Read an invoice to start the ledger."}</div></div></div>
    ${ok === false ? `<pre class="mono">${esc(L.report.join("\n"))}</pre>` : ""}`;
  const rows = L.entries
    .map((e) => {
      let what = "";
      if (e.kind === "signoff") what = `<span class="chip ${e.decision === "approved" ? "pos" : "neg"}">${esc(e.decision)}</span> by ${esc(e.signer)} as ${esc(S.config.roles?.[e.role] || e.role)}`;
      else if (e.agent === "settle") what = `<span class="chip acc">Sent to PayPal</span> batch <span class="mono">${esc(e.outcome?.payout_batch_id)}</span> · ${usd(e.outcome?.amount)}`;
      else {
        const o = e.outcome || {};
        const d = o.decided || o;
        what = o.status === "PENDING" ? `<span class="chip warn">Pay, awaiting approval</span> ${esc(d.invoice)} · ${usd(d.amount)}` : `<span class="chip neg">Blocked</span> ${esc(d.invoice || "")} · ${(d.codes || []).join(", ")}`;
      }
      return `<tr><td class="r num">${e.seq}</td><td>${when(e.time)}</td><td>${what}</td><td class="mono hash">${short(e.sha256)}</td><td class="mono hash">${short(e.prev)}</td></tr>`;
    })
    .join("");
  $("#ledger-tbl").innerHTML = `<thead><tr><th class="r">#</th><th>When</th><th>Entry</th><th>sha256</th><th>Previous</th></tr></thead><tbody>${rows}</tbody>`;
}
$("#verify").addEventListener("click", loadLedger);

function renderRecords() {
  const E = S.erp;
  const t = (head, rows) => `<table class="tbl"><thead><tr>${head.map((h) => `<th class="${h.r ? "r" : ""}">${h.t}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
  $("#records").innerHTML = `
    <div class="section"><h3>Vendor master</h3>${t([{ t: "Supplier" }, { t: "PayPal account on file" }, { t: "Work" }], E.vendors.map((v) => `<tr><td>${esc(v.name)}</td><td>${esc(v.paypal_email)}</td><td>${esc(v.category)}</td></tr>`))}</div>
    <div class="grid2">
      <div class="section"><h3>Purchase orders</h3>${t([{ t: "PO" }, { t: "Supplier" }, { t: "Amount", r: 1 }, { t: "Status" }], E.purchase_orders.map((p) => `<tr><td>${esc(p.po)}</td><td>${esc(p.vendor)}<div class="muted small">${esc(p.item)}</div></td><td class="r num">${usd(p.amount)}</td><td><span class="chip ${p.status === "paid" ? "pos" : ""}">${esc(p.status)}</span></td></tr>`))}</div>
      <div class="section"><h3>Payments made</h3>${t([{ t: "Invoice" }, { t: "Supplier" }, { t: "Amount", r: 1 }, { t: "Paid" }], E.payments.map((p) => `<tr><td>${esc(p.invoice_no)}</td><td>${esc(p.vendor)}${p.payout_batch_id ? `<div class="mono hash">${esc(p.payout_batch_id)}</div>` : ""}</td><td class="r num">${usd(p.amount)}</td><td>${esc(p.paid_on)}</td></tr>`))}</div>
    </div>`;
}

$("#reset").addEventListener("click", async () => {
  if (!confirm("Start the demo over? The current ledger is moved aside, not deleted.")) return;
  await api("/api/reset", { method: "POST" });
  selected = null;
  $("#detail").innerHTML = `<div class="empty">Choose an invoice. The agent reads it, the policy checks it, and nothing is paid until people approve.</div>`;
  await load();
  renderRecords();
  toast("Fresh inbox");
});

load();
