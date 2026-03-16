let activeEvent = null;
let exhibitors = [];
let currentAction = null; // { type: 'dropoff'|'pickup', eventExhibitorId, reservedPhones, displayName }
let currentExpected = null;

let passwordFlow = null; // { event, mode, resolve, reject }
let confirmFlow = null; // { title, message, okText, cancelText, onOk }
let exhibitorFlow = null; // { mode: 'add'|'edit', eventExhibitorId }

let reportsOpen = false;

let signaturesFlow = null; // { eventExhibitorId, displayName }

function labelForActionType(type) {
  return type === "dropoff" ? "Sign Out" : "Sign In";
}

function labelPastTense(type) {
  return type === "dropoff" ? "Signed Out" : "Signed In";
}

function setEventTotals(rows) {
  const el = $("eventTotals");
  if (!el) return;

  const safeRows = Array.isArray(rows) ? rows : [];
  let reserved = 0;
  let signedOut = 0;
  let signedIn = 0;

  for (const x of safeRows) {
    reserved += Number(x?.reserved_phones || 0);
    signedOut += Number(x?.dropoff_confirmed_phones || 0);
    signedIn += Number(x?.pickup_confirmed_phones || 0);
  }

  el.innerHTML = [
    `<span class="badge">Reserved: ${reserved}</span>`,
    `<span class="badge">Signed Out: ${signedOut}</span>`,
    `<span class="badge">Signed In: ${signedIn}</span>`,
  ].join("");
}

const $ = (id) => document.getElementById(id);
const BASE_PATH = window.location.pathname.replace(/\/$/, "");

function withBase(path) {
  if (!BASE_PATH || BASE_PATH === "/") return path; // running at site root
  if (path.startsWith("/")) return `${BASE_PATH}${path}`;
  return `${BASE_PATH}/${path}`;
}

function show(el, yes) {
  el.classList.toggle("hidden", !yes);
}

function onDoubleTap(el, handler, maxDelayMs = 320) {
  let lastTap = 0;
  el.addEventListener(
    "touchend",
    (e) => {
      const now = Date.now();
      if (now - lastTap <= maxDelayMs) {
        lastTap = 0;
        e.preventDefault();
        handler(e);
        return;
      }
      lastTap = now;
    },
    { passive: false }
  );
}

function setActiveEventHeader() {
  const el = $("activeEvent");
  if (!activeEvent) {
    el.textContent = "";
    return;
  }
  el.textContent = activeEvent.name;
}

async function api(path, options) {
  const res = await fetch(withBase(path), options);
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      msg = body.detail || msg;
    } catch {}
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return await res.json();
  return await res.text();
}

function isUnauthorizedError(err) {
  return err && (err.status === 401 || err.status === 403);
}

async function bounceToEventsView() {
  activeEvent = null;
  setActiveEventHeader();
  show($("eventView"), false);
  show($("eventsView"), true);
  await loadEvents();
}

function _tokenKey(eventId) {
  return `event_token_${eventId}`;
}

function getEventToken(eventId) {
  if (!eventId) return null;
  return sessionStorage.getItem(_tokenKey(eventId));
}

function setEventToken(eventId, token) {
  sessionStorage.setItem(_tokenKey(eventId), token);
}

async function apiEvent(eventId, path, options = {}) {
  const token = getEventToken(eventId);
  const headers = new Headers(options.headers || {});
  if (token) headers.set("X-Event-Token", token);
  return await api(path, { ...options, headers });
}

async function apiEventBlob(eventId, path, options = {}) {
  const token = getEventToken(eventId);
  const headers = new Headers(options.headers || {});
  if (token) headers.set("X-Event-Token", token);

  const res = await fetch(withBase(path), { ...options, headers });
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      msg = body.detail || msg;
    } catch {}
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return await res.blob();
}

