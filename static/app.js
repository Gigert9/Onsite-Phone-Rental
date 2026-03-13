let activeEvent = null;
let exhibitors = [];
let currentAction = null; // { type: 'dropoff'|'pickup', eventExhibitorId, reservedPhones, displayName }
let currentExpected = null;

let passwordFlow = null; // { event, mode, resolve, reject }
let confirmFlow = null; // { title, message, okText, cancelText, onOk }

const $ = (id) => document.getElementById(id);

function show(el, yes) {
  el.classList.toggle("hidden", !yes);
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
  const res = await fetch(path, options);
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      msg = body.detail || msg;
    } catch {}
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return await res.json();
  return await res.text();
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
  await loadExhibitors();
}

async function loadExhibitors() {
  const list = $("exhibitorsList");
  list.textContent = "Loading...";
  exhibitors = await apiEvent(activeEvent.event_id, `/api/events/${activeEvent.event_id}/exhibitors`);

  if (!exhibitors.length) {
    list.innerHTML = '<div class="muted">No exhibitors yet. Import the Excel file.</div>';
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
      statusBadges.push(`<span class="badge">Dropped Off: -/${x.reserved_phones}</span>`);
    } else {
      const dropMismatch = dropoffTotal > x.reserved_phones;
      statusBadges.push(
        `<span class="badge ${dropMismatch ? "badge--warn" : ""}">Dropped Off: ${dropoffTotal}/${x.reserved_phones}</span>`
      );
    }

    if (!pickupTotal) {
      statusBadges.push(`<span class="badge">Picked up: -/${expectedPickup}</span>`);
    } else {
      const pickMismatch = pickupTotal > expectedPickup;
      statusBadges.push(
        `<span class="badge ${pickMismatch ? "badge--warn" : ""}">Picked up: ${pickupTotal}/${expectedPickup}</span>`
      );
    }

    div.innerHTML = `
      <div>
        <div class="item__title"></div>
        <div class="item__meta">${statusBadges.join(" ")}</div>
      </div>
      <div class="item__right">
        <button class="btn">Drop-off</button>
        <button class="btn">Pick-up</button>
      </div>
    `;
    div.querySelector(".item__title").textContent = x.display_name;

    const [dropBtn, pickBtn] = div.querySelectorAll("button");
    dropBtn.addEventListener("click", () => openActionSheet("dropoff", x));
    pickBtn.addEventListener("click", () => openActionSheet("pickup", x));

    list.appendChild(div);
  }
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

  $("sheetTitle").textContent = type === "dropoff" ? "Drop-off" : "Pick-up";
  const expectedLabel = type === "dropoff" ? "Reserved" : "Expected pick-up";
  const alreadyLabel = type === "dropoff" ? "Already dropped off" : "Already picked up";
  $("sheetSub").textContent = `${x.display_name} • Reserved: ${x.reserved_phones} • ${expectedLabel}: ${currentExpected} • ${alreadyLabel}: ${already}`;
  $("confirmPhones").value = String(remaining > 0 ? remaining : 1);
  $("printedName").value = "";
  $("noteText").value = "";
  $("actionStatus").textContent = "";

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
    await apiEvent(activeEvent.event_id, `/api/event-exhibitors/${currentAction.eventExhibitorId}/${currentAction.type}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        confirmed_phones: confirmed,
        printed_name: printedName,
        signature: sigPad.toDataURL(),
        note,
      }),
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

$("downloadReport").addEventListener("click", async (e) => {
  e.preventDefault();
  if (!activeEvent) return;
  try {
    const csv = await apiEvent(
      activeEvent.event_id,
      `/api/events/${activeEvent.event_id}/report?format=csv`
    );
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const safeName = String(activeEvent.name || "event").replace(/[^a-z0-9-_]+/gi, "_");
    a.href = url;
    a.download = `${safeName}_report.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    alert(err.message || String(err));
  }
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
