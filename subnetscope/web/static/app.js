/* subnetscope web — client-side helpers.
   Bookmarks (localStorage), alerts polling, browser notifications,
   chart helpers. No build step. */

(function () {
  "use strict";

  // ---------------------------------------------------------------- bookmarks (local)
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

  var DESKTOP_NOTIFIED_KEY = "subnetscope_alerts_desktop_notified_v1";
  function loadDesktopNotified() {
    try {
      var raw = localStorage.getItem(DESKTOP_NOTIFIED_KEY);
      if (!raw) return new Set();
      return new Set(JSON.parse(raw).map(String));
    } catch (e) { return new Set(); }
  }
  function saveDesktopNotified(s) {
    var arr = Array.from(s).slice(-400);
    localStorage.setItem(DESKTOP_NOTIFIED_KEY, JSON.stringify(arr));
  }
  var desktopNotified = loadDesktopNotified();

  function ensureNotificationPermission() {
    if (!("Notification" in window)) return Promise.resolve("unsupported");
    if (Notification.permission === "granted") return Promise.resolve("granted");
    if (Notification.permission === "denied")  return Promise.resolve("denied");
    return Notification.requestPermission();
  }

  function notify(title, body, tag) {
    if (!("Notification" in window)) return;
    if (Notification.permission !== "granted") return;
    try {
      new Notification(title, { body: body, tag: tag, icon: "/static/favicon.svg" });
    } catch (e) { /* ignore */ }
  }

  function showAlertToast(lines) {
    var el = document.getElementById("sn-alert-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "sn-alert-toast";
      el.setAttribute("role", "status");
      el.style.cssText = [
        "position:fixed", "bottom:20px", "right:20px", "max-width:min(380px,92vw)",
        "z-index:99999", "padding:12px 14px", "border-radius:10px",
        "background:rgba(30,30,35,0.96)", "border:1px solid rgba(255,255,255,0.12)",
        "color:#e4e4e7", "font-size:13px", "line-height:1.35", "white-space:pre-wrap",
        "box-shadow:0 8px 32px rgba(0,0,0,0.45)", "display:none",
      ].join(";");
      document.body.appendChild(el);
    }
    el.textContent = lines.join("\n");
    el.style.display = "block";
    if (el._hideT) clearTimeout(el._hideT);
    el._hideT = setTimeout(function () { el.style.display = "none"; }, 10000);
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
      case "slot-open":
        if (p && p.slots_free != null) return subj + " — " + p.slots_free + " UID slot(s) opened";
        return subj + " — slot opened";
      case "tempo-near":
        if (p && p.blocks_to_tick != null) {
          var h = subj + " — validators send tasks in " + p.blocks_to_tick +
                  " block" + (p.blocks_to_tick === 1 ? "" : "s");
          if (p.watch_hotkeys) h += " (your hotkey)";
          return h;
        }
        return subj + " — validators start sending tasks soon";
      case "new-subnet":
        return subj + " — new subnet appeared";
      default:
        return subj + " — " + a.kind;
    }
  }

  // Friendly chip label (keeps the raw kind as the CSS class for styling).
  function kindLabel(k) {
    return k === "tempo-near" ? "validator tasks" : k;
  }

  function parseAlertPayloads(items) {
    items.forEach(function (a) {
      if (a.payload && !a.payload_obj) {
        try { a.payload_obj = JSON.parse(a.payload); }
        catch (e) { a.payload_obj = null; }
      }
    });
  }

  function processDesktopNotifications(items) {
    var fresh = items.filter(function (a) { return !desktopNotified.has(String(a.id)); });
    if (!fresh.length) return;
    var tail = fresh.slice(-8);
    var toastLines = [];
    tail.forEach(function (a) {
      desktopNotified.add(String(a.id));
      var title = alertHeadline(a);
      var body = a.message || "";
      notify(title, body, "subnetscope-alert-" + a.id);
      toastLines.push(title);
    });
    saveDesktopNotified(desktopNotified);
    if (Notification.permission !== "granted" && toastLines.length) {
      showAlertToast(toastLines);
    }
  }

  function renderAlertsUi(items) {
    var panel = document.getElementById("alerts-list");
    var badge = document.getElementById("alerts-badge");
    if (!panel || !badge) return;
    var unseen = items.filter(function (a) { return !seenAlerts.has(a.id); });
    badge.textContent = unseen.length > 0 ? String(unseen.length) : "";
    badge.classList.toggle("has", unseen.length > 0);
    if (items.length === 0) {
      panel.innerHTML = '<div class="empty">No alerts yet. Open slots, validator task rounds (watch hotkeys on subnet), and new subnets will show here.</div>';
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
                 '<span class="kind ' + a.kind + '">' + kindLabel(a.kind) + '</span>' +
               '</div>' +
               '<div class="alert-msg">' + escapeHtml(a.message) + '</div>' +
               '<div class="meta">' +
                 '<span>' + when + '</span>' +
               '</div>' +
             '</div>';
    }).join("");
  }

  function renderAlerts(items) {
    parseAlertPayloads(items);
    processDesktopNotifications(items);
    renderAlertsUi(items);
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

    /* Ask once for desktop notifications (Chrome / Firefox / Edge). */
    ensureNotificationPermission();

    pollAlerts();
    setInterval(pollAlerts, 12000);

    /* column header sort → updates #filters input[name=sort], HTMX refreshes table */
    function defaultOrderForKey(key) {
      var asc = {
        fee: 1, name: 1, netuid: 1, type: 1, category: 1, gpu: 1, reward: 1,
        top1: 1, fullness: 1, gini: 1, price: 1,
      };
      return asc[key] ? "asc" : "desc";
    }
    document.body.addEventListener("click", function (e) {
      var th = e.target.closest("th.sort-th[data-sort-key]");
      if (!th) return;
      e.preventDefault();
      var key = th.getAttribute("data-sort-key");
      if (!key) return;
      var form = document.getElementById("filters");
      if (!form) return;
      var inp = form.querySelector('input[name="sort"]');
      if (!inp) return;
      var cur = (inp.value || "").trim();
      var first = cur.split(/\s*,\s*/)[0] || "";
      var m = first.match(/^([^:]+)(?::(asc|desc))?$/i);
      var ck = m ? m[1].toLowerCase() : "";
      var ord = (m && m[2]) ? m[2].toLowerCase() : defaultOrderForKey(key);
      if (ck === key.toLowerCase()) {
        ord = (ord === "asc") ? "desc" : "asc";
      } else {
        ord = defaultOrderForKey(key);
      }
      inp.value = key + ":" + ord;
      inp.dispatchEvent(new Event("change", { bubbles: true }));
    });
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

/* ═══ Copy-to-clipboard buttons (.copy-btn[data-copy]) ═══ */
(function () {
  "use strict";
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
  }
  function flash(btn) {
    var prev = btn.textContent;
    btn.classList.add("copied");
    btn.textContent = "✓";
    setTimeout(function () {
      btn.classList.remove("copied");
      btn.textContent = prev;
    }, 1100);
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest(".copy-btn") : null;
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    var text = btn.getAttribute("data-copy");
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { flash(btn); })
        .catch(function () { fallbackCopy(text); flash(btn); });
    } else {
      fallbackCopy(text);
      flash(btn);
    }
  });
})();