async function loadEvents() {
  const list = $("eventsList");
  list.textContent = "Loading...";
  const events = await api("/api/events");

  if (!events.length) {
    list.innerHTML = '<div class="muted">No events yet.</div>';
    return;
  }

  list.innerHTML = "";
  for (const ev of events) {
    const div = document.createElement("div");
    div.className = "item";
    div.innerHTML = `
      <div>
        <div class="item__title"></div>
        <div class="item__meta">Created: ${ev.created_at}</div>
      </div>
      <div class="item__right">
        <button class="btn btn--primary">Open</button>
        <button class="btn">Delete</button>
      </div>
    `;
    div.querySelector(".item__title").textContent = ev.name;

    const [openBtn, delBtn] = div.querySelectorAll("button");
    openBtn.addEventListener("click", () => openEvent(ev));
    delBtn.addEventListener("click", () => confirmDeleteEvent(ev));
    list.appendChild(div);
  }
}

function removeEventToken(eventId) {
  sessionStorage.removeItem(_tokenKey(eventId));
}

function openConfirmSheet({ title, message, okText = "OK", cancelText = "Cancel", onOk }) {
  const sheet = $("confirmSheet");
  $("confirmTitle").textContent = title;
  $("confirmMessage").textContent = message;
  $("confirmOkBtn").textContent = okText;
  $("confirmCancelBtn").textContent = cancelText;
  $("confirmStatus").textContent = "";

  confirmFlow = { title, message, okText, cancelText, onOk };
  show(sheet, true);
  sheet.setAttribute("aria-hidden", "false");
}

function closeConfirmSheet() {
  const sheet = $("confirmSheet");
  show(sheet, false);
  sheet.setAttribute("aria-hidden", "true");
  $("confirmStatus").textContent = "";
  confirmFlow = null;
}

async function confirmDeleteEvent(ev) {
  openConfirmSheet({
    title: "Delete Event",
    message: `Delete “${ev.name}”? This is permanent and cannot be undone.`,
    okText: "Delete",
    cancelText: "Cancel",
    onOk: async () => {
      const status = $("confirmStatus");
      status.textContent = "Deleting...";
      await api(`/api/events/${ev.event_id}`, { method: "DELETE" });
      removeEventToken(ev.event_id);

      // If they delete the currently open event, bounce back.
      if (activeEvent && activeEvent.event_id === ev.event_id) {
        activeEvent = null;
        setActiveEventHeader();
        show($("eventView"), false);
        show($("eventsView"), true);
      }

      closeConfirmSheet();
      await loadEvents();
    },
  });
}

async function openEvent(ev) {
  try {
    await ensureEventAccess(ev);
  } catch {
    return;
  }

  activeEvent = ev;
  setActiveEventHeader();
  $("eventTitle").textContent = ev.name;
  show($("eventsView"), false);
  show($("eventView"), true);
  $("importStatus").textContent = "";
  try {
    await loadExhibitors();
  } catch (e) {
    // Avoid leaving the UI in a perpetual "Loading..." state.
    $("exhibitorsList").textContent = e.message || String(e);
  }
}

