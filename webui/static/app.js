/* Global connection controller shared by every page.
   Owns the nav status widget + connect/disconnect + record toggle.
   Pages use window.api(...) and window.canStatus() for their own data. */
(function () {
  "use strict";
  const $ = s => document.querySelector(s);

  window.api = (path, body) => fetch(path, {
    method: body !== undefined ? "POST" : "GET",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  }).then(r => r.json());

  let st = { connected: false, recording: false };
  window.canStatus = () => st;

  async function refresh() {
    try { st = await window.api("/api/status"); }
    catch (e) { st = { connected: false }; }
    const c = !!st.connected;
    $("#g-dot").classList.toggle("on", c);
    $("#g-conn").textContent = c
      ? ("LIVE · " + (st.mode || "") + (st.scheme ? " · " + st.scheme : ""))
      : "OFFLINE";
    $("#g-frames").textContent = c ? ((st.frame_count || 0).toLocaleString() + " fr") : "";
    $("#g-rec").classList.toggle("on", !!st.recording);
    const cb = $("#g-connect");
    cb.textContent = c ? "Disconnect" : "Connect";
    cb.classList.toggle("danger", c); cb.classList.toggle("primary", !c);
    const rb = $("#g-record");
    rb.textContent = st.recording ? "■ Stop" : "● Record";
    rb.classList.toggle("danger", !!st.recording);
    rb.disabled = !c;
    $("#g-mode").disabled = c;
  }
  window.canRefresh = refresh;

  document.addEventListener("DOMContentLoaded", () => {
    // highlight the current nav link
    document.querySelectorAll(".nav a.link").forEach(a => {
      if (a.getAttribute("href") === location.pathname) a.classList.add("active");
    });
    $("#g-connect").addEventListener("click", async () => {
      if (st.connected) { await window.api("/api/disconnect", {}); }
      else {
        const r = await window.api("/api/connect", { mode: $("#g-mode").value, bitrate: 500000 });
        if (!r.ok) alert("Connect failed: " + r.error);
      }
      refresh();
    });
    $("#g-record").addEventListener("click", async () => {
      if (st.recording) { await window.api("/api/record/stop", {}); }
      else {
        const r = await window.api("/api/record/start", { label: ($("#g-label").value || "capture") });
        if (!r.ok) alert("Record failed: " + r.error);
      }
      refresh();
    });
    refresh();
    setInterval(refresh, 1000);
  });
})();
