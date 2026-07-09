/* Global connection controller (topbar) shared by every page.
   Pages use window.api(...) and window.canStatus(). */
(function () {
  "use strict";
  const $ = s => document.querySelector(s);

  window.api = (path, body) => fetch(path, {
    method: body !== undefined ? "POST" : "GET",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  }).then(r => r.json());

  let st = { connected: false };
  window.canStatus = () => st;

  async function refresh() {
    try { st = await window.api("/api/status"); }
    catch (e) { st = { connected: false }; }
    const c = !!st.connected;
    $("#c-dot").classList.toggle("on", c);
    $("#c-conn").textContent = c
      ? ("Connected · " + (st.mode || "") + (st.scheme ? " · " + st.scheme : ""))
      : "Disconnected";
    $("#c-frames").textContent = c ? ((st.frame_count || 0).toLocaleString() + " fr · " + (st.unique_ids || 0) + " IDs") : "";
    $("#c-rec").classList.toggle("on", !!st.recording);
    const cb = $("#c-connect");
    cb.textContent = c ? "Disconnect" : "Connect";
    cb.classList.toggle("danger", c); cb.classList.toggle("primary", !c);
    const rb = $("#c-record");
    rb.textContent = st.recording ? "Stop rec" : "Record";
    rb.classList.toggle("danger", !!st.recording);
    rb.disabled = !c;
    $("#c-mode").disabled = c;
  }
  window.canRefresh = refresh;

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".sidebar nav a").forEach(a => {
      if (a.getAttribute("href") === location.pathname) a.classList.add("active");
    });
    $("#c-connect").addEventListener("click", async () => {
      if (st.connected) await window.api("/api/disconnect", {});
      else {
        const r = await window.api("/api/connect", { mode: $("#c-mode").value, bitrate: 500000 });
        if (!r.ok) alert("Connect failed: " + r.error);
      }
      refresh();
    });
    $("#c-record").addEventListener("click", async () => {
      if (st.recording) await window.api("/api/record/stop", {});
      else {
        const r = await window.api("/api/record/start", { label: ($("#c-label").value || "capture") });
        if (!r.ok) alert("Record failed: " + r.error);
      }
      refresh();
    });
    refresh();
    setInterval(refresh, 1000);
  });
})();