async function loadExhibitors(_retry = false) {
  const list = $("exhibitorsList");
  list.textContent = "Loading...";

  try {
    exhibitors = await apiEvent(activeEvent.event_id, `/api/events/${activeEvent.event_id}/exhibitors`);
  } catch (e) {
    // If the token expired (or the server restarted and lost in-memory tokens),
    // clear the stored token and bounce back to Events so the user can re-open and re-auth.
    if (!_retry && activeEvent && isUnauthorizedError(e)) {
      removeEventToken(activeEvent.event_id);
      await bounceToEventsView();
      return;
    }
    throw e;
  }

  setEventTotals(exhibitors);

  if (!exhibitors.length) {
    list.innerHTML = '<div class="muted">No exhibitors yet. Import the Excel file or add one manually.</div>';
    return;
  }

  list.innerHTML = "";
  for (const x of exhibitors) {
    const div = document.createElement("div");

    const dropoffTotal = x.dropoff_confirmed_phones != null ? x.dropoff_confirmed_phones : 0;
    const pickupTotal = x.pickup_confirmed_phones != null ? x.pickup_confirmed_phones : 0;
    const expectedPickup = dropoffTotal > 0 ? dropoffTotal : x.reserved_phones;
    const pickupComplete = pickupTotal >= expectedPickup && expectedPickup >= 0;
    const dropoffDone = dropoffTotal > 0;

    div.className = "item";
    if (pickupComplete) div.classList.add("item--pickup");
    else if (dropoffDone) div.classList.add("item--dropoff");

    const statusBadges = [];
    statusBadges.push(`<span class="badge">Reserved: ${x.reserved_phones}</span>`);

    if (!dropoffDone) {
      statusBadges.push(`<span class="badge">Signed Out: -/${x.reserved_phones}</span>`);
    } else {
      const dropMismatch = dropoffTotal > x.reserved_phones;
      statusBadges.push(
        `<span class="badge ${dropMismatch ? "badge--warn" : ""}">Signed Out: ${dropoffTotal}/${x.reserved_phones}</span>`
      );
    }

    if (!pickupTotal) {
      statusBadges.push(`<span class="badge">Signed In: -/${expectedPickup}</span>`);
    } else {
      const pickMismatch = pickupTotal > expectedPickup;
      statusBadges.push(
        `<span class="badge ${pickMismatch ? "badge--warn" : ""}">Signed In: ${pickupTotal}/${expectedPickup}</span>`
      );
    }

    const boothText = x.booth ? String(x.booth) : "";
    const boothSuffix = boothText ? ` / ${boothText}` : "";
    const sigLinkHtml = x.has_signature ? '<a class="link item__sigLink" href="#">View signatures</a>' : "";

    div.innerHTML = `
      <button class="item__delete" type="button" aria-label="Delete exhibitor" title="Delete">X</button>
      <div>
        <div class="item__titleRow">
          <div class="item__title item__name" role="button" tabindex="0" title="Edit exhibitor"></div>
          <div class="item__booth" role="button" tabindex="0" title="Edit booth"></div>
        </div>
        <div class="item__meta">
          <div class="item__badges">${statusBadges.join("")}</div>
          ${sigLinkHtml}
        </div>
      </div>
      <div class="item__right">
        <button class="btn">Sign Out</button>
        <button class="btn">Sign In</button>
      </div>
    `;

    const nameEl = div.querySelector(".item__name");
    const boothEl = div.querySelector(".item__booth");
    nameEl.textContent = x.name;
    boothEl.textContent = boothSuffix;

    const delBtn = div.querySelector(".item__delete");
    const [dropBtn, pickBtn] = div.querySelectorAll(".item__right button");
    dropBtn.addEventListener("click", () => openActionSheet("dropoff", x));
    pickBtn.addEventListener("click", () => openActionSheet("pickup", x));

    const openEdit = () => openExhibitorSheet("edit", x);
    nameEl.addEventListener("dblclick", openEdit);
    boothEl.addEventListener("dblclick", openEdit);
    onDoubleTap(nameEl, openEdit);
    onDoubleTap(boothEl, openEdit);

    // Front-end guard for clarity; back-end enforces the rule.
    if (dropoffDone) delBtn.disabled = true;
    delBtn.addEventListener("click", () => {
      if (delBtn.disabled) return;
      confirmDeleteExhibitor(x);
    });

    const sigLink = div.querySelector(".item__sigLink");
    if (sigLink) {
      sigLink.addEventListener("click", (e) => {
        e.preventDefault();
        openSignaturesSheet(x);
      });
    }

    list.appendChild(div);
  }
}

function openSignaturesSheet(x) {
  if (!activeEvent) return;
  signaturesFlow = { eventExhibitorId: x.event_exhibitor_id, displayName: x.display_name };
  $("signaturesTitle").textContent = "Signatures";
  $("signaturesSub").textContent = x.display_name;
  $("signaturesStatus").textContent = "Loading...";
  $("signaturesList").innerHTML = "";

  const sheet = $("signaturesSheet");
  show(sheet, true);
  sheet.setAttribute("aria-hidden", "false");

  loadSignatures().catch((e) => {
    $("signaturesStatus").textContent = e.message || String(e);
  });
}

function closeSignaturesSheet() {
  signaturesFlow = null;
  const sheet = $("signaturesSheet");
  show(sheet, false);
  sheet.setAttribute("aria-hidden", "true");
  $("signaturesStatus").textContent = "";
  $("signaturesList").innerHTML = "";
}

