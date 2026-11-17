// Simple Aisle panel: pairing, devices, store links and the change queue.
// Vanilla web component, no build step. Admin-only (require_admin on the panel
// and on every websocket command it calls).

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "never");
const ago = (t) => {
  if (!t) return "never";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
};
const OP_LABELS = { add: "Add", edit: "Edit", remove: "Remove", complete: "Check off", uncomplete: "Uncheck", move: "Move" };

class SimpleAislePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = null;
    this._pair = null;
    this._error = "";
    this._timer = null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._load();
  }
  set narrow(v) { this._narrow = v; }
  set panel(v) { this._panel = v; }

  connectedCallback() {
    this._interval = setInterval(() => this._load(), 15000);
    this._render();
  }
  disconnectedCallback() {
    clearInterval(this._interval);
    clearInterval(this._timer);
  }

  async _ws(type, extra = {}) {
    return this._hass.callWS({ type: `simple_aisle/${type}`, ...extra });
  }

  async _load() {
    if (!this._hass) return;
    try {
      this._data = await this._ws("state");
      this._error = "";
    } catch (e) {
      this._error = e.message || String(e);
    }
    this._render();
  }

  async _act(fn) {
    try {
      await fn();
      this._error = "";
    } catch (e) {
      this._error = e.message || String(e);
    }
    await this._load();
  }

  async _startPair() {
    try {
      this._pair = await this._ws("pair");
      this._error = "";
      clearInterval(this._timer);
      this._timer = setInterval(() => {
        if (this._pair && this._pair.expires_at * 1000 < Date.now()) { this._pair = null; clearInterval(this._timer); }
        this._render();
      }, 1000);
    } catch (e) {
      this._error = e.message || String(e);
    }
    this._render();
  }

  _storeName(id) {
    const link = (this._data?.links || []).find((l) => l.store_id === id);
    return link?.store_name || "Unknown store";
  }

  _listName(entityId) {
    const list = (this._data?.lists || []).find((l) => l.entity_id === entityId);
    return list?.name || entityId;
  }

  _render() {
    const d = this._data;
    const pair = this._pair;
    const left = pair ? Math.max(0, Math.round(pair.expires_at - Date.now() / 1000)) : 0;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; padding:16px; color:var(--primary-text-color); font-family:var(--paper-font-body1_-_font-family, system-ui); }
        .wrap { max-width:960px; margin:0 auto; display:grid; gap:16px; }
        header { display:flex; align-items:center; gap:12px; }
        header h1 { font-size:24px; margin:0; font-weight:500; flex:1; }
        .card { background:var(--card-background-color); border-radius:var(--ha-card-border-radius,12px); box-shadow:var(--ha-card-box-shadow,none); border:1px solid var(--divider-color); padding:16px; }
        .card h2 { margin:0 0 12px; font-size:18px; font-weight:500; }
        .muted { color:var(--secondary-text-color); font-size:14px; }
        .row { display:flex; align-items:center; gap:12px; padding:10px 0; border-top:1px solid var(--divider-color); }
        .row:first-of-type { border-top:none; }
        .row .main { flex:1; min-width:0; }
        .row .title { font-weight:500; }
        button { font:inherit; border-radius:8px; border:1px solid var(--divider-color); background:var(--secondary-background-color); color:var(--primary-text-color); padding:6px 12px; cursor:pointer; }
        button.primary { background:var(--primary-color); color:var(--text-primary-color,#fff); border-color:transparent; }
        button.danger { color:var(--error-color); }
        .pair { display:flex; gap:24px; flex-wrap:wrap; align-items:flex-start; }
        .qr { background:#fff; padding:8px; border-radius:8px; width:240px; }
        .qr svg { width:100%; height:auto; display:block; }
        code { font-family:ui-monospace, Menlo, monospace; font-size:15px; word-break:break-all; background:var(--secondary-background-color); padding:2px 6px; border-radius:6px; }
        .badge { font-size:12px; padding:2px 8px; border-radius:10px; background:var(--secondary-background-color); }
        .badge.active { background:rgba(67,160,71,.18); color:var(--success-color,#43a047); }
        .badge.suspended, .badge.list_missing, .badge.failed { background:rgba(219,68,55,.15); color:var(--error-color); }
        .error { color:var(--error-color); }
        ol { margin:8px 0 0 18px; padding:0; } li { margin:4px 0; }
      </style>
      <div class="wrap">
        <header>
          <h1>Simple Aisle</h1>
          <button id="sync" title="Sends your notification so a phone opens Simple Aisle">Request sync</button>
        </header>
        ${this._error ? `<div class="card error">${esc(this._error)}</div>` : ""}
        ${!d ? `<div class="card muted">Loading…</div>` : `
        <div class="card">
          <h2>Status</h2>
          <div class="muted">
            ${d.household_id ? `Connected household: <b>${esc(d.household_name || "Simple Aisle household")}</b>` : "Not connected to a household yet. Pair a device to start."}
            · Changes waiting for a phone: <b>${d.pending}</b>
            · Last sync: ${esc(ago(d.stats?.last_sync))}
          </div>
          <p class="muted">Home Assistant can't wake Simple Aisle on an iPhone. Changes made here reach Simple Aisle the next time a paired phone or iPad syncs: when it's opened, on its sync schedule (iOS decides the exact time), or from a <i>Request sync</i> notification.</p>
        </div>

        <div class="card">
          <h2>Pair a device</h2>
          ${pair ? `
            <div class="pair">
              <div class="qr">${pair.svg}</div>
              <div>
                <ol>
                  <li>On the iPhone or iPad, open <b>Simple Aisle → Settings → Integrations → Home Assistant</b>.</li>
                  <li>Tap <b>Connect Home Assistant</b> and scan this code. The Camera app works too.</li>
                </ol>
                <p class="muted">Can't scan? Choose <i>Enter code instead</i> in Simple Aisle and type:</p>
                <p>Address: ${pair.urls.map((u) => `<code>${esc(u)}</code>`).join(" or ")}</p>
                <p>Code: <code>${esc(pair.code)}</code></p>
                <p class="muted">Works once, for ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} more. Each device gets its own key, which you can revoke below.</p>
                <button id="cancelpair">Done</button>
              </div>
            </div>` : `
            <p class="muted">Shows a one-time code (valid 10 minutes) for Simple Aisle to scan. Store choices are shared by the whole household, so each extra phone only needs to pair.</p>
            <button class="primary" id="pair">Pair device</button>`}
          ${d.urls?.length ? "" : `<p class="error">Home Assistant has no local or remote URL set. Set one in Settings → System → Network.</p>`}
        </div>

        <div class="card">
          <h2>Stores and lists</h2>
          ${d.links.length ? d.links.map((l) => `
            <div class="row">
              <div class="main">
                <div class="title">${esc(l.store_name)} → ${esc(this._listName(l.entity_id))}</div>
                <div class="muted">${esc(l.entity_id)} · linked ${esc(when(l.created_at))}</div>
              </div>
              <span class="badge ${esc(l.state)}">${esc({ active: "Syncing", suspended: "Paused", list_missing: "List missing" }[l.state] || l.state)}</span>
              <button data-unlink="${esc(l.store_id)}">Unlink…</button>
            </div>`).join("") : `<p class="muted">No stores linked yet. Choose stores in Simple Aisle (Settings → Integrations → Home Assistant).</p>`}
        </div>

        <div class="card">
          <h2>Devices</h2>
          ${d.devices.length ? d.devices.map((dev) => `
            <div class="row">
              <div class="main">
                <div class="title">${esc(dev.name)}</div>
                <div class="muted">Paired ${esc(when(dev.created_at))} · last seen ${esc(ago(dev.last_seen))}${dev.app_version ? ` · Simple Aisle ${esc(dev.app_version)}` : ""}</div>
              </div>
              <button class="danger" data-revoke="${esc(dev.device_id)}" data-name="${esc(dev.name)}">Revoke</button>
            </div>`).join("") : `<p class="muted">No devices paired.</p>`}
        </div>

        <div class="card">
          <h2>Changes from Home Assistant</h2>
          ${d.ops.length ? d.ops.map((op) => `
            <div class="row">
              <div class="main">
                <div class="title">${esc(OP_LABELS[op.type] || op.type)} · ${esc(this._storeName(op.store_id))}</div>
                <div class="muted">${esc(ago(op.created_at))}${op.attempts ? ` · tried ${op.attempts}×` : ""}${op.last_reason ? ` · ${esc(op.last_reason)}` : ""}</div>
              </div>
              <span class="badge ${esc(op.state)}">${op.state === "failed" ? "Failed" : op.lease_device ? "Syncing" : "Waiting"}</span>
              ${op.state === "failed" ? `<button data-retry="${esc(op.op_id)}">Retry</button>` : ""}
              <button data-discard="${esc(op.op_id)}">Discard</button>
            </div>`).join("") : `<p class="muted">Nothing waiting.</p>`}
        </div>`}
      </div>`;

    const $ = (sel) => this.shadowRoot.querySelector(sel);
    $("#sync")?.addEventListener("click", () => this._act(async () => {
      const r = await this._ws("request_sync");
      if (!r.notifications_sent) throw new Error("No notify service is set. Choose one in the integration's options (Settings → Devices & services → Simple Aisle → Configure).");
    }));
    $("#pair")?.addEventListener("click", () => this._startPair());
    $("#cancelpair")?.addEventListener("click", () => { this._pair = null; this._load(); });
    this.shadowRoot.querySelectorAll("[data-revoke]").forEach((b) => b.addEventListener("click", () => {
      if (confirm(`Revoke ${b.dataset.name}? It stops syncing until it pairs again.`)) this._act(() => this._ws("revoke", { device_id: b.dataset.revoke }));
    }));
    this.shadowRoot.querySelectorAll("[data-unlink]").forEach((b) => b.addEventListener("click", () => {
      if (!confirm("Unlink this store? Simple Aisle stops syncing it.")) return;
      const remove = confirm("Also remove the items Simple Aisle put on the list?\n\nOK = remove them, Cancel = leave them on the list. Items you added in Home Assistant yourself are never removed.");
      this._act(() => this._ws("unlink", { store_id: b.dataset.unlink, remove_items: remove }));
    }));
    this.shadowRoot.querySelectorAll("[data-retry]").forEach((b) => b.addEventListener("click", () => this._act(() => this._ws("retry", { op_id: b.dataset.retry }))));
    this.shadowRoot.querySelectorAll("[data-discard]").forEach((b) => b.addEventListener("click", () => {
      if (confirm("Discard this change? Simple Aisle won't receive it.")) this._act(() => this._ws("discard", { op_id: b.dataset.discard }));
    }));
  }
}

customElements.define("simple-aisle-panel", SimpleAislePanel);
