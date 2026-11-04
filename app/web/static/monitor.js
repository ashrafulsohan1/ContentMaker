/* ContentMaker job monitor — vanilla-JS SSE client (BUILD_PLAN 1.11, no build step).

   Attaches to GET /api/jobs/{id}/events and mirrors every full-snapshot event
   (snapshot / update / final) into the server-rendered #monitor DOM. Every
   event carries the whole job payload, so updates are idempotent renders of
   truth-as-of-now and a reconnect (EventSource retry) self-heals.

   A terminal page never opens the stream (data-terminal="1"), so the
   reload-on-final cannot loop. DOM nodes are looked up per event — htmx may
   have swapped the fragment between events. */
(function () {
  "use strict";

  var monitor = document.getElementById("monitor");
  if (!monitor || monitor.getAttribute("data-terminal") === "1") return;
  var jobId = monitor.getAttribute("data-job-id");
  if (!jobId) return;

  var TERMINAL = { completed: true, partial: true, failed: true, cancelled: true };
  var RETRYABLE_ASSET = { failed: true, skipped: true };

  function usd(value) {
    return "$" + (Number(value) || 0).toFixed(4);
  }

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  function apply(data) {
    setText("job-status", data.status);
    setText("job-error", data.error || "");

    var progress = data.progress || {};
    var done = progress.done || 0;
    var total = progress.total || 0;
    var percent = progress.percent || 0;
    var fill = document.getElementById("job-progress-fill");
    if (fill) fill.style.width = percent + "%";
    var bar = document.querySelector("#monitor .progress");
    if (bar) bar.setAttribute("aria-valuenow", String(percent));
    setText("job-progress-label", done + " of " + total + " assets done (" + percent + "%)");

    setText("job-estimate", usd(data.estimate_usd));
    setText("job-spent", usd(data.spent_usd));
    setText("job-updated", data.updated_at || "");

    (data.assets || []).forEach(function (asset) {
      var row = document.querySelector('#monitor tr[data-asset-id="' + asset.id + '"]');
      if (!row) return;
      var status = row.querySelector(".asset-status");
      if (status) status.textContent = asset.status;
      var attempts = row.querySelector(".asset-attempts");
      if (attempts) attempts.textContent = String(asset.attempts || 0);
      var spent = row.querySelector(".asset-spent");
      if (spent) spent.textContent = usd(asset.spent_usd);
      var error = row.querySelector(".asset-error");
      if (error) error.textContent = asset.error || "";
      var form = row.querySelector(".asset-retry form");
      if (form) form.hidden = !RETRYABLE_ASSET[asset.status];
    });

    if (TERMINAL[data.status]) {
      var cancel = document.getElementById("cancel-form");
      if (cancel) cancel.hidden = true;
    }
  }

  var source = new EventSource("/api/jobs/" + encodeURIComponent(jobId) + "/events");
  ["snapshot", "update", "final"].forEach(function (name) {
    source.addEventListener(name, function (event) {
      apply(JSON.parse(event.data));
      if (name === "final") {
        source.close();
        window.location.reload(); // server-rendered final state: retry buttons etc.
      }
    });
  });
})();