async function loadSignatures() {
  if (!activeEvent || !signaturesFlow) return;
  const list = $("signaturesList");
  const status = $("signaturesStatus");
  status.textContent = "";

  const rows = await apiEvent(
    activeEvent.event_id,
    `/api/event-exhibitors/${signaturesFlow.eventExhibitorId}/actions`
  );

  if (!rows.length) {
    list.innerHTML = '<div class="muted">No signed actions yet.</div>';
    return;
  }

  list.innerHTML = "";
  for (const r of rows) {
    const div = document.createElement("div");
    div.className = "item";
    const typeLabel = labelForActionType(r.action_type);
    const when = r.action_at || "";
    const who = r.printed_name || "";
    const note = r.note || "";
    const hasSig = !!r.has_signature;

    div.innerHTML = `
      <div>
        <div class="item__title">${typeLabel} • ${r.quantity} phone(s)</div>
        <div class="item__meta">${when}${who ? ` • ${who}` : ""}${note ? ` • ${note}` : ""}</div>
      </div>
      <div class="item__right">
        <button class="btn btn--small" ${hasSig ? "" : "disabled"}>Open signature</button>
      </div>
    `;

    const btn = div.querySelector("button");
    btn.addEventListener("click", async () => {
      if (!hasSig) return;
      try {
        const blob = await apiEventBlob(activeEvent.event_id, r.signature_url);
        const url = URL.createObjectURL(blob);
        window.open(url, "_blank", "noopener,noreferrer");
        // Let the new tab load; then revoke after a short delay.
        setTimeout(() => URL.revokeObjectURL(url), 30_000);
      } catch (e) {
        status.textContent = e.message || String(e);
      }
    });

    list.appendChild(div);
  }
}

function openExhibitorSheet(mode, x = null) {
  if (!activeEvent) return;
  exhibitorFlow = { mode, eventExhibitorId: x ? x.event_exhibitor_id : null };

  $("exhibitorTitle").textContent = mode === "add" ? "Add Exhibitor" : "Edit Exhibitor";
  $("exhibitorSub").textContent = activeEvent.name || "";
  $("exhibitorStatus").textContent = "";

  if (mode === "add") {
    $("exhibitorName").value = "";
    $("exhibitorBooth").value = "";
    $("exhibitorReservedPhones").value = "0";
  } else {
    $("exhibitorName").value = x?.name || "";
    $("exhibitorBooth").value = x?.booth || "";
    $("exhibitorReservedPhones").value = String(x?.reserved_phones ?? 0);
  }

  // Only reserved phones is specified for Add; keep it read-only when editing.
  $("exhibitorReservedPhones").disabled = mode !== "add";

  const sheet = $("exhibitorSheet");
  show(sheet, true);
  sheet.setAttribute("aria-hidden", "false");
  setTimeout(() => $("exhibitorName").focus(), 0);
}

function closeExhibitorSheet() {
  exhibitorFlow = null;
  const sheet = $("exhibitorSheet");
  show(sheet, false);
  sheet.setAttribute("aria-hidden", "true");
  $("exhibitorStatus").textContent = "";
}

