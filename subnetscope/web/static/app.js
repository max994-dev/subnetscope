/* subnetscope web — client-side helpers.
   Watchlist (localStorage), alerts polling, browser notifications,
   chart helpers. No build step. */

(function () {
  "use strict";

  // ---------------------------------------------------------------- watchlist
  var WATCH_KEY = "subnetscope_watch_v1";
  function loadWatch() {
    try {
      var raw = localStorage.getItem(WATCH_KEY);
      if (!raw) return new Set();
      return new Set(JSON.parse(raw));
    } catch (e) { return new Set(); }
  }
  function saveWatch(s) {
    localStorage.setItem(WATCH_KEY, JSON.stringify(Array.from(s)));
  }
  window.snWatch = {
    set: loadWatch(),
    has: function (n) { return this.set.has(parseInt(n, 10)); },
    toggle: function (n) {
      n = parseInt(n, 10);
      if (this.set.has(n)) this.set.delete(n);
      else this.set.add(n);
      saveWatch(this.set);
      this.refreshUi();
      return this.set.has(n);
    },
    refreshUi: function () {
      var self = this;
      document.querySelectorAll(".star-btn").forEach(function (btn) {
        var n = parseInt(btn.getAttribute("data-netuid"), 10);
        var on = self.set.has(n);
        btn.textContent = on ? "★" : "☆";
        btn.classList.toggle("starred", on);
        var row = btn.closest("tr");
        if (row) row.setAttribute("data-watched", on ? "1" : "0");
      });
      var counter = document.getElementById("watch-count");
      if (counter) counter.textContent = String(self.set.size);
      // Apply "show only watched" filter if active
      var showOnly = document.getElementById("show-only-watched");
      if (showOnly && showOnly.checked) {
        document.querySelectorAll("tbody tr").forEach(function (tr) {
          tr.style.display = (tr.getAttribute("data-watched") === "1") ? "" : "none";
        });
      } else {
        document.querySelectorAll("tbody tr").forEach(function (tr) {
          tr.style.display = "";
        });
      }
    }
  };

  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".star-btn");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    window.snWatch.toggle(btn.getAttribute("data-netuid"));
  });

  // Re-apply watch UI after every HTMX swap (table refresh, search, etc.)
  document.body.addEventListener("htmx:afterSwap", function () {
    window.snWatch.refreshUi();
  });

  document.addEventListener("change", function (e) {
    if (e.target && e.target.id === "show-only-watched") {
      window.snWatch.refreshUi();
    }
  });

  // ---------------------------------------------------------------- alerts
  var ALERTS_SEEN_KEY = "subnetscope_alerts_seen_v1";
  function loadSeen() {
    try { return new Set(JSON.parse(localStorage.getItem(ALERTS_SEEN_KEY) || "[]")); }
    catch (e) { return new Set(); }
  }
  function saveSeen(s) {
    var arr = Array.from(s).slice(-500);
    localStorage.setItem(ALERTS_SEEN_KEY, JSON.stringify(arr));
  }
  var seenAlerts = loadSeen();

  function ensureNotificationPermission() {
    if (!("Notification" in window)) return Promise.resolve("unsupported");
    if (Notification.permission === "granted") return Promise.resolve("granted");
    if (Notification.permission === "denied")  return Promise.resolve("denied");
    return Notification.requestPermission();
  }

  function notify(title, body, tag) {
    if (!("Notification" in window)) return;
    if (Notification.permission !== "granted") return;
    try { new Notification(title, { body: body, tag: tag, icon: "/static/favicon.svg" }); }
    catch (e) { /* ignore */ }
  }

  // Compose a short, scannable headline per alert kind, leading with the
  // subnet identity. Used as the first row of each panel item AND as the
  // browser-notification title.
  function alertHeadline(a) {
    var subj = a.netuid != null
      ? "sn" + a.netuid + (a.name ? " " + a.name : "")
      : "(network)";
    var p = a.payload_obj || null;
    switch (a.kind) {
      case "recommended":
        if (p && p.score != null) return subj + " — score " + Math.round(p.score) + "/100";
        return subj + " — recommended";
      case "burn-jump":
        if (p && p.ratio != null) return subj + " — burn fee " + p.ratio.toFixed(1) + "× in 1h";
        return subj + " — burn fee jumped";
      case "slot-open":
        if (p && p.slots_free != null) return subj + " — " + p.slots_free + " UID slot(s) opened";
        return subj + " — slot opened";
      case "tempo-near":
        if (p && p.blocks_to_tick != null) return subj + " — emission tick in " + p.blocks_to_tick + " blocks";
        return subj + " — emission tick soon";
      case "new-subnet":
        return subj + " — new subnet appeared";
      default:
        return subj + " — " + a.kind;
    }
  }

  function renderAlerts(items) {
    var panel = document.getElementById("alerts-list");
    var badge = document.getElementById("alerts-badge");
    if (!panel || !badge) return;
    // Parse payload JSON once, attach to alert object.
    items.forEach(function (a) {
      if (a.payload && !a.payload_obj) {
        try { a.payload_obj = JSON.parse(a.payload); }
        catch (e) { a.payload_obj = null; }
      }
    });
    var unseen = items.filter(function (a) { return !seenAlerts.has(a.id); });
    badge.textContent = unseen.length > 0 ? String(unseen.length) : "";
    badge.classList.toggle("has", unseen.length > 0);
    if (items.length === 0) {
      panel.innerHTML = '<div class="empty">No alerts yet. Recommendations and burn-fee jumps will show up here.</div>';
      return;
    }
    panel.innerHTML = items.slice(0, 50).map(function (a) {
      var when = new Date(a.ts * 1000).toLocaleTimeString();
      var headline = escapeHtml(alertHeadline(a));
      var headlineWrapped = a.netuid != null
        ? '<a href="/subnet/' + a.netuid + '" class="alert-headline">' + headline + '</a>'
        : '<span class="alert-headline">' + headline + '</span>';
      return '<div class="alert-item">' +
               '<div class="alert-row">' +
                 headlineWrapped +
                 '<span class="kind ' + a.kind + '">' + a.kind + '</span>' +
               '</div>' +
               '<div class="alert-msg">' + escapeHtml(a.message) + '</div>' +
               '<div class="meta">' +
                 '<span>' + when + '</span>' +
               '</div>' +
             '</div>';
    }).join("");
    // Surface new ones as desktop notifications. Title = subject + key fact.
    unseen.slice(-5).forEach(function (a) {
      notify(alertHeadline(a), a.message, "alert-" + a.id);
    });
  }

  function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function pollAlerts() {
    fetch("/api/alerts?limit=50")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.alerts) return;
        renderAlerts(data.alerts);
      })
      .catch(function () { /* silent */ });
  }

  document.addEventListener("DOMContentLoaded", function () {
    window.snWatch.refreshUi();

    var bell = document.getElementById("bell-btn");
    var panel = document.getElementById("alerts-panel");
    if (bell && panel) {
      bell.addEventListener("click", function () {
        panel.classList.toggle("open");
        if (panel.classList.contains("open")) {
          ensureNotificationPermission();
          // Mark all currently-shown alerts as seen
          document.querySelectorAll("#alerts-list .alert-item").forEach(function (el) {
            // ids are not in DOM; we mark all returned ids on next poll
          });
          // simpler: mark every alert id we currently know as seen on close
        }
      });
      var clear = document.getElementById("alerts-clear");
      if (clear) {
        clear.addEventListener("click", function () {
          // mark everything currently in the panel as seen
          fetch("/api/alerts?limit=200").then(function(r){return r.json();}).then(function(d){
            (d.alerts || []).forEach(function(a){ seenAlerts.add(a.id); });
            saveSeen(seenAlerts);
            renderAlerts(d.alerts || []);
          });
        });
      }
      // Click outside closes panel
      document.addEventListener("click", function (e) {
        if (!panel.classList.contains("open")) return;
        if (e.target === bell || bell.contains(e.target)) return;
        if (panel.contains(e.target)) return;
        panel.classList.remove("open");
        // Mark alerts in panel as seen so badge clears
        fetch("/api/alerts?limit=200").then(function(r){return r.json();}).then(function(d){
          (d.alerts || []).forEach(function(a){ seenAlerts.add(a.id); });
          saveSeen(seenAlerts);
          renderAlerts(d.alerts || []);
        });
      });

      pollAlerts();
      setInterval(pollAlerts, 15000);
    }
  });

  // ---------------------------------------------------------------- charts
  // Used by detail.html. Expects window.snHistory data injected.
  window.snDrawSparkline = function (canvasId, points, color, valueFmt) {
    var canvas = document.getElementById(canvasId);
    if (!canvas || !points || points.length === 0) return;
    var ctx = canvas.getContext("2d");
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = w * dpr; canvas.height = h * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    var values = points.map(function (p) { return p.v; });
    var min = Math.min.apply(null, values);
    var max = Math.max.apply(null, values);
    if (min === max) { min -= 1; max += 1; }

    function px(i) { return (i / (points.length - 1)) * (w - 4) + 2; }
    function py(v) { return h - 4 - ((v - min) / (max - min)) * (h - 8); }

    // Filled area
    ctx.beginPath();
    ctx.moveTo(px(0), h);
    for (var i = 0; i < points.length; i++) ctx.lineTo(px(i), py(points[i].v));
    ctx.lineTo(px(points.length - 1), h);
    ctx.closePath();
    ctx.fillStyle = color + "22";
    ctx.fill();

    // Line
    ctx.beginPath();
    ctx.moveTo(px(0), py(points[0].v));
    for (var j = 1; j < points.length; j++) ctx.lineTo(px(j), py(points[j].v));
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    ctx.stroke();
  };
})();