async function saveExhibitor() {
  if (!activeEvent || !exhibitorFlow) return;
  const status = $("exhibitorStatus");
  const name = $("exhibitorName").value.trim();
  const booth = $("exhibitorBooth").value.trim();
  const reserved = parseInt($("exhibitorReservedPhones").value, 10);

  if (!name) {
    status.textContent = "Exhibitor name is required.";
    return;
  }

  status.textContent = "Saving...";

  try {
    if (exhibitorFlow.mode === "add") {
      if (!Number.isFinite(reserved) || reserved < 0) {
        status.textContent = "Reserved phones must be a valid number.";
        return;
      }
      await apiEvent(activeEvent.event_id, `/api/events/${activeEvent.event_id}/exhibitors`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          booth,
          reserved_phones: reserved,
        }),
      });
    } else {
      await apiEvent(activeEvent.event_id, `/api/event-exhibitors/${exhibitorFlow.eventExhibitorId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, booth }),
      });
    }

    closeExhibitorSheet();
    await loadExhibitors();
  } catch (e) {
    status.textContent = e.message;
  }
}

function confirmDeleteExhibitor(x) {
  openConfirmSheet({
    title: "Delete Exhibitor",
    message: `Delete “${x.display_name}”? This cannot be undone.`,
    okText: "Delete",
    cancelText: "Cancel",
    onOk: async () => {
      const status = $("confirmStatus");
      status.textContent = "Deleting...";
      await apiEvent(activeEvent.event_id, `/api/event-exhibitors/${x.event_exhibitor_id}`, { method: "DELETE" });
      closeConfirmSheet();
      await loadExhibitors();
    },
  });
}

function openActionSheet(type, x) {
  currentAction = {
    type,
    eventExhibitorId: x.event_exhibitor_id,
    reservedPhones: x.reserved_phones,
    displayName: x.display_name,
  };

  const expectedDropoff = x.reserved_phones;
  const expectedPickup =
    x.dropoff_confirmed_phones != null ? x.dropoff_confirmed_phones : x.reserved_phones;
  currentExpected = type === "dropoff" ? expectedDropoff : expectedPickup;
  const already = type === "dropoff" ? (x.dropoff_confirmed_phones || 0) : (x.pickup_confirmed_phones || 0);
  const remaining = Math.max(0, currentExpected - already);

  $("sheetTitle").textContent = labelForActionType(type);
  const expectedLabel = type === "dropoff" ? "Reserved" : "Expected sign in";
  const alreadyLabel = type === "dropoff" ? "Already signed out" : "Already signed in";
  $("sheetSub").textContent = `${x.display_name} • Reserved: ${x.reserved_phones} • ${expectedLabel}: ${currentExpected} • ${alreadyLabel}: ${already}`;
  $("confirmPhones").value = String(remaining > 0 ? remaining : 1);
  $("printedName").value = "";
  $("noteText").value = "";
  $("actionStatus").textContent = "";

  // Phone IDs: editable only on sign out; auto-populated on sign in.
  const idsEl = $("phoneIdsText");
  if (type === "dropoff") {
    idsEl.readOnly = false;
    idsEl.value = "";
  } else {
    idsEl.readOnly = true;
    idsEl.value = (x.dropoff_phone_ids || "").trim();
  }

  // Chargers
  const hasChargerInfo = typeof x.dropoff_confirmed_chargers !== "undefined";
  const dropRow = $("dropoffChargerRow");
  const pickRow = $("pickupChargerRow");
  const chargerIncluded = $("chargerIncluded");
  const chargerQty = $("chargerQty");
  const confirmChargers = $("confirmChargers");

  if (type === "dropoff") {
    show(dropRow, true);
    show(pickRow, false);
    chargerIncluded.checked = false;
    chargerQty.value = "0";
    chargerQty.disabled = true;
  } else {
    show(dropRow, false);
    show(pickRow, hasChargerInfo);
    const expectedCh = x.dropoff_confirmed_chargers != null ? x.dropoff_confirmed_chargers : 0;
    const alreadyCh = x.pickup_confirmed_chargers != null ? x.pickup_confirmed_chargers : 0;
    const remainingCh = Math.max(0, expectedCh - alreadyCh);
    confirmChargers.value = String(remainingCh);
  }

  updateDiscrepancyUI();
  const sheet = $("actionSheet");
  show(sheet, true);
  sheet.setAttribute("aria-hidden", "false");

  // Canvas needs to be resized after it becomes visible,
  // otherwise its backing store can be initialized at 1x1.
  requestAnimationFrame(() => {
    sigPad.resize();
    sigPad.clear();
  });
}

$("chargerIncluded").addEventListener("change", () => {
  const checked = $("chargerIncluded").checked;
  const qty = $("chargerQty");
  qty.disabled = !checked;
  if (!checked) qty.value = "0";
  if (checked && (!qty.value || qty.value === "0")) qty.value = "1";
});

function updateDiscrepancyUI() {
  if (!currentAction) return;
  const confirmed = parseInt($("confirmPhones").value, 10);
  const warn = $("discrepancyWarning");
  const noteLabel = $("noteLabel");

  if (!Number.isFinite(confirmed) || currentExpected == null) {
    show(warn, false);
    show(noteLabel, false);
    warn.textContent = "";
    return;
  }

  const type = currentAction.type;
  const already =
    type === "dropoff"
      ? (exhibitors.find((e) => e.event_exhibitor_id === currentAction.eventExhibitorId)?.dropoff_confirmed_phones || 0)
      : (exhibitors.find((e) => e.event_exhibitor_id === currentAction.eventExhibitorId)?.pickup_confirmed_phones || 0);

  const exceeds = confirmed + already > currentExpected;
  if (!exceeds) {
    show(warn, false);
    show(noteLabel, false);
    warn.textContent = "";
    return;
  }

  warn.textContent = `Too many: expected max ${currentExpected}, already ${already}, adding ${confirmed} would make ${confirmed + already}. A note is required to continue.`;
  show(warn, true);
  show(noteLabel, true);
}

function closeActionSheet() {
  currentAction = null;
  const sheet = $("actionSheet");
  show(sheet, false);
  sheet.setAttribute("aria-hidden", "true");
}

async function doImport() {
  const fileInput = $("excelFile");
  const status = $("importStatus");

  if (!fileInput.files || !fileInput.files.length) {
    status.textContent = "Please choose a file.";
    return;
  }

  const fd = new FormData();
  fd.append("file", fileInput.files[0]);
  status.textContent = "Importing...";

  try {
    const result = await apiEvent(activeEvent.event_id, `/api/events/${activeEvent.event_id}/import-excel`, {
      method: "POST",
      body: fd,
    });
    status.textContent = `Imported rows: ${result.imported_rows}. Created: ${result.created}. Updated: ${result.updated}.`;
    await loadExhibitors();
  } catch (e) {
    status.textContent = e.message;
  }
}

async function saveAction() {
  if (!currentAction) return;

  const status = $("actionStatus");
  const confirmed = parseInt($("confirmPhones").value, 10);
  const printedName = $("printedName").value.trim();
  const note = $("noteText").value.trim();
  const phoneIds = $("phoneIdsText").value.trim();

  if (!Number.isFinite(confirmed) || confirmed < 0) {
    status.textContent = "Please enter a valid phone count.";
    return;
  }
  if (!printedName) {
    status.textContent = "Printed name is required.";
    return;
  }
  if (!sigPad.hasInk()) {
    status.textContent = "Signature is required.";
    return;
  }

  const x = exhibitors.find((e) => e.event_exhibitor_id === currentAction.eventExhibitorId);
  const already =
    currentAction.type === "dropoff" ? (x?.dropoff_confirmed_phones || 0) : (x?.pickup_confirmed_phones || 0);
  if (currentExpected != null && confirmed + already > currentExpected && !note) {
    status.textContent = "A note is required when the total would exceed the expected count.";
    return;
  }

  status.textContent = "Saving...";

  try {
    const payload = {
      confirmed_phones: confirmed,
      printed_name: printedName,
      signature: sigPad.toDataURL(),
      note,
    };

    if (currentAction.type === "dropoff") {
      if (confirmed > 0) {
        if (!phoneIds) {
          status.textContent = "Phone ID numbers are required when signing out phones.";
          return;
        }
        const parts = phoneIds
          .split(/[\r\n,;]+/)
          .map((s) => s.trim())
          .filter(Boolean);
        if (parts.length !== confirmed) {
          status.textContent = `Please provide exactly ${confirmed} phone ID number(s). Got ${parts.length}.`;
          return;
        }
      }

      payload.phone_ids = phoneIds;
      payload.charger_included = $("chargerIncluded").checked;
      payload.charger_qty = parseInt($("chargerQty").value, 10);
    } else {
      const c = parseInt($("confirmChargers").value, 10);
      if (Number.isFinite(c) && c >= 0) payload.confirmed_chargers = c;
    }

    await apiEvent(activeEvent.event_id, `/api/event-exhibitors/${currentAction.eventExhibitorId}/${currentAction.type}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    status.textContent = "Saved.";
    closeActionSheet();
    await loadExhibitors();
  } catch (e) {
    status.textContent = e.message;
  }
}

// --- Signature pad (pointer events) ---
const sigPad = (() => {
  const canvas = $("sigCanvas");
  const ctx = canvas.getContext("2d");

  let drawing = false;
  let ink = false;
  let last = null;

  function resizeForDPR() {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.floor(rect.width * dpr));
    const h = Math.max(1, Math.floor(rect.height * dpr));

    if (canvas.width !== w || canvas.height !== h) {
      const img = ink ? canvas.toDataURL("image/png") : null;
      canvas.width = w;
      canvas.height = h;

      // Reset transform so scaling doesn't compound across resizes.
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.scale(dpr, dpr);

      clear();

      if (img) {
        // restore existing strokes roughly
        const im = new Image();
        im.onload = () => ctx.drawImage(im, 0, 0, rect.width, rect.height);
        im.src = img;
      }
    }
  }

  function clear() {
    const rect = canvas.getBoundingClientRect();
    ctx.clearRect(0, 0, rect.width, rect.height);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, rect.width, rect.height);
    ctx.strokeStyle = "#111827";
    ctx.lineWidth = 2.5;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    drawing = false;
    last = null;
    ink = false;
  }

  function pointFromEvent(ev) {
    const rect = canvas.getBoundingClientRect();
    return {
      x: ev.clientX - rect.left,
      y: ev.clientY - rect.top,
    };
  }

  function down(ev) {
    ev.preventDefault();
    canvas.setPointerCapture(ev.pointerId);
    drawing = true;
    last = pointFromEvent(ev);
  }

  function move(ev) {
    if (!drawing) return;
    ev.preventDefault();
    const p = pointFromEvent(ev);
    if (!last) last = p;

    ctx.beginPath();
    ctx.moveTo(last.x, last.y);
    ctx.lineTo(p.x, p.y);
    ctx.stroke();

    last = p;
    ink = true;
  }

  function up(ev) {
    if (!drawing) return;
    ev.preventDefault();
    drawing = false;
    last = null;
  }

  function hasInk() {
    return ink;
  }

  function toDataURL() {
    return canvas.toDataURL("image/png");
  }

  window.addEventListener("resize", () => {
    resizeForDPR();
  });

  canvas.addEventListener("pointerdown", down);
  canvas.addEventListener("pointermove", move);
  canvas.addEventListener("pointerup", up);
  canvas.addEventListener("pointercancel", up);

  // init
  setTimeout(() => {
    resizeForDPR();
    clear();
  }, 0);

  return { clear, hasInk, toDataURL, resize: resizeForDPR };
})();

// --- Wire up UI ---
$("createEventBtn").addEventListener("click", async () => {
  const name = $("eventName").value.trim();
  if (!name) return;

  const created = await api("/api/events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });

  $("eventName").value = "";
  await loadEvents();

  // Immediately set password and unlock for the new event.
  const ev = { event_id: created.event_id, name: created.name, has_password: false };
  try {
    await ensureEventAccess(ev);
    await loadEvents();
    await openEvent({ ...ev, has_password: true });
  } catch {
    // user cancelled
  }
});

$("importBtn").addEventListener("click", doImport);
$("backToEventsBtn").addEventListener("click", async () => {
  activeEvent = null;
  setActiveEventHeader();
  show($("eventView"), false);
  show($("eventsView"), true);
  await loadEvents();
});

$("closeSheetBtn").addEventListener("click", closeActionSheet);
$("actionSheet").addEventListener("click", (e) => {
  if (e.target === $("actionSheet")) closeActionSheet();
});

$("clearSigBtn").addEventListener("click", () => sigPad.clear());
$("saveActionBtn").addEventListener("click", saveAction);
$("confirmPhones").addEventListener("input", updateDiscrepancyUI);

function setReportsMenuOpen(open) {
  reportsOpen = !!open;
  const menu = $("reportsMenu");
  show(menu, reportsOpen);
  menu.setAttribute("aria-hidden", reportsOpen ? "false" : "true");
}

$("reportsBtn").addEventListener("click", () => {
  setReportsMenuOpen(!reportsOpen);
});

document.addEventListener("click", (e) => {
  if (!reportsOpen) return;
  const root = $("reportsDropdown");
  if (root && !root.contains(e.target)) setReportsMenuOpen(false);
});

async function downloadCsvReport(path, filenameSuffix) {
  if (!activeEvent) return;
  const csv = await apiEvent(activeEvent.event_id, path);
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const safeName = String(activeEvent.name || "event").replace(/[^a-z0-9-_]+/gi, "_");
  a.href = url;
  a.download = `${safeName}_${filenameSuffix}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

$("downloadHistoryBtn").addEventListener("click", async () => {
  try {
    setReportsMenuOpen(false);
    await downloadCsvReport(`/api/events/${activeEvent.event_id}/report?format=csv`, "exhibitor_history");
  } catch (err) {
    alert(err.message || String(err));
  }
});

$("downloadOverviewBtn").addEventListener("click", async () => {
  try {
    setReportsMenuOpen(false);
    await downloadCsvReport(`/api/events/${activeEvent.event_id}/overview?format=csv`, "exhibitor_overview");
  } catch (err) {
    alert(err.message || String(err));
  }
});

// --- Exhibitor modal wiring ---
$("addExhibitorBtn").addEventListener("click", () => openExhibitorSheet("add"));
$("closeExhibitorBtn").addEventListener("click", closeExhibitorSheet);
$("exhibitorSheet").addEventListener("click", (e) => {
  if (e.target === $("exhibitorSheet")) closeExhibitorSheet();
});
$("saveExhibitorBtn").addEventListener("click", saveExhibitor);

// --- Signatures modal wiring ---
$("closeSignaturesBtn").addEventListener("click", closeSignaturesSheet);
$("signaturesSheet").addEventListener("click", (e) => {
  if (e.target === $("signaturesSheet")) closeSignaturesSheet();
});

// --- Confirm modal wiring ---
$("closeConfirmBtn").addEventListener("click", closeConfirmSheet);
$("confirmCancelBtn").addEventListener("click", closeConfirmSheet);
$("confirmOkBtn").addEventListener("click", async () => {
  if (!confirmFlow) return;
  try {
    await confirmFlow.onOk();
  } catch (e) {
    $("confirmStatus").textContent = e.message || String(e);
  }
});

// --- Password modal flow ---
function openPasswordSheet(mode, ev) {
  const sheet = $("passwordSheet");
  const title = $("passwordTitle");
  const sub = $("passwordSub");
  const confirmLabel = $("passwordConfirmLabel");
  const saveBtn = $("savePasswordBtn");

  title.textContent = mode === "set" ? "Set Event Password" : "Enter Event Password";
  sub.textContent = ev?.name ? ev.name : "";
  show(confirmLabel, mode === "set");
  saveBtn.textContent = mode === "set" ? "Save" : "Unlock";
  $("passwordValue").value = "";
  $("passwordConfirm").value = "";
  $("passwordStatus").textContent = "";
  show(sheet, true);
  sheet.setAttribute("aria-hidden", "false");
  setTimeout(() => $("passwordValue").focus(), 0);
}

function closePasswordSheet(cancel = true) {
  const sheet = $("passwordSheet");
  show(sheet, false);
  sheet.setAttribute("aria-hidden", "true");
  $("passwordStatus").textContent = "";

  if (cancel && passwordFlow) {
    const rej = passwordFlow.reject;
    passwordFlow = null;
    rej(new Error("Cancelled"));
  }
}

async function ensureEventAccess(ev) {
  const existing = getEventToken(ev.event_id);
  if (existing) return;

  const mode = ev.has_password ? "unlock" : "set";
  if (passwordFlow) throw new Error("Password prompt already open");
  return await new Promise((resolve, reject) => {
    passwordFlow = { event: ev, mode, resolve, reject };
    openPasswordSheet(mode, ev);
  });
}

$("closePasswordBtn").addEventListener("click", () => closePasswordSheet(true));

$("savePasswordBtn").addEventListener("click", async () => {
  if (!passwordFlow) return;

  const { event: ev, mode } = passwordFlow;
  const status = $("passwordStatus");
  const pwd = $("passwordValue").value;
  const confirm = $("passwordConfirm").value;

  if (!pwd || pwd.length < 4) {
    status.textContent = "Password must be at least 4 characters.";
    return;
  }

  try {
    status.textContent = mode === "set" ? "Saving password..." : "Unlocking...";

    if (mode === "set") {
      if (pwd !== confirm) {
        status.textContent = "Passwords do not match.";
        return;
      }
      await api(`/api/events/${ev.event_id}/set-password`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pwd }),
      });
    }

    const unlocked = await api(`/api/events/${ev.event_id}/unlock`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pwd }),
    });
    setEventToken(ev.event_id, unlocked.token);

    const resolve = passwordFlow.resolve;
    passwordFlow = null;
    closePasswordSheet(false);
    resolve(true);
  } catch (e) {
    status.textContent = e.message;
  }
});

// start
loadEvents().catch((e) => {
  $("eventsList").textContent = e.message;
});
