/* StockSophist Chartsfrontend.
   Multi-pane chart-style charts (Lightweight Charts v5):
   tick-based price updates, 1s..Month timeframes, drawing tools with
   saved layouts, 1/2/3/4 chart grids.*/
(function () {
  "use strict";
  var LWC = window.LightweightCharts;
  if (!LWC) { document.body.innerHTML = "<p style='padding:20px'>Chart library failed to load.</p>"; return; }

  var $ = function (id) { return document.getElementById(id); };
  var TF_SEC = { "1s": 1, "15s": 15, "1m": 60, "3m": 180, "5m": 300,
                 "15m": 900, "30m": 1800, "1H": 3600, "4H": 14400 };
  var BIG_TF = { "D": 1, "W": 1, "M": 1 };
  /* Sort key for timeframe buttons: smallest first (1s, 15s, 1m ... D, W, M). */
  function tfSortKey(tf) {
    if (TF_SEC[tf]) return TF_SEC[tf];
    if (tf === "D") return 86400;
    if (tf === "W") return 604800;
    if (tf === "M") return 2592000;
    return 1e15;
  }
  var TF_LIST = ["1s", "15s", "1m", "3m", "5m", "15m", "30m", "1H", "4H", "D", "W", "M"];
  var ALERT_TF_LIST = ["1s", "15s", "1m", "3m", "5m", "15m", "30m", "1H", "4H", "D", "W", "M"];
  // premium-style deep history targets (bars) per timeframe
  var APP_VERSION = "3.4.39";  // must match server VERSION; mismatch banner on load
  var DEEP_BARS = { "1s": 7200, "15s": 5760,
                    "1m": 20000, "3m": 20000, "5m": 20000,
                    "15m": 20000, "30m": 20000, "1H": 20000, "4H": 20000,
                    "D": 5000, "W": 2000, "M": 600 };
  var DEEP_BRICKS = 5000;
  var QUOTE_MS = 1000, RENKO_MS = 5000, RESYNC_MS = 30000;
  var STALE_S = 10; // quote older than this (per MT5 clock) => STALE feed badge
  var LS_KEY = "stocksophist_layout_v3";
  var LS_ALERTS = "stocksophist_alerts_v1", LS_TG = "stocksophist_tg_v1", LS_CHARTSET = "stocksophist_chartset_v1";
  var DRAW_COLOR = "#2962ff", SEL_COLOR = "#ff9800";

  /* ----------------------------------------------------- drawing styles
     chart-style format options, per drawing tool (each tool remembers
     its own style, like TV). Persisted in localStorage.*/
  var DrawStyle = {
    KEY: "stocksophist_draw_style",
    DEF: { color: "#2962ff", width: 2, lineStyle: "solid", lineOpacity: 1,
           fillColor: "#2962ff", fillOpacity: 0.12 },
    /* built-in TV-like defaults per tool */
    TOOL_DEF: { hline: { lineStyle: "dashed" }, vline: { lineStyle: "dashed" },
                hray: { lineStyle: "dashed" } },
    SHAPES: { rect: 1, ellipse: 1, long: 1, short: 1 },
    _map: null,
    load: function () {
      var m = { _def: {}, tools: {} };
      try {
        var raw = JSON.parse(localStorage.getItem(this.KEY) || "{}");
        if (raw && typeof raw === "object") {
          if (raw._def) m._def = raw._def;
          if (raw.tools) m.tools = raw.tools;
          /* back-compat: very first version stored a flat style object */
          if (raw.color) m._def = raw;
        }
      } catch (e) {}
      this._map = m;
      return m;
    },
    save: function () {
      try { localStorage.setItem(this.KEY, JSON.stringify(this._map)); } catch (e) {}
    },
    /* effective style for a tool (or "_def" for the global default) */
    get: function (tool) {
      if (!this._map) this.load();
      var t = tool || "_def";
      return Object.assign({}, this.DEF, this.TOOL_DEF[t] || {},
                           this._map._def, (this._map.tools[t] || {}));
    },
    /* style stored on a drawing instance (with back-compat for old ones) */
    of: function (d) {
      if (!this._map) this.load();
      return Object.assign({}, this.DEF, (d && d.style) || {});
    },
    /* user changed props for the given tool context */
    set: function (tool, props) {
      if (!this._map) this.load();
      var t = tool || "_def";
      this._map.tools[t] = Object.assign({}, this._map.tools[t] || {}, props);
      this.save();
    }
  };


  /* -------------- Deep history: max-depth sync UI
     The server keeps a max-depth M1 archive per symbol (everything MT5 can
     give) plus Renko brick caches built from it. This pill shows the live
     sync progress for the active symbol; clicking opens the detail modal.
     On boot the client also warms the server's queue with every symbol used
     in any named profile, so switching profiles is instant.*/
  var HistUI = {
    _timer: null,
    init: function () {
      var b = $("btn-hist");
      if (b) b.addEventListener("click", function () { HistUI.open(); });
      this.poll();
      this._timer = setInterval(function () { HistUI.poll(); }, 5000);
      // Warm the server queue with profile symbols once the app is up.
      setTimeout(function () { HistUI.warmProfiles(); }, 30000);
    },
    _sym: function () {
      try {
        var p = (typeof Layout !== "undefined" && Layout.activePane) ? Layout.activePane() : null;
        if (p && p.state && p.state.symbol) return p.state.symbol;
      } catch (e) {}
      return "XAUUSD";
    },
    _short: function (n) {
      n = n || 0;
      if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
      if (n >= 1000) return (n / 1000).toFixed(0) + "k";
      return "" + n;
    },
    _fmtDate: function (t) {
      try {
        var d = new Date(t * 1000);
        return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2);
      } catch (e) { return "?"; }
    },
    poll: function () {
      var self = this, sym = this._sym();
      fetch("/api/history/deep/status?symbol=" + encodeURIComponent(sym))
        .then(function (r) { return r.json(); })
        .then(function (j) { self._paint(j); })
        .catch(function () { /* keep the last painted state on error*/ });
    },
    _paint: function (j) {
      var b = $("btn-hist"); if (!b) return;
      var op = j.op || {}, arch = j.archive, label, tip;
      if (op.status === "running") {
        var m = /([\d,]+) bars/.exec(op.detail || "");
        label = "v " + this._short(parseInt((m ? m[1] : "0").replace(/,/g, ""), 10) || 0);
        b.classList.add("busy");
        tip = "Syncing max history for " + j.symbol + " - " + (op.stage || "") +
              (op.detail ? " - " + op.detail : "") + " (click for details)";
      } else {
        b.classList.remove("busy");
        label = (arch && arch.bars) ? this._short(arch.bars) : "Hist";
        tip = "Deep history for " + j.symbol +
              ((arch && arch.bars)
                ? " - " + this._short(arch.bars) + " bars (" + this._fmtDate(arch.first) +
                  " -> " + this._fmtDate(arch.last) + "). Click for details / re-sync."
                : " - not synced yet. Click to start the max-depth sync.");
      }
      b.textContent = label;
      b.title = tip;
      this._last = j;
    },
    open: function () {
      var self = this, sym = this._sym();
      var archLine = '<div class="statusline">Loading...</div>';
      Modal.open("Deep history - " + escHtml(sym),
        '<div class="note" style="margin-bottom:8px">The server keeps a <b>max-depth M1 archive</b> ' +
        'for each symbol - everything MT5 can give, walked back to the broker\u2019s oldest bar. ' +
        'Charts and indicators use a fast working set; Renko bricks are pre-built from the same archive, ' +
        'so candle<->Renko switching stays instant. Re-running only fetches what\u2019s missing.</div>' +
        '<div id="hist-arch">' + archLine + '</div>' +
        '<div id="hist-op" class="note" style="margin:8px 0"></div>' +
        '<div class="drow">' +
        '<button class="btn primary" id="hist-sync">(down) Sync max history now</button>' +
        '<span class="note" id="hist-msg" style="align-self:center"></span>' +
        '</div>' +
        '<div class="note" style="margin-top:10px">Other symbols (watchlist + all your profiles) sync ' +
        'in the background on their own - switching to them later costs nothing.</div>',
        '<button class="btn ghost" id="hist-close">Close</button>');
      $("hist-close").addEventListener("click", Modal.close);
      $("hist-sync").addEventListener("click", function () {
        $("hist-msg").textContent = "Starting...";
        fetch("/api/history/deep", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ symbol: sym }) })
          .then(function (r) { return r.json(); })
          .then(function (j) {
            $("hist-msg").textContent = j.note || (j.queued ? "Sync running - watch the top-bar pill." : "Already syncing.");
            self.refresh(sym);
          })
          .catch(function (e) { $("hist-msg").textContent = "Error: " + e.message; });
      });
      this.refresh(sym);
      this._modalTimer = setInterval(function () {
        if (!$("hist-arch")) { clearInterval(self._modalTimer); return; }
        self.refresh(sym);
      }, 3000);
    },
    refresh: function (sym) {
      var self = this;
      fetch("/api/history/deep/status?symbol=" + encodeURIComponent(sym))
        .then(function (r) { return r.json(); })
        .then(function (j) {
          var arch = j.archive, op = j.op || {};
          var a = $("hist-arch");
          if (a) a.innerHTML = (arch && arch.bars)
            ? '<div class="statusline ok">' + self._short(arch.bars) + " bars - " +
              self._fmtDate(arch.first) + " -> " + self._fmtDate(arch.last) + "</div>"
            : '<div class="statusline">No archive yet - press "Sync max history now".</div>';
          var o = $("hist-op");
          if (o) o.textContent = op.status === "running"
            ? ("(wait) " + (op.stage || "syncing") + (op.detail ? " - " + op.detail : "") +
               (op.elapsed_s != null ? " (" + Math.round(op.elapsed_s) + "s)" : ""))
            : (op.status === "done"
              ? ("[ok] " + (op.message || "done"))
              : (op.status === "failed"
                ? ("[!] " + (op.error || op.message || "failed"))
                : ""));
          self._paint(j);
        })
        .catch(function () {});
    },
    warmProfiles: function () {
      // Collect every symbol used in any named profile + the open panes and
      // ask the server to deep-sync them in the background.
      var syms = {};
      try {
        var raw = localStorage.getItem("stocksophist_named_layouts");
        var layouts = raw ? JSON.parse(raw) : {};
        Object.keys(layouts).forEach(function (name) {
          var L = layouts[name];
          (L.panes || []).forEach(function (p) {
            if (p && p.symbol) syms[String(p.symbol).trim()] = 1;
          });
        });
      } catch (e) {}
      try {
        (Layout.panes || []).forEach(function (p) {
          if (p && p.state && p.state.symbol) syms[String(p.state.symbol).trim()] = 1;
        });
      } catch (e) {}
      var list = Object.keys(syms);
      if (!list.length) return;
      fetch("/api/history/warm", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols: list }) }).catch(function () {});
    }
  };

  /* 
     Feed-health pill (topbar, next to the Hist pill). Mirrors HistUI's
     poll/paint pattern: GET /api/health every 15s, color the pill and put
     the full health snapshot in the hover tooltip. Green = stream_up and
     quotes fresh; amber = stale quotes or stream down but a recent tick;
     red = otherwise. No persistence.*/
  var HealthUI = {
    _timer: null,
    init: function () {
      this.poll();
      this._timer = setInterval(function () { HealthUI.poll(); }, 15000);
    },
    poll: function () {
      var self = this;
      fetch("/api/health").then(function (r) { return r.json(); })
        .then(function (j) { self.paint(j); })
        .catch(function () { self.paint(null); });
    },
    paint: function (j) {
      var pill = $("health-pill"), txt = $("health-txt");
      if (!pill) return;
      var cls = "hpill hp-bad", tip = "Feed health - no data (server unreachable?)";
      if (j) {
        var tick = j.secs_since_tick;
        var ok = !!(j.stream_up && !j.quote_stale);
        var degraded = !!j.quote_stale ||
          (!j.stream_up && tick != null && tick < 120);
        cls = "hpill " + (ok ? "hp-ok" : (degraded ? "hp-warn" : "hp-bad"));
        function ago(s) {
          if (s == null) return "null";
          s = Math.max(0, s);
          return s < 60 ? s.toFixed(1) + "s" : Math.floor(s / 60) + "m " + Math.round(s % 60) + "s";
        }
        var dt = null;
        try { if (j.last_bar_time) dt = new Date(j.last_bar_time * 1000).toLocaleString(); } catch (e) {}
        tip = "feed: " + j.feed + "\n" +
          "stream_up: " + j.stream_up + "\n" +
          "secs_since_tick: " + ago(tick) + "\n" +
          "secs_since_heartbeat: " + ago(j.secs_since_heartbeat) + "\n" +
          "quote_stale: " + j.quote_stale + "\n" +
          "last_bar_time: " + (dt || "null") + "\n" +
          "hist_sync: " + j.hist_sync;
        if (txt) txt.textContent = "Feed " + (j.feed ? String(j.feed).toUpperCase() : "?");
      } else if (txt) {
        txt.textContent = "Feed ?";
      }
      pill.className = cls;
      pill.title = tip;
    }
  };

  /* 
     News markers - DISPLAY ONLY. USD High (red) / Medium (gold) events from
     GET /api/news?from=<unix>&to=<unix>&medium=1 are drawn as vertical
     dashed lines on the overlay canvas of EVERY pane (ChartPane.drawNews).
     Pure visuals: no interaction with signals, alerts, replay, or the alert
     engine. Visibility is a client-only topbar toggle, persisted in
     localStorage, DEFAULT ON.*/
  var NewsMarks = {
    LS: "stocksophist_news_marks",
    events: [],        // [{ts,title,impact,forecast,previous}]
    _win: null,        // {from,to} time window covered by `events`
    _timer: null, _pending: false,
    COLORS: { High: "#ef5350", Medium: "#ff9800" },

    on: function () {
      try { return localStorage.getItem(this.LS) !== "0"; } catch (e) { return true; }
    },
    set: function (v) {
      try { localStorage.setItem(this.LS, v ? "1" : "0"); } catch (e) {}
      this.paintBtn();
      if (typeof Layout !== "undefined" && Layout.panes)
        Layout.panes.forEach(function (p) { p.draw(); });
    },
    paintBtn: function () {
      var b = $("btn-newsmarks");
      if (b) b.classList.toggle("on", this.on());
    },
    init: function () {
      var self = this;
      this.paintBtn();
      var b = $("btn-newsmarks");
      if (b) b.addEventListener("click", function () { self.set(!self.on()); });
      this.refresh(true);
      /* refetch every 30 min; the pane draw path also triggers a debounced
         refetch when the viewport outgrows the fetched window*/
      this._timer = setInterval(function () { self.refresh(true); }, 30 * 60 * 1000);
    },
    /* union of the visible time windows of all panes, padded; Renko panes
       are mapped back to real times for the fetch window*/
    window: function () {
      var f = 1e18, t = 0;
      try {
        (Layout.panes || []).forEach(function (p) {
          var tsa = p.chart && p.chart.timeScale();
          var lr = tsa && tsa.getVisibleLogicalRange ? tsa.getVisibleLogicalRange() : null;
          var nsd = (p.seriesData || []).length;
          if (!lr || !nsd) return;
          var i0 = Math.max(0, Math.ceil(lr.from)), i1 = Math.min(nsd - 1, Math.floor(lr.to));
          if (i0 > i1) return;
          var t0 = p.seriesData[i0].time, t1 = p.seriesData[i1].time;
          if (p.state.mode === "renko") { t0 = p.renkoReal(t0); t1 = p.renkoReal(t1); }
          if (t0 < f) f = t0;
          if (t1 > t) t = t1;
        });
      } catch (e) {}
      if (f > t) {
        var n = Math.floor(Date.now() / 1000);
        return { from: n - 3 * 86400, to: n + 7 * 86400 };
      }
      return { from: Math.floor(f) - 86400, to: Math.ceil(t) + 7 * 86400 };
    },
    needs: function (w) {
      return !this._win || w.from < this._win.from || w.to > this._win.to;
    },
    refresh: function (force) {
      var self = this;
      if (!this.on()) return;
      var w = this.window();
      if (!force && !this.needs(w)) return;
      fetch("/api/news?from=" + w.from + "&to=" + w.to + "&medium=1")
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (j && j.ok && j.events) {
            self.events = j.events;
            self._win = { from: w.from, to: w.to };
            if (typeof Layout !== "undefined" && Layout.panes)
              Layout.panes.forEach(function (p) { p.draw(); });
          }
        })
        .catch(function () { /* keep the last good set on error*/ });
    },
    /* called from ChartPane.draw(): debounced refetch when the viewport
       outgrows the fetched window*/
    maybeRefresh: function (vt0, vt1) {
      if (!this.on() || this._pending) return;
      if (!this.needs({ from: vt0, to: vt1 })) return;
      this._pending = true;
      var self = this;
      setTimeout(function () { self._pending = false; self.refresh(false); }, 2000);
    }
  };

  /* line-style helpers shared by the drawing renderer */
  function drawDashOf(style) {
    return style === "dashed" ? [7, 5] : style === "dotted" ? [2, 4] : [];
  }
  function applyDrawStroke(ctx, st, col, wScale) {
    ctx.strokeStyle = col;
    ctx.globalAlpha = (st.lineOpacity === undefined ? 1 : st.lineOpacity);
    ctx.lineWidth = Math.max(1, (st.width || 2) * (wScale || 1));
    ctx.setLineDash(drawDashOf(st.lineStyle));
  }
  function applyDrawFill(ctx, st, color) {
    ctx.globalAlpha = (st.fillOpacity === undefined ? 0.12 : st.fillOpacity);
    ctx.fillStyle = color || st.fillColor || "#2962ff";
  }

  /* ----------------------------------------- alert sound / tune engine
     Built-in soothing tunes are synthesized with Web Audio (no files).
     "Classic" plays the old bundled alert.wav. "Custom (uploaded)" plays
     a user audio file stored in localStorage. Toggle: chart settings ->
     Alert sound. Tune: chart settings -> Alert tune.*/
  var _alertAudio = null, _customAudio = null, _audioCtx = null;
  /* server-side custom tune. null = not checked yet; the HEAD probe
     runs once at page load and again whenever Chart settings opens.*/
  var _serverToneKnown = null;
  function _serverHasTone(cb) {
    if (_serverToneKnown !== null) { cb(_serverToneKnown); return; }
    fetch("/api/alerts/tone", { method: "HEAD" }).then(function (r) {
      _serverToneKnown = (r.status === 200);
      cb(_serverToneKnown);
    }).catch(function () { _serverToneKnown = false; cb(false); });
  }
  function _playAudioUrl(url) {
    if (!_customAudio || _customAudio._src !== url) {
      _customAudio = new Audio(url); _customAudio._src = url;
    }
    _customAudio.currentTime = 0;
    var pr = _customAudio.play();
    if (pr && pr.catch) pr.catch(function () {});
  }
  var ALERT_TUNES = ["Soothing Chime", "Soft Bell", "Gentle Ping", "Ding Dong",
                     "Classic", "Custom (uploaded)"];
  function audioCtx() {
    if (!_audioCtx) {
      try { _audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
      catch (e) { return null; }
    }
    if (_audioCtx && _audioCtx.state === "suspended") _audioCtx.resume();
    return _audioCtx;
  }
  /* Unlock Web Audio on the first user gesture (autoplay policy). */
  document.addEventListener("pointerdown", function () { audioCtx(); }, { once: true });
  function softNote(ctx, dest, freq, t0, dur, peak) {
    try {
      var o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = freq;
      var t = ctx.currentTime + t0;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(Math.max(peak, 0.0002), t + 0.07);
      g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
      o.connect(g); g.connect(dest); o.start(t); o.stop(t + dur + 0.1);
    } catch (e) {}
  }
  function playSynthTune(name) {
    var ctx = audioCtx(); if (!ctx) return false;
    var dest = ctx.destination;
    if (name === "Soft Bell") {
      softNote(ctx, dest, 523.25, 0, 1.8, 0.13);
      softNote(ctx, dest, 1244.5, 0, 1.2, 0.045);
      softNote(ctx, dest, 1866.8, 0, 0.9, 0.022);
    } else if (name === "Gentle Ping") {
      softNote(ctx, dest, 880, 0, 1.1, 0.15);
      softNote(ctx, dest, 1318.5, 0.03, 0.9, 0.045);
    } else if (name === "Ding Dong") {
      softNote(ctx, dest, 659.25, 0, 0.9, 0.15);
      softNote(ctx, dest, 523.25, 0.38, 1.3, 0.15);
    } else { /* "Soothing Chime" default*/
      softNote(ctx, dest, 523.25, 0, 1.5, 0.13);
      softNote(ctx, dest, 783.99, 0.3, 1.7, 0.11);
    }
    return true;
  }
  function playTuneByName(tune) {
    tune = tune || "Soothing Chime";
    if (tune === "Classic") {
      if (!_alertAudio) _alertAudio = new Audio("/static/sound/alert.wav");
      _alertAudio.currentTime = 0;
      var pr = _alertAudio.play();
      if (pr && pr.catch) pr.catch(function () {});
      return;
    }
    if (tune === "Custom (uploaded)") {
      /* server copy first (DATA_DIR/tones), then the legacy
         browser-localStorage copy, then the gentle default.*/
      if (_serverToneKnown) {
        _playAudioUrl("/api/alerts/tone");
        return;
      }
      var url = null;
      try { url = localStorage.getItem("stocksophist_custom_alert_tune"); } catch (e) {}
      if (url) {
        _playAudioUrl(url);
        return;
      }
      tune = "Soothing Chime"; /* no custom uploaded yet -> gentle default*/
    }
    playSynthTune(tune);
  }
  function playAlertSound() {
    try {
      var cfg = ChartSet.cfg();
      if (!cfg.alertSound) return;
      playTuneByName(cfg.alertTune);
    } catch (e) {}
  }
  var ALERT_COLOR = "#ff9800";

  function roundPx(p) { return Math.round(p * 100) / 100; }

  /* ----------------------------------------------- chart-style layouts */
  var ARRANGEMENTS = {
    1: [{ id: "full", name: "Single", areas: [["a"]], cols: "1fr", rows: "1fr" }],
    2: [{ id: "cols", name: "Side by side", areas: [["a", "b"]], cols: "1fr 1fr", rows: "1fr" },
        { id: "rows", name: "Stacked", areas: [["a"], ["b"]], cols: "1fr", rows: "1fr 1fr" }],
    3: [{ id: "cols", name: "3 columns", areas: [["a", "b", "c"]], cols: "1fr 1fr 1fr", rows: "1fr" },
        { id: "rows", name: "3 rows", areas: [["a"], ["b"], ["c"]], cols: "1fr", rows: "1fr 1fr 1fr" },
        { id: "topwide", name: "1 top + 2 bottom", areas: [["a", "a"], ["b", "c"]], cols: "1fr 1fr", rows: "1fr 1fr" },
        { id: "bottomwide", name: "2 top + 1 bottom", areas: [["a", "b"], ["c", "c"]], cols: "1fr 1fr", rows: "1fr 1fr" }],
    4: [{ id: "grid", name: "2 x 2 grid", areas: [["a", "b"], ["c", "d"]], cols: "1fr 1fr", rows: "1fr 1fr" },
        { id: "cols", name: "4 columns", areas: [["a", "b", "c", "d"]], cols: "1fr 1fr 1fr 1fr", rows: "1fr" },
        { id: "rows", name: "4 rows", areas: [["a"], ["b"], ["c"], ["d"]], cols: "1fr", rows: "1fr 1fr 1fr 1fr" },
        { id: "bigleft", name: "Large left", areas: [["a", "b"], ["a", "c"], ["a", "d"]], cols: "2fr 1fr", rows: "1fr 1fr 1fr" },
        { id: "bigright", name: "Large right", areas: [["b", "a"], ["c", "a"], ["d", "a"]], cols: "1fr 2fr", rows: "1fr 1fr 1fr" },
        { id: "bigtop", name: "Large top", areas: [["a", "a", "a"], ["b", "c", "d"]], cols: "1fr 1fr 1fr", rows: "2fr 1fr" },
        { id: "bigbottom", name: "Large bottom", areas: [["b", "c", "d"], ["a", "a", "a"]], cols: "1fr 1fr 1fr", rows: "1fr 2fr" }]
  };
  var AREA_LETTERS = ["a", "b", "c", "d"];
  function arrDef(n, id) {
    var list = ARRANGEMENTS[n] || ARRANGEMENTS[1];
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
    return list[0];
  }

  /* ---------------------------------------------------------------- modal */
  var Modal = {
    /* opts.sticky - backdrop clicks won't close (used by the MT5
       sync dialog, where an accidental click must not swallow the results).*/
    _sticky: false,
    open: function (title, bodyHtml, footHtml, opts) {
      this._sticky = !!(opts && opts.sticky);
      $("m-title").textContent = title;
      $("m-body").innerHTML = bodyHtml || "";
      $("m-foot").innerHTML = footHtml || "";
      $("modal-root").classList.add("open");
      // reset to centered position on each open
      var mb = document.querySelector("#modal-root .mbox");
      if (mb) { mb.style.left = ""; mb.style.top = ""; mb.style.transform = ""; }
    },
    close: function () {
      Modal._sticky = false;
      $("modal-root").classList.remove("open");
      var mb = document.querySelector("#modal-root .mbox");
      if (mb) { mb.classList.remove("wide"); mb.classList.remove("isetw"); }
    }
  };

  /* -------------- Fix data: progress + completion dialog
     Starts POST /api/data/heal (background job) and polls
     GET /api/data/heal/status for the live step. The completion summary
     stays open until Close - it reports per series, honestly, including
     "MT5 gave no data" instead of a false "all complete".*/
  var HealUI = {
    _timer: null, _job: null, _done: null,
    start: function (payload, onDone) {
      var self = this;
      this._done = onDone || null;
      this._job = null;
      Modal.open("Fix data",
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">' +
        '<div class="spin" id="heal-spin" style="width:22px;height:22px;flex:0 0 22px;border:3px solid var(--border);' +
        'border-top-color:var(--accent);border-radius:50%"></div>' +
        '<div id="heal-step" class="note">Starting\u2026</div></div>' +
        '<div style="height:6px;background:var(--border);border-radius:3px;overflow:hidden;margin-bottom:8px">' +
        '<div id="heal-bar" style="height:100%;width:0%;background:var(--accent);transition:width .3s"></div></div>' +
        '<div id="heal-prog" class="note" style="margin-bottom:8px"></div>' +
        '<div id="heal-results" style="max-height:220px;overflow:auto"></div>',
        '<button class="btn" id="heal-close">Close</button>',
        { sticky: true });
      $("heal-close").addEventListener("click", function () {
        self.stop(); Modal.close();
      });
      this._timer = setInterval(function () { self.poll(); }, 600);
      fetch("/api/data/heal", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {}) })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (res) {
          if (!res.ok || !res.j.ok) throw new Error((res.j && res.j.error) || "heal failed to start");
          self._job = res.j.job;
          self.poll();
        })
        .catch(function (e) { self.fail(e.message); });
    },
    stop: function () {
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
    },
    fail: function (msg) {
      this.stop();
      var el = $("heal-step");
      if (el) el.innerHTML = '<span style="color:var(--warn)">' + escHtml(msg) + "</span>";
      var sp = $("heal-spin");
      if (sp) sp.style.display = "none";
    },
    poll: function () {
      var self = this;
      if (!this._job) return;
      fetch("/api/data/heal/status?job=" + encodeURIComponent(this._job))
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (res) {
          var j = res.j;
          if (!res.ok || !j.ok) throw new Error((j && j.error) || "status failed");
          var step = $("heal-step");
          if (step) step.textContent = j.step || "";
          var bar = $("heal-bar"), prog = $("heal-prog");
          var total = j.series_total || 1, done = j.series_done || 0;
          if (bar) bar.style.width = Math.round(100 * done / Math.max(total, 1)) + "%";
          if (prog) prog.textContent = total > 1 ? ("Series " + done + " of " + total) : "";
          if (j.done) self.finish(j);
        })
        .catch(function (e) { self.fail(e.message); });
    },
    _line: function (r) {
      var name = "<b>" + escHtml(r.symbol || "?") + " " + escHtml(r.tf || "") + "</b> \u2014 ";
      if (r.skipped) return name + "tick chart: heals itself from the live tick stream, nothing to repair.";
      if (!r.live) return name + '<span style="color:var(--warn)">MT5 is not connected \u2014 cannot backfill.</span>';
      if (r.error) return name + '<span style="color:var(--warn)">failed: ' + escHtml(r.error) + "</span>";
      var n = r.slots_healed || 0;
      /* honest missing-data reporting. heal_data now sends
         missing_slots/missing_ranges when gaps remain after the fill -
         "gap found but MT5 gave nothing" falls through to
         "no gaps - data is complete" unless missing_slots/missing_ranges
         are inspected. Never claim completeness while gaps remain.*/
      var miss = r.missing_slots || 0;
      if (miss > 0) {
        var rng = (r.missing_ranges && r.missing_ranges.length)
          ? " (" + r.missing_ranges.map(function (x) { return escHtml(x); }).join(", ") + ")" : "";
        var msg = name + '<span style="color:var(--warn)">Still missing ' + miss + " candle" +
          (miss === 1 ? "" : "s") + rng +
          " \u2014 MT5 returned no data for this range; will retry automatically.</span>";
        if (n > 0)
          msg += ' <span style="color:var(--up)">Restored ' + n + " other candle" +
            (n === 1 ? "" : "s") + ".</span>";
        return msg;
      }
      if (!r.tail_ok)
        return name + '<span style="color:var(--warn)">MT5 gave no data' +
          (r.tail_err ? " (" + escHtml(String(r.tail_err).slice(0, 90)) + ")" : "") +
          " \u2014 terminal may still be syncing.</span>" +
          (n > 0 ? " Still restored " + n + " cached gap candle(s)." : " No gaps fixed; will retry automatically.");
      if (n > 0) {
        var extra = r.bars_added ? (" (" + r.bars_added + " bar(s) pulled from MT5)") : "";
        /* say when bars were rebuilt from MT5 tick history
           (copy_ticks_range) because the rate history would not serve them.
           say when glitch/reconstruction bars were repaired.*/
        if (r.tick_rebuilt > 0)
          extra += (extra ? ", " : " (") + r.tick_rebuilt + " rebuilt from MT5 tick history" + (extra ? "" : ")");
        if (r.bars_repaired > 0)
          extra += (extra ? ", " : " (") + r.bars_repaired + " glitch bar(s) repaired from MT5" + (extra ? "" : ")");
        return name + '<span style="color:var(--up)">' + n + " missing candle" +
          (n === 1 ? "" : "s") + " restored" + extra + ".</span>";
      }
      /* a repair can happen with zero missing slots (a bad bar
         superseded in place) - report it instead of "data is complete".*/
      if (r.bars_repaired > 0)
        return name + '<span style="color:var(--up)">' + r.bars_repaired + " glitch bar" +
          (r.bars_repaired === 1 ? "" : "s") + " repaired from MT5.</span>";
      return name + "no gaps \u2014 data is complete.";
    },
    finish: function (j) {
      this.stop();
      var sp = $("heal-spin");
      if (sp) sp.style.display = "none";
      var step = $("heal-step");
      if (step) {
        var n = j.total_healed || 0, m = j.total_missing || 0;
        /* the top line stays honest too - "nothing to repair"
           only when nothing is actually missing.
           credit bars rebuilt from MT5 tick history.
           credit glitch bars repaired from MT5 rates.*/
        var tnote = (j.total_tick_rebuilt > 0)
          ? " (<b>" + j.total_tick_rebuilt + "</b> rebuilt from MT5 tick history)" : "";
        var rnote = (j.total_repaired > 0)
          ? " (<b>" + j.total_repaired + "</b> glitch bar(s) repaired from MT5)" : "";
        step.innerHTML = !j.live
          ? "Finished \u2014 MT5 is not connected."
          : (m > 0 ? ("Finished \u2014 <b>" + m + "</b> candle(s) still missing; " +
               "MT5 returned no data. Will retry automatically." +
               (n > 0 ? " Restored <b>" + n + "</b> in the meantime" + tnote + "." : ""))
          : (n > 0 ? ("Finished \u2014 restored <b>" + n + "</b> missing candle(s)" + tnote + rnote + ".")
                   : (j.total_repaired > 0
                       ? "Finished \u2014 repaired <b>" + j.total_repaired + "</b> glitch bar(s) from MT5."
                       : "Finished \u2014 nothing to repair.")));
      }
      var bar = $("heal-bar");
      if (bar) bar.style.width = "100%";
      var prog = $("heal-prog");
      if (prog) prog.textContent = "";
      var box = $("heal-results");
      if (box) {
        var rs = j.results || [];
        box.innerHTML = rs.length
          ? rs.map(function (r) { return '<div class="note" style="margin:4px 0">' + this._line(r) + "</div>"; }, this).join("")
          : '<div class="note">No series healed.</div>';
      }
      try { if (this._done) this._done(); } catch (e) {}
    }
  };

  /* drag the modal by its title bar so the chart behind stays visible */
  (function modalDrag() {
    var head = document.querySelector("#modal-root .mhead");
    var box = document.querySelector("#modal-root .mbox");
    if (!head || !box) return;
    var sx, sy, bx, by, dragging = false;
    head.addEventListener("mousedown", function (e) {
      if (e.target.tagName === "BUTTON") return;
      dragging = true;
      var r = box.getBoundingClientRect();
      // Switch from centered to explicit positioning.
      box.style.transform = "none";
      box.style.left = r.left + "px";
      box.style.top = r.top + "px";
      sx = e.clientX; sy = e.clientY; bx = r.left; by = r.top;
      e.preventDefault();
    });
    document.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      var nx = Math.max(-box.offsetWidth + 80,
                        Math.min(window.innerWidth - 80, bx + e.clientX - sx));
      var ny = Math.max(0, Math.min(window.innerHeight - 40, by + e.clientY - sy));
      box.style.left = nx + "px";
      box.style.top = ny + "px";
    });
    document.addEventListener("mouseup", function () { dragging = false; });
  })();

  /* ------------------------------------------------------------- telegram */
  var TG = {
    cfg: function () {
      try {
        return Object.assign({ enabled: false, token: "", chatId: "", onPrice: true, onSweep: true },
          JSON.parse(localStorage.getItem(LS_TG) || "{}"));
      } catch (e) { return { enabled: false, token: "", chatId: "", onPrice: true, onSweep: true }; }
    },
    save: function (c) { try { localStorage.setItem(LS_TG, JSON.stringify(c)); } catch (e) {} },
    send: function (text, kind) {
      var c = this.cfg();
      if (!c.enabled || !c.token || !c.chatId) return;
      if (kind === "price" && !c.onPrice) return;
      if (kind === "sweep" && !c.onSweep) return;
      fetch("https://api.telegram.org/bot" + c.token + "/sendMessage", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: c.chatId, text: text, disable_web_page_preview: true })
      }).catch(function () {});
    }
  };

  /* ---------------------------------------------------------- price alerts
     Server-owned alert definitions (chart-style). The browser keeps a
     synced cache (Alerts._cache): evaluation runs in the server-side
     Alert Engine on each alert's own timeframe \u2014 browser open
     or not \u2014 while lifecycle, dedup, the trigger log and delivery live
     server-side behind /api/alerts/*. Popup + sound for engine-fired alerts
     arrive here via Alerts.pollFires().*/
  var STATUS_LABEL = {
    active: "Active",
    stopped_manual: "Stopped",
    stopped_triggered: "Triggered",
    stopped_expired: "Expired"
  };
  var Alerts = {
    _cache: [],

    _api: function (path, opts) {
      opts = opts || {};
      /* timeout - a save must never hang silently (the Create
         alert button appears dead when the request queues behind
         stuck connections).*/
      var controller = new AbortController();
      var timer = setTimeout(function () { controller.abort(); }, 30000);
      var o = { headers: { "Content-Type": "application/json" }, signal: controller.signal };
      if (opts.method) o.method = opts.method;
      if (opts.body !== undefined) o.body = JSON.stringify(opts.body);
      return fetch(path, o).then(function (r) {
        clearTimeout(timer);
        return r.text().then(function (t) {
          var j = null;
          try { j = JSON.parse(t); } catch (e) {}
          if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
          if (!j) throw new Error("empty response from server");
          return j;
        });
      }).catch(function (e) {
        clearTimeout(timer);
        if (e.name === "AbortError") throw new Error("Request timed out after 30s - try again");
        throw e;
      });
    },

    refresh: function (cb) {
      var self = this;
      this._api("/api/alerts").then(function (j) {
        self._cache = j.alerts || [];
        updateAlertBadge();
        Layout.panes.forEach(function (p) { p.draw(); });
        AlertPanel.refreshList();
        if (cb) cb();
      }).catch(function () { if (cb) cb(); });
    },

    list: function () { return this._cache; },

    forSymbol: function (symbol) {
      return this._cache.filter(function (a) { return a.symbol === symbol; });
    },

    activePriceAlerts: function (symbol) {
      return this._cache.filter(function (a) {
        return a.kind === "price" && a.status === "active" && a.symbol === symbol;
      });
    },

    byId: function (id) {
      var found = null;
      this._cache.forEach(function (a) { if (a.id === id) found = a; });
      return found;
    },

    /* one-time migration of the old browser-local price alerts */
    migrate: function () {
      var raw = null;
      try {
        raw = localStorage.getItem(LS_ALERTS) ||
              localStorage.getItem("stocksophist_tg_alerts");
      } catch (e) { raw = null; }
      function done() {
        try {
          localStorage.removeItem(LS_ALERTS);
          localStorage.removeItem("stocksophist_tg_alerts");
          localStorage.removeItem("stocksophist_tg_cfg");
        } catch (e) {}
      }
      if (!raw) { done(); return; }
      var arr = null;
      try { arr = JSON.parse(raw); } catch (e) { arr = null; }
      if (!arr || !arr.length) { done(); return; }
      var self = this, migrated = 0;
      (function next(i) {
        if (i >= arr.length) {
          done();
          self.refresh();
          if (migrated) toast("Migrated " + migrated + " price alert" +
                              (migrated === 1 ? "" : "s") + " to the Alerts panel");
          return;
        }
        var a = arr[i] || {};
        var px = parseFloat(a.price);
        var dup = self._cache.some(function (x) {
          return x.kind === "price" && x.symbol === a.symbol &&
                 String(x.price) === String(a.price);
        });
        if (dup || !a.symbol || !isFinite(px)) { next(i + 1); return; }
        self._api("/api/alerts", { method: "POST", body: {
          kind: "price", symbol: a.symbol, price: px, condition: "crossing",
          note: a.note || "", freq: a.mode === "every" ? "every" : "once",
          cooldown_min: a.cooldownMin || 0,
          name: a.symbol + " Crossing " + px
        } }).then(function () { migrated++; next(i + 1); },
                 function () { next(i + 1); });
      })(0);
    },

    /* Price cross detection now lives in the server-side Alert Engine
       it evaluates every active alert on its own timeframe in the
       background, so nothing is missed when the browser is closed or the
       chart shows another timeframe. Popup + sound for engine-fired alerts
       arrive via pollFires() below.*/
    _seenFires: {},
    _lastFirePoll: 0,

    pollFires: function () {
      var self = this;
      var since = this._lastFirePoll || (Math.floor(Date.now() / 1000) - 15);
      this._api("/api/alerts/log?limit=60&since=" + since).then(function (j) {
        var evs = ((j && j.events) || []).filter(function (e) { return !e.sys; });
        if (!evs.length) {
          if (!self._lastFirePoll) self._lastFirePoll = Math.floor(Date.now() / 1000);
          return;
        }
        var maxTs = since;
        evs.forEach(function (e) {
          if ((e.triggered_at || 0) > maxTs) maxTs = e.triggered_at;
        });
        self._lastFirePoll = maxTs;
        evs.sort(function (x, y) { return (x.triggered_at || 0) - (y.triggered_at || 0); });
        evs.forEach(function (e) {
          if (self._seenFires[e.id]) return;
          self._seenFires[e.id] = 1;
          var def = null;
          (self._cache || []).forEach(function (a) { if (a.id === e.alert_id) def = a; });
          var n = (def && def.notify) || {};
          if (n.popup !== false) {
            /* persistent bottom-right notification (stays until closed)
               pass alert id so the card gets a one-click Stop button*/
            var _price = (e.price_at_fire !== null && e.price_at_fire !== undefined) ? " @ " + e.price_at_fire : "";
            // show the full alert message in the popup, not just symbol+price
            var _detail = (def && def.symbol ? def.symbol + " " : "") + _price.trim();
            if (e.text) _detail = (_detail ? _detail + "\n" : "") + e.text;
            AlertNotify.show("(bell) " + (e.title || "Alert"), _detail || null, e.alert_id);
          }
          if (n.sound !== false) playAlertSound();
        });
        self.refresh();
      }).catch(function () {});
    },


    open: function () { AlertPanel.toggle(); },

    stopResume: function (a) {
      var self = this;
      this._api("/api/alerts/" + a.id +
                (a.status === "active" ? "/stop" : "/resume"),
                { method: "POST" })
        .then(function () { self.refresh(); })
        .catch(function (e) { toast("Failed: " + e.message); });
    },

    remove: function (a) {
      var self = this;
      if (!confirm("Delete alert '" + a.name + "'?")) return;
      this._api("/api/alerts/" + a.id, { method: "DELETE" })
        .then(function () { self.refresh(); toast("Alert deleted"); })
        .catch(function (e) { toast("Failed: " + e.message); });
    },

    clone: function (a) {
      var self = this;
      var c = {};
      ["kind", "symbol", "indicator_id", "indicator_name", "condition",
       "price", "note", "freq", "cooldown_min", "notify", "message",
       "webhook_url"].forEach(function (k) { c[k] = a[k]; });
      c.name = (a.name || "Alert") + " (copy)";
      this._api("/api/alerts", { method: "POST", body: c })
        .then(function () { self.refresh(); toast("Alert cloned"); })
        .catch(function (e) { toast("Failed: " + e.message); });
    },

    /* -------------- create / edit dialog: price alert */
    createDialog: function (symbol, price, existing) {
      var self = this;
      var a = existing || {
        symbol: symbol || "", condition: "crossing", price: price,
        name: "", note: "", freq: "once", cooldown_min: 5,
        expires_at: null,
        notify: { popup: true, sound: true, telegram: true, webhook: true },
        webhook_url: "", message: "{{text}}"
      };
      var n = a.notify || {};
      function expLocal(exp) {
        if (!exp) return "";
        return fmtTZ_YMDHM(exp * 1000);
      }
      var body =
        '<label class="f">Symbol</label>' +
        '<input type="text" id="pa-sym" value="' + escHtml(a.symbol || "") + '">' +
        '<label class="f">Condition</label>' +
        '<select id="pa-cond">' +
        ["crossing", "crossing up", "crossing down"].map(function (c) {
          return '<option value="' + c + '"' +
            ((a.condition || "crossing") === c ? " selected" : "") + ">" +
            c.charAt(0).toUpperCase() + c.slice(1) + "</option>";
        }).join("") + "</select>" +
        '<label class="f">Price</label>' +
        '<input type="number" id="pa-price" step="any" value="' +
        escHtml(a.price === undefined || a.price === null || isNaN(a.price) ?
                "" : String(a.price)) + '">' +
        '<label class="f">Name (optional)</label>' +
        '<input type="text" id="pa-name" value="' + escHtml(a.name || "") + '" ' +
        'placeholder="e.g. London high watch">' +
        '<label class="f">Note (optional)</label>' +
        '<input type="text" id="pa-note" value="' + escHtml(a.note || "") + '">' +
        '<label class="f">Alert me</label>' +
        '<label class="chk"><input type="radio" name="pa-mode" value="once"' +
        (a.freq !== "every" ? " checked" : "") + ' style="width:auto;margin-right:8px"> Just once</label>' +
        '<label class="chk"><input type="radio" name="pa-mode" value="every"' +
        (a.freq === "every" ? " checked" : "") + ' style="width:auto;margin-right:8px"> ' +
        "Every time price reaches this level</label>" +
        '<div id="pa-cdwrap"><label class="f">Cooldown between alerts (minutes)</label>' +
        '<input type="number" id="pa-cd" min="0" step="1" value="' +
        escHtml(String(a.cooldown_min || 0)) + '">' +
        '<div class="note">For "every time" alerts: minimum wait before it can fire ' +
        "again. 0 = no cooldown.</div></div>" +
        '<label class="f">Expires (optional)</label>' +
        '<input type="datetime-local" id="pa-exp" value="' + expLocal(a.expires_at) + '">' +
        '<div class="note" style="margin-top:2px">Leave empty for no expiry. ' +
        "An expired alert stops itself (it is kept, not deleted).</div>" +
        '<label class="f">Notify me via</label>' +
        '<div class="pa-notify">' +
        '<label class="chk"><input type="checkbox" id="pa-n-pop"' +
        (n.popup !== false ? " checked" : "") + "> Popup</label>" +
        '<label class="chk"><input type="checkbox" id="pa-n-snd"' +
        (n.sound !== false ? " checked" : "") + "> Sound</label>" +
        '<label class="chk"><input type="checkbox" id="pa-n-tg"' +
        (n.telegram !== false ? " checked" : "") + "> Telegram</label>" +
        '<label class="chk"><input type="checkbox" id="pa-n-wh"' +
        (n.webhook !== false ? " checked" : "") + "> Webhook</label>" +
        "</div>" +
        '<label class="f">Webhook URL (optional - used when Webhook is ticked)</label>' +
        '<input type="text" id="pa-whurl" value="' + escHtml(a.webhook_url || "") +
        '" placeholder="https://..." autocomplete="off">' +
        '<label class="f">Message</label>' +
        '<input type="text" id="pa-msg" value="' + escHtml(a.message || "{{text}}") + '">' +
        '<div class="note" style="margin-top:2px">Placeholders: {{text}} {{symbol}} ' +
        "{{price}} {{name}}.</div>";
      Modal.open(existing ? "Edit alert" : "Create alert", body,
        '<button class="btn ghost" id="pa-cancel">Cancel</button>' +
        '<button class="btn" id="pa-save">' + (existing ? "Save" : "Create alert") + "</button>");
      var mb = document.querySelector("#modal-root .mbox");
      if (mb) mb.classList.add("wide");
      function syncCd() {
        var m = document.querySelector('input[name="pa-mode"]:checked');
        $("pa-cdwrap").style.display = (m && m.value === "every") ? "block" : "none";
      }
      Array.prototype.forEach.call(document.querySelectorAll('input[name="pa-mode"]'),
        function (r) { r.addEventListener("change", syncCd); });
      syncCd();
      $("pa-cancel").addEventListener("click", Modal.close);
      $("pa-save").addEventListener("click", function () {
        var sym = $("pa-sym").value.trim() || symbol || "";
        var px = parseFloat($("pa-price").value);
        if (!sym) { toast("Enter a symbol"); return; }
        if (!isFinite(px)) { toast("Enter a valid price"); return; }
        var m = document.querySelector('input[name="pa-mode"]:checked');
        var expV = $("pa-exp").value;
        var payload = {
          kind: "price", symbol: sym,
          condition: $("pa-cond").value, price: roundPx(px),
          name: $("pa-name").value.trim() ||
                (sym + " " + $("pa-cond").value + " " + roundPx(px)),
          note: $("pa-note").value.trim(),
          freq: (m && m.value === "every") ? "every" : "once",
          cooldown_min: Math.max(0, parseFloat($("pa-cd").value) || 0),
          expires_at: expV ? Math.floor(new Date(expV).getTime() / 1000) : null,
          notify: {
            popup: $("pa-n-pop").checked, sound: $("pa-n-snd").checked,
            telegram: $("pa-n-tg").checked, webhook: $("pa-n-wh").checked
          },
          webhook_url: $("pa-whurl").value.trim(),
          message: $("pa-msg").value.trim() || "{{text}}"
        };
        var done = function () {
          Modal.close(); self.refresh();
          toast(existing ? "Alert updated" : "Alert created: " + sym + " @ " + roundPx(px));
        };
        /* visible saving state - the button must never look dead
           while the request is in flight.*/
        var saveBtn = $("pa-save");
        saveBtn.disabled = true; saveBtn.textContent = "Saving...";
        var saveFail = function (e) {
          saveBtn.disabled = false;
          saveBtn.textContent = existing ? "Save" : "Create alert";
          toast("Save failed: " + e.message);
        };
        if (existing)
          self._api("/api/alerts/" + existing.id, { method: "PUT", body: payload })
            .then(done).catch(saveFail);
        else
          self._api("/api/alerts", { method: "POST", body: payload })
            .then(done).catch(saveFail);
      });
      setTimeout(function () { var p = $("pa-price"); if (p) { p.focus(); p.select(); } }, 60);
    },

    /* -------------- create / edit dialog: indicator alert */
    indicatorDialog: function (pane, inst, meta, existing) {
      var self = this;
      var uid = inst.id + "@" + pane.id;
      var a = existing || {
        name: (meta.name || inst.id) + " - " + pane.state.symbol,
        freq: "every", cooldown_min: 0, expires_at: null,
        notify: { popup: true, sound: true, telegram: true, webhook: true },
        webhook_url: "", message: "{{text}}"
      };
      var n = a.notify || {};
      var tf0 = (a.timeframe && ALERT_TF_LIST.indexOf(a.timeframe) >= 0) ? a.timeframe :
                (ALERT_TF_LIST.indexOf(pane.state.tf) >= 0 ? pane.state.tf : "1m");
      /* Renko pane: the alert evaluates on Renko bricks - the timeframe
         selects the bar series the bricks are built from (15s bricks are
         built from real 15-second bars), like the Renko engine on a Renko chart
         in TV.*/
      var renkoPane = pane.state.mode === "renko";
      var tfField = '<select id="ia-tf">' + ALERT_TF_LIST.map(function (t) {
          return '<option value="' + t + '"' + (t === tf0 ? " selected" : "") + ">" + t + "</option>";
        }).join("") + "</select>";
      if (renkoPane)
        tfField = '<div class="note" style="margin:0 0 4px"><b>Renko</b> - box ' +
        escHtml(String(pane.state.box)) + '</div>' + tfField;
      var body =
        '<div class="note" style="margin:0 0 10px">Fires on <b>any signal</b> this indicator raises.</div>' +
        '<label class="f">Indicator</label>' +
        '<div class="note" style="margin:0 0 4px"><b>' + escHtml(meta.name || inst.id) +
        "</b> - " + escHtml(pane.state.symbol) + "</div>" +
        '<label class="f">Timeframe</label>' + tfField +
        '<div class="note" style="margin-top:2px">' + (renkoPane ?
          "Bricks are built from the timeframe selected above - the indicator " +
          "is calculated on those bricks in the background, no matter which " +
          "timeframe the chart shows, even with the browser closed." :
          "The indicator is calculated on this timeframe " +
          "in the background - no matter which timeframe the chart shows, and even with the " +
          "browser closed.") + "</div>" +
        '<label class="f">Name (optional)</label>' +
        '<input type="text" id="ia-name" value="' + escHtml(a.name || "") + '">' +
        '<label class="f">Alert me</label>' +
        '<label class="chk"><input type="radio" name="ia-mode" value="once"' +
        (a.freq !== "every" ? " checked" : "") + ' style="width:auto;margin-right:8px"> Just once</label>' +
        '<label class="chk"><input type="radio" name="ia-mode" value="every"' +
        (a.freq === "every" ? " checked" : "") + ' style="width:auto;margin-right:8px"> ' +
        "Every time a signal fires</label>" +
        '<div id="ia-cdwrap"><label class="f">Cooldown between alerts (minutes)</label>' +
        '<input type="number" id="ia-cd" min="0" step="1" value="' +
        escHtml(String(a.cooldown_min || 0)) + '"></div>' +
        '<label class="f">Notify me via</label>' +
        '<div class="pa-notify">' +
        '<label class="chk"><input type="checkbox" id="ia-n-pop"' +
        (n.popup !== false ? " checked" : "") + "> Popup</label>" +
        '<label class="chk"><input type="checkbox" id="ia-n-snd"' +
        (n.sound !== false ? " checked" : "") + "> Sound</label>" +
        '<label class="chk"><input type="checkbox" id="ia-n-tg"' +
        (n.telegram !== false ? " checked" : "") + "> Telegram</label>" +
        '<label class="chk"><input type="checkbox" id="ia-n-wh"' +
        (n.webhook !== false ? " checked" : "") + "> Webhook</label>" +
        "</div>" +
        '<label class="f">Webhook URL (optional - used when Webhook is ticked)</label>' +
        '<input type="text" id="ia-whurl" value="' + escHtml(a.webhook_url || "") +
        '" placeholder="https://..." autocomplete="off">' +
        '<label class="f">Message</label>' +
        '<input type="text" id="ia-msg" value="' + escHtml(a.message || "{{text}}") + '">' +
        '<div class="note" style="margin-top:2px">Placeholders: {{text}} {{symbol}} ' +
        "{{price}} {{name}}. Keep your keywords in the message so the Telegram " +
        "sent to your configured Telegram chat as-is.</div>";
      Modal.open((existing ? "Edit alert - " : "Create alert - ") + (meta.name || inst.id), body,
        '<button class="btn ghost" id="ia-cancel">Cancel</button>' +
        '<button class="btn" id="ia-save">' + (existing ? "Save" : "Create alert") + "</button>");
      function syncCd() {
        var m = document.querySelector('input[name="ia-mode"]:checked');
        $("ia-cdwrap").style.display = (m && m.value === "every") ? "block" : "none";
      }
      Array.prototype.forEach.call(document.querySelectorAll('input[name="ia-mode"]'),
        function (r) { r.addEventListener("change", syncCd); });
      syncCd();
      $("ia-cancel").addEventListener("click", Modal.close);
      $("ia-save").addEventListener("click", function () {
        var m = document.querySelector('input[name="ia-mode"]:checked');
        var payload = {
          kind: "indicator", indicator_id: uid,
          indicator_name: meta.name || inst.id, symbol: pane.state.symbol,
          condition: "any_signal",
          timeframe: $("ia-tf").value,
          chart_mode: renkoPane ? "renko" : "",
          box: renkoPane ? pane.state.box : 0,
          settings: inst.settings || {},
          name: $("ia-name").value.trim() ||
                ((meta.name || inst.id) + " - " + pane.state.symbol),
          freq: (m && m.value === "every") ? "every" : "once",
          cooldown_min: Math.max(0, parseFloat($("ia-cd").value) || 0),
          notify: {
            popup: $("ia-n-pop").checked, sound: $("ia-n-snd").checked,
            telegram: $("ia-n-tg").checked, webhook: $("ia-n-wh").checked
          },
          webhook_url: $("ia-whurl").value.trim(),
          message: $("ia-msg").value.trim() || "{{text}}"
        };
        var done = function () {
          Modal.close(); self.refresh();
          toast(existing ? "Alert updated" : "Alert created for " + (meta.name || inst.id));
        };
        /* visible saving state - the button must never look dead
           while the request is in flight.*/
        var saveBtn = $("ia-save");
        saveBtn.disabled = true; saveBtn.textContent = "Saving...";
        var saveFail = function (e) {
          saveBtn.disabled = false;
          saveBtn.textContent = existing ? "Save" : "Create alert";
          toast("Save failed: " + e.message);
        };
        if (existing)
          self._api("/api/alerts/" + existing.id, { method: "PUT", body: payload })
            .then(done).catch(saveFail);
        else
          self._api("/api/alerts", { method: "POST", body: payload })
            .then(done).catch(saveFail);
      });
    }
  };

  function updateAlertBadge() {
    var n = Alerts.list().filter(function (a) { return a.status === "active"; }).length;
    ["alertbar-cnt"].forEach(function (id) {
      var el = $(id);
      if (el) { el.textContent = n; el.classList.toggle("show", n > 0); }
    });
  }

  /* ------------------------------- right-side Alerts / Log panel
     chart-style: clock button in the right rail toggles this panel.
     Alerts tab = alert definitions with owner + lifecycle status.
     Log tab = triggered-alert history with per-destination delivery state.*/
  var AlertPanel = {
    visible: false, tab: "alerts", filter: "all", search: "", sort: "newest",

    toggle: function (show) {
      this.visible = (show === undefined) ? !this.visible : !!show;
      $("alertpanel").classList.toggle("open", this.visible);
      $("alertbar-btn").classList.toggle("on", this.visible);
      if (this.visible) this.render();
    },

    init: function () {
      var self = this;
      $("alertbar-btn").addEventListener("click", function () { self.toggle(); });
      $("ap-close").addEventListener("click", function () { self.toggle(false); });
      Array.prototype.forEach.call(document.querySelectorAll(".ap-tab"), function (t) {
        t.addEventListener("click", function () { self.setTab(t.getAttribute("data-tab")); });
      });
      $("ap-search").addEventListener("input", function () {
        self.search = this.value; self.renderList();
      });
      Array.prototype.forEach.call(document.querySelectorAll(".ap-chip"), function (c) {
        c.addEventListener("click", function () {
          Array.prototype.forEach.call(document.querySelectorAll(".ap-chip"),
            function (x) { x.classList.remove("on"); });
          c.classList.add("on");
          self.filter = c.getAttribute("data-f");
          self.renderList();
        });
      });
      $("ap-sort").addEventListener("change", function () {
        self.sort = this.value; self.renderList();
      });
      $("ap-add").addEventListener("click", function () {
        var sym = "";
        try { sym = ((Layout.panes[Layout.activeId] || {}).state || {}).symbol || ""; }
        catch (e) { sym = ""; }
        Alerts.createDialog(sym, NaN, null);
      });
      $("ap-clearlog").addEventListener("click", function () {
        if (!confirm("Clear the entire alert log?")) return;
        Alerts._api("/api/alerts/log", { method: "DELETE" })
          .then(function () { self.renderLog(); toast("Alert log cleared"); })
          .catch(function (e) { toast("Failed: " + e.message); });
      });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && self.visible &&
            !$("modal-root").classList.contains("open")) self.toggle(false);
      });
    },

    setTab: function (t) {
      this.tab = t;
      Array.prototype.forEach.call(document.querySelectorAll(".ap-tab"), function (x) {
        x.classList.toggle("on", x.getAttribute("data-tab") === t);
      });
      $("ap-alerts-tools").style.display = t === "alerts" ? "block" : "none";
      $("ap-log-tools").style.display = t === "log" ? "flex" : "none";
      this.render();
    },

    render: function () {
      if (!this.visible) return;
      if (this.tab === "alerts") this.renderList(); else this.renderLog();
    },

    refreshList: function () {
      if (this.visible && this.tab === "alerts") this.renderList();
    },

    _filtered: function () {
      var list = Alerts.list().slice(), self = this;
      if (this.filter === "active")
        list = list.filter(function (a) { return a.status === "active"; });
      else if (this.filter === "stopped")
        list = list.filter(function (a) { return a.status !== "active"; });
      var q = this.search.trim().toLowerCase();
      if (q) list = list.filter(function (a) {
        return ((a.name || "") + " " + (a.symbol || "") + " " +
                (a.indicator_name || "") + " " + (a.note || ""))
               .toLowerCase().indexOf(q) >= 0;
      });
      if (this.sort === "name")
        list.sort(function (x, y) { return (x.name || "").localeCompare(y.name || ""); });
      else if (this.sort === "symbol")
        list.sort(function (x, y) {
          return (x.symbol || "").localeCompare(y.symbol || "") ||
                 (x.name || "").localeCompare(y.name || "");
        });
      else
        list.sort(function (x, y) { return (y.created_at || 0) - (x.created_at || 0); });
      return list;
    },

    _summary: function (a) {
      if (a.kind === "indicator") {
        var s = "Any signal";
        if (a.chart_mode === "renko") s += " on Renko (box " + a.box +
          (a.timeframe ? " - " + a.timeframe : "") + ")";
        else if (a.timeframe) s += " on " + a.timeframe;
        if (a.freq === "once") s += " - once";
        else if (a.cooldown_min > 0) s += " - cooldown " + a.cooldown_min + "m";
        return s;
      }
      var c = a.condition || "crossing";
      c = c.charAt(0).toUpperCase() + c.slice(1);
      var s2 = c + " @ " + a.price;
      if (a.freq === "once") s2 += " - once";
      else if (a.cooldown_min > 0) s2 += " - cooldown " + a.cooldown_min + "m";
      return s2;
    },

    _owner: function (a) {
      if (a.kind === "indicator") return a.indicator_name || a.indicator_id || "Indicator";
      return "Price";
    },

    renderList: function () {
      var self = this, list = this._filtered();
      function ago(ts) {
        if (!ts) return "never";
        var s = Math.floor(Date.now() / 1000) - ts;
        if (s < 60) return "just now";
        if (s < 3600) return Math.floor(s / 60) + "m ago";
        if (s < 86400) return Math.floor(s / 3600) + "h ago";
        return Math.floor(s / 86400) + "d ago";
      }
      var html = list.length ? list.map(function (a) {
        var trig = "x" + (a.trigger_count || 0) +
                   (a.last_triggered_at ? " - last " + ago(a.last_triggered_at) : "");
        return '<div class="ap-row" data-id="' + escHtml(a.id) + '">' +
          '<div class="ap-rowmain">' +
          '<div class="ap-r1"><span class="ap-name">' + escHtml(a.name || "Alert") + "</span>" +
          '<span class="st st-' + escHtml(a.status) + '">' +
          escHtml(STATUS_LABEL[a.status] || a.status) + "</span></div>" +
          '<div class="ap-r2">' + escHtml(a.symbol || "") + " - " +
          escHtml(self._owner(a)) + "</div>" +
          '<div class="ap-r3">' + escHtml(self._summary(a)) + " - " +
          escHtml(trig) + "</div>" +
          "</div>" +
          '<div class="ap-acts">' +
          '<button data-act="stop" title="' +
          (a.status === "active" ? "Stop" : "Resume") + '">' +
          (a.status === "active" ? "&#10074;&#10074;" : "&#9654;") + "</button>" +
          '<button data-act="edit" title="Edit">&#9998;</button>' +
          '<button data-act="clone" title="Clone">&#10697;</button>' +
          '<button data-act="del" title="Delete">&#128465;</button>' +
          "</div></div>";
      }).join("") : '<div class="ap-empty">No alerts yet.<br>Hover the price axis and click ' +
        "<b>+</b>, right-click the chart -> 'Add alert', or use '+ New alert' above.</div>";
      $("ap-list").innerHTML = html;
      Array.prototype.forEach.call(
        $("ap-list").querySelectorAll("[data-act]"), function (b) {
          b.addEventListener("click", function (e) {
            e.stopPropagation();
            var row = b.closest(".ap-row");
            var a = Alerts.byId(row.getAttribute("data-id"));
            if (!a) return;
            var act = b.getAttribute("data-act");
            if (act === "stop") Alerts.stopResume(a);
            else if (act === "del") Alerts.remove(a);
            else if (act === "clone") Alerts.clone(a);
            else if (act === "edit") {
              if (a.kind === "indicator") {
                /* recover pane + instance from the indicator_id (instId@paneId) */
                var mm = /^(.+)@(\d+)$/.exec(a.indicator_id || ""), done2 = false;
                if (mm) Layout.panes.forEach(function (p) {
                  if (done2 || String(p.id) !== mm[2]) return;
                  (p.state.indicators || []).forEach(function (inst) {
                    if (done2 || inst.id !== mm[1]) return;
                    done2 = true;
                    Alerts.indicatorDialog(p, inst,
                      (typeof indById === "function" && indById(inst.id)) || { name: inst.id }, a);
                  });
                });
                if (!done2) toast("Indicator not on the chart - clone the alert instead");
              } else Alerts.createDialog(a.symbol, a.price, a);
            }
          });
        });
    },

    renderLog: function () {
      var self = this;
      $("ap-list").innerHTML = '<div class="ap-empty">Loading log...</div>';
      Alerts._api("/api/alerts/log?limit=200").then(function (j) {
        var evs = j.events || [];
        function dlChip(dest, label) {
          var v = String((dest || {})[label] || "");
          var cls = "dl", txt = label + ": ";
          if (!v || v === "disabled") { txt += "off"; }
          else if (v.indexOf("sent") === 0) { cls += " ok"; txt += "[ok]"; }
          else if (v.indexOf("failed") === 0) { cls += " err"; txt += "[x]"; }
          else if (v.indexOf("duplicate") === 0) { cls += " warn"; txt += "dup"; }
          else if (v.indexOf("ignored") === 0) { txt += "ignored"; }
          else { cls += " warn"; txt += v.slice(0, 18); }
          return '<span class="' + cls + '" title="' + escHtml(v) + '">' +
                 escHtml(txt) + "</span>";
        }
        var html = evs.length ? evs.map(function (e) {
          var ms = (e.triggered_at || 0) * 1000;
          var ts = fmtTZ_HMS(ms), day = fmtTZ_YMD(ms);
          var kindCls = e.kind === "price" ? "k-price" :
                        e.kind === "indicator" ? "k-indicator" : "";
          var kindLbl = e.sys ? "SYS" : (e.kind || "").toUpperCase();
          return '<div class="ap-logrow' + (e.sys ? " sys" : "") + '">' +
            '<div class="ap-lr1"><span class="ap-kind ' + kindCls + '">' +
            escHtml(kindLbl) + "</span>" +
            '<span class="ap-ltime" title="' + escHtml(day) + " " + escHtml(selZoneShort()) + '">' + escHtml(ts) + " " + escHtml(selZoneShort()) + "</span>" +
            (e.price_at_fire !== null && e.price_at_fire !== undefined ?
              '<span class="ap-lprice">@ ' + escHtml(String(e.price_at_fire)) + "</span>" : "") +
            "</div>" +
            '<div class="ap-ltitle">' + escHtml(e.title || "") + "</div>" +
            (e.sys ? "" : '<div class="ap-lchips">' +
              dlChip(e.delivery, "telegram") + dlChip(e.delivery, "webhook") + "</div>") +
            '<div class="ap-ltext">' + escHtml(e.text || "") + "</div>" +
            "</div>";
        }).join("") : '<div class="ap-empty">No triggered alerts yet.<br>' +
          "Fired alerts appear here with their delivery status.</div>";
        $("ap-list").innerHTML = html;
        Array.prototype.forEach.call(
          $("ap-list").querySelectorAll(".ap-logrow"), function (row) {
            row.addEventListener("click", function () { row.classList.toggle("exp"); });
          });
      }).catch(function () {
        $("ap-list").innerHTML = '<div class="ap-empty">Log unavailable.</div>';
      });
    }
  };

  /* ------------------------------- per-indicator alerts (chart-style)
     The ... menu on each indicator legend row opens a "Create alert" dialog like
     the chart's: condition (any signal the indicator raises), frequency,
     notification destinations, an editable message, and a webhook URL.
     Definitions live server-side (/api/alerts); this is just the UI hook.*/
  var IndAlerts = {
    /* server alert definition for an indicator instance, if one exists */
    findAlert: function (pane, inst) {
      var uid = inst.id + "@" + pane.id, found = null;
      Alerts.list().forEach(function (a) {
        if (a.kind === "indicator" && a.indicator_id === uid) found = a;
      });
      return found;
    },

    open: function (pane, inst, meta) {
      Alerts.indicatorDialog(pane, inst, meta, this.findAlert(pane, inst));
    },

    /* stop (or resume) every server alert belonging to an indicator
       instance - called when the instance is removed, or from the Alerts
       panel. Hiding the indicator (eye icon) never stops alerts: a hidden
       indicator keeps firing; its plots are just not drawn.*/
    setStopped: function (pane, inst, stop) {
      var uid = inst.id + "@" + pane.id, pending = 0, self = this;
      Alerts.list().forEach(function (a) {
        if (a.kind !== "indicator" || a.indicator_id !== uid) return;
        var want = stop ? "stopped_manual" : "active";
        if (a.status === want) return;
        pending++;
        Alerts._api("/api/alerts/" + a.id + (stop ? "/stop" : "/resume"),
                    { method: "POST" })
          .then(function () { if (--pending === 0) Alerts.refresh(); },
                function () { if (--pending === 0) Alerts.refresh(); });
      });
    }
  };

  /* ------------------------------- inline SVG icon system
     24px grid, 2px stroke, round caps - one visual language everywhere.
     currentColor recolors with themes; no icon fonts, no emoji.*/
  var ICONS = (function () {
    function ic(paths) {
      return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" ' +
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
        'stroke-linejoin="round" aria-hidden="true">' + paths + "</svg>";
    }
    return {
      "theme-warm": ic('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
      "theme-oled": ic('<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>'),
      "theme-light": ic('<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/>'),
      "save": ic('<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/>'),
      "watchlist": ic('<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1" fill="currentColor"/><circle cx="3.5" cy="12" r="1" fill="currentColor"/><circle cx="3.5" cy="18" r="1" fill="currentColor"/>'),
      "data": ic('<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5"/><path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>'),
      "bell": ic('<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>'),
      "send": ic('<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/>'),
      "zap": ic('<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>'),
      "gear": ic('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.9 2.9l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.2a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.9-2.9l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.2a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.9-2.9l.1.1a1.7 1.7 0 0 0 1.9.3h0a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.2a1.7 1.7 0 0 0 1 1.5h0a1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.9 2.9l-.1.1a1.7 1.7 0 0 0-.3 1.9v0a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.2a1.7 1.7 0 0 0-1.4 1z"/>'),
      "grid": ic('<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>'),
      "chev-d": ic('<path d="M6 9l6 6 6-6"/>'),
      "cross": ic('<path d="M12 5v14M5 12h14"/>'),
      "trend": ic('<path d="M3 20 10 13l4 4 7-9"/><path d="M15 8h6v6"/>'),
      "ray": ic('<path d="M4 20 20 4"/><circle cx="4" cy="20" r="1.6" fill="currentColor" stroke="none"/>'),
      "hline": ic('<path d="M3 12h18"/><path d="M3 12l2.5-2.5M3 12l2.5 2.5M21 12l-2.5-2.5M21 12l-2.5 2.5"/>'),
      "hray": ic('<path d="M3 12h16"/><path d="M15 6l6 6-6 6"/><circle cx="3" cy="12" r="1.6" fill="currentColor" stroke="none"/>'),
      "vline": ic('<path d="M12 3v18"/><path d="M12 3 9.5 5.5M12 3l2.5 2.5M12 21l-2.5-2.5M12 21l2.5-2.5"/>'),
      "rect": ic('<rect x="4" y="6" width="16" height="12" rx="1"/>'),
      "ellipse": ic('<ellipse cx="12" cy="12" rx="8" ry="6"/>'),
      "fib": ic('<path d="M4 19 9 14l3 3 4-6 4-4"/><path d="M4 15h16M4 11h16M4 7h16" stroke-dasharray="2 2"/>'),
      "text": ic('<path d="M5 6V4h14v2M12 4v16M9 20h6"/>'),
      "eraser": ic('<path d="M20 20H8L3.3 15.3a1 1 0 0 1 0-1.4L12 5.2a2 2 0 0 1 2.8 0l4.7 4.7a2 2 0 0 1 0 2.8L13 19"/>'),
      "sync": ic('<path d="M20 11a8 8 0 0 0-14.9-3"/><path d="M4 13a8 8 0 0 0 14.9 3"/><path d="M5 4v5h5M19 20v-5h-5"/>'),
      "replay": ic('<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 3v4.2h-4.2"/><path d="M10 9.2v5.6l4.8-2.8z" fill="currentColor" stroke="none"/>'),
      "heal": ic('<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>'),
      "news": ic('<path d="M4 6h12a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M7 10h7M7 13.5h7M7 17h4"/>')
    };
  })();

  /* ================================================================ replay
     bar-replay (chart-style). Topbar "Replay" arms picking -
     the next click on any chart sets the replay cursor. The floating control
     bar then steps the cursor bar-by-bar or auto-plays; every pane slices
     its own series at the same cursor time and re-runs its indicators
     server-side with `upto`, so signals can never peek at future bars.
     Live polling pauses while replay owns the series (the quote strip stays
     live); the alert engine is untouched, so replay never fires Telegram
     alerts. Exiting restores the full live chart.*/
  var Replay = {
    active: false, picking: false,
    cursorT: 0, anchorId: null, full: {},
    playing: false, speed: 5, timer: null,
    _lastIndAt: 0, _rgen: 0,

    /* entry -------------------------------------------------------- */
    arm: function () {
      if (this.active) { this.exit(); return; }
      if (this.picking) { this.cancelPick(); return; }
      var panes = (typeof Layout !== "undefined" && Layout.panes) || [];
      var ok = panes.some(function (p) {
        return p && p.seriesData && p.seriesData.length > 1;
      });
      if (!ok) { toast("No chart data loaded yet - wait for the candles first."); return; }
      this.picking = true;
      document.body.classList.add("replay-picking");
      var b = document.getElementById("btn-replay");
      if (b) b.classList.add("on");
      toast("Replay: click any bar on a chart to start from there. Esc cancels.");
    },
    cancelPick: function () {
      if (!this.picking) return;
      this.picking = false;
      document.body.classList.remove("replay-picking");
      var b = document.getElementById("btn-replay");
      if (b && !this.active) b.classList.remove("on");
    },

    /* picking (called from the pane mousedown hook) ----------------- */
    pick: function (pane, x, y) {
      var d = null;
      try { d = pane.toData(x, y); } catch (e) {}
      var full = (pane.seriesData || []).filter(function (b) { return !b.forming; });
      this.cancelPick();
      if (!d || !full.length) return;
      var t = d.t, best = full[0].time, i;
      if (pane.state.mode === "renko") {
        /* toData stores real formation times while seriesData is in
           slot space - snap on rtime so the cursor stays a real time (the
           server's `upto` filter and the replay clock both need real time).*/
        best = pane._brickRt(full[0]);
        for (i = 0; i < full.length; i++) {
          var rt = pane._brickRt(full[i]);
          if (rt <= t) best = rt; else break;
        }
      } else {
        for (i = 0; i < full.length; i++) {
          if (full[i].time <= t) best = full[i].time; else break;
        }
      }
      this.activate(pane, best);
    },

    activate: function (pane, t) {
      var self = this;
      this.pause();
      this.active = true;
      this.anchorId = pane.id;
      this.cursorT = t;
      this.full = {};
      Layout.panes.forEach(function (p) {
        if (!p || !p.seriesData) return;
        self.full[p.id] = p.seriesData.filter(function (b) { return !b.forming; });
        p.stopModeTimers();   // pause live resync / renko refresh; quote timer stays
      });
      if (!this.full[this.anchorId] || !this.full[this.anchorId].length) {
        this.exit(true);
        toast("Replay: no usable bars on that chart.");
        return;
      }
      document.body.classList.add("replaying");
      var b = document.getElementById("btn-replay");
      if (b) b.classList.add("on");
      this.showBar();
      this.render(true, true);
      toast("Replay on - step or press play. Indicators re-run at every step; nothing here sends Telegram alerts.");
    },

    /* transport ------------------------------------------------------ */
    _anchorPane: function () {
      var ap = null;
      Layout.panes.forEach(function (p) { if (p && p.id === this.anchorId) ap = p; }, this);
      return ap;
    },
    _times: function () {
      var f = this.full[this.anchorId] || [], out = [], i;
      /* a Renko anchor pane stores seriesData in slot space -
         stepping needs real times to compare against the cursor.*/
      var ap = this._anchorPane();
      var renko = !!(ap && ap.state.mode === "renko" && ap._brickRt);
      for (i = 0; i < f.length; i++) out.push(renko ? ap._brickRt(f[i]) : f[i].time);
      return out;
    },
    _idxAtCursor: function (times) {
      var idx = 0, i;
      for (i = 0; i < times.length; i++) {
        if (times[i] <= this.cursorT) idx = i; else break;
      }
      return idx;
    },
    step: function (dir) {
      if (!this.active) return;
      var times = this._times(), n = times.length;
      if (!n) return;
      var idx = this._idxAtCursor(times) + dir;
      if (idx < 0) idx = 0;
      if (idx >= n) {
        if (this.playing) { this.pause(); toast("Replay reached the newest bar."); }
        return;
      }
      this.cursorT = times[idx];
      this.render(true, false);
    },
    jumpStart: function () {
      if (!this.active) return;
      var times = this._times();
      if (times.length) { this.cursorT = times[0]; this.render(true, true); }
    },
    play: function () {
      if (this.playing || !this.active) return;
      this.playing = true;
      this._tickBtn();
      var self = this;
      this.timer = setInterval(function () { self.step(1); },
                               Math.max(50, Math.round(1000 / self.speed)));
    },
    pause: function () {
      if (this.timer) { clearInterval(this.timer); this.timer = null; }
      var was = this.playing;
      this.playing = false;
      this._tickBtn();
      if (was && this.active) this.render(true, false);  // settle indicators
    },
    toggle: function () { if (this.playing) this.pause(); else this.play(); },
    setSpeed: function (s) {
      s = parseInt(s, 10);
      if (!(s > 0)) return;
      this.speed = s;
      if (this.playing) { this.pause(); this.play(); }
    },

    exit: function (silent) {
      if (!this.active && !this.picking) return;
      if (this.timer) { clearInterval(this.timer); this.timer = null; }
      this.playing = false;
      this.cancelPick();
      this.active = false;
      this.full = {};
      this.anchorId = null;
      document.body.classList.remove("replaying");
      var b = document.getElementById("btn-replay");
      if (b) b.classList.remove("on");
      this.hideBar();
      /* Restore the full live series, then let the normal resync path pick
         up anything the market did while we were replaying.*/
      Layout.panes.forEach(function (p) {
        if (!p) return;
        p.startModeTimers();
        try { p.load(false); } catch (e) {}
      });
      if (!silent) toast("Replay off - back to live.");
    },

    /* rendering -------------------------------------------------------- */
    render: function (withInd, fitView) {
      var self = this;
      if (!this.active) return;
      this._rgen++;
      var gen = this._rgen;
      var now = Date.now();
      /* During autoplay the candles move every tick (cheap, local) while the
         heavier server-side indicator recompute is throttled; a manual step
         or pause always settles indicators immediately.*/
      var doInd = withInd && (now - this._lastIndAt > 700 || !this.playing);
      Layout.panes.forEach(function (p) {
        if (!p) return;
        if (p.state.mode === "renko") self._renderRenko(p, gen, fitView);
        else self._renderCandles(p, fitView);
      });
      if (doInd) {
        this._lastIndAt = now;
        Layout.panes.forEach(function (p) { if (p) p.loadIndicator(true); });
      }
      this._clock();
    },

    _renderCandles: function (p, fitView) {
      var full = this.full[p.id] || [], vis = [], i;
      for (i = 0; i < full.length; i++) {
        if (full[i].time <= this.cursorT) vis.push(full[i]); else break;
      }
      try { p.series.setData(vis); } catch (e) { return; }
      try { if (p.setVolumeData) p.setVolumeData(vis); } catch (e) {}
      /* point the "live" bar at the cursor so open-ended level
         lines anchor at the replay edge (and tick-update paths, which are
         paused anyway, can't resurrect a stale live bar). load(false) on
         exit restores it.*/
      if (vis.length) p.cur = Object.assign({}, vis[vis.length - 1]);
      p.applyMarkers();
      p.draw();
      /* Autoplay keeps the cursor in view (last ~170 bars); a manual step
         leaves the viewport alone so you can inspect.*/
      if ((this.playing || fitView) && vis.length > 1) {
        try {
          p.chart.timeScale().setVisibleLogicalRange({
            from: Math.max(0, vis.length - 170),
            to: vis.length - 1 + 8
          });
        } catch (e) {}
      }
    },

    _renderRenko: function (p, gen, fitView) {
      var self = this;
      var url = "/api/renko?symbol=" + encodeURIComponent(p.state.symbol) +
        "&box=" + p.state.box + "&bricks=" + DEEP_BRICKS +
        "&tf=" + encodeURIComponent(p.state.tf) +
        "&upto=" + this.cursorT;
      p.api(url).then(function (res) {
        /* Stale fetch: the cursor moved again while this was in flight. */
        if (!self.active || gen !== self._rgen) return;
        if (!res || !res.bricks) return;
        try { p.series.setData(res.bricks); } catch (e) { return; }
        /* keep the brick list for slot<->real time mapping
           (axis labels, crosshair) during replay.*/
        try { p.bricks = res.bricks; } catch (e) {}
        /* applyMarkers() reads seriesData (not p.bricks) for the
           slot/real-time snap - keep it in sync like the live path does.*/
        try { p.seriesData = res.bricks; } catch (e) {}
        try { if (p.volSeries) p.volSeries.applyOptions({ visible: false }); } catch (e) {}
        /* same cursor-anchoring as the candle path above. */
        if (res.bricks.length)
          p.cur = Object.assign({}, res.bricks[res.bricks.length - 1]);
        p.applyMarkers();
        p.draw();
        if ((self.playing || fitView) && res.bricks.length > 1) {
          try {
            p.chart.timeScale().setVisibleLogicalRange({
              from: Math.max(0, res.bricks.length - 170),
              to: res.bricks.length - 1 + 8
            });
          } catch (e) {}
        }
      }).catch(function () { /* keep old bricks on transient error*/ });
    },

    /* control bar -------------------------------------------------------- */
    showBar: function () {
      var bar = document.getElementById("replaybar");
      if (!bar) return;
      bar.style.display = "flex";
      this._tickBtn();
      var sel = document.getElementById("rp-speed");
      if (sel) sel.value = String(this.speed);
      this._clock();
    },
    hideBar: function () {
      var bar = document.getElementById("replaybar");
      if (bar) bar.style.display = "none";
    },
    _tickBtn: function () {
      var b = document.getElementById("rp-play");
      if (!b) return;
      b.innerHTML = this.playing ? Replay._svgPause : Replay._svgPlay;
      b.title = this.playing ? "Pause (auto-play)" : "Play (auto-play)";
      b.classList.toggle("on", this.playing);
    },
    _clock: function () {
      var el = document.getElementById("rp-clock");
      if (!el || !this.active) return;
      var txt;
      try {
        txt = new Intl.DateTimeFormat("en-GB", {
          timeZone: "Asia/Calcutta", weekday: "short", day: "2-digit",
          month: "short", hour: "2-digit", minute: "2-digit", hour12: false
        }).format(new Date(this.cursorT * 1000)) + " IST";
      } catch (e) {
        txt = new Date(this.cursorT * 1000).toISOString();
      }
      el.textContent = txt;
    },

    init: function () {
      var self = this;
      var b = document.getElementById("btn-replay");
      if (b) b.addEventListener("click", function () { self.arm(); });
      var bar = document.getElementById("replaybar");
      if (bar) {
        bar.addEventListener("click", function (e) {
          var t = e.target.closest("[data-rp]");
          if (!t) return;
          var act = t.getAttribute("data-rp");
          if (act === "start") self.jumpStart();
          else if (act === "back") { self.pause(); self.step(-1); }
          else if (act === "play") self.toggle();
          else if (act === "fwd") { self.pause(); self.step(1); }
          else if (act === "live") self.exit();
        });
        var sel = document.getElementById("rp-speed");
        if (sel) sel.addEventListener("change", function () { self.setSpeed(sel.value); });
      }
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && self.picking) self.cancelPick();
      });
    }
  };
  Replay._svgPlay = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M8 5v14l11-7z" fill="currentColor"/></svg>';
  Replay._svgPause = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M7 5h4v14H7zM13 5h4v14h-4z" fill="currentColor"/></svg>';

  /* ------------------------------- UI theme
     data-theme on <html> drives all CSS tokens. Chart canvas colors come
     from ChartSet.defaults() which reads the same tokens, so an
     uncustomized chart always follows the UI theme; explicit user
     color choices in ChartSet keep winning over the theme.*/
  var Theme = {
    key: "stocksophist-theme",
    order: ["warm", "oled", "light"],
    names: { warm: "Warm dark", oled: "OLED black", light: "Light" },
    get: function () {
      try { return localStorage.getItem(this.key) || "warm"; } catch (e) { return "warm"; }
    },
    /* CSS var reads are cached: getComputedStyle forces style recalc, so
       reading ~15 vars on every overlay draw would jank zoom/pan gestures.
       Cache is rebuilt on every theme switch.*/
    _vars: null,
    _varNames: ["--chart-bg", "--chart-text", "--chart-grid", "--candle-up",
      "--candle-down", "--chart-cross", "--text-3", "--accent", "--warn"],
    css: function (name) {
      if (!this._vars) this.readVars();
      return (this._vars && this._vars[name]) || "";
    },
    readVars: function () {
      var v = {}, self = this;
      try {
        var cs = getComputedStyle(document.documentElement);
        this._varNames.forEach(function (n) { v[n] = (cs.getPropertyValue(n) || "").trim(); });
      } catch (e) {}
      this._vars = v;
    },
    set: function (t, silent) {
      if (this.order.indexOf(t) < 0) t = "warm";
      document.documentElement.setAttribute("data-theme", t);
      try { localStorage.setItem(this.key, t); } catch (e) {}
      this.readVars();
      if (typeof ChartSet !== "undefined") ChartSet.invalidate();
      this.paint();
      if (!silent && typeof Layout !== "undefined" && Layout.panes)
        ChartSet.applyAll();
    },
    cycle: function () {
      var i = this.order.indexOf(this.get());
      this.set(this.order[(i + 1) % this.order.length]);
    },
    paint: function () {
      var b = document.getElementById("btn-theme"), t = this.get();
      if (!b) return;
      b.innerHTML = ICONS["theme-" + t] || "";
      b.title = "Theme: " + (this.names[t] || t) + " - click to switch";
    },
    init: function () {
      this.set(this.get(), true);
      /* panes are built before init runs; if the saved theme is not the
         default, push its colors into the already-created charts now*/
      try {
        if (typeof Layout !== "undefined" && Layout.panes && Layout.panes.length)
          ChartSet.applyAll();
      } catch (e) {}
      var b = document.getElementById("btn-theme"), self = this;
      if (b) b.addEventListener("click", function () { self.cycle(); });
    }
  };

  /* ------------------------------- chart/canvas settings (all functional) */
  var ChartSet = {
    key: LS_CHARTSET,
    defaults: function () {
      var v = function (n, fb) { var x = Theme.css(n); return x || fb; };
      return {
        // canvas - follows the UI theme unless the user customized them
        bg: v("--chart-bg", "#141216"), text: v("--chart-text", "#8a857e"), fontSize: 11,
        grid: true, gridColor: v("--chart-grid", "rgba(236,236,236,.055)"),
        up: v("--candle-up", "#2ebd85"), down: v("--candle-down", "#f6465d"),
        wickUp: v("--candle-up", "#2ebd85"), wickDown: v("--candle-down", "#f6465d"),
        borderVisible: false,
        crossMode: "normal", crossColor: v("--chart-cross", "#6f9bff"),
        // scales and lines
        scaleSide: "right",            // right | left
        scaleMode: "normal",           // normal | log | percentage
        lastValue: true,               // last value line on price scale
        priceLine: true, priceLineColor: v("--text-3", "#7d776f"),
        bidAsk: true,                  // live bid/ask dashed lines
        prevClose: false, prevCloseColor: v("--text-3", "#7d776f"),
        dayHL: false, dayHLColor: v("--accent", "#6f9bff"),
        countdown: true,               // countdown to bar close on price line
        plusButton: true,              // "+" on price-axis hover
        showVolume: true,              // MT5 tick-volume histogram
        // status line
        legend: true,
        // time scale
        dateFormat: "dd MMM ''yy", time12h: false,
        timezone: "Asia/Kolkata", // chart time zone; axis labels render in this zone
        // alerts
        alertColor: Theme.css("--warn") || "#f0b429", alertSound: true, alertTune: "Soothing Chime"
      };
    },
    cfg: function () {
      try { return Object.assign(this.defaults(), JSON.parse(localStorage.getItem(this.key) || "{}")); }
      catch (e) { return this.defaults(); }
    },
    save: function (c) { try { localStorage.setItem(this.key, JSON.stringify(c)); } catch (e) {} this.invalidate(); },
    /* cfg() hits localStorage + JSON.parse on every call - fine for dialogs,
       deadly in per-frame overlay draws. cached() memoizes until save(),
       theme switch, or explicit invalidate().*/
    _ccache: null,
    cached: function () { if (!this._ccache) this._ccache = this.cfg(); return this._ccache; },
    invalidate: function () { this._ccache = null; },
    scaleModeNum: function () {
      var m = this.cfg().scaleMode;
      return m === "log" ? 1 : m === "percentage" ? 2 : 0;
    },
    apply: function (pane) {
      var c = this.cfg();
      pane.chart.applyOptions({
        layout: { background: { color: c.bg }, textColor: c.text, fontSize: c.fontSize,
                  fontFamily: "system-ui, 'Segoe UI', -apple-system, Roboto, sans-serif" },
        grid: { vertLines: { visible: !!c.grid, color: c.gridColor },
                horzLines: { visible: !!c.grid, color: c.gridColor } },
        crosshair: {
          mode: c.crossMode === "hidden" ? 2 : c.crossMode === "magnet" ? 1 : 0,
          vertLine: { color: c.crossColor, labelBackgroundColor: c.crossColor },
          horzLine: { color: c.crossColor, labelBackgroundColor: c.crossColor }
        },
        rightPriceScale: { visible: c.scaleSide === "right", borderColor: c.gridColor },
        leftPriceScale: { visible: c.scaleSide === "left", borderColor: c.gridColor },
        localization: {
          dateFormat: c.dateFormat,
          /* on a Renko pane the crosshair/axis show the brick's
             real formation time (rtime), not its slot time.*/
          timeFormatter: (function (orig) {
            return function (t) {
              if (pane.state.mode === "renko") {
                var rt = pane.renkoReal(t);
                if (rt !== t) return orig(rt);
              }
              return orig(t);
            };
          })(makeTimeFormatter(resolveZone(c.timezone), c.time12h))
        },
        timeScale: {
          tickMarkFormatter: (function (orig) {
            return function (time, tickMarkType, locale) {
              if (pane.state.mode === "renko" && typeof time === "number") {
                var rt = pane.renkoReal(time);
                if (rt !== time) return orig(rt, tickMarkType, locale);
              }
              return orig(time, tickMarkType, locale);
            };
          })(makeTickMarkFormatter(resolveZone(c.timezone)))
        }
      });
      var scaleId = c.scaleSide === "left" ? "left" : "right";
      pane.series.applyOptions({
        priceScaleId: scaleId,
        lastValueVisible: !!c.lastValue,
        priceLineVisible: false, // we draw our own price line (with countdown)
        upColor: c.up, downColor: c.down,
        wickUpColor: c.wickUp, wickDownColor: c.wickDown,
        borderVisible: !!c.borderVisible, borderUpColor: c.up, borderDownColor: c.down
      });
      try { pane.chart.priceScale(scaleId).applyOptions({ mode: this.scaleModeNum() }); } catch (e) {}
      pane.legendEl.style.display = c.legend ? "block" : "none";
      /* volume histogram visibility - candles only, Renko untouched. */
      try {
        var showV = !!c.showVolume && pane.state.mode !== "renko" && !!pane.volSeries;
        pane.volSeries.applyOptions({ visible: showV });
        if (showV && pane.seriesData) pane.setVolumeData(pane.seriesData);
      } catch (e) {}
      pane.refreshRefLines();
    },
    applyAll: function () { Layout.panes.forEach(function (p) { ChartSet.apply(p); p.draw(); }); }
  };
  /* Time-axis labels in the user-selected timezone (default IST).
     LWC hands us a unix-seconds timestamp (intraday) or a date string.*/
  function makeTimeFormatter(tz, use12h) {
    var zone = tz || "Asia/Kolkata", dtf = null;
    try {
      dtf = new Intl.DateTimeFormat("en-GB", {
        timeZone: zone, hour: "2-digit", minute: "2-digit", hour12: !!use12h
      });
    } catch (e) { dtf = null; }
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return function (t) {
      var ms = typeof t === "number" ? t * 1000 : Date.parse(t);
      if (!isFinite(ms)) return "";
      if (dtf) { try { return dtf.format(new Date(ms)); } catch (e) {} }
      var d = new Date(ms); // last-resort fallback: UTC
      if (use12h) {
        var h = d.getUTCHours(), ap = h >= 12 ? "PM" : "AM"; h = h % 12; if (!h) h = 12;
        return h + ":" + p(d.getUTCMinutes()) + " " + ap;
      }
      return p(d.getUTCHours()) + ":" + p(d.getUTCMinutes());
    };
  }
  /* -- Selected timezone everywhere (default IST) ----
     Every timestamp the app shows renders in the user-selected timezone
     (Chart settings -> Time scale -> Timezone), defaulting to
     Asia/Kolkata: chart axis, crosshair, alert log, "Hist"
     date, alert-dialog expiry, drawing coordinates. Changing the setting
     re-renders everything (ChartSet.applyAll + panel refreshes).*/
  var IST_ZONE = "Asia/Kolkata";
  /* Some browser/ICU builds (older Chromium) report only the legacy alias
     "Asia/Calcutta" and throw on "Asia/Kolkata". Resolve the stored zone to
     one this browser's Intl actually supports so times never silently fall
     back to UTC. The stored value stays canonical ("Asia/Kolkata").*/
  var ZONE_ALIASES = {
    "Asia/Kolkata": ["Asia/Calcutta"],
    "Asia/Calcutta": ["Asia/Kolkata"]
  };
  function _zoneSupported(z) {
    try { new Intl.DateTimeFormat("en", { timeZone: z }); return true; }
    catch (e) { return false; }
  }
  function resolveZone(z) {
    z = z || IST_ZONE;
    if (_zoneSupported(z)) return z;
    var alts = ZONE_ALIASES[z] || [], i;
    for (i = 0; i < alts.length; i++) if (_zoneSupported(alts[i])) return alts[i];
    return z;
  }
  function selZone() {
    try { var z = ChartSet.cfg().timezone; if (z) return resolveZone(z); } catch (e) {}
    return resolveZone(IST_ZONE);
  }
  function selZoneShort() {
    /* "IST", "GMT+4", ... - the little suffix on log timestamps */
    try {
      var ps = new Intl.DateTimeFormat("en",
        { timeZone: selZone(), timeZoneName: "short" }).formatToParts(new Date());
      for (var i = 0; i < ps.length; i++)
        if (ps[i].type === "timeZoneName") return ps[i].value;
    } catch (e) {}
    return "";
  }
  var _tzFmtCache = {};
  function _tzFmt(zone, opts) {
    var k = zone + "|" + JSON.stringify(opts);
    if (!_tzFmtCache[k]) {
      try {
        _tzFmtCache[k] = new Intl.DateTimeFormat("en-GB",
          Object.assign({ timeZone: zone }, opts));
      } catch (e) { _tzFmtCache[k] = null; }
    }
    return _tzFmtCache[k];
  }
  function _p2(n) { return (n < 10 ? "0" : "") + n; }
  /* Last-resort fallback shift - exact only for IST (no DST, ever). For
     other zones the Intl path above is authoritative.*/
  function _tzFallbackShift(zone) { return zone === IST_ZONE ? 19800000 : 0; }
  /* ms = unix milliseconds. */
  function fmtTZ_HMS(ms, zone) {
    zone = zone || selZone();
    var f = _tzFmt(zone, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
    if (f) { try { return f.format(new Date(ms)); } catch (e) {} }
    var d = new Date(ms + _tzFallbackShift(zone));
    return _p2(d.getUTCHours()) + ":" + _p2(d.getUTCMinutes()) + ":" + _p2(d.getUTCSeconds());
  }
  function fmtTZ_HM(ms, zone) {
    zone = zone || selZone();
    var f = _tzFmt(zone, { hour: "2-digit", minute: "2-digit", hour12: false });
    if (f) { try { return f.format(new Date(ms)); } catch (e) {} }
    var d = new Date(ms + _tzFallbackShift(zone));
    return _p2(d.getUTCHours()) + ":" + _p2(d.getUTCMinutes());
  }
  function fmtTZ_YMD(ms, zone) {
    zone = zone || selZone();
    var f = _tzFmt(zone, { year: "numeric", month: "2-digit", day: "2-digit" });
    if (f) {
      try {
        var p = f.formatToParts(new Date(ms)), y = "", m = "", dd = "";
        p.forEach(function (x) {
          if (x.type === "year") y = x.value;
          else if (x.type === "month") m = x.value;
          else if (x.type === "day") dd = x.value;
        });
        if (y && m && dd) return y + "-" + m + "-" + dd;
      } catch (e) {}
    }
    return new Date(ms + _tzFallbackShift(zone)).toISOString().slice(0, 10);
  }
  function fmtTZ_YMDHM(ms, zone) {
    return fmtTZ_YMD(ms, zone) + " " + fmtTZ_HM(ms, zone);
  }
  /* Time-axis tick marks in the chart timezone (default IST). LWC calls this
     for every axis label; tickMarkType: 0=Year 1=Month 2=DayOfMonth 3=Time
     4=TimeWithSeconds. Without this, axis ticks render in the browser's
     local zone while the crosshair label renders in IST.*/
  function makeTickMarkFormatter(tz) {
    var zone = tz || IST_ZONE, fT = null, fTS = null, fD = null, fM = null, fY = null, fTD = null;
    try {
      var base = { timeZone: zone };
      fT = new Intl.DateTimeFormat("en-GB", Object.assign({ hour: "2-digit", minute: "2-digit", hour12: false }, base));
      fTS = new Intl.DateTimeFormat("en-GB", Object.assign({ hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }, base));
      fD = new Intl.DateTimeFormat("en-GB", Object.assign({ day: "numeric", month: "short" }, base));
      // time + date for intraday crosshair ("13:20, 25 Sept")
      fTD = new Intl.DateTimeFormat("en-GB", Object.assign({ hour: "2-digit", minute: "2-digit", hour12: false, day: "numeric", month: "short" }, base));
      fM = new Intl.DateTimeFormat("en-GB", Object.assign({ month: "short", year: "2-digit" }, base));
      fY = new Intl.DateTimeFormat("en-GB", Object.assign({ year: "numeric" }, base));
    } catch (e) { fT = fTS = fD = fM = fY = fTD = null; }
    var OFF = (zone === IST_ZONE || zone === "Asia/Calcutta") ? 19800000 : 0; // fallback shift for IST
    return function (time, tickMarkType, locale) {
      var ms, isNum = typeof time === "number";
      if (isNum) ms = time * 1000;
      else {
        var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(time));
        if (m) {
          var mon = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"][+m[2]];
          if (tickMarkType === 1) return mon + " '" + m[1].slice(2);
          if (tickMarkType === 0) return m[1];
          return (+m[3]) + " " + mon;
        }
        ms = Date.parse(time);
      }
      if (!isFinite(ms)) return "";
      var d = new Date(ms);
      function fmt(f, fb) {
        if (f) { try { return f.format(d); } catch (e) {} }
        var s = new Date(ms + OFF);
        return fb(s);
      }
      if (tickMarkType === 4) return fmt(fTS, function (s) {
        return _p2(s.getUTCHours()) + ":" + _p2(s.getUTCMinutes()) + ":" + _p2(s.getUTCSeconds()); });
      /* intraday ticks show TIME ONLY in AM/PM ("09:25 pm").
         The date appears only on the tick where the day changes
         (midnight IST) - e.g. "26 Sept".*/
      if (tickMarkType === 3) {
        var s0 = new Date(ms + OFF);
        var hh = s0.getUTCHours(), mm = s0.getUTCMinutes();
        if (hh === 0 && mm === 0) {
          return fmt(fD, function (s) {
            return s.getUTCDate() + " " + ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"][s.getUTCMonth()]; });
        }
        var ap = hh >= 12 ? "pm" : "am", h12 = hh % 12 || 12;
        return h12 + ":" + _p2(mm) + " " + ap;
      }
      if (tickMarkType === 2) return fmt(fD, function (s) {
        return s.getUTCDate() + " " + ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"][s.getUTCMonth()]; });
      if (tickMarkType === 1) return fmt(fM, function (s) {
        return ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
          "Oct", "Nov", "Dec"][s.getUTCMonth()] + " '" + String(s.getUTCFullYear()).slice(2); });
      return fmt(fY, function (s) { return String(s.getUTCFullYear()); });
    };
  }
  function allTimezones() {
    var list = null;
    try {
      if (typeof Intl !== "undefined" && Intl.supportedValuesOf)
        list = Intl.supportedValuesOf("timeZone").slice();
    } catch (e) {}
    if (!list) list = ["UTC", "Asia/Kolkata", "Asia/Dubai", "Asia/Singapore", "Asia/Tokyo",
      "Australia/Sydney", "Europe/London", "Europe/Berlin", "Europe/Moscow",
      "America/New_York", "America/Chicago", "America/Los_Angeles"];
    /* Always offer the canonical default, even on ICU builds that only know
       the legacy alias (e.g. "Asia/Calcutta"): resolveZone() maps it at use.*/
    if (list.indexOf("Asia/Kolkata") < 0) list.unshift("Asia/Kolkata");
    return list;
  }

  /* ---------------------------------------------------- indicator library */
  var IND_LIB = null, IND_ERRORS = [];
  /* global indicator defaults from the server (settings.json ->
     indicator_settings). indDefaults() layers them over the factory
     defaults from each indicator's INPUTS, so a changed setting applies
     immediately AND survives restarts. null = not loaded yet.*/
  var IND_SETTINGS = null;
  function indSettingsLoaded(cb) {
    if (IND_SETTINGS !== null) return cb();
    fetch("/api/indicator_settings").then(function (r) { return r.json(); })
      .then(function (j) {
        IND_SETTINGS = (j.indicator_settings && typeof j.indicator_settings === "object")
          ? j.indicator_settings : {};
        cb();
      })
      .catch(function () { IND_SETTINGS = {}; cb(); });
  }
  /* force=true bypasses the cache - the backend hot-loads .py
     files from disk on every call, so pasting a new indicator into the
     folder shows up with no app restart.*/
  function indLibrary(cb, force) {
    function done() { cb(IND_LIB || []); }
    if (IND_LIB && !force) return indSettingsLoaded(done);
    fetch("/api/indicators").then(function (r) { return r.json(); })
      .then(function (j) {
        IND_LIB = j.indicators || []; IND_ERRORS = j.errors || [];
        indSettingsLoaded(done);
      })
      .catch(function () {
        if (!force) IND_LIB = [];
        indSettingsLoaded(done);
      });
  }
  /* persist one indicator's settings as the new global default
     (server-side settings.json). Fire-and-forget from the dialog: the
     instance settings are already saved in the pane state, so a failed
     POST only loses the global default - reported honestly, never silent.*/
  function saveIndDefault(indId, vals, onDone) {
    fetch("/api/indicator_settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ indicator: indId, settings: vals })
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (!j.ok && j.error) toast("Global default NOT saved: " + j.error);
      if (IND_SETTINGS) IND_SETTINGS[indId] = vals;
      if (onDone) onDone();
    }).catch(function () {
      toast("Global default NOT saved (server unreachable)");
      if (onDone) onDone();
    });
  }
  function indById(id) {
    for (var i = 0; i < (IND_LIB || []).length; i++)
      if (IND_LIB[i].id === id) return IND_LIB[i];
    return null;
  }
  function indDefaults(meta) {
    var d = {};
    (meta.inputs || []).forEach(function (i) { d[i.key] = i.default; });
    /* layer saved global defaults over the factory defaults.
       Only keys the indicator declares are overridden, so a stale
       override from an older version can never inject junk.*/
    var saved = (IND_SETTINGS || {})[meta.id] || {};
    for (var k in saved) if (k in d) d[k] = saved[k];
    return d;
  }
  function anyIndOn(list) {
    /* "On" means enabled and computing (a hidden indicator still calculates
       and fires its alerts - only its plots are hidden).*/
    return (list || []).some(function (i) { return i.enabled; });
  }
  function migrateIndicators(saved) {
    var si = saved && saved.indicators;
    if (Array.isArray(si)) return si;
    // legacy saved format migrates to EMA x4 (the standard
    // indicators the old defaults referenced were removed).
    if (si && si.enabled)
      return [{ id: "ema4", enabled: true, visible: true,
                settings: si.settings || {} }];
    // Fresh pane (no saved indicator list): EMA x4 on by default.
    return [{ id: "ema4", enabled: true, visible: true, settings: {} }];
  }
  function dashOf(style) { return style === "Dotted" ? [2, 2] : style === "Dashed" ? [6, 4] : []; }
  function labelPx(size) {
    return { Tiny: 10, Small: 11, Normal: 12, Large: 14, Huge: 16 }[size] || 11;
  }

  function toast(msg) {
    var t = $("toast");
    t.textContent = msg;
    t.style.opacity = 1;
    clearTimeout(t._h);
    t._h = setTimeout(function () { t.style.opacity = 0; }, 3500);
  }
  /* Persistent alert notifications (bottom-right stacked cards).
     Unlike toast(), these stay until the user closes them. Multiple alerts
     stack vertically. Includes a "Close all" button.*/
  var AlertNotify = {
    _count: 0,
    show: function (title, detail, alertId) {
      var stack = $("alert-stack");
      // Add/update the header with Close All button (only once)
      var head = stack.querySelector(".astack-head");
      if (!head) {
        head = document.createElement("div");
        head.className = "astack-head";
        head.innerHTML = '<span>(bell) Alerts <span id="astack-count"></span></span>' +
                        '<button id="astack-closeall">Close all</button>';
        stack.appendChild(head);
        head.querySelector("#astack-closeall").addEventListener("click", function () {
          AlertNotify.closeAll();
        });
      }
      this._count++;
      this._updateCount();
      var card = document.createElement("div");
      card.className = "alert-card";
      card.setAttribute("data-aid", "a" + Date.now() + "_" + this._count);
      var now = new Date();
      var timeStr = ("0" + now.getHours()).slice(-2) + ":" +
                    ("0" + now.getMinutes()).slice(-2) + ":" +
                    ("0" + now.getSeconds()).slice(-2);
      card.innerHTML =
        '<div class="ac-head">' +
          '<span class="ac-title">' + escHtml(title) + '</span>' +
          '<button class="ac-close" title="Dismiss">&times;</button>' +
        '</div>' +
        (detail ? '<div class="ac-detail">' + escHtml(detail) + '</div>' : '') +
        '<div class="ac-time">' + timeStr +
          (alertId ? ' <button class="ac-stop" title="Stop this alert so it never fires again">(stop) Stop alert</button>' : '') +
        '</div>';
      card.querySelector(".ac-close").addEventListener("click", function () {
        card.remove();
        AlertNotify._count--;
        AlertNotify._updateCount();
        AlertNotify._maybeRemoveHead();
      });
      // one-click kill for a runaway alert, right from its popup.
      var stopBtn = card.querySelector(".ac-stop");
      if (stopBtn && alertId) {
        stopBtn.addEventListener("click", function () {
          fetch("/api/alerts/" + encodeURIComponent(alertId) + "/stop", { method: "POST" })
            .then(function () { toast("Alert stopped"); })
            .catch(function () { toast("Could not stop alert"); });
          card.remove();
          AlertNotify._count--;
          AlertNotify._updateCount();
          AlertNotify._maybeRemoveHead();
          if (typeof Alerts !== "undefined") Alerts.refresh();
        });
      }
      // Insert after the header (newest on top)
      stack.insertBefore(card, head.nextSibling);
      // Cap at 20 cards to prevent DOM bloat; remove oldest
      var cards = stack.querySelectorAll(".alert-card");
      if (cards.length > 20) {
        cards[cards.length - 1].remove();
        this._count--;
        this._updateCount();
      }
    },
    _updateCount: function () {
      var el = $("astack-count");
      if (el) el.textContent = this._count > 0 ? "(" + this._count + ")" : "";
    },
    _maybeRemoveHead: function () {
      var stack = $("alert-stack");
      if (this._count <= 0) {
        var head = stack.querySelector(".astack-head");
        if (head) head.remove();
        this._count = 0;
      }
    },
    closeAll: function () {
      var stack = $("alert-stack");
      var cards = stack.querySelectorAll(".alert-card");
      for (var i = 0; i < cards.length; i++) cards[i].remove();
      this._count = 0;
      this._maybeRemoveHead();
    }
  };
  function escHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function uid() { return "d" + Math.random().toString(36).slice(2, 10); }

  /* script-style colors are #RRGGBBAA. The native palette picker only takes
     #RRGGBB, so color fields render as palette swatch + opacity slider.
     Chart settings may also hold CSS rgb()/rgba() strings - parse those too. */
  function cssToRgba8(v) {
    var s = String(v == null ? "" : v).trim();
    var m = s.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$/i);
    if (m) {
      function hx(n) {
        n = Math.max(0, Math.min(255, parseInt(n, 10) || 0));
        return ("0" + n.toString(16)).slice(-2);
      }
      var a = m[4] === undefined ? 1 : parseFloat(m[4]);
      return { rgb: "#" + hx(m[1]) + hx(m[2]) + hx(m[3]),
               alpha: Math.max(0, Math.min(100, Math.round(a * 100))) };
    }
    var hex = s.replace(/[^0-9a-fA-F]/g, "");
    if (hex.length === 3) hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
    var alpha = 100;
    if (hex.length >= 8) alpha = Math.round(parseInt(hex.slice(6, 8), 16) * 100 / 255);
    return { rgb: "#" + (hex + "000000").slice(0, 6).toUpperCase(), alpha: alpha };
  }
  function colorRgb6(v) { return cssToRgba8(v).rgb; }
  function colorAlphaPct(v) { return cssToRgba8(v).alpha; }
  function joinColor8(rgb6, alphaPct) {
    var a = Math.max(0, Math.min(100, parseFloat(alphaPct) || 0));
    var aa = ("0" + Math.round(a * 255 / 100).toString(16)).slice(-2).toUpperCase();
    return (colorRgb6(rgb6) + aa).toUpperCase();
  }
  /* Every opacity slider in the app (indicator settings, chart settings)
     live-updates its % label as it drags. */
  document.addEventListener("input", function (e) {
    var t = e.target;
    if (t && t.getAttribute && t.hasAttribute("data-calpha")) {
      var row = t.closest ? t.closest(".colorrow") : null;
      var pct = row ? row.querySelector(".colorpct") : null;
      if (pct) pct.textContent = t.value + "%";
    }
  });

  /* ==================================================== favourite timeframes
     chart-style: star any timeframe and it pins to the pane bar as a
     one-click button. Full list (with stars) lives behind the v menu.
     Stored per browser in localStorage. */
  var LS_TF_FAVS = "stocksophist_tf_favs";
  var TF_FAV_DEFAULTS = ["1m", "3m", "5m", "15m", "1H", "D"];
  var TfFavs = {
    list: function () {
      try {
        var l = JSON.parse(localStorage.getItem(LS_TF_FAVS) || "null");
        if (Array.isArray(l)) return l.filter(function (t) { return TF_LIST.indexOf(t) >= 0; });
      } catch (e) {}
      return TF_FAV_DEFAULTS.slice();
    },
    save: function (l) {
      try { localStorage.setItem(LS_TF_FAVS, JSON.stringify(l)); } catch (e) {}
      Layout.panes.forEach(function (p) { if (p.renderTfBar) p.renderTfBar(); });
    },
    isFav: function (tf) { return this.list().indexOf(tf) >= 0; },
    toggle: function (tf) {
      var l = this.list(), i = l.indexOf(tf);
      if (i >= 0) l.splice(i, 1); else l.push(tf);
      this.save(l);
    }
  };
  /* Clicking anywhere outside a TF menu closes it. */
  document.addEventListener("mousedown", function () {
    try {
      Layout.panes.forEach(function (p) { if (p.closeTfPop) p.closeTfPop(); });
    } catch (e) {}
  });

  /* ============================================================ ChartPane */
  function ChartPane(rootEl, id, saved, onChange) {
    this.id = id;
    this.onChange = onChange; // called whenever persistent state changes
    this.state = {
      symbol: (saved && saved.symbol) || "XAUUSD",
      tf: (saved && saved.tf) || "1m",
      mode: (saved && saved.mode) || "candles",
      box: (saved && saved.box) || 2.5,
      drawings: (saved && saved.drawings) || []
    };
    window.stocksophistTF = this.state.tf;  // initial chart TF for diagnostics
    this.cur = null;        // candle currently being tick-updated
    this.bricks = [];       // renko bricks
    this.anchor = 0;
    this.selectedId = null;
    this.pending = null;    // in-progress drawing {type, p1}
    this.drag = null;       // drag-move state
    this._drawRaf = null;   // pending rAF redraw (drag throttle)
    this.timers = { quote: null, renko: null, resync: null };
    this.modeBadge = "DEMO";
    this.indLines = [];      // indicator overlay lines (canvas)
    this.indBoxes = [];      // indicator overlay boxes (canvas)
    this.indAreas = [];      // indicator fills (Bollinger / cloud)
    this.indBarColors = {};  // indicator per-bar candle colors
    this.indSweeps = {};     // uid -> sweep list (for live sweep alerts)
    this.indMarkers = [];    // indicator BUY/SELL markers (via LWC markers plugin)
    this.indLabels = [];     // script-style signal labels (canvas boxes)
    this.indDashboards = []; // dashboard objects (stacked)
    this.firedSweeps = {};   // sweep-alert dedup per indicator load
    this.lastBid = null;
    this.lastQuote = null;   // full {bid, ask} for the trade panel
    this.tradePos = [];      // open MT5 positions on this pane's symbol
    this.tradeOrd = [];      // pending MT5 orders on this pane's symbol
    this.tradeDrag = null;   // active SL/TP/pending drag state
    this.dataEnd = 0;        // last bar/brick time (for future line extension)
    this.refLines = {};      // priceLine / bid / ask / prevClose / dayHi / dayLo
    this.state.indicators = migrateIndicators(saved);
    /* instant start: reopen the saved viewport on reload. */
    this._savedView = (saved && saved.view) || null;
    this._viewRestored = false;
    this._settingView = false;
    this.buildDom(rootEl);
    this.buildChart();
    /* remember the viewport (debounced) so a reload reopens
       exactly where the user left off. */
    (function (pane) {
      var vpTimer = null;
      try {
        pane.chart.timeScale().subscribeVisibleTimeRangeChange(function (r) {
          if (pane._settingView) return;
          if (!r || r.from == null || r.to == null) return;
          if (vpTimer) clearTimeout(vpTimer);
          vpTimer = setTimeout(function () {
            pane._savedView = { from: r.from, to: r.to };
            try { pane.persist(); } catch (e) {}
          }, 2000);
        });
      } catch (e) {}
    })(this);
    this.wireUi();
    this.load(true);
    this.startTimers();
  }

  ChartPane.prototype.buildDom = function (rootEl) {
    var self = this;
    rootEl.className = "pane";
    rootEl.dataset.pane = this.id;
    var tfOpts = TF_LIST.map(function (t) {
      return '<option value="' + t + '"' + (t === self.state.tf ? " selected" : "") + ">" + t + "</option>";
    }).join("");
    rootEl.innerHTML =
      '<div class="pane-bar">' +
        '<input class="sym" value="' + self.state.symbol + '" title="Symbol (Enter to load)">' +
        '<span class="tffavs" title="Favourite timeframes - star more via the v menu"></span>' +
        '<button class="tfmenu" title="All timeframes">v</button>' +
        '<button class="fxbtn' + (anyIndOn(self.state.indicators) ? " on" : "") + '" title="Indicators">fx</button>' +
        '<div class="seg mode">' +
          '<button data-m="candles" class="' + (self.state.mode === "candles" ? "on" : "") + '">Candles</button>' +
          '<button data-m="renko" class="' + (self.state.mode === "renko" ? "on" : "") + '">Renko</button>' +
        "</div>" +
        '<span class="boxwrap" style="display:' + (self.state.mode === "renko" ? "flex" : "none") + '">' +
          'Box <input class="box" value="' + self.state.box + '"><button class="boxapply">Apply</button>' +
        "</span>" +
        '<span class="quote"><b class="bid">--</b> / <span class="ask">--</span></span>' +
        '<span class="stale" style="display:none" title="Price feed is stale - showing last known data">STALE</span>' +
        '<span class="barsstale" style="display:none" title="Quotes are live but MT5 stopped sending bar updates - candles are frozen. Check the MT5 terminal connection.">BARS STALE</span>' +
        '<span class="hist" style="display:none" title="Earliest loaded bar"></span>' +
        '<span class="loadstat" style="display:none;color:#8b98ab;font-size:11px" title="What the pane is doing right now"></span>' +
        '<span class="badge demo">DEMO</span>' +
      "</div>" +
      '<div class="chartwrap"><div class="chart"></div><canvas class="draw"></canvas>' +
      '<div class="legend"></div>' +
      '<div class="indlegend"></div>' +
      '<button class="axisplus" title="Add alert at this price">+</button>' +
      '<button class="gtd-floatbtn" title="Go to date - jump the chart to any day"></button>' +
      '<div class="inddash"></div>' +
      '<div class="fmtbar" style="display:none"></div>' +
      '<div class="objbar" style="display:none"></div></div>';
    this.el = rootEl;
    this.chartEl = rootEl.querySelector(".chart");
    this.wrapEl = rootEl.querySelector(".chartwrap");
    this.canvas = rootEl.querySelector("canvas.draw");
    this.ctx = this.canvas.getContext("2d");
    this.legendEl = rootEl.querySelector(".legend");
    this.axisPlus = rootEl.querySelector(".axisplus");
    this.dashEl = rootEl.querySelector(".inddash");
    this.indLegendEl = rootEl.querySelector(".indlegend");
    this.symInput = rootEl.querySelector(".sym");
    this.tfBar = rootEl.querySelector(".tffavs");
    this.tfMenuBtn = rootEl.querySelector(".tfmenu");
    this.tfPop = null; // created on demand
    this.boxInput = rootEl.querySelector(".box");
    this.bidEl = rootEl.querySelector(".bid");
    this.askEl = rootEl.querySelector(".ask");
    this.staleEl = rootEl.querySelector(".stale");
    this.barsStaleEl = rootEl.querySelector(".barsstale");
    this.histEl = rootEl.querySelector(".hist");
    this.badgeEl = rootEl.querySelector(".badge");
    this.loadStatEl = rootEl.querySelector(".loadstat");
    this.fmtBar = rootEl.querySelector(".fmtbar");
    this.objBar = rootEl.querySelector(".objbar");
    this.buildFmtBar();
    this.buildObjBar();
    this.textEditing = null;
  };

  ChartPane.prototype.buildChart = function () {
    var self = this;
    var tc = ChartSet.defaults();
    this.chart = LWC.createChart(this.chartEl, {
      layout: { background: { color: tc.bg }, textColor: tc.text,
                fontFamily: "system-ui, 'Segoe UI', -apple-system, Roboto, sans-serif" },
      grid: { vertLines: { color: tc.gridColor }, horzLines: { color: tc.gridColor } },
      timeScale: { timeVisible: true, secondsVisible: true },
      rightPriceScale: { borderColor: tc.gridColor }
    });
    this.series = this.chart.addSeries(LWC.CandlestickSeries, {
      upColor: tc.up, downColor: tc.down,
      wickUpColor: tc.wickUp, wickDownColor: tc.wickDown,
      borderVisible: false
    });
    /* MT5 tick-volume histogram under the candles. Its own price
       scale (margins) so volume never disturbs price autoscale; hidden in
       Renko mode (Renko untouched). */
    this.volSeries = this.chart.addSeries(LWC.HistogramSeries, {
      priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false,
      priceFormat: { type: "volume" }
    });
    this.chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.84, bottom: 0 }, borderVisible: false
    });
    this.priceLine = null;
    this.chart.subscribeCrosshairMove(function (p) { self.onCrosshair(p); });
    /* coalesce to one overlay paint per animation frame during
       zoom/pan gestures (was: synchronous draw per range event). The chart
       canvas paints first, the overlay follows in the same frame - drawings
       and indicator plots stay glued to the candles instead of freezing
       mid-gesture and snapping afterwards. */
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(function () { self.drawSoon(); });
    /* per-frame overlay sync tracker. LWC v5 exposes no price-scale
       change subscription, so this tiny rAF loop watches both the visible
       time range and the price-scale mapping (via the last close as a fixed
       reference price). Any change schedules one coalesced repaint - drawings
       and indicator plots stay glued to the candles during time zoom/pan AND
       price-axis drags, at most one overlay paint per frame, ~zero work idle. */
    this._syncKey = "\0"; this._syncDead = false;
    (function trackOverlay() {
      if (self._syncDead) return;
      try {
        var lr = self.chart.timeScale().getVisibleLogicalRange();
        var rp = (self.cur && isFinite(self.cur.close)) ? self.cur.close : null;
        var y = (rp !== null && self.series) ? self.series.priceToCoordinate(rp) : null;
        var key = (lr ? lr.from.toFixed(2) + "," + lr.to.toFixed(2) : "?") + "|" +
                  ((y === null || y === undefined) ? "?" : y.toFixed(1));
        if (key !== self._syncKey) { self._syncKey = key; self.drawSoon(); }
      } catch (e) { /* chart not ready yet */ }
      requestAnimationFrame(trackOverlay);
    })();
    new ResizeObserver(function () { self.resize(); }).observe(this.wrapEl);
    this.resize();
    this.wireDrawEvents();
    this.axisPlus.addEventListener("click", function (e) {
      e.stopPropagation();
      if (self.axisPlus._price != null) Alerts.createDialog(self.state.symbol, self.axisPlus._price);
      self.axisPlus.style.display = "none";
      self.draw();
    });
    this.wrapEl.addEventListener("mouseleave", function () { self.axisPlus.style.display = "none"; });
    this.wrapEl.addEventListener("contextmenu", function (e) {
      e.preventDefault();
      var r = self.wrapEl.getBoundingClientRect();
      var d = self.toData(e.clientX - r.left, e.clientY - r.top);
      self.pending = null;
      CtxMenu.show(e.clientX, e.clientY, [
        d ? { label: "Add alert at " + roundPx(d.p), fn: function () { Alerts.createDialog(self.state.symbol, d.p); self.draw(); } } : null,
        d ? { label: "Add horizontal line here", fn: function () { self.addDrawing({ type: "hline", p1: { t: d.t, p: d.p }, p2: null }); } } : null,
        { sep: true },
        { label: "Indicators...", fn: function () { IndUI.openMenu(self); } },
        { label: "Price alerts...", fn: function () { Alerts.open(); } },
        { label: "Chart settings...", fn: function () { ChartSet.open(); } },
        { sep: true },
        { label: "Remove all drawings", fn: function () { self.state.drawings = self.state.drawings.filter(function (x) { return x.locked; }); self.selectedId = null; self.persist(); self.draw(); self.refreshFmtBar(); } }
      ]);
      self.draw();
    });
    ChartSet.apply(this);
  };

  ChartPane.prototype.resize = function () {
    var w = this.wrapEl.clientWidth, h = this.wrapEl.clientHeight;
    if (!w || !h) return;
    this.chart.applyOptions({ width: w, height: h });
    var dpr = window.devicePixelRatio || 1;
    this.canvas.width = w * dpr;
    this.canvas.height = h * dpr;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.draw();
  };

  ChartPane.prototype.wireUi = function () {
    var self = this;
    this.symInput.addEventListener("change", function () {
      self.state.symbol = self.symInput.value.trim() || "XAUUSD";
      self._resolveNoted = null;  // user picked a new symbol; allow fresh notice
      self.load(true); self.persist();
    });
    this.tfMenuBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      self.toggleTfPop();
    });
    /* floating bottom-right calendar button */
    var gtdBtn = this.el.querySelector(".gtd-floatbtn");
    if (gtdBtn) {
      gtdBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        self.openGoToDate();
      });
    }

    this.renderTfBar();
    var btns = this.el.querySelectorAll(".seg.mode button");
    btns.forEach(function (b) {
      b.addEventListener("click", function () {
        btns.forEach(function (x) { x.classList.remove("on"); });
        b.classList.add("on");
        self.state.mode = b.dataset.m;
        self.el.querySelector(".boxwrap").style.display = self.state.mode === "renko" ? "flex" : "none";
        self.load(true); self.persist();
      });
    });
    this.el.querySelector(".boxapply").addEventListener("click", function () {
      var v = parseFloat(self.boxInput.value);
      if (isFinite(v) && v >= 0.1 && v <= 50) { self.state.box = v; self.load(true); self.persist(); }
    });
    this.el.querySelector(".fxbtn").addEventListener("click", function () { IndUI.openMenu(self); });
    this.el.addEventListener("mousedown", function () { Layout.setActive(self.id); });
  };

  /* Favourite-timeframe bar: one-click buttons for starred TFs + v menu. */
  ChartPane.prototype.setTf = function (tf) {
    if (TF_LIST.indexOf(tf) < 0) return;
    this.closeTfPop();
    if (this.state.tf === tf) { this.renderTfBar(); return; }
    this.state.tf = tf;
    window.stocksophistTF = tf;  // let DATA-panel diagnostics read the chart TF
    this.load(true); this.persist();
    this.renderTfBar();
  };
  ChartPane.prototype.renderTfBar = function () {
    var self = this;
    if (!this.tfBar) return;
    var favs = TfFavs.list().slice().sort(function (a, b) {
      return tfSortKey(a) - tfSortKey(b);
    });
    var html = favs.map(function (t) {
      return '<button class="tfbtn' + (t === self.state.tf ? " on" : "") +
             '" data-tf="' + t + '">' + t + "</button>";
    }).join("");
    this.tfBar.innerHTML = html;
    Array.prototype.forEach.call(this.tfBar.querySelectorAll(".tfbtn"), function (b) {
      b.addEventListener("click", function () { self.setTf(b.getAttribute("data-tf")); });
    });
  };
  ChartPane.prototype.closeTfPop = function () {
    if (this.tfPop && this.tfPop.parentNode) this.tfPop.parentNode.removeChild(this.tfPop);
    this.tfPop = null;
  };
  /* "Go to date" - calendar popup to jump the chart to any day. */
  ChartPane.prototype.openGoToDate = function () {
    var self = this;
    // Remove any existing popup
    var old = document.querySelector(".gotodate-pop");
    if (old) old.remove();
    var pop = document.createElement("div");
    pop.className = "gotodate-pop";
    // Default to today in IST
    var now = new Date(Date.now() + 19800000);
    var iso = now.toISOString().slice(0, 10);
    pop.innerHTML =
      '<div class="gtd-title"> Go to date</div>' +
      '<input type="date" class="gtd-input" value="' + iso + '" max="' + iso + '">' +
      '<div class="gtd-row">' +
        '<button class="gtd-go">Go</button>' +
        '<button class="gtd-cancel">Cancel</button>' +
      '</div>';
    document.body.appendChild(pop);
    // Position above the bottom-right floating calendar button
    var btn = this.el.querySelector(".gtd-floatbtn");
    if (btn) {
      var r = btn.getBoundingClientRect();
      pop.style.left = Math.max(8, Math.min(window.innerWidth - 240, r.right - 220)) + "px";
      pop.style.top = Math.max(8, r.top - 190) + "px";
    } else {
      pop.style.left = "50%"; pop.style.top = "40%";
    }
    var input = pop.querySelector(".gtd-input");
    function close() { if (pop.parentNode) pop.parentNode.removeChild(pop); }
    pop.querySelector(".gtd-cancel").addEventListener("click", close);
    pop.querySelector(".gtd-go").addEventListener("click", function () {
      var v = input.value; // YYYY-MM-DD
      if (!v) { close(); return; }
      // Convert to IST midnight -> epoch seconds, then scroll chart there.
      // IST = UTC+5:30, so IST midnight = 18:30 UTC previous day.
      var parts = v.split("-");
      var utcMs = Date.UTC(+parts[0], +parts[1] - 1, +parts[2], 0, 0, 0) - 19800000;
      var ts = Math.floor(utcMs / 1000);
      try {
        // Center the visible range on that date (keep current zoom width).
        var range = self.chart.timeScale().getVisibleRange();
        var half = 12 * 3600; // default 12h each side
        if (range && range.from != null && range.to != null) {
          half = Math.max(3600, (range.to - range.from) / 2);
        }
        /* on a Renko pane the axis is in slot time - center on the
           slot of the first brick at/after the target date. */
        if (self.state.mode === "renko" && self.bricks && self.bricks.length) {
          var cslot = self.renkoSlot(ts);
          if (cslot < 1e9) {
            self.chart.timeScale().setVisibleRange({ from: cslot - half, to: cslot + half });
            close();
            return;
          }
        }
        self.chart.timeScale().setVisibleRange({ from: ts - half, to: ts + half });
      } catch (e) {}
      close();
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") pop.querySelector(".gtd-go").click();
      if (e.key === "Escape") close();
    });
    setTimeout(function () { input.focus(); }, 50);
  };

  ChartPane.prototype.toggleTfPop = function () {
    var self = this;
    if (this.tfPop) { this.closeTfPop(); return; }
    Layout.panes.forEach(function (p) { if (p !== self) p.closeTfPop(); });
    var pop = document.createElement("div");
    pop.className = "tfpop";
    function row(t) {
      var fav = TfFavs.isFav(t);
      return '<div class="tfrow' + (t === self.state.tf ? " cur" : "") + '" data-tf="' + t + '">' +
             '<span class="tfl">' + t + "</span>" +
             '<button class="star' + (fav ? " fav" : "") + '" data-star="' + t + '" title="' +
             (fav ? "Remove from favourites" : "Add to favourites") + '">' +
             (fav ? "*" : "*") + "</button></div>";
    }
    var groups = [["Seconds", ["1s", "15s"]], ["Minutes", ["1m", "3m", "5m", "15m", "30m"]],
                  ["Hours", ["1H", "4H"]], ["Daily+", ["D", "W", "M"]]];
    var html = "";
    groups.forEach(function (g) {
      html += '<div class="tfgroup">' + g[0] + "</div>";
      g[1].forEach(function (t) { html += row(t); });
    });
    pop.innerHTML = html;
    document.body.appendChild(pop);
    var r = this.tfMenuBtn.getBoundingClientRect();
    pop.style.left = Math.min(r.left, window.innerWidth - 210) + "px";
    pop.style.top = (r.bottom + 4) + "px";
    this.tfPop = pop;
    pop.addEventListener("click", function (e) {
      var star = e.target.getAttribute && e.target.getAttribute("data-star");
      if (star) {
        e.stopPropagation();
        TfFavs.toggle(star);
        self.toggleTfPop(); self.toggleTfPop(); // re-render open menu
        return;
      }
      var rw = e.target.closest ? e.target.closest(".tfrow") : null;
      if (rw) self.setTf(rw.getAttribute("data-tf"));
    });
    pop.addEventListener("mousedown", function (e) { e.stopPropagation(); });
  };

  ChartPane.prototype.setSymbol = function (sym) {
    this.symInput.value = sym;
    this.state.symbol = sym;
    this._resolveNoted = null;
    this.load(true); this.persist();
  };

  /* When the server substitutes the broker's real symbol for a symbol the
     broker doesn't list (e.g. GOLD -> GOLD#), adopt it visibly: update the
     symbol box + saved layout and tell the user once. No reload here - the
     data we just got is already for the resolved symbol. */
  ChartPane.prototype.noteResolved = function (res) {
    if (!res || !res.resolved_from || this._resolveNoted === res.resolved_from) return;
    this._resolveNoted = res.resolved_from;
    this.state.symbol = res.symbol;
    this.symInput.value = res.symbol;
    this.persist();
    toast(res.resolve_note || ("Showing " + res.symbol));
  };

  ChartPane.prototype.persist = function () { this.onChange(); };
  ChartPane.prototype.saveState = function () { this.persist(); }; // alias

  ChartPane.prototype.serialize = function () {
    /* instant start: persist the viewport with the layout. */
    var view = null;
    try {
      var r = this.chart.timeScale().getVisibleRange();
      if (r && r.from != null && r.to != null) view = { from: r.from, to: r.to };
    } catch (e) {}
    return { symbol: this.state.symbol, tf: this.state.tf, mode: this.state.mode,
             box: this.state.box, drawings: this.state.drawings,
             indicators: this.state.indicators,
             view: view || this._savedView || null };
  };

  /* ----------------------------------------------------------- data load */
  /* single api() definition (there were two - the later
     "harden the API helper" patch silently overrode this one and killed
     thetimeout +in-flight abort. Every chart request
     then had NO timeout: when MT5 wedged, requests hung forever and
     permanently clogged the browser's 6-per-origin connection slots, so
     even "Create alert" (also timeout-less) queued behind them forever -
     the button appeared to do nothing, and the clog survived the MT5
     restart until the tab was refreshed). This merged version keeps the
     timeout + abort AND the text-first JSON hardening.
     Per-slot abort: "load" requests abort previous "load" requests
     (rapid TF switches), "quote" requests abort previous "quote"
     requests - the 1s quote poll must NEVER abort a candle/indicator
     load. */
  ChartPane.prototype.api = function (path, timeoutMs, slot) {
    var self = this;
    slot = slot || "load";
    var key = "_apiCtrl_" + slot;
    try {
      if (self[key]) self[key].abort();
    } catch (e) {}
    var controller = new AbortController();
    self[key] = controller;
    var ms = timeoutMs || 30000;
    var timer = setTimeout(function () { controller.abort(); }, ms);
    return fetch(path, { signal: controller.signal }).then(function (r) {
      clearTimeout(timer);
      /* text-first: never throw a bare "not valid JSON" on proxy/error pages. */
      return r.text().then(function (t) {
        var j = null;
        try { j = JSON.parse(t); } catch (e) { /* non-JSON (shouldn't happen now) */ }
        if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
        if (!j) throw new Error("empty response from server");
        return j;
      });
    }).catch(function (e) {
      clearTimeout(timer);
      if (e.name === "AbortError") throw new Error("Request timed out after " + (ms / 1000) + "s");
      throw e;
    });
  };

  ChartPane.prototype.load = function (fit) {
    var self = this;
    /* leaving Renko mode (mode/TF/symbol change) must drop the
       Renko loading overlay IMMEDIATELY - not wait for candle data that
       may take forever on a sick MT5. Otherwise a candle pane sits under
       a stale "Loading Renko bricks..." spinner. */
    if (this.state.mode !== "renko") this.hideRenkoLoading();
    /* bar replay: a user-initiated reload (TF/symbol/mode/box change)
       ends the replay first - the new series has its own bar times, so the
       old cursor is meaningless. The resync path (fit=false) never hits this. */
    if (fit && typeof Replay !== "undefined" && Replay.active) Replay.exit(true);
    this.stopModeTimers();
    /* User-initiated load (mode/TF/symbol/box change): fresh start, drop any
       zombie tracking - the resync path (fit=false) keeps it instead.
       Bump the transition generation: any async response (renko poll,
       indicator fetch, quote) stamped with an older generation is stale and
       must not mutate the pane after the user moved on. */
    if (fit) {
      this._zombieTkey = null; this._zombieSince = null; this._lastHistEnd = null;
      this._renkoWatch = null;
      this._gen = (this._gen || 0) + 1;
      /* a mode/TF/symbol/box switch re-filters the indicator overlay
         immediately. Parts tagged for the old mode stop drawing (and their
         sweeps stop arming) the instant the user switches - no waiting for
         the new mode's fetch to resolve. */
      this._rebuildOverlay();
    }
    var gen = this._gen || 0;
    if (this.state.mode === "renko") return this.loadRenko(fit);
    var tf = this.state.tf;
    if (tf === "1s" || tf === "15s") {
      var sec = tf === "1s" ? 1 : 15;
      self.setLoadStat("Loading " + tf + " bars");
      return this.api("/api/tickbars?symbol=" + encodeURIComponent(this.state.symbol) +
                       "&sec=" + sec + "&bars=" + (DEEP_BARS[tf] || 400))
        .then(function (res) {
          self.clearLoadStat();
          if (gen !== self._gen || self.state.mode !== "candles") return;
          self.setCandles(res, fit); if (res.quote) self.updateQuote(res.quote);
        })
        .catch(function (e) {
          self.clearLoadStat();
          if (gen !== self._gen) return;
          toast(e.message);
          self.setTf("1m");
          self.load(fit); self.persist();
        });
    }
    /* retry candle load on failure (up to 3 attempts with backoff).
       A single failed /api/candles (e.g. server busy after a
       browser refresh) leaves the chart blank with only a toast. Retries
       run automatically so a transient failure recovers without manual reload. */
    /* two-phase load - 500 bars instantly for immediate display,
       then deep history in the background. Fixes the "blank then slow refill"
       complaint on refresh/new tab. */
    var fastBars = 500;
    var deepBars = DEEP_BARS[tf] || 600;
    var fastUrl = "/api/candles?symbol=" + encodeURIComponent(this.state.symbol) +
                    "&tf=" + encodeURIComponent(tf) + "&bars=" + fastBars + "&fast=1";
    var deepUrl = "/api/candles?symbol=" + encodeURIComponent(this.state.symbol) +
                    "&tf=" + encodeURIComponent(tf) + "&bars=" + deepBars;
    var attempts = 0;
    function tryCandles() {
      attempts++;
      // Phase 1: fast load (500 bars) for instant display.
      self.setLoadStat(attempts > 1 ? "Retrying candle load (" + attempts + "/3)" : "Loading candles");
      return self.api(fastUrl, 30000)
        .then(function (res) {
          if (gen !== self._gen || self.state.mode !== "candles") return;
          // fast=1 serves the disk cache only. On a truly fresh
          // install (no disk cache yet) it returns no bars - skip the
          // "No data" toast and let the deep phase below do first paint.
          if (res.bars && res.bars.length) {
            self.setCandles(res, fit);
            self.clearLoadStat();
          }
          // Phase 2: deep history in background (non-blocking).
          if (deepBars > fastBars) {
            self.setLoadStat("Loading full history");
            self.api(deepUrl, 60000)
              .then(function (deepRes) {
                if (gen !== self._gen || self.state.mode !== "candles") return;
                // Merge: deep history replaces, keeping the live tail.
                self.setCandles(deepRes, false);
                self.clearLoadStat();
              })
              .catch(function () { /* deep fill optional; fast data already shown */
                self.clearLoadStat();
              });
          }
        })
        .catch(function (e) {
          if (gen !== self._gen) return;
          if (attempts < 3) {
            self.setLoadStat("Candle load failed - retrying (" + attempts + "/3)");
            setTimeout(tryCandles, 2000 * attempts);
          } else {
            self.clearLoadStat();
            toast("Failed to load candles after 3 tries: " + e.message);
          }
        });
    }
    return tryCandles();
  };

  /* honest pane status line - what the pane is doing right now,
     with a live elapsed timer, instead of a silent empty chart. */
  ChartPane.prototype.setLoadStat = function (text) {
    var self = this;
    if (this._loadTick) { clearInterval(this._loadTick); this._loadTick = null; }
    if (!text) { if (this.loadStatEl) this.loadStatEl.style.display = "none"; return; }
    if (!this.loadStatEl) return;
    var t0 = Date.now();
    this.loadStatEl.style.display = "";
    function paint() {
      self.loadStatEl.textContent = text + " - " + Math.round((Date.now() - t0) / 1000) + "s";
    }
    paint();
    this._loadTick = setInterval(paint, 1000);
  };
  ChartPane.prototype.clearLoadStat = function () { this.setLoadStat(null); };

  /* tick-volume histogram helpers. Colors follow the candle
     up/down colors at 45% opacity, like MT5's volume display. */
  ChartPane.prototype.volBarColor = function (b) {
    var c = ChartSet.cached();
    return joinColor8(b.close >= b.open ? c.up : c.down, 45);
  };
  ChartPane.prototype.setVolumeData = function (bars) {
    if (!this.volSeries) return;
    var self = this, data = [];
    for (var i = 0; i < bars.length; i++) {
      var b = bars[i];
      data.push({ time: b.time, value: b.volume || 0,
                  color: self.volBarColor(b) });
    }
    try { this.volSeries.setData(data); } catch (e) {}
  };
  ChartPane.prototype.updateVolumeBar = function (b) {
    if (!this.volSeries || !b) return;
    try {
      this.volSeries.update({ time: b.time, value: b.volume || 0,
                              color: this.volBarColor(b) });
    } catch (e) {}
  };

  ChartPane.prototype.setCandles = function (res, fit) {
    var bars = res.bars;
    if (!bars || !bars.length) { toast("No data for " + this.state.symbol + " " + this.state.tf); return; }
    this.hideRenkoLoading();
    this.clearLoadStat();
    this.noteResolved(res);
    this.series.setData(bars);
    /* on the resync path (fit=false) the data swap wipes
       indicator bar colors - setData with plain bars removes indicator
       coloring until the next indicator fetch returns (minutes on a slow
       terminal), so colors "disappear in between". Re-apply the
       last-known colors by bar time; the soft refresh replaces them when
       the fresh result arrives. Full loads (fit=true) keep the plain
       restore - bar times may belong to another symbol/TF there. */
    try {
      var _cmap = this.indBarColors, _hasC = false, _ck;
      for (_ck in _cmap) { _hasC = true; break; }
      if (!fit && _hasC && this.state.mode === "candles") {
        this.series.setData(bars.map(function (b) {
          var c = _cmap[b.time];
          return c ? { time: b.time, open: b.open, high: b.high, low: b.low,
                       close: b.close, color: c } : b;
        }));
      }
    } catch (e) {}
    this.setVolumeData(bars);   // tick-volume histogram
    this.applyMarkers();   // resync point: re-apply indicator markers
    this.cur = Object.assign({}, bars[bars.length - 1]);
    this._histEarliest = res.earliest || null;
    if (res.earliest) {
      var d = new Date(res.earliest * 1000);
      this.histEl.textContent = "Hist " + fmtTZ_YMD(d.getTime());
      this.histEl.title = "Earliest loaded bar - " + (res.total || bars.length) + " bars loaded";
      this.histEl.style.display = "";
    } else {
      this.histEl.textContent = "";
      this.histEl.style.display = "none";
    }
    /* instant start: the first paint always establishes the
       viewport - reopen the saved one if it still overlaps the data,
       otherwise fitContent. Later loads only fit when asked (fit=true),
       so the 30s resync and the phase-2 deep swap never move the user's
       view. */
    if (!this._viewRestored) {
      this._viewRestored = true;
      var sv = this._savedView,
          first = bars[0].time, last = bars[bars.length - 1].time;
      if (sv && sv.from < last && sv.to > first) {
        this._settingView = true;
        try { this.chart.timeScale().setVisibleRange({ from: sv.from, to: sv.to }); }
        catch (e) { try { this.chart.timeScale().fitContent(); } catch (e2) {} }
        this._settingView = false;
      } else {
        try { this.chart.timeScale().fitContent(); } catch (e) {}
      }
    } else if (fit) {
      this.chart.timeScale().fitContent();
    }
    this.draw();
    this.startModeTimers();
    this.seriesData = bars;
    this.dataEnd = bars[bars.length - 1].time;
    /* Freeze-watchdog discipline : a resync that brings no newer
       history (stuck feed) must not restart the BARS STALE countdown or
       clear a raised badge - only genuine progress resets the watchdog.
       (The old code nulled _barWatch on every resync, so the 30s resync
       kept restarting the 180s window and a frozen feed could never raise.) */
    var newEnd = bars[bars.length - 1].time;
    var progressed = !this._lastHistEnd || newEnd > this._lastHistEnd;
    this._lastHistEnd = newEnd;
    if (progressed) {
      this._barWatch = null;
      this._zombieTkey = null; this._zombieSince = null;
      this.setBarsStale(false);
    }
    /* Freshness fast-path: the backend stamps every /api/candles and
       /api/tickbars response with server_time + as_of (newest bar, forming
       slot included). If fresh quotes are flowing but the newest bar is
       older than 1.5 slots, the freeze is at the source - raise immediately
       instead of waiting out the 180s quote clock. DEMO is exempt (its bars
       are historical by design); D/W/M are exempt (a static newest bar is
       normal there for hours); a dead quote stream (weekend) is exempt too -
       that's the STALE badge's job, not this one's. */
    if (res.server_time && res.as_of &&
        (this.modeBadge === "LIVE-MT5" || this.modeBadge === "LIVE-OANDA") &&
        !BIG_TF[this.state.tf] && this._qtime &&
        res.server_time - this._qtime < 120) {
      var _csec = TF_SEC[this.state.tf] || 60;
      if (res.server_time - res.as_of > Math.max(180, _csec * 1.5))
        this.setBarsStale(true);
    }
    this.refreshRefLines();
    /* Resync (fit=false) uses the change-gated soft refresh: no blink, and
       no recompute when nothing changed. Initial load/tf/mode change uses
       the full reload so overlays are complete from the first paint. */
    if (fit) this.loadIndicator(false); else this.softIndicatorRefresh(bars);
  };

  /* the first 15s/1s Renko load pulls tick history in a background
     thread on the server ("in parallel") - /api/renko answers immediately
     with {loading:true} instead of hanging the mode switch. The pane shows
     this overlay meanwhile; the 5s renko poll paints the bricks when the
     pull finishes. */
  ChartPane.prototype.showRenkoLoading = function () {
    var el = this._renkoLoadEl;
    if (!el) {
      el = document.createElement("div");
      el.className = "renkoload";
      el.innerHTML = '<div class="spin"></div><div style="max-width:340px">' +
        '<div style="font-weight:600;margin-bottom:6px">Loading Renko bricks...</div>' +
        '<div id="renko-op" style="min-height:20px"></div>' +
        '<span style="font-size:11px;color:#8b98ab">first load pulls tick history in the background</span></div>';
      this.wrapEl.appendChild(el);
      this._renkoLoadEl = el;
    }
    el.style.display = "flex";
  };
  /* render the background tick pull's live progress
     (what / how long / stuck? / real error) into the overlay. */
  ChartPane.prototype.updateRenkoProgress = function (op) {
    var el = this._renkoLoadEl;
    if (!el) return;
    var box = el.querySelector("#renko-op");
    if (box) OpsUI.render(box, op);
  };
  ChartPane.prototype.hideRenkoLoading = function () {
    if (this._renkoLoadEl) this._renkoLoadEl.style.display = "none";
  };

  ChartPane.prototype.loadRenko = function (fit) {
    var self = this, gen = this._gen || 0;
    this.api("/api/renko?symbol=" + encodeURIComponent(this.state.symbol) +
             "&box=" + this.state.box + "&bricks=" + DEEP_BRICKS +
             "&tf=" + encodeURIComponent(this.state.tf))
      .then(function (res) {
        /* Stale load: the user switched mode/TF/symbol/box while this was
           in flight - never paint Renko bricks onto another mode's pane. */
        if (gen !== self._gen || self.state.mode !== "renko") return;
        if (res.loading) {
          /* server is pulling tick history in the background.
             Show the loading overlay and keep the 5s poll running - it
             paints the bricks when the pull finishes.
             narrate the pull's live progress inside the overlay. */
          self.showRenkoLoading();
          self.updateRenkoProgress(res.progress);
          self.startModeTimers();
          return;
        }
        self.hideRenkoLoading();
        self.bricks = res.bricks;
        self.anchor = res.anchor;
        self.noteResolved(res);
        self.series.setData(res.bricks);
        /* volume is a candle feature - never on Renko. */
        try { if (self.volSeries) self.volSeries.applyOptions({ visible: false }); } catch (e) {}
        self.applyMarkers();   // resync point: re-apply indicator markers
        self._histEarliest = res.earliest || null;
        if (res.earliest) {
          var d = new Date(res.earliest * 1000);
          self.histEl.textContent = "Hist " + fmtTZ_YMD(d.getTime());
          self.histEl.title = "Earliest loaded brick - " + res.bricks.length + " bricks loaded";
          self.histEl.style.display = "";
        } else {
          self.histEl.textContent = "";
          self.histEl.style.display = "none";
        }
        if (fit) self.chart.timeScale().fitContent();
        if (res.quote) self.updateQuote(res.quote);
        self.draw();
        self.startModeTimers();
        self.seriesData = res.bricks;
        self.dataEnd = res.bricks.length ? res.bricks[res.bricks.length - 1].time : 0;
        self.refreshRefLines();
        self._barWatch = null; self.setBarsStale(false);
        /* Same blink-free split as setCandles: full reload on mode/tf
           change (fit), change-gated soft refresh on the 5s Renko poll. */
        if (fit) self.loadIndicator(false);
        else self.softIndicatorRefresh(res.bricks);
      })
      .catch(function (e) {
        if (gen !== self._gen) return;
        /* a failed background pull must not leave the loading
           overlay up forever - the 5s poll keeps retrying honestly. */
        self.hideRenkoLoading();
        toast(e.message);
      });
  };

  ChartPane.prototype.refreshRenko = function () {
    var self = this, gen = this._gen || 0;
    this.api("/api/renko?symbol=" + encodeURIComponent(this.state.symbol) +
             "&box=" + this.state.box + "&bricks=" + DEEP_BRICKS +
             "&tf=" + encodeURIComponent(this.state.tf))
      .then(function (res) {
        /* Stale poll: the user left Renko mode (or changed TF/symbol/box)
           while this was in flight - never paint bricks onto a candle pane. */
        if (gen !== self._gen || self.state.mode !== "renko") return;
        if (res.loading) {
          /* background tick pull still running - keep the overlay
             up; the next poll retries.
             refresh the pull's live progress narration. */
          self.showRenkoLoading();
          self.updateRenkoProgress(res.progress);
          return;
        }
        var firstPaint = self._renkoLoadEl &&
                         self._renkoLoadEl.style.display !== "none";
        self.hideRenkoLoading();
        self._renkoFails = 0;
        var nb = res.bricks;
        var lastOld = self.bricks[self.bricks.length - 1];
        var lastNew = nb[nb.length - 1];
        var changed = !lastOld || !lastNew || lastOld.time !== lastNew.time ||
                      lastOld.close !== lastNew.close || nb.length !== self.bricks.length;
        if (changed) {
          self.bricks = nb;
          self.anchor = res.anchor;
          self.series.setData(nb);
          self.applyMarkers();   // resync point: re-apply indicator markers
          self.draw();
          /* bricks that arrived via the background pull never got
             the initial fitContent - fit once on first paint. */
          if (firstPaint) self.chart.timeScale().fitContent();
          /* A newly closed brick can confirm fresh signals (the port only
             signals on closed bricks) - recompute the Renko indicator now
             instead of waiting for the 30s resync. Forming-brick wiggles
             must not trigger this. */
          var lcT = 0;
          for (var bi = nb.length - 1; bi >= 0; bi--) {
            if (!nb[bi].forming) { lcT = nb[bi].time; break; }
          }
          if (lcT && lcT !== self._lastClosedBrickT) {
            self._lastClosedBrickT = lcT;
            self.loadIndicator(true);
          }
        }
        if (res.quote) self.updateQuote(res.quote);
      })
      .catch(function (e) {
        /* keep old bricks on transient error, but don't fail
           silently forever - after 3 consecutive failures say so out loud.
           a persistently failing first load must not spin forever -
           hide the overlay and show the real error (feed-aware wording comes
           from the server). */
        self._renkoFails = (self._renkoFails || 0) + 1;
        if (self._renkoFails === 3) {
          self.hideRenkoLoading();
          toast((e && e.message) || "Renko refresh keeps failing - bricks may be stale.");
        }
      });
  };

  /* --------------------------------------------------------------- timers */
  ChartPane.prototype.startTimers = function () {
    var self = this;
    this.stopTimers();
    this.timers.quote = setInterval(function () {
      var p = "/api/quote?symbol=" + encodeURIComponent(self.state.symbol) +
              "&tf=" + encodeURIComponent(self.state.tf) +
              "&mode=" + self.state.mode;
      if (self.state.mode === "renko") {
        p += "&box=" + self.state.box;
        /* tell the server the newest brick we already show, so
           renko_live.new_bricks carries exactly the bricks we're missing -
           the server's chain is the single brick series now. */
        var bl = self.bricks;
        p += "&lastbrick=" + (bl && bl.length ? bl[bl.length - 1].time : 0);
      }
      /* quote polls run on their own abort slot - they must never
         cancel an in-flight candle/indicator load. */
      self.api(p, 10000, "quote")
        .then(function (res) { self.onQuote(res); })
        .catch(function () { self.onQuoteError(); });
    }, QUOTE_MS);
  };

  ChartPane.prototype.startModeTimers = function () {
    var self = this;
    this.stopModeTimers();
    if (this.state.mode === "renko") {
      this.timers.renko = setInterval(function () { self.refreshRenko(); }, RENKO_MS);
    } else {
      this.timers.resync = setInterval(function () { self.load(false); }, RESYNC_MS);
    }
  };

  ChartPane.prototype.stopModeTimers = function () {
    var self = this;
    ["renko", "resync"].forEach(function (k) {
      if (self.timers[k]) { clearInterval(self.timers[k]); self.timers[k] = null; }
    });
  };

  ChartPane.prototype.stopTimers = function () {
    this.stopModeTimers();
    if (this.timers.quote) { clearInterval(this.timers.quote); this.timers.quote = null; }
  };

  ChartPane.prototype.destroy = function () {
    this.stopTimers();
    this._syncDead = true;
    if (this._drawRaf) { cancelAnimationFrame(this._drawRaf); this._drawRaf = null; }
    if (this.chart) this.chart.remove();
    this._markersPlugin = null;
    this.el.innerHTML = "";
  };

  /* ------------------------------------------------------------ tick flow */
  ChartPane.prototype.updateQuote = function (q) {
    this.bidEl.textContent = q.bid.toFixed(2);
    this.askEl.textContent = q.ask.toFixed(2);
    this.lastQuote = q;
    var prev = this.lastBid;
    this.lastBid = q.bid;
    this.checkSweeps(prev, q.bid);
    this.refreshPriceLine(q);
  };

  /* Live price / bid / ask / prev-close / day-HL reference lines (settings-driven) */
  ChartPane.prototype.refreshPriceLine = function (q) {
    var c = ChartSet.cfg(), self = this;
    /* Persistent price lines: created once, then only moved via applyOptions.
       The old code removed and re-created every line on every quote poll,
       which made the price line flicker once a second. */
    function ensure(key, price, color, style, title) {
      if (price === null || price === undefined || !isFinite(price)) {
        if (self.refLines[key]) {
          try { self.series.removePriceLine(self.refLines[key]); } catch (e) {}
          delete self.refLines[key];
        }
        return;
      }
      var lineStyle = style === "dashed" ? LWC.LineStyle.Dashed : LWC.LineStyle.Solid;
      if (self.refLines[key]) {
        try {
          self.refLines[key].applyOptions({
            price: price, color: color, lineWidth: 1, lineStyle: lineStyle,
            axisLabelVisible: true, title: title || ""
          });
        } catch (e) { delete self.refLines[key]; }
      } else {
        try {
          self.refLines[key] = self.series.createPriceLine({
            price: price, color: color, lineWidth: 1, lineStyle: lineStyle,
            axisLabelVisible: true, title: title || ""
          });
        } catch (e) {}
      }
    }
    var keep = {};
    if (c.priceLine) {
      var title = "";
      if (c.countdown && q && q.time) {
        var sec = TF_SEC[this.state.tf];
        if (sec) {
          var left = sec - (q.time % sec);
          title = Math.floor(left / 60) + ":" + ("0" + (left % 60)).slice(-2);
        }
      }
      keep.px = 1; ensure("px", q.bid, c.priceLineColor, "dashed", title);
    }
    if (c.bidAsk) {
      keep.bid = 1; ensure("bid", q.bid, "#2962ff", "dashed", "");
      keep.ask = 1; ensure("ask", q.ask, "#ef5350", "dashed", "");
    }
    if (c.prevClose && this._prevClose) {
      keep.pc = 1; ensure("pc", this._prevClose, c.prevCloseColor, "solid", "Prev close");
    }
    if (c.dayHL && this._dayHi) {
      keep.dh = 1; ensure("dh", this._dayHi, c.dayHLColor, "dashed", "H");
    }
    if (c.dayHL && this._dayLo) {
      keep.dl = 1; ensure("dl", this._dayLo, c.dayHLColor, "dashed", "L");
    }
    /* Remove lines whose toggle was switched off. */
    Object.keys(this.refLines).forEach(function (k) {
      if (!keep[k]) {
        try { self.series.removePriceLine(self.refLines[k]); } catch (e) {}
        delete self.refLines[k];
      }
    });
  };

  /* Reference values: previous daily close + today's high/low (UTC day). */
  ChartPane.prototype.refreshRefLines = function () {
    var self = this;
    this._prevClose = null; this._dayHi = null; this._dayLo = null;
    fetch("/api/prev_close?symbol=" + encodeURIComponent(this.state.symbol))
      .then(function (r) { return r.json(); })
      .then(function (j) { self._prevClose = j.prev_close || null; })
      .catch(function () {});
    var bars = this.seriesData || [];
    if (bars.length) {
      /* group by REAL day on Renko (bricks render at slot times). */
      var rtOf = function (b) {
        return (b.rtime !== undefined && b.rtime !== null) ? b.rtime : b.time;
      };
      var day = new Date(rtOf(bars[bars.length - 1]) * 1000).getUTCDate();
      var hi = -1e18, lo = 1e18;
      for (var i = bars.length - 1; i >= 0; i--) {
        if (new Date(rtOf(bars[i]) * 1000).getUTCDate() !== day) break;
        if (bars[i].high > hi) hi = bars[i].high;
        if (bars[i].low < lo) lo = bars[i].low;
      }
      if (hi > -1e17) { this._dayHi = hi; this._dayLo = lo; }
    }
  };

  /* ------------------------------------------ tick flow
     The server is the single source of truth for bars. Every quote poll also
     returns MT5's own forming bar (live_bar) or Renko state (renko_live); the
     client renders exactly that and never invents OHLC from poll ticks.
     Out-of-order poll responses are dropped via the seq guard; a feed older
     than STALE_S seconds (per the MT5 clock) raises the STALE badge instead
     of silently freezing. */
  ChartPane.prototype.onQuote = function (res) {
    var q = res && res.quote;
    if (!q) { this.onQuoteError(); return; }
    // Ordering guards: drop stale / out-of-order poll responses so an old
    // reply can never overwrite newer data.
    if (res.seq && this._qseq && res.seq <= this._qseq) return;
    if (res.seq) this._qseq = res.seq;
    if (this._qtime && q.time < this._qtime) return;
    this._qtime = q.time;
    this._qfail = 0;
    if (q.bid !== undefined && q.bid !== null) this._liveBid = q.bid; // long/short Open PnL label
    this.noteResolved(res);
    this.updateQuote(q);
    /* bar replay: the replay cursor owns the visible series - a live
       quote must never rewrite replayed history mid-replay. The quote strip
       itself stays live (it is real market data). */
    if (typeof Replay !== "undefined" && Replay.active) return;
    var serverNow = res.server_time || q.time;
    this.setStale(serverNow - q.time > STALE_S);
    if (this.state.mode === "renko") {
      if (res.renko_live) {
        this.adoptRenkoLive(res.renko_live);
        this.watchRenko(res.renko_live, q);
      }
    } else if (res.live_bar) {
      this.adoptLiveBar(res.live_bar);
      this.watchBars(res.live_bar, q);
    }
    // No live_bar (e.g. demo 1s/15s): quote line/alerts still update; bars
    // refresh on the normal resync timer.
  };

  ChartPane.prototype.onQuoteError = function () {
    this._qfail = (this._qfail || 0) + 1;
    if (this._qfail >= 3) this.setStale(true);
  };

  ChartPane.prototype.setStale = function (on) {
    if (this._stale === on) return;
    this._stale = on;
    if (this.staleEl) this.staleEl.style.display = on ? "" : "none";
  };

  /* Bars-freeze watchdog : quotes can stay live while MT5 stops
     delivering bar updates (half-dead terminal: tick stream flows, rate
     history frozen). If fresh quotes keep arriving for 3+ minutes while
     the server's forming bar (time+close) never moves, raise BARS STALE
     so a frozen candle series is never mistaken for a quiet market.
     Demo mode is immune: its quote clock is static, so the window never
     elapses. Renko mode doesn't watch (adoptRenkoLive has its own path). */
  ChartPane.prototype.watchBars = function (lb, q) {
    var f = lb && lb.forming;
    var tkey = f ? f.time : "none";          // forming slot
    var ckey = f ? (f.time + ":" + f.close) : "none";  // slot + price
    var w = this._barWatch;
    if (!w) {
      /* (Re)start watching. A resync clears the watch object - the zombie
         clock must survive that (setCandles decides), so don't touch it. */
      this._barWatch = { tkey: tkey, ckey: ckey, qt: q.time };
      return;
    }
    if (w.tkey !== tkey) {
      /* Slot rolled: the feed is alive - clear zombie tracking + badge. */
      this._barWatch = { tkey: tkey, ckey: ckey, qt: q.time };
      this._zombieTkey = null; this._zombieSince = null;
      this.setBarsStale(false);
      return;
    }
    if (w.ckey !== ckey) {
      /* Zombie forming bar: the slot time is frozen but OHLC still moves
         (half-dead MT5 rate history - ticks flow, slots don't roll). The old
         (time:close) key reset on every tick, so this never raised. The slot
         must still roll on schedule: allow 1.5 slots, then raise BARS STALE.
         D/W/M are exempt - a static slot time is normal there for hours. */
      w.ckey = ckey;
      if (this._zombieTkey !== tkey) { this._zombieTkey = tkey; this._zombieSince = q.time; }
      var sec = TF_SEC[this.state.tf] || 60;
      if (!BIG_TF[this.state.tf] && q.time - this._zombieSince > Math.max(180, sec * 1.5))
        this.setBarsStale(true);
      return;
    }
    if (q.time - w.qt > 180) this.setBarsStale(true);
  };

  /* Renko freeze watchdog : bricks only close when price moves a
     full box, so a quiet market legitimately shows no new bricks for
     minutes - a time-only clock would false-positive constantly. The
     failure mode from the report is different: the price line
     keeps moving but bricks stop forming. So this watches for brick
     progress (new bricks, or the forming brick's close moving with the
     quote) and raises BARS STALE only when the quote has moved more than
     two boxes away from the last observed forming close with no brick
     progress for 3+ minutes. Demo mode is immune (static quote clock). */
  ChartPane.prototype.watchRenko = function (rl, q) {
    var box = parseFloat(this.state.box) || 1;
    var nb = (rl.new_bricks || []).length;
    var f = rl.forming;
    var key = nb + "|" + (f ? f.time + ":" + f.close : "noform");
    var w = this._renkoWatch;
    if (!w || w.key !== key) {
      this._renkoWatch = { key: key, qt: q.time, close: f ? f.close : null };
      this.setBarsStale(false);
      return;
    }
    if (w.close !== null && f && q.bid != null &&
        Math.abs(q.bid - w.close) > 2 * box && q.time - w.qt > 180)
      this.setBarsStale(true);
  };

  ChartPane.prototype.setBarsStale = function (on) {
    if (this._barsStale === on) return;
    this._barsStale = on;
    if (this.barsStaleEl) {
      this.barsStaleEl.style.display = on ? "" : "none";
      /* The badge covers both series: say which one is frozen. */
      if (on) this.barsStaleEl.title = this.state.mode === "renko"
        ? "Quotes are live but no new Renko bricks are forming - the brick engine is stuck. Check the MT5 terminal connection."
        : "Quotes are live but MT5 stopped sending bar updates - candles are frozen. Check the MT5 terminal connection.";
    }
  };

  /* Adopt the server's authoritative forming bar - never invent OHLC.
     Forensics: _lbNoForming / _lbBehind count the two silent-freeze paths so
     the next frozen candle is diagnosable from the devtools console. */
  ChartPane.prototype.adoptLiveBar = function (lb) {
    var f = lb && lb.forming;
    if (!f) { this._lbNoForming = (this._lbNoForming || 0) + 1; return; }
    var sec = TF_SEC[this.state.tf];
    if (!this.cur) {
      this.cur = Object.assign({}, f);
      this.series.update(this.cur);
      this.updateVolumeBar(this.cur);
      this._syncTail(this.cur, true);
      this._olderSince = null;
      return;
    }
    if (f.time === this.cur.time) {
      this.cur = Object.assign({}, f);
      this.series.update(this.cur);
      this.updateVolumeBar(this.cur);
      this._syncTail(this.cur, false);
      this._olderSince = null;
      return;
    }
    if (f.time > this.cur.time) {
      // Bar rolled over on MT5: finalize the old bar from the server's
      // closed bar (exact OHLC), then open the new forming bar.
      if (lb.prev_closed && lb.prev_closed.time === this.cur.time) {
        this.series.update(lb.prev_closed);
        this.updateVolumeBar(lb.prev_closed);
        this._syncTail(lb.prev_closed, false);
      } else if (sec && !BIG_TF[this.state.tf] && f.time - this.cur.time > sec * 1.5) {
        // Gap: MT5 skipped buckets (feed hole or long stall). Never fabricate
        // the missing bars - backfill them from the server instead. Cooldown:
        // a flapping live_bar must never cause a reload storm.
        var nowMs = Date.now();
        if (!this._lastGapLoad || nowMs - this._lastGapLoad > 5000) {
          this._lastGapLoad = nowMs;
          this.load(false);
        }
        return;
      }
      this.cur = Object.assign({}, f);
      this.series.update(this.cur);
      this.updateVolumeBar(this.cur);
      this._syncTail(this.cur, true);
      this._olderSince = null;
      return;
    }
    /* f.time < cur.time: the server's forming bar is older than our newest
       bar (stale MT5 rates / zombie forming slot that the vault couldn't
       heal, e.g. empty vault). A momentary boundary race is normal - but
       ignoring it FOREVER freezes the candle under a live price line with no
       badge and no recovery (the reported bug). So: tolerate 30s, then
       reconcile from the server (cooldown-guarded) and say so in the
       console for forensics. */
    var nowMs = Date.now();
    this._lbBehind = (this._lbBehind || 0) + 1;
    if (!this._olderSince) this._olderSince = nowMs;
    if (nowMs - this._olderSince > 30000) {
      this._olderSince = nowMs;
      try {
        console.warn("[stocksophist] live_bar behind chart 30s+ (server " +
          new Date(f.time * 1000).toISOString() + " < chart " +
          new Date(this.cur.time * 1000).toISOString() +
          ") - reconciling from server");
      } catch (e) {}
      if (!this._lastGapLoad || nowMs - this._lastGapLoad > 5000) {
        this._lastGapLoad = nowMs;
        this.load(false);
      }
    }
  };

  /* Keep seriesData's tail identical to what the chart shows. */
  ChartPane.prototype._syncTail = function (bar, isNew) {
    var sd = this.seriesData;
    if (!sd || !sd.length) return;
    if (isNew) {
      if (sd[sd.length - 1].time === bar.time) sd[sd.length - 1] = Object.assign({}, bar);
      else sd.push(Object.assign({}, bar));
    } else {
      for (var i = sd.length - 1; i >= 0 && i >= sd.length - 4; i--) {
        if (sd[i].time === bar.time) { sd[i] = Object.assign({}, bar); break; }
      }
    }
  };

  /* Adopt the server's authoritative Renko state .
     the server's chain is the single brick series - new_bricks are
     chain bricks newer than our lastbrick, so adopting them can never
     disagree with the next chart poll. */
  ChartPane.prototype.adoptRenkoLive = function (rl) {
    if (rl.anchor == null || !this.bricks || !this.bricks.length) return;
    var self = this, structural = false;
    (rl.new_bricks || []).forEach(function (b) {
      var last = self.bricks[self.bricks.length - 1];
      if (last && last.forming) {
        self.bricks[self.bricks.length - 1] = b; // forming brick just closed
      } else {
        if (b.time <= last.time) return; // already have it (refresh beat poll)
        self.bricks.push(b);
      }
      structural = true;
    });
    this.anchor = rl.anchor;
    var fb = this.bricks[this.bricks.length - 1];
    if (rl.forming) {
      if (fb && fb.forming && fb.time === rl.forming.time) {
        this.bricks[this.bricks.length - 1] = rl.forming;
        this.series.update(rl.forming);
      } else {
        if (fb && fb.forming) this.bricks[this.bricks.length - 1] = rl.forming;
        else this.bricks.push(rl.forming);
        structural = true;
      }
    } else if (fb && fb.forming) {
      // Price sitting exactly on the anchor: flatten the forming brick.
      this.bricks[this.bricks.length - 1] =
        { time: fb.time, open: rl.anchor, high: rl.anchor, low: rl.anchor,
          close: rl.anchor, forming: true, dir: fb.dir };
      structural = true;
    }
    // Brick open/close changes the point set -> full setData (brick closes
    // are rare, so this stays cheap); plain forming updates use update().
    if (structural) { this.series.setData(this.bricks); this.draw(); }
    this.seriesData = this.bricks;
    /* A brick just closed via the live tick path - same rule as the 5s
       poll: the Renko indicator must recompute on the new closed brick. */
    var alcT = 0;
    for (var ai = this.bricks.length - 1; ai >= 0; ai--) {
      if (!this.bricks[ai].forming) { alcT = this.bricks[ai].time; break; }
    }
    if (alcT && alcT !== this._lastClosedBrickT) {
      this._lastClosedBrickT = alcT;
      this.loadIndicator(true);
    }
  };

  /* ------------------------------------------------------------- crosshair */
  ChartPane.prototype.onCrosshair = function (p) {
    if (!p || !p.time || !p.seriesData) {
      this.legendEl.textContent = "";
      this.hideSignalTip();
      this.hideNewsTip();
      return;
    }
    var d = p.seriesData.get(this.series);
    if (!d) { this.legendEl.textContent = ""; this.hideSignalTip(); return; }
    function f(x) { return Number(x).toFixed(2); }
    this.legendEl.textContent =
      this.state.symbol + "  " + this.state.tf.toUpperCase() +
      "   O " + f(d.open) + "  H " + f(d.high) + "  L " + f(d.low) + "  C " + f(d.close);
    this.showSignalTip(p);
    this.showNewsTip(p);  /* pure-visual news marker tooltip */
  };

  /* Signal explanation tooltip: hovering a bar that carries a BUY/SELL marker
     shows the indicator's exact per-signal explanation (e.g. Renko's
     "Logic: Swept ..." line). Nothing is recomputed here - the text comes
     verbatim from the backend marker payload. */
  ChartPane.prototype.signalTipEl = function () {
    if (!this._signalTip) {
      var el = document.createElement("div");
      el.className = "signaltip";
      el.style.display = "none";
      this.wrapEl.appendChild(el);
      this._signalTip = el;
    }
    return this._signalTip;
  };
  ChartPane.prototype.hideSignalTip = function () {
    if (this._signalTip) this._signalTip.style.display = "none";
  };
  ChartPane.prototype.showSignalTip = function (p) {
    /* Canvas signal-label boxes first (point hit-test), then the legacy
       time-based marker lookup. */
    var pt = p.point, boxes = this._labelBoxes || [], i, b;
    if (pt) {
      for (i = 0; i < boxes.length; i++) {
        b = boxes[i];
        if (pt.x >= b.x0 && pt.x <= b.x1 && pt.y >= b.y0 && pt.y <= b.y1) {
          if (b.explain) { this.renderSignalTip(b.side, b.label, b.explain, p); return; }
        }
      }
    }
    var info = this._markerInfo || [];
    if (!info.length) { this.hideSignalTip(); return; }
    var t = Number(p.time);
    var hit = null;
    for (i = 0; i < info.length; i++) {
      if (Number(info[i].t) === t) { hit = info[i]; break; }
    }
    if (!hit || !hit.explain) { this.hideSignalTip(); return; }
    this.renderSignalTip(hit.side, hit.label, hit.explain, p);
  };
  ChartPane.prototype.renderSignalTip = function (side, label, explain, p) {
    var el = this.signalTipEl();
    el.innerHTML = "";
    var hd = document.createElement("div");
    hd.className = "st-head " + side;
    hd.textContent = (side === "buy" ? "^ BUY" : "v SELL") + "  " + label;
    el.appendChild(hd);
    var bd = document.createElement("div");
    bd.className = "st-body";
    bd.textContent = explain;
    el.appendChild(bd);
    // position near the crosshair, clamped inside the pane
    var r = this.wrapEl.getBoundingClientRect();
    var x = (p.point ? p.point.x : r.width / 2) + 16;
    var y = (p.point ? p.point.y : 60) + 16;
    el.style.display = "block";
    var ew = el.offsetWidth, eh = el.offsetHeight;
    if (x + ew > r.width - 8) x = r.width - ew - 8;
    if (y + eh > r.height - 8) y = r.height - eh - 8;
    el.style.left = Math.max(8, x) + "px";
    el.style.top = Math.max(8, y) + "px";
  };

  /* ================================================================ drawings */
  ChartPane.prototype.px = function (t, p) {
    /* drawings store real times from older versions or slot times;
       map to slot space on Renko so both render. */
    var x = this.chart.timeScale().timeToCoordinate(this.renkoSlot(t));
    var y = this.series.priceToCoordinate(p);
    return (x === null || x === undefined || y === null || y === undefined) ? null : { x: x, y: y };
  };

  ChartPane.prototype.toData = function (x, y) {
    var t = this.chart.timeScale().coordinateToTime(x);
    var p = this.series.coordinateToPrice(y);
    if (t === null || t === undefined || !isFinite(p)) return null;
    /* in Renko mode coordinateToTime yields a slot time - store
       the brick's REAL formation time so drawings stay valid if the pane
       switches back to candles (px() maps real->slot at render). */
    if (this.state.mode === "renko") t = this.renkoReal(t);
    return { t: t, p: p };
  };

  ChartPane.prototype.wireDrawEvents = function () {
    var self = this;
    /* Capture phase : this runs BEFORE lightweight-charts' own
       mousedown handlers on the chart container. When we start dragging a
       drawing (or an SL/TP line), we stopPropagation so the chart never sees
       the gesture - the chart must not pan UNDER the drag, or
       coordinateToTime() at the pointer returns ~the same t while the
       scale scrolls with the mouse: drawings move only vertically
       while the whole chart slides sideways. Clicks on our own overlay UI
       are swallowed for the same reason. */
    this.wrapEl.addEventListener("mousedown", function (e) {
      /* bar replay: picking the replay start swallows the gesture -
         the click never reaches drawings, trade lines or chart pan. */
      if (typeof Replay !== "undefined" && Replay.picking) {
        var rr = self.wrapEl.getBoundingClientRect();
        Replay.pick(self, e.clientX - rr.left, e.clientY - rr.top);
        e.stopPropagation();
        e.preventDefault();
        return;
      }
      if (e.button === 2) { self.pending = null; self.draw(); return; } // right-click cancels
      if (e.target.closest(".pane-bar")) return;
      /* an open inline text editor commits on any outside click (Enter/Esc
         are handled by the editor itself) */
      if (self.textEditing) {
        if (e.target.closest && e.target.closest(".txtedit,.objbar")) return;
        self.commitTextEdit(true);
        return;
      }
      /* overlay UI lives inside chartwrap: clicks there must NOT deselect drawings
         (this kills the format toolbar before a control's change can apply),
         and must NOT reach the chart (a toolbar click must not pan the chart) */
      if (e.target.closest && e.target.closest(".fmtbar,.objbar,.indlegend,.inddash,.axisplus")) {
        e.stopPropagation();
        return;
      }
      var r = self.wrapEl.getBoundingClientRect();
      var x = e.clientX - r.left, y = e.clientY - r.top;
      var tool = Layout.tool;
      if (tool === "cursor") {
        /* trade lines (SL/TP/pending) take priority when the panel is live */
        if (typeof Trade !== "undefined" && Trade.visible && Trade.live) {
          var th = Trade.hitTest(self, x, y);
          if (th) {
            var startPrice = th.kind === "entry" ? th.pos.price_open :
                             th.kind === "sl" ? th.pos.sl :
                             th.kind === "tp" ? th.pos.tp : th.ord.price;
            self.tradeDrag = { hit: th, sx: x, sy: y, moved: false,
                               price: startPrice, startPrice: startPrice,
                               mx: e.clientX, my: e.clientY };
            self.selectedId = null;
            e.stopPropagation(); // chart must not pan while an SL/TP line drags
            self.draw();
            return;
          }
        }
        var hit = self.hitTest(x, y);
        self.selectedId = hit ? hit.id : null;
        self.refreshFmtBar();
        var d0 = self.toData(x, y);
        /* locked drawings select (so they can be unlocked) but never drag */
        if (hit && d0 && !hit.locked) {
          self.drag = { id: hit.id, sx: x, sy: y, t0: d0.t, p0: d0.p,
                        part: self._hitPart, /* long/short: entry|stop|target|move */
                        orig: JSON.parse(JSON.stringify(self.getDrawing(hit.id))) };
          /* swallow the gesture (capture phase) - lightweight-charts
             must not pan under a drawing drag. Without this the time scale
             scrolled with the pointer, dt cancelled out, and drawings moved
             vertically only while the chart slid sideways. */
          e.stopPropagation();
        }
        self.draw();
        return;
      }
      var d = self.toData(x, y);
      if (!d) return;
      if (tool === "hline") {
        self.addDrawing({ type: "hline", p1: { t: d.t, p: d.p }, p2: null });
        self.finishToolUse();
      } else if (tool === "hray") {
        self.addDrawing({ type: "hray", p1: { t: d.t, p: d.p }, p2: null });
        self.finishToolUse();
      } else if (tool === "vline") {
        self.addDrawing({ type: "vline", p1: { t: d.t, p: d.p }, p2: null });
        self.finishToolUse();
      } else if (tool === "text") {
        /* chart-style: inline editor at the click point; Enter or
           click-outside commits, Esc cancels */
        self.beginTextEdit(x, y, null);
      } else if (tool === "eraser") {
        var h2 = self.hitTest(x, y);
        if (h2 && !h2.locked) self.removeDrawing(h2.id);
        self.finishToolUse();
      } else if (tool === "cross") {
        self.selectedId = null; self.refreshFmtBar(); self.draw(); // pure crosshair inspection
      } else if (tool === "long" || tool === "short") {
        if (!self.pending || self.pending.type !== tool) {
          self.pending = { type: tool, p1: { t: d.t, p: d.p }, sx: x, sy: y, dragged: false };
        } else {
          self.addDrawing(self.buildLongShort(tool, self.pending.p1, { t: d.t, p: d.p }));
          self.pending = null;
          self.finishToolUse();
        }
      } else if (tool === "trend" || tool === "rect" || tool === "ray" ||
                 tool === "ellipse" || tool === "fib") {
        if (!self.pending || self.pending.type !== tool) {
          self.pending = { type: tool, p1: { t: d.t, p: d.p }, sx: x, sy: y, dragged: false };
        } else {
          self.addDrawing({ type: tool, p1: self.pending.p1, p2: { t: d.t, p: d.p } });
          self.pending = null;
          self.finishToolUse();
        }
      }
      self.draw();
    }, true); /* capture: runs before lightweight-charts' own mousedown */
    this.wrapEl.addEventListener("mousemove", function (e) {
      var r = self.wrapEl.getBoundingClientRect();
      var x = e.clientX - r.left, y = e.clientY - r.top;
      self.axisHover(x, y);
      if (self.tradeDrag) {
        var td = self.toData(x, y);
        if (td && td.p) {
          if (Math.abs(x - self.tradeDrag.sx) + Math.abs(y - self.tradeDrag.sy) > 4)
            self.tradeDrag.moved = true;
          self.tradeDrag.price = td.p;
          self.tradeDrag.mx = e.clientX; self.tradeDrag.my = e.clientY;
          self.drawSoon();
        }
        return;
      }
      if (self.drag) {
        var dd = self.toData(x, y);
        if (dd) {
          var dr = self.getDrawing(self.drag.id);
          if (dr) {
            var dt = dd.t - self.drag.t0, dp = dd.p - self.drag.p0;
            var o = self.drag.orig;
            if ((dr.type === "long" || dr.type === "short") && self.drag.part &&
                self.drag.part !== "move") {
              /* the chart app position tool: entry/stop/target lines drag
                 vertically (price only - the box keeps its time anchor) */
              var pk = self.drag.part === "entry" ? "p1" :
                       self.drag.part === "stop" ? "p2" : "p3";
              if (pk === "p3" && !o.p3) {
                o.p3 = { t: o.p1.t, p: o.p1.p + (o.p1.p - o.p2.p) }; // legacy mirror
              }
              dr[pk] = { t: o[pk].t, p: dd.p };
            } else {
              dr.p1 = { t: o.p1.t + dt, p: o.p1.p + dp };
              if (dr.p2) dr.p2 = { t: o.p2.t + dt, p: o.p2.p + dp };
              if (dr.p3) dr.p3 = { t: o.p3.t + dt, p: o.p3.p + dp };
            }
            self.drawSoon();
          }
        }
        return;
      }
      if (self.pending) {
        self.pending.px = x; self.pending.py = y;
        if (self.pending.sx !== undefined &&
            Math.hypot(x - self.pending.sx, y - self.pending.sy) > 6) self.pending.dragged = true;
        self.drawSoon();
      }
    });
    window.addEventListener("mouseup", function (e) {
      if (self.drag) { self.drag = null; self.persist(); }
      /* chart-style click-drag: releasing after a drag completes a
         two-point drawing (a plain click keeps the two-click workflow) */
      if (self.pending && self.pending.dragged && !self.drag) {
        var r = self.wrapEl.getBoundingClientRect();
        var dd = self.toData(e.clientX - r.left, e.clientY - r.top);
        var pd = self.pending;
        if (dd && e.target && self.wrapEl.contains(e.target)) {
          if (pd.type === "long" || pd.type === "short")
            self.addDrawing(self.buildLongShort(pd.type, pd.p1, { t: dd.t, p: dd.p }));
          else
            self.addDrawing({ type: pd.type, p1: pd.p1, p2: { t: dd.t, p: dd.p } });
          self.pending = null;
          self.finishToolUse();
          self.draw();
        } else {
          self.pending.dragged = false;
        }
      }
      if (self.tradeDrag) {
        var td = self.tradeDrag; self.tradeDrag = null;
        Trade.hideDragTip();
        if (td.moved && Math.abs(td.price - td.startPrice) > 1e-9 &&
            (td.hit.kind === "sl" || td.hit.kind === "tp" || td.hit.kind === "pend")) {
          Trade.finishDrag(self, td);   // modify on the server; reverts visually on failure
        } else if (!td.moved) {
          Trade.popupFor(self, td.hit, e.clientX, e.clientY);  // click: action menu
        }
        self.draw();
      }
    });
    /* chart-style: double-click a drawing to attach/edit its text label.
       Centered dialog (the old native prompt popped at the top of the window). */
    this.wrapEl.addEventListener("dblclick", function (e) {
      if (Layout.tool !== "cursor" || e.target.closest(".pane-bar")) return;
      if (e.target.closest && e.target.closest(".fmtbar,.objbar,.indlegend,.inddash,.axisplus")) return;
      var r = self.wrapEl.getBoundingClientRect();
      var hit = self.hitTest(e.clientX - r.left, e.clientY - r.top);
      if (!hit || hit.locked) return;
      TextDialog.open("Text", "Text:", hit.text || "", function (txt) {
        if (txt === null) return; // cancelled
        hit.text = txt;
        self.selectedId = hit.id;
        self.persist(); self.draw(); self.refreshObjBar();
      });
    });
    /* clicks anywhere outside the pane (topbar, other panes) commit an open
       inline text editor; clicks inside the pane are handled above */
    document.addEventListener("mousedown", function (e) {
      if (self.textEditing && self.wrapEl &&
          !self.wrapEl.contains(e.target) &&
          !(e.target.closest && e.target.closest(".txtedit"))) {
        self.commitTextEdit(true);
      }
    });
  };

  /* chart-style "+" on the price axis: hover the scale, click to alert. */
  ChartPane.prototype.axisHover = function (x, y) {
    var btn = this.axisPlus, w = this.wrapEl.clientWidth;
    if (!ChartSet.cached().plusButton || Layout.tool !== "cursor" || x < w - 80) {
      btn.style.display = "none"; return;
    }
    var p = this.series.coordinateToPrice(y);
    if (p === null || p === undefined || !isFinite(p)) { btn.style.display = "none"; return; }
    btn._price = roundPx(p);
    btn.style.display = "block";
    btn.style.right = "66px";
    btn.style.top = Math.max(0, y - 11) + "px";
    btn.title = "Add alert at " + btn._price;
  };

  ChartPane.prototype.addDrawing = function (d) {
    d.id = uid();
    /* an explicitly provided style (e.g. the inline text editor) wins;
       otherwise the tool's remembered default applies */
    d.style = Object.assign(DrawStyle.get(d.type), d.style || {});
    this.state.drawings.push(d);
    this.selectedId = d.id;
    this.persist();
    this.draw();
    this.refreshFmtBar();
  };

  /* chart-style long/short position: p1 = entry, p2 = stop,
     p3 = target. The second click (or drag end) decides which side is
     which: for a long, dragging UP from entry sets the target and the stop
     mirrors below entry; dragging DOWN sets the stop and the target
     mirrors above. Short is the mirror image. qty = position size in the
     instrument's units (ounces for XAUUSD), editable in the Style tab. */
  ChartPane.prototype.buildLongShort = function (tool, p1, endPt) {
    var eP = p1.p, xP = endPt.p, stopP, tgtP;
    if (tool === "long") {
      if (xP >= eP) { tgtP = xP; stopP = eP - (xP - eP); }
      else { stopP = xP; tgtP = eP + (eP - xP); }
    } else {
      if (xP <= eP) { tgtP = xP; stopP = eP + (eP - xP); }
      else { stopP = xP; tgtP = eP - (xP - eP); }
    }
    return { type: tool, p1: p1, p2: { t: endPt.t, p: stopP },
             p3: { t: endPt.t, p: tgtP }, qty: 1 };
  };
  /* target price of a long/short (legacy drawings have no p3 - they keep
     the old 1:1 mirror target) */
  ChartPane.prototype.lsTarget = function (d) {
    if (d.p3 && isFinite(d.p3.p)) return d.p3.p;
    return d.p1.p + (d.p1.p - d.p2.p);
  };

  ChartPane.prototype.removeDrawing = function (id) {
    this.state.drawings = this.state.drawings.filter(function (d) { return d.id !== id; });
    if (this.selectedId === id) this.selectedId = null;
    this.persist();
    this.draw();
    this.refreshFmtBar();
  };

  ChartPane.prototype.getDrawing = function (id) {
    for (var i = 0; i < this.state.drawings.length; i++)
      if (this.state.drawings[i].id === id) return this.state.drawings[i];
    return null;
  };

  /* ------------------------------------------ drawing format toolbar
     chart-style: line color / width / style (solid-dashed-dotted) /
     line transparency + box fill color / fill transparency. Per tool. */
  var DRAW_TOOLS = ["trend", "ray", "hline", "hray", "vline", "rect",
                    "ellipse", "fib", "long", "short", "text"];

  ChartPane.prototype.buildFmtBar = function () {
    var self = this, bar = this.fmtBar;
    if (!bar) return;
    function ctl(cls) { return bar.querySelector(cls); }
    bar.innerHTML =
      '<span class="ff" title="Line color"><input type="color" class="ff-line" value="#2962ff"></span>' +
      '<span class="ff" title="Line width"><select class="ff-width">' +
        "<option>1</option><option>2</option><option>3</option><option>4</option></select></span>" +
      '<span class="ff" title="Line style"><select class="ff-style">' +
        '<option value="solid">Solid</option><option value="dashed">Dashed</option>' +
        '<option value="dotted">Dotted</option></select></span>' +
      '<span class="ff" title="Line transparency"><input type="range" class="ff-lop" min="10" max="100" value="100">' +
        '<em class="ff-lopv">100%</em></span>' +
      '<span class="ff ff-fillrow" title="Fill color"><input type="color" class="ff-fill" value="#2962ff"></span>' +
      '<span class="ff ff-fillrow" title="Fill transparency"><input type="range" class="ff-fop" min="0" max="100" value="12">' +
        '<em class="ff-fopv">12%</em></span>';
    function apply() {
      var key = self._fmtKey || "_def";
      var props = {
        color: ctl(".ff-line").value,
        width: parseInt(ctl(".ff-width").value, 10) || 2,
        lineStyle: ctl(".ff-style").value,
        lineOpacity: (parseInt(ctl(".ff-lop").value, 10) || 100) / 100,
        fillColor: ctl(".ff-fill").value,
        fillOpacity: (parseInt(ctl(".ff-fop").value, 10) || 0) / 100
      };
      DrawStyle.set(key, props);
      var sel = self.selectedId ? self.getDrawing(self.selectedId) : null;
      if (sel) sel.style = Object.assign(DrawStyle.of(sel), props);
      ctl(".ff-lopv").textContent = ctl(".ff-lop").value + "%";
      ctl(".ff-fopv").textContent = ctl(".ff-fop").value + "%";
      self.draw(); self.persist();
    }
    ["change", "input"].forEach(function (ev) {
      bar.addEventListener(ev, function (e) {
        if (e.target && /ff-(line|width|style|lop|fill|fop)/.test(e.target.className || "")) apply();
      });
    });
  };

  ChartPane.prototype.fmtKey = function () {
    var sel = this.selectedId ? this.getDrawing(this.selectedId) : null;
    if (sel) return sel.type;
    return DRAW_TOOLS.indexOf(Layout.tool) >= 0 ? Layout.tool : null;
  };

  /* Legacy entry point - the old fixed format bar is retired; this now
     drives the floating chart-style object toolbar. All call sites stay. */
  ChartPane.prototype.refreshFmtBar = function () {
    if (this.fmtBar) this.fmtBar.style.display = "none";
    this.refreshObjBar();
  };

  /* ================================================== chart-style
     drawing UX : centered text dialog, floating object toolbar,
     single-use tools, inline text editor, lock, per-object settings with
     Style/Text/Coordinates/Visibility tabs, full Fib retracement options. */

  /* -- centered text dialog: replaces the native prompt() (which pops at
     the top of the window) for label/text editing --- */
  var TextDialog = {
    _ov: null,
    open: function (title, label, initial, cb) {
      this.close();
      var ov = document.createElement("div");
      ov.className = "txtdlg-ov";
      ov.innerHTML =
        '<div class="txtdlg" role="dialog" aria-label="' + escHtml(title) + '">' +
        '<div class="th">' + escHtml(title) + '</div>' +
        '<div class="tb"><label>' + escHtml(label) + '</label>' +
        '<input type="text" maxlength="200"></div>' +
        '<div class="tf"><button class="cancel">Cancel</button>' +
        '<button class="ok">OK</button></div></div>';
      document.body.appendChild(ov);
      this._ov = ov;
      var input = ov.querySelector("input"), done = false;
      function finish(ok) {
        if (done) return; done = true;
        var v = ok ? input.value : null;
        TextDialog.close();
        if (cb) cb(v);
      }
      ov.querySelector(".ok").addEventListener("click", function () { finish(true); });
      ov.querySelector(".cancel").addEventListener("click", function () { finish(false); });
      ov.addEventListener("mousedown", function (e) { if (e.target === ov) finish(false); });
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") finish(true);
        else if (e.key === "Escape") { e.stopPropagation(); finish(false); }
      });
      input.value = initial || "";
      setTimeout(function () { input.focus(); input.select(); }, 0);
    },
    close: function () {
      if (this._ov && this._ov.parentNode) this._ov.parentNode.removeChild(this._ov);
      this._ov = null;
    }
  };

  /* -- svg icons for the floating toolbar --- */
  var DICO = {
    gear: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    lock: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>',
    unlock: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>',
    trash: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>'
  };

  var DRAW_LINE_TOOLS = { trend: 1, ray: 1, hline: 1, hray: 1, vline: 1 };
  var DRAW_SHAPES = { rect: 1, ellipse: 1, long: 1, short: 1 };
  var DRAW_TEXTUAL = { text: 1, rect: 1 };   // objects with a real Text tab
  var DRAW_TF_LIST = ["1s", "15s", "1m", "3m", "5m", "15m", "1H", "4H", "D", "W", "M"];

  /* -- fib retracement model: defaults preserve the pre-3.3.8 look --- */
  function fibDefaults() {
    return {
      levels: [
        { v: 0, on: true, color: "#787b86" }, { v: 0.236, on: true, color: "#f23645" },
        { v: 0.382, on: true, color: "#f23645" }, { v: 0.5, on: true, color: "#ff9800" },
        { v: 0.618, on: true, color: "#ff9800" }, { v: 0.786, on: true, color: "#ffeb3b" },
        { v: 1, on: true, color: "#787b86" }, { v: 1.272, on: false, color: "#9c27b0" },
        { v: 1.618, on: false, color: "#9c27b0" }, { v: 2.618, on: false, color: "#9c27b0" }
      ],
      extend: "right", bg: false, bgOpacity: 0.12, reverse: false,
      prices: true, labels: true, labelPos: "right", trendLine: false,
      lineStyle: "solid", useOneColor: true, oneColor: "#2962ff"
    };
  }
  function fibOf(d) {
    var base = fibDefaults();
    var f = Object.assign(base, d.fib || {});
    if (!d.fib || !d.fib.levels) f.levels = base.levels;
    return f;
  }
  function fibLevelPrice(d, f, v) {
    var base = f.reverse ? d.p2.p : d.p1.p;
    var dir = f.reverse ? (d.p1.p - d.p2.p) : (d.p2.p - d.p1.p);
    return base + dir * v;
  }

  /* -- text styling shared by the renderer --- */
  function textFontOf(d) {
    var size = d.fontSize || 14;
    return (d.fontItalic ? "italic " : "") + (d.fontBold ? "bold " : "") + size + "px sans-serif";
  }

  /* IST <-> datetime-local helpers for the Coordinates tab */
  /* datetime-local <-> epoch in the SELECTED chart timezone (default IST).
     datetime-local carries no zone, so we convert via the zone's wall clock.
     The reverse conversion iterates the zone offset (handles DST zones). */
  function _zoneParts(zone, ms) {
    var o = {};
    try {
      new Intl.DateTimeFormat("en-CA", {
        timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hour12: false
      }).formatToParts(new Date(ms)).forEach(function (x) { o[x.type] = x.value; });
    } catch (e) { return null; }
    if (o.hour === "24") o.hour = "00"; // midnight edge in some locales
    return o;
  }
  function epochToZoneLocal(t, zone) {
    zone = zone || selZone();
    var o = _zoneParts(zone, t * 1000);
    if (o && o.year) return o.year + "-" + o.month + "-" + o.day + "T" + o.hour + ":" + o.minute;
    var dt = new Date((t + 19800) * 1000), p = function (n) { return (n < 10 ? "0" : "") + n; };
    return dt.getUTCFullYear() + "-" + p(dt.getUTCMonth() + 1) + "-" + p(dt.getUTCDate()) +
           "T" + p(dt.getUTCHours()) + ":" + p(dt.getUTCMinutes());
  }
  function zoneLocalToEpoch(s, zone) {
    zone = zone || selZone();
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(s || "");
    if (!m) return null;
    if (zone === IST_ZONE) // fast path: IST never has DST
      return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) / 1000 - 19800;
    var guess = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
    for (var i = 0; i < 3; i++) {
      var o = _zoneParts(zone, guess), off;
      if (!o || !o.year) break;
      var wall = Date.UTC(+o.year, +o.month - 1, +o.day, +o.hour, +o.minute);
      off = wall - guess; // zone offset at guess
      var cand = guess - off;
      var o2 = _zoneParts(zone, cand);
      if (o2 && o2.year) {
        var wall2 = Date.UTC(+o2.year, +o2.month - 1, +o2.day, +o2.hour, +o2.minute);
        if (wall2 - cand === off) return Math.floor(cand / 1000);
      }
      guess = cand;
    }
    return Math.floor(guess / 1000);
  }

  /* -- single-use drawing tools: after one drawing, back to the cursor --- */
  ChartPane.prototype.finishToolUse = function () {
    var t = Layout.tool;
    if (t && t !== "cursor" && (DRAW_TOOLS.indexOf(t) >= 0 || t === "eraser")) Layout.setTool("cursor");
  };

  /* -- floating object toolbar -------------------------------------------
     sel mode: near the selected drawing - style + text + settings + lock + delete
     tool mode: top-center - default style for the armed drawing tool */
  ChartPane.prototype.buildObjBar = function () {
    var self = this, bar = this.objBar;
    if (!bar) return;
    /* clicks on the bar must not reach the chart (no deselect, no drawing) */
    bar.addEventListener("mousedown", function (e) { e.stopPropagation(); });
    bar.addEventListener("dblclick", function (e) { e.stopPropagation(); });
  };

  ChartPane.prototype.objMode = function () {
    var sel = this.selectedId ? this.getDrawing(this.selectedId) : null;
    if (sel && Layout.activePane() === this) return { kind: "sel", d: sel };
    var t = (Layout.activePane() === this) ? Layout.tool : null;
    if (t && DRAW_TOOLS.indexOf(t) >= 0) return { kind: "tool", tool: t };
    return null;
  };

  ChartPane.prototype.refreshObjBar = function () {
    var bar = this.objBar;
    if (!bar) return;
    var mode = this.objMode(), self = this;
    if (!mode) { bar.style.display = "none"; this._objMode = null; return; }
    this._objMode = mode.kind + ":" + (mode.d ? mode.d.id : mode.tool);
    var isSel = mode.kind === "sel", d = mode.d || null;
    var tool = isSel ? d.type : mode.tool;
    var st = isSel ? DrawStyle.of(d) : DrawStyle.get(tool);
    function opt(v, t, cur) { return '<option value="' + v + '"' + (String(cur) === String(v) ? " selected" : "") + ">" + t + "</option>"; }
    var h = "";
    if (isSel) h += '<span class="grip" title="Selected drawing">......</span>';
    else h += '<span class="ob" style="font-weight:700" title="Armed tool">' + escHtml(tool) + "</span>";
    h += '<span class="sep"></span>';
    /* line color */
    h += '<span class="ob" title="Line color"><input type="color" data-k="color" value="' +
         escHtml(st.color || "#2962ff") + '"></span>';
    /* line width + style for line-ish tools and shapes */
    if (DRAW_LINE_TOOLS[tool] || DRAW_SHAPES[tool] || tool === "fib") {
      h += '<select data-k="width" title="Line width">' + opt(1, "1px", st.width) + opt(2, "2px", st.width) +
           opt(3, "3px", st.width) + opt(4, "4px", st.width) + "</select>";
      h += '<select data-k="lineStyle" title="Line style">' + opt("solid", "Solid", st.lineStyle) +
           opt("dashed", "Dashed", st.lineStyle) + opt("dotted", "Dotted", st.lineStyle) + "</select>";
    }
    /* fill for shapes */
    if (DRAW_SHAPES[tool]) {
      h += '<span class="ob" title="Fill color"><input type="color" data-k="fillColor" value="' +
           escHtml(st.fillColor || "#2962ff") + '"></span>';
    }
    /* text size for textual tools */
    if (DRAW_TEXTUAL[tool] || tool === "fib") {
      var fsz = isSel ? (d.fontSize || 14) : 14;
      h += '<select data-k="fontSize" title="Text size">' +
           [10, 11, 12, 13, 14, 16, 18, 20, 24].map(function (s) { return opt(s, s + "px", fsz); }).join("") +
           "</select>";
    }
    if (isSel) {
      h += '<span class="sep"></span>';
      if (DRAW_TEXTUAL[tool]) {
        h += '<button class="ob" data-act="text" title="Edit text" style="font-weight:700">T</button>';
      }
      h += '<button class="ob" data-act="settings" title="Settings"' + (d.locked ? " disabled" : "") + ">" + DICO.gear + "</button>";
      h += '<button class="ob' + (d.locked ? " on" : "") + '" data-act="lock" title="' +
           (d.locked ? "Unlock" : "Lock") + '">' + (d.locked ? DICO.lock : DICO.unlock) + "</button>";
      h += '<button class="ob" data-act="del" title="Delete"' + (d.locked ? " disabled" : "") + ">" +
           DICO.trash + "</button>";
    }
    bar.innerHTML = h;
    function applyPatch(patch, fontPatch) {
      if (isSel) {
        if (d.locked) return;
        d.style = Object.assign(DrawStyle.of(d), patch);
        if (fontPatch) for (var k in fontPatch) d[k] = fontPatch[k];
        self.persist(); self.draw(); self.refreshObjBar();
      } else {
        DrawStyle.set(tool, Object.assign({}, patch, fontPatch));
        self.draw();
      }
    }
    bar.querySelectorAll("input[type=color],select").forEach(function (el) {
      el.addEventListener("input", function () {
        var k = el.dataset.k, v = el.value, patch = {}, fp = null;
        if (k === "width" || k === "fontSize") v = parseInt(v, 10) || (k === "width" ? 2 : 14);
        if (k === "fontSize") { fp = { fontSize: v }; }
        else patch[k] = v;
        applyPatch(patch, fp);
      });
      el.addEventListener("change", function () { el.dispatchEvent(new Event("input")); });
    });
    bar.querySelectorAll("button[data-act]").forEach(function (b) {
      b.addEventListener("click", function () {
        var act = b.dataset.act;
        if (act === "text") {
          TextDialog.open("Text", "Text:", d.text || "", function (v) {
            if (v === null || d.locked) return;
            d.text = v; self.persist(); self.draw(); self.refreshObjBar();
          });
        } else if (act === "settings") {
          DrawSettings.open(self, d);
        } else if (act === "lock") {
          d.locked = !d.locked; self.persist(); self.draw(); self.refreshObjBar();
        } else if (act === "del") {
          if (!d.locked) { self.removeDrawing(d.id); }
        }
      });
    });
    bar.style.display = "flex";
    this.placeObjBar();
  };

  ChartPane.prototype.placeObjBar = function () {
    var bar = this.objBar;
    if (!bar || bar.style.display === "none" || !this._objMode) return;
    var mode = this.objMode(), w = this.wrapEl.clientWidth;
    var bw = bar.offsetWidth || 220, bh = bar.offsetHeight || 34;
    var x, y;
    if (mode && mode.kind === "sel" && mode.d) {
      var d = mode.d, pa = this.px(d.p1.t, d.p1.p), pb = d.p2 ? this.px(d.p2.t, d.p2.p) : null;
      var ax = pa ? pa.x : 40, ay = pa ? pa.y : 60;
      if (pa && pb) { ax = (pa.x + pb.x) / 2; ay = Math.min(pa.y, pb.y); }
      x = ax - bw / 2; y = ay - bh - 10;
      if (y < 4) y = ay + 14;
    } else {
      x = w / 2 - bw / 2; y = 6;
    }
    x = Math.max(6, Math.min(w - bw - 6, x));
    y = Math.max(4, y);
    bar.style.display = "flex";
    bar.style.left = Math.round(x) + "px";
    bar.style.top = Math.round(y) + "px";
    bar.style.transform = "none";
  };

  /* -- inline text editor: chart-style text tool ----------------------
     Click places an editable box at the point; type, Enter/click-outside
     commits, Esc cancels. The tool deactivates after one use. */
  ChartPane.prototype.beginTextEdit = function (x, y, d) {
    var self = this;
    this.commitTextEdit(false); // close any stale editor, discarding
    var dd = d ? { t: d.p1.t, p: d.p1.p } : this.toData(x, y);
    if (!dd) return;
    var size = d ? (d.fontSize || 14) : 14;
    var st = d ? DrawStyle.of(d) : DrawStyle.get("text");
    if (!d && st.fontSize) size = st.fontSize; // armed-tool default from the objbar
    var ed = document.createElement("div");
    ed.className = "txtedit";
    ed.innerHTML =
      '<div class="te-bar"><span class="ob" style="font-weight:700">T</span>' +
      '<select data-k="size">' + [10, 11, 12, 13, 14, 16, 18, 20, 24].map(function (s) {
        return '<option value="' + s + '"' + (s === size ? " selected" : "") + ">" + s + "</option>";
      }).join("") + "</select>" +
      '<button class="ob" data-k="bold" title="Bold">B</button>' +
      '<button class="ob" data-k="italic" title="Italic" style="font-style:italic">I</button>' +
      '<input type="color" data-k="color" value="' + escHtml(st.color || "#2962ff") + '" title="Text color">' +
      '<button class="ob" data-k="cancel" title="Cancel">x</button></div>' +
      '<input class="te-input" type="text" placeholder="Add text" maxlength="200">';
    var pw = this.wrapEl.clientWidth;
    ed.style.left = Math.max(4, Math.min(pw - 190, x)) + "px";
    ed.style.top = Math.max(30, y) + "px";
    this.wrapEl.appendChild(ed);
    var input = ed.querySelector(".te-input");
    var state = { size: size, bold: !!(d && d.fontBold), italic: !!(d && d.fontItalic),
                  color: st.color || "#2962ff" };
    function syncToggles() {
      ed.querySelector('[data-k="bold"]').classList.toggle("on", state.bold);
      ed.querySelector('[data-k="italic"]').classList.toggle("on", state.italic);
      input.style.fontSize = state.size + "px";
      input.style.fontWeight = state.bold ? "700" : "400";
      input.style.fontStyle = state.italic ? "italic" : "normal";
      input.style.color = state.color;
    }
    syncToggles();
    ed.querySelector('[data-k="size"]').addEventListener("change", function () {
      state.size = parseInt(this.value, 10) || 14; syncToggles();
    });
    ed.querySelector('[data-k="bold"]').addEventListener("click", function () {
      state.bold = !state.bold; syncToggles(); input.focus();
    });
    ed.querySelector('[data-k="italic"]').addEventListener("click", function () {
      state.italic = !state.italic; syncToggles(); input.focus();
    });
    ed.querySelector('[data-k="color"]').addEventListener("input", function () {
      state.color = this.value; syncToggles();
    });
    ed.querySelector('[data-k="cancel"]').addEventListener("click", function (e) {
      e.stopPropagation(); self.commitTextEdit(false);
    });
    ed.addEventListener("mousedown", function (e) { e.stopPropagation(); });
    input.addEventListener("keydown", function (e) {
      e.stopPropagation();
      if (e.key === "Enter") self.commitTextEdit(true);
      else if (e.key === "Escape") self.commitTextEdit(false);
    });
    input.addEventListener("mousedown", function (e) { e.stopPropagation(); });
    this.textEditing = { editorEl: ed, input: input, p1: dd, d: d || null, state: state };
    if (d && d.text) { input.value = d.text; input.select(); }
    setTimeout(function () { input.focus(); }, 0);
  };

  ChartPane.prototype.commitTextEdit = function (save) {
    var te = this.textEditing;
    if (!te) return;
    this.textEditing = null; // idempotent: re-entrant calls no-op
    var txt = (te.input.value || "").trim();
    if (te.editorEl.parentNode) te.editorEl.parentNode.removeChild(te.editorEl);
    if (save && txt) {
      var s = te.state;
      if (te.d) {
        if (!te.d.locked) {
          te.d.text = txt; te.d.fontSize = s.size; te.d.fontBold = s.bold;
          te.d.fontItalic = s.italic;
          te.d.style = Object.assign(DrawStyle.of(te.d), { color: s.color });
          this.selectedId = te.d.id;
        }
      } else {
        this.addDrawing({ type: "text", p1: te.p1, p2: null, text: txt,
          fontSize: s.size, fontBold: s.bold, fontItalic: s.italic,
          style: Object.assign(DrawStyle.get("text"), { color: s.color }) });
      }
      this.persist(); this.draw();
    }
    this.finishToolUse();
    this.refreshFmtBar();
  };

  /* -- drawing settings panel (chart-style, tabbed) --- */
  var DrawSettings = {
    open: function (pane, d) {
      /* snapshot the drawing so Cancel/x truly reverts every tab's edits
         (applyTab writes onto d when switching tabs, so closing without OK
         must roll those writes back) */
      var snap = JSON.stringify({
        style: d.style, text: d.text, textColor: d.textColor,
        fontSize: d.fontSize, fontBold: d.fontBold, fontItalic: d.fontItalic,
        textHAlign: d.textHAlign, textVAlign: d.textVAlign,
        p1: d.p1, p2: d.p2, showTf: d.showTf, fib: d.fib
      });
      function revert() {
        var o = JSON.parse(snap);
        d.style = o.style; d.text = o.text; d.textColor = o.textColor;
        d.fontSize = o.fontSize; d.fontBold = o.fontBold; d.fontItalic = o.fontItalic;
        d.textHAlign = o.textHAlign; d.textVAlign = o.textVAlign;
        d.p1 = o.p1; d.p2 = o.p2; d.showTf = o.showTf; d.fib = o.fib;
        pane.draw(); pane.refreshFmtBar();
      }
      var tabs = [["style", "Style"]];
      if (DRAW_TEXTUAL[d.type] || d.type === "fib") tabs.push(["text", "Text"]);
      tabs.push(["coord", "Coordinates"], ["vis", "Visibility"]);
      var ov = document.createElement("div");
      ov.className = "dset-ov";
      var names = { trend: "Trend Line", ray: "Ray", hline: "Horizontal Line", hray: "Horizontal Ray",
        vline: "Vertical Line", rect: "Rectangle", ellipse: "Ellipse", fib: "Fib Retracement",
        long: "Long Position", short: "Short Position", text: "Text" };
      ov.innerHTML =
        '<div class="dset" role="dialog"><div class="dh"><span>' + escHtml(names[d.type] || d.type) +
        '</span><button class="x" title="Close">x</button></div>' +
        '<div class="dtabs">' + tabs.map(function (t, i) {
          return '<button data-tab="' + t[0] + '"' + (i === 0 ? ' class="on"' : "") + ">" + t[1] + "</button>";
        }).join("") + '</div><div class="dbody"></div>' +
        '<div class="dfoot"><button class="cancel">Cancel</button><button class="ok">OK</button></div></div>';
      document.body.appendChild(ov);
      var body = ov.querySelector(".dbody");
      function opt(v, t, cur) { return '<option value="' + v + '"' + (String(cur) === String(v) ? " selected" : "") + ">" + t + "</option>"; }
      function colorRow(id, label, val) {
        return '<div class="row"><label>' + label + '</label><input type="color" id="' + id +
               '" value="' + escHtml(val || "#2962ff") + '"></div>';
      }
      function renderTab(tab) {
        var st = DrawStyle.of(d), h = "";
        if (tab === "style") {
          if (d.type === "fib") {
            h += DrawSettings.fibStyleHtml(d, opt);
          } else {
            h += colorRow("ds-color", "Line color", st.color);
            h += '<div class="row"><label>Line width</label><select id="ds-width">' +
                 [1, 2, 3, 4].map(function (w) { return opt(w, w + "px", st.width); }).join("") + "</select></div>";
            h += '<div class="row"><label>Line style</label><select id="ds-lstyle">' +
                 opt("solid", "Solid", st.lineStyle) + opt("dashed", "Dashed", st.lineStyle) +
                 opt("dotted", "Dotted", st.lineStyle) + "</select></div>";
            h += '<div class="row"><label>Transparency</label><input type="range" id="ds-lop" min="10" max="100" value="' +
                 Math.round((st.lineOpacity === undefined ? 1 : st.lineOpacity) * 100) + '"><span class="pct" id="ds-lopv"></span></div>';
            if (DRAW_SHAPES[d.type]) {
              h += colorRow("ds-fill", "Background", st.fillColor);
              h += '<div class="row"><label>Background transparency</label><input type="range" id="ds-fop" min="0" max="100" value="' +
                   Math.round((st.fillOpacity === undefined ? 0.12 : st.fillOpacity) * 100) + '"><span class="pct" id="ds-fopv"></span></div>';
            }
            if (d.type === "text") {
              h += '<div class="row"><label>Text size</label><select id="ds-fsize">' +
                   [10, 11, 12, 13, 14, 16, 18, 20, 24].map(function (s) { return opt(s, s + "px", d.fontSize || 14); }).join("") +
                   "</select></div>";
            }
            if (d.type === "long" || d.type === "short") {
              h += '<div class="row"><label>Qty (oz)</label><input type="number" id="ds-qty" step="0.01" min="0.01" value="' +
                   ((d.qty && isFinite(d.qty) && d.qty > 0) ? d.qty : 1) +
                   '"><span class="pct">Amount = dist x qty</span></div>';
            }
          }
        } else if (tab === "text") {
          h += '<div class="row"><label>Text</label><input type="text" id="ds-text" style="flex:1;min-width:200px" maxlength="200" value="' +
               escHtml(d.text || "") + '"></div>';
          h += '<div class="row"><label>Size</label><select id="ds-fsize">' +
               [10, 11, 12, 13, 14, 16, 18, 20, 24].map(function (s) { return opt(s, s + "px", d.fontSize || 14); }).join("") +
               "</select><span class=\"seg\"><button data-b=\"bold\" class=\"" + (d.fontBold ? "on" : "") +
               '" style="font-weight:700">B</button><button data-b="italic" class="' + (d.fontItalic ? "on" : "") +
               '" style="font-style:italic">I</button></span></div>';
          h += colorRow("ds-tcolor", "Text color", d.textColor || st.color);
          if (d.type === "rect") {
            h += '<div class="row"><label>Horizontal</label><span class="seg">' +
                 ["left", "center", "right"].map(function (a) {
                   return '<button data-ha="' + a + '" class="' + ((d.textHAlign || "center") === a ? "on" : "") + '">' +
                          a[0].toUpperCase() + a.slice(1) + "</button>";
                 }).join("") + "</span></div>";
            h += '<div class="row"><label>Vertical</label><span class="seg">' +
                 ["top", "middle", "bottom"].map(function (a) {
                   return '<button data-va="' + a + '" class="' + ((d.textVAlign || "middle") === a ? "on" : "") + '">' +
                          a[0].toUpperCase() + a.slice(1) + "</button>";
                 }).join("") + "</span></div>";
          }
        } else if (tab === "coord") {
          h += '<div class="row"><label>Point 1 time</label><input type="datetime-local" id="ds-t1" value="' +
               epochToZoneLocal(d.p1.t) + '"><span class="pct">' + escHtml(selZoneShort()) + '</span></div>';
          h += '<div class="row"><label>Point 1 price</label><input type="number" id="ds-p1" step="0.01" value="' +
               d.p1.p.toFixed(2) + '"></div>';
          if (d.p2) {
            h += '<div class="row"><label>Point 2 time</label><input type="datetime-local" id="ds-t2" value="' +
                 epochToZoneLocal(d.p2.t) + '"><span class="pct">' + escHtml(selZoneShort()) + '</span></div>';
            h += '<div class="row"><label>Point 2 price</label><input type="number" id="ds-p2" step="0.01" value="' +
                 d.p2.p.toFixed(2) + '"></div>';
          }
          if ((d.type === "long" || d.type === "short") && d.p3) {
            h += '<div class="row"><label>Target time</label><input type="datetime-local" id="ds-t3" value="' +
                 epochToZoneLocal(d.p3.t) + '"><span class="pct">' + escHtml(selZoneShort()) + '</span></div>';
            h += '<div class="row"><label>Target price</label><input type="number" id="ds-p3" step="0.01" value="' +
                 d.p3.p.toFixed(2) + '"></div>';
          }
        } else if (tab === "vis") {
          var all = !d.showTf;
          h += '<div class="row"><label class="chk"><input type="checkbox" id="ds-visall"' +
               (all ? " checked" : "") + '> Show on all timeframes</label></div>';
          h += '<div class="row" id="ds-vislist" style="' + (all ? "opacity:.4;pointer-events:none" : "") + '">';
          DRAW_TF_LIST.forEach(function (tf) {
            h += '<label class="chk" style="min-width:64px"><input type="checkbox" data-tf="' + tf + '"' +
                 (!d.showTf || d.showTf.indexOf(tf) >= 0 ? " checked" : "") + "> " + tf + "</label>";
          });
          h += "</div>";
        }
        body.innerHTML = h;
        /* wire tab-local controls */
        function pct(id, vid) {
          var r = body.querySelector("#" + id), v = body.querySelector("#" + vid);
          if (r && v) { var f = function () { v.textContent = r.value + "%"; }; r.addEventListener("input", f); f(); }
        }
        pct("ds-lop", "ds-lopv"); pct("ds-fop", "ds-fopv"); pct("ds-fbgop", "ds-fbgopv");
        body.querySelectorAll(".seg button").forEach(function (b) {
          b.addEventListener("click", function () {
            if (b.dataset.b) { b.classList.toggle("on"); return; }
            var sibs = b.parentNode.querySelectorAll("button");
            sibs.forEach(function (s) { s.classList.remove("on"); });
            b.classList.add("on");
          });
        });
        var va = body.querySelector("#ds-visall");
        if (va) va.addEventListener("change", function () {
          body.querySelector("#ds-vislist").style.opacity = va.checked ? ".4" : "1";
          body.querySelector("#ds-vislist").style.pointerEvents = va.checked ? "none" : "auto";
        });
        var oc = body.querySelector("#ds-oneColor");
        if (oc) {
          var tog = function () {
            body.querySelectorAll(".fiblv input[type=color]").forEach(function (c) {
              c.disabled = oc.checked;
            });
          };
          oc.addEventListener("change", tog); tog();
        }
      }
      function collect(tab) { /* writes the visible tab's values onto d*/ }
      var cur = "style";
      renderTab(cur);
      ov.querySelectorAll(".dtabs button").forEach(function (b) {
        b.addEventListener("click", function () {
          DrawSettings.applyTab(pane, d, cur, body);
          cur = b.dataset.tab;
          ov.querySelectorAll(".dtabs button").forEach(function (x) { x.classList.remove("on"); });
          b.classList.add("on");
          renderTab(cur);
        });
      });
      function close() { if (ov.parentNode) ov.parentNode.removeChild(ov); }
      function cancelClose() { revert(); close(); }
      ov.querySelector(".x").addEventListener("click", cancelClose);
      ov.querySelector(".cancel").addEventListener("click", cancelClose);
      ov.addEventListener("mousedown", function (e) { if (e.target === ov) cancelClose(); });
      ov.querySelector(".ok").addEventListener("click", function () {
        if (d.locked) { revert(); close(); return; }
        DrawSettings.applyTab(pane, d, cur, body);
        pane.persist(); pane.draw(); pane.refreshFmtBar();
        close();
      });
    },
    /* full chart-style fib panel (Style tab for fib drawings) */
    fibStyleHtml: function (d, opt) {
      var f = fibOf(d), h = "";
      h += '<div class="row"><label class="chk"><input type="checkbox" id="ds-ftrend"' +
           (f.trendLine ? " checked" : "") + "> Trend line</label></div>";
      h += '<div class="row"><label>Levels line</label><select id="ds-flstyle">' +
           opt("solid", "Solid", f.lineStyle) + opt("dashed", "Dashed", f.lineStyle) +
           opt("dotted", "Dotted", f.lineStyle) + "</select>";
      h += '<div class="row"><label>Extend</label><select id="ds-fextend">' +
           opt("none", "Don't extend", f.extend) + opt("right", "Extend right", f.extend) +
           opt("left", "Extend left", f.extend) + opt("both", "Extend both", f.extend) + "</select></div>";
      f.levels.forEach(function (L, i) {
        h += '<div class="fiblv"><input type="checkbox" data-fi="' + i + '"' + (L.on ? " checked" : "") + ">" +
             '<input type="text" data-fv="' + i + '" value="' + L.v + '">' +
             '<input type="color" data-fc="' + i + '" value="' + escHtml(L.color || "#787b86") + '"></div>';
      });
      h += '<div class="row"><label class="chk"><input type="checkbox" id="ds-oneColor"' +
           (f.useOneColor ? " checked" : "") + '> Use one color</label>' +
           '<input type="color" id="ds-oneC" value="' + escHtml(f.oneColor || "#2962ff") + '"></div>';
      h += '<div class="row"><label class="chk"><input type="checkbox" id="ds-fbg"' +
           (f.bg ? " checked" : "") + "> Background</label>" +
           '<input type="range" id="ds-fbgop" min="0" max="100" value="' + Math.round(f.bgOpacity * 100) +
           '"><span class="pct" id="ds-fbgopv"></span></div>';
      h += '<div class="row"><label class="chk"><input type="checkbox" id="ds-frev"' +
           (f.reverse ? " checked" : "") + "> Reverse</label>" +
           '<label class="chk"><input type="checkbox" id="ds-fprices"' + (f.prices ? " checked" : "") +
           "> Prices</label>" +
           '<label class="chk"><input type="checkbox" id="ds-flabels"' + (f.labels ? " checked" : "") +
           "> Levels</label></div>";
      h += '<div class="row"><label>Labels</label><select id="ds-flpos">' +
           opt("right", "Right", f.labelPos) + opt("left", "Left", f.labelPos) + "</select></div>";
      return h;
    },
    /* read the currently rendered tab back onto the drawing */
    applyTab: function (pane, d, tab, body) {
      function val(id) { var e = body.querySelector("#" + id); return e ? e.value : null; }
      function chk(id) { var e = body.querySelector("#" + id); return e ? e.checked : false; }
      var st = DrawStyle.of(d);
      if (tab === "style") {
        if (d.type === "fib") {
          var f = fibOf(d);
          f.trendLine = chk("ds-ftrend");
          f.lineStyle = val("ds-flstyle") || "solid";
          f.extend = val("ds-fextend") || "right";
          f.levels.forEach(function (L, i) {
            var c = body.querySelector('[data-fi="' + i + '"]'),
                v = body.querySelector('[data-fv="' + i + '"]'),
                cc = body.querySelector('[data-fc="' + i + '"]');
            if (c) L.on = c.checked;
            if (v && isFinite(parseFloat(v.value))) L.v = parseFloat(v.value);
            if (cc) L.color = cc.value;
          });
          f.useOneColor = chk("ds-oneColor");
          f.oneColor = val("ds-oneC") || "#2962ff";
          f.bg = chk("ds-fbg");
          f.bgOpacity = Math.max(0, Math.min(100, parseInt(val("ds-fbgop"), 10) || 0)) / 100;
          f.reverse = chk("ds-frev");
          f.prices = chk("ds-fprices");
          f.labels = chk("ds-flabels");
          f.labelPos = val("ds-flpos") || "right";
          d.fib = f;
        } else {
          st.color = val("ds-color") || st.color;
          st.width = parseInt(val("ds-width"), 10) || st.width;
          st.lineStyle = val("ds-lstyle") || st.lineStyle;
          st.lineOpacity = Math.max(10, Math.min(100, parseInt(val("ds-lop"), 10) || 100)) / 100;
          if (DRAW_SHAPES[d.type]) {
            st.fillColor = val("ds-fill") || st.fillColor;
            st.fillOpacity = Math.max(0, Math.min(100, parseInt(val("ds-fop"), 10) || 0)) / 100;
          }
          if (d.type === "text") d.fontSize = parseInt(val("ds-fsize"), 10) || d.fontSize || 14;
          if (d.type === "long" || d.type === "short") {
            var qv = parseFloat(val("ds-qty"));
            if (isFinite(qv) && qv > 0) d.qty = qv;
          }
          d.style = st;
        }
      } else if (tab === "text") {
        var t = val("ds-text");
        if (t !== null) d.text = t;
        d.fontSize = parseInt(val("ds-fsize"), 10) || d.fontSize || 14;
        var bb = body.querySelector('[data-b="bold"]'), bi = body.querySelector('[data-b="italic"]');
        d.fontBold = !!(bb && bb.classList.contains("on"));
        d.fontItalic = !!(bi && bi.classList.contains("on"));
        d.textColor = val("ds-tcolor") || d.textColor;
        var ha = body.querySelector("[data-ha].on"), va2 = body.querySelector("[data-va].on");
        if (ha) d.textHAlign = ha.dataset.ha;
        if (va2) d.textVAlign = va2.dataset.va;
      } else if (tab === "coord") {
        var t1 = zoneLocalToEpoch(val("ds-t1")), p1 = parseFloat(val("ds-p1"));
        if (t1 !== null) d.p1.t = t1;
        if (isFinite(p1)) d.p1.p = p1;
        if (d.p2) {
          var t2 = zoneLocalToEpoch(val("ds-t2")), p2 = parseFloat(val("ds-p2"));
          if (t2 !== null) d.p2.t = t2;
          if (isFinite(p2)) d.p2.p = p2;
        }
        if ((d.type === "long" || d.type === "short") && d.p3) {
          var t3 = zoneLocalToEpoch(val("ds-t3")), p3 = parseFloat(val("ds-p3"));
          if (t3 !== null) d.p3.t = t3;
          if (isFinite(p3)) d.p3.p = p3;
        }
      } else if (tab === "vis") {
        var all = chk("ds-visall");
        if (all) { d.showTf = null; }
        else {
          var sel = [];
          body.querySelectorAll("[data-tf]").forEach(function (c) { if (c.checked) sel.push(c.dataset.tf); });
          d.showTf = sel.length ? sel : null;
        }
      }
    }
  };

  ChartPane.prototype.hitTest = function (x, y) {
    var ds = this.state.drawings, tf = this.state.tf;
    this._hitPart = null; /* set by the long/short branch when it hits*/
    for (var i = ds.length - 1; i >= 0; i--) {
      var d = ds[i], a, b, pa, pb;
      /* hidden on this timeframe => not selectable either */
      if (d.showTf && d.showTf.length && d.showTf.indexOf(tf) < 0) continue;
      if (d.type === "hline") {
        pa = this.px(d.p1.t, d.p1.p);
        if (pa && Math.abs(y - pa.y) < 6) return d;
      } else if (d.type === "trend") {
        pa = this.px(d.p1.t, d.p1.p); pb = this.px(d.p2.t, d.p2.p);
        if (pa && pb && distToSeg(x, y, pa.x, pa.y, pb.x, pb.y) < 6) return d;
      } else if (d.type === "rect") {
        pa = this.px(d.p1.t, d.p1.p); pb = this.px(d.p2.t, d.p2.p);
        if (pa && pb && x >= Math.min(pa.x, pb.x) - 4 && x <= Math.max(pa.x, pb.x) + 4 &&
            y >= Math.min(pa.y, pb.y) - 4 && y <= Math.max(pa.y, pb.y) + 4) return d;
      } else if (d.type === "text") {
        pa = this.px(d.p1.t, d.p1.p);
        if (pa) {
          /* match the renderer's box: fillRect(pa.x-2, pa.y-th+4, tw+10, th) */
          this.ctx.font = textFontOf(d);
          var tw2 = this.ctx.measureText(d.text || "").width;
          var th2 = (d.fontSize || 14) + 8;
          if (x >= pa.x - 6 && x <= pa.x - 2 + tw2 + 14 &&
              y >= pa.y - th2 && y <= pa.y + 8) return d;
        }
      } else if (d.type === "ray") {
        pa = this.px(d.p1.t, d.p1.p); pb = this.px(d.p2.t, d.p2.p);
        if (pa && pb) {
          var rw = this.wrapEl.clientWidth;
          var rdx = pb.x - pa.x, rdy = pb.y - pa.y;
          var rey = pa.y + (rdx ? rdy * (rw - pa.x) / rdx : 0);
          if (distToSeg(x, y, pa.x, pa.y, rw, rey) < 6) return d;
        }
      } else if (d.type === "hray") {
        pa = this.px(d.p1.t, d.p1.p);
        if (pa && Math.abs(y - pa.y) < 6 && x >= pa.x - 4) return d;
      } else if (d.type === "vline") {
        pa = this.px(d.p1.t, d.p1.p);
        if (pa && Math.abs(x - pa.x) < 6) return d;
      } else if (d.type === "ellipse") {
        pa = this.px(d.p1.t, d.p1.p); pb = this.px(d.p2.t, d.p2.p);
        if (pa && pb) {
          var cx = (pa.x + pb.x) / 2, cy = (pa.y + pb.y) / 2;
          var rx = Math.abs(pb.x - pa.x) / 2, ry = Math.abs(pb.y - pa.y) / 2;
          if (rx > 4 && ry > 4) {
            var v = ((x - cx) / rx) * ((x - cx) / rx) + ((y - cy) / ry) * ((y - cy) / ry);
            if (Math.abs(v - 1) < 0.3) return d;
          } else if (Math.abs(x - cx) < 8 && Math.abs(y - cy) < 8) return d;
        }
      } else if (d.type === "fib") {
        pa = this.px(d.p1.t, d.p1.p); pb = this.px(d.p2.t, d.p2.p);
        if (pa && pb) {
          var HF = fibOf(d);
          var fw2 = this.wrapEl.clientWidth;
          /* match the renderer's horizontal extent for the extend setting */
          var lx0, lx1;
          if (HF.extend === "left") { lx0 = 0; lx1 = Math.max(pa.x, pb.x); }
          else if (HF.extend === "both") { lx0 = 0; lx1 = fw2; }
          else if (HF.extend === "none") { lx0 = Math.min(pa.x, pb.x); lx1 = Math.max(pa.x, pb.x); }
          else { lx0 = Math.min(pa.x, pb.x); lx1 = fw2; } /* "right" (default)*/
          for (var fi = 0; fi < HF.levels.length; fi++) {
            if (!HF.levels[fi].on) continue;
            var fy = this.series.priceToCoordinate(fibLevelPrice(d, HF, HF.levels[fi].v));
            if (fy !== null && fy !== undefined && Math.abs(y - fy) < 6 && x >= lx0 - 4 && x <= lx1 + 4) return d;
          }
        }
      } else if (d.type === "long" || d.type === "short") {
        /* chart-style position tool: grabbable entry / stop / target
           lines (part recorded in _hitPart for the drag handler), otherwise
           the whole figure moves*/
        pa = this.px(d.p1.t, d.p1.p); pb = this.px(d.p2.t, d.p2.p);
        var pc = (d.p3 && isFinite(d.p3.p)) ? this.px(d.p3.t, d.p3.p) : null;
        if (pa && pb) {
          var lqx0 = Math.min(pa.x, pb.x, pc ? pc.x : 1e18) - 6;
          var lqx1 = Math.max(pa.x, pb.x, pc ? pc.x : -1e18) + 96;
          var lqy0 = Math.min(pa.y, pb.y, pc ? pc.y : 1e18) - 6;
          var lqy1 = Math.max(pa.y, pb.y, pc ? pc.y : -1e18) + 6;
          if (x >= lqx0 && x <= lqx1 && y >= lqy0 && y <= lqy1) {
            var near = function (yy) { return Math.abs(y - yy) <= 7; };
            if (near(pa.y)) this._hitPart = "entry";
            else if (near(pb.y)) this._hitPart = "stop";
            else if (pc && near(pc.y)) this._hitPart = "target";
            else this._hitPart = "move";
            return d;
          }
        }
      }
    }
    return null;
  };

  function distToSeg(px, py, x1, y1, x2, y2) {
    var dx = x2 - x1, dy = y2 - y1;
    var l2 = dx * dx + dy * dy;
    var t = l2 ? ((px - x1) * dx + (py - y1) * dy) / l2 : 0;
    t = Math.max(0, Math.min(1, t));
    var cx = x1 + t * dx, cy = y1 + t * dy;
    return Math.hypot(px - cx, py - cy);
  }

  ChartPane.prototype.draw = function () {
    var ctx = this.ctx, self = this;
    var w = this.wrapEl.clientWidth, h = this.wrapEl.clientHeight;
    if (!w || !h) return;
    ctx.clearRect(0, 0, w, h);
    /* Viewport culling: skip overlay elements fully outside the visible time
       range. Dense indicators (e.g. 70k+ one-bar ORB segments) would otherwise
       stroke every segment on every pan frame and jank. Nothing visible is
       ever skipped - off-screen elements are invisible by definition.*/
    var vt0 = -1e18, vt1 = 1e18;
    try {
      var tsa = this.chart.timeScale();
      /* Primary: derive the visible time bounds from the visible logical
         range mapped onto seriesData. (coordinateToTime at the overlay's
         right edge returns null - the overlay wrapper is wider than the
         chart plot area because it spans the price-scale zone - which used
         to silently disable culling entirely, leaving off-screen BUY/SELL
         labels pinned to the screen edge by the box clamp below.)*/
      var lr = tsa.getVisibleLogicalRange ? tsa.getVisibleLogicalRange() : null;
      var nsd = (this.seriesData || []).length;
      if (lr && nsd > 0) {
        var li0 = Math.max(0, Math.ceil(lr.from));
        var li1 = Math.min(nsd - 1, Math.floor(lr.to));
        if (li0 <= li1) {
          vt0 = this.seriesData[li0].time;
          vt1 = this.seriesData[li1].time;
        }
      }
      if (vt0 === -1e18) {
        /* Fallback: probe leftwards from the overlay edge until the
           coordinate projects onto the plot area.*/
        var ca = tsa.coordinateToTime(0), cb = null, px = w;
        while (px > 0 && cb == null) { cb = tsa.coordinateToTime(px); px -= 4; }
        if (ca != null && cb != null) { vt0 = Math.min(ca, cb); vt1 = Math.max(ca, cb); }
      }
    } catch (e) { /* no culling if the scale is not ready*/ }
    function inView(E) {
      var t0 = E.t0 != null ? E.t0 : (E.t != null ? E.t : -1e18);
      /* Open-ended levels (t0 set, no t1) extend right from t0 - they are
         visible whenever t0 is not beyond the right edge.*/
      var t1 = E.t1 != null ? E.t1 : (E.t0 != null ? 1e18 : t0);
      /* overlay times are real; the viewport is in slot space. */
      if (self.state.mode === "renko") { t0 = self.renkoSlot(t0); t1 = self.renkoSlot(t1); }
      return t1 >= vt0 && t0 <= vt1;
    }
    /* peak lines draw AFTER labels so they stay visible */
    /* fills paint first, so lines draw on top of them. */
    (this.indAreas || []).forEach(function (A) { self.drawIndArea(A); });
    this.indLines.forEach(function (L) { if (!L.is_peak && inView(L)) self.drawIndLine(L); });
    (this.indBoxes || []).forEach(function (B) { if (inView(B)) self.drawIndBox(B); });
    this.drawIndLabels(vt0, vt1);
    this.indLines.forEach(function (L) { if (L.is_peak && inView(L)) self.drawIndLine(L); });
    this.drawAlertLines();
    /* USD news markers - pure visual overlay (vertical dashed
       lines), no interaction with signals/alerts/replay.*/
    this.drawNews(vt0, vt1);
    if (typeof Trade !== "undefined" && Trade.visible) Trade.drawOn(this);
    /* visibility tab: a drawing can be hidden on chosen timeframes */
    this.state.drawings.forEach(function (d) {
      if (d.showTf && d.showTf.length && d.showTf.indexOf(self.state.tf) < 0) return;
      self.drawOne(d, d.id === self.selectedId);
    });
    /* keep the floating object toolbar glued near its drawing while panning */
    this.placeObjBar();
    if (this.pending && this.pending.px !== undefined) {
      var p1 = this.px(this.pending.p1.t, this.pending.p1.p);
      if (p1) {
        var pst = DrawStyle.get(this.pending.type);
        applyDrawStroke(ctx, pst, pst.color, 1);
        ctx.setLineDash([4, 4]); /* rubber-band preview always dashed*/
        ctx.beginPath();
        var pt = this.pending.type;
        if (pt === "long" || pt === "short") {
          /* live position-tool preview: the real boxes/labels, not a line */
          var pmd = this.toData(this.pending.px, this.pending.py);
          if (pmd) this.drawLongShort(ctx, this.buildLongShort(pt, this.pending.p1, pmd), false);
        } else if (pt === "trend" || pt === "ray" || pt === "fib") {
          ctx.moveTo(p1.x, p1.y); ctx.lineTo(this.pending.px, this.pending.py);
        } else if (pt === "ellipse") {
          ctx.ellipse((p1.x + this.pending.px) / 2, (p1.y + this.pending.py) / 2,
                      Math.abs(this.pending.px - p1.x) / 2, Math.abs(this.pending.py - p1.y) / 2, 0, 0, 7);
        } else {
          ctx.strokeRect(p1.x, p1.y, this.pending.px - p1.x, this.pending.py - p1.y);
        }
        ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1;
      }
    }
  };

  /* requestAnimationFrame-throttled redraw: mousemove drags coalesce to one
     paint per frame so dragging drawings never janks; state still persists
     only on mouseup.*/
  ChartPane.prototype.drawSoon = function () {
    if (this._drawRaf) return;
    var self = this;
    this._drawRaf = requestAnimationFrame(function () {
      self._drawRaf = null;
      self.draw();
    });
  };

  ChartPane.prototype.drawOne = function (d, sel) {
    var ctx = this.ctx;
    var st = DrawStyle.of(d);
    var col = sel ? SEL_COLOR : (st.color || DRAW_COLOR);
    ctx.fillStyle = col;
    var a, b, pa, pb;
    if (d.type === "trend") {
      pa = this.px(d.p1.t, d.p1.p); pb = this.px(d.p2.t, d.p2.p);
      if (!pa || !pb) return;
      applyDrawStroke(ctx, st, col, sel ? 1.4 : 1);
      ctx.beginPath(); ctx.moveTo(pa.x, pa.y); ctx.lineTo(pb.x, pb.y); ctx.stroke();
      ctx.globalAlpha = 1; ctx.setLineDash([]);
      dot(ctx, pa.x, pa.y, col); dot(ctx, pb.x, pb.y, col);
    } else if (d.type === "hline") {
      pa = this.px(d.p1.t, d.p1.p);
      if (!pa) return;
      var w = this.wrapEl.clientWidth;
      applyDrawStroke(ctx, st, col, sel ? 1.4 : 1);
      ctx.beginPath(); ctx.moveTo(0, pa.y); ctx.lineTo(w, pa.y); ctx.stroke();
      ctx.globalAlpha = 1; ctx.setLineDash([]);
      if (sel) dot(ctx, 5, pa.y, col);   // grab handle when selected
    } else if (d.type === "rect") {
      pa = this.px(d.p1.t, d.p1.p); pb = this.px(d.p2.t, d.p2.p);
      if (!pa || !pb) return;
      var x = Math.min(pa.x, pb.x), y = Math.min(pa.y, pb.y);
      var rw = Math.abs(pb.x - pa.x), rh = Math.abs(pb.y - pa.y);
      applyDrawFill(ctx, st);
      ctx.fillRect(x, y, rw, rh);
      applyDrawStroke(ctx, st, col, sel ? 1.4 : 1);
      ctx.strokeRect(x, y, rw, rh);
      ctx.globalAlpha = 1; ctx.setLineDash([]);
      if (sel) {   // corner handles when selected
        ctx.fillStyle = col;
        [[x, y], [x + rw, y], [x, y + rh], [x + rw, y + rh]].forEach(function (pt) {
          ctx.fillRect(pt[0] - 3, pt[1] - 3, 6, 6);
        });
      }
      /* chart-style: text drawn INSIDE the box, honoring the Text tab
         alignment settings (horizontal + vertical)*/
      if (d.text) {
        ctx.font = textFontOf(d);
        ctx.globalAlpha = 1;
        ctx.fillStyle = d.textColor || st.color || DRAW_COLOR;
        var twa = ctx.measureText(d.text).width, tsz = d.fontSize || 14;
        var ha = d.textHAlign || "center", va = d.textVAlign || "middle";
        var txp = ha === "left" ? x + 6 : ha === "right" ? x + rw - twa - 6 : x + (rw - twa) / 2;
        var typ = va === "top" ? y + tsz + 4 : va === "bottom" ? y + rh - 6 : y + rh / 2 + tsz / 2 - 2;
        /* clip to the box so long text doesn't spill over the chart */
        ctx.save();
        ctx.beginPath(); ctx.rect(x + 2, y + 2, Math.max(0, rw - 4), Math.max(0, rh - 4)); ctx.clip();
        ctx.fillText(d.text, txp, typ);
        ctx.restore();
        ctx.globalAlpha = 1;
      }
    } else if (d.type === "text") {
      pa = this.px(d.p1.t, d.p1.p);
      if (!pa) return;
      ctx.font = textFontOf(d);
      ctx.globalAlpha = 1;
      var tw = ctx.measureText(d.text || "").width;
      var th = (d.fontSize || 14) + 8;
      ctx.fillStyle = "rgba(20,24,35,0.9)";
      ctx.fillRect(pa.x - 2, pa.y - th + 4, tw + 10, th);
      ctx.globalAlpha = (st.lineOpacity === undefined ? 1 : st.lineOpacity);
      ctx.fillStyle = st.color || DRAW_COLOR;
      ctx.fillText(d.text || "", pa.x + 3, pa.y - 2);
      ctx.globalAlpha = 1;
      if (sel) {
        ctx.strokeStyle = col; ctx.setLineDash([4, 3]); ctx.lineWidth = 1;
        ctx.strokeRect(pa.x - 2, pa.y - th + 4, tw + 10, th);
        ctx.setLineDash([]);
      }
    } else if (d.type === "ray") {
      pa = this.px(d.p1.t, d.p1.p); pb = this.px(d.p2.t, d.p2.p);
      if (!pa || !pb) return;
      var rw = this.wrapEl.clientWidth;
      var rdx = pb.x - pa.x, rdy = pb.y - pa.y;
      var ey = pa.y + (rdx ? rdy * (rw - pa.x) / rdx : 0);
      applyDrawStroke(ctx, st, col, sel ? 1.4 : 1);
      ctx.beginPath(); ctx.moveTo(pa.x, pa.y); ctx.lineTo(rw, ey); ctx.stroke();
      ctx.globalAlpha = 1; ctx.setLineDash([]);
      dot(ctx, pa.x, pa.y, col); dot(ctx, pb.x, pb.y, col);
    } else if (d.type === "hray") {
      pa = this.px(d.p1.t, d.p1.p);
      if (!pa) return;
      applyDrawStroke(ctx, st, col, sel ? 1.4 : 1);
      ctx.beginPath(); ctx.moveTo(pa.x, pa.y); ctx.lineTo(this.wrapEl.clientWidth, pa.y); ctx.stroke();
      ctx.globalAlpha = 1; ctx.setLineDash([]);
      dot(ctx, pa.x, pa.y, col);
    } else if (d.type === "vline") {
      pa = this.px(d.p1.t, d.p1.p);
      if (!pa) return;
      applyDrawStroke(ctx, st, col, sel ? 1.4 : 1);
      ctx.beginPath(); ctx.moveTo(pa.x, 0); ctx.lineTo(pa.x, this.wrapEl.clientHeight); ctx.stroke();
      ctx.globalAlpha = 1; ctx.setLineDash([]);
      if (sel) dot(ctx, pa.x, 5, col);   // grab handle when selected
    } else if (d.type === "ellipse") {
      pa = this.px(d.p1.t, d.p1.p); pb = this.px(d.p2.t, d.p2.p);
      if (!pa || !pb) return;
      ctx.beginPath();
      ctx.ellipse((pa.x + pb.x) / 2, (pa.y + pb.y) / 2,
                  Math.abs(pb.x - pa.x) / 2, Math.abs(pb.y - pa.y) / 2, 0, 0, 7);
      applyDrawFill(ctx, st);
      ctx.fill();
      applyDrawStroke(ctx, st, col, sel ? 1.4 : 1);
      ctx.stroke();
      ctx.globalAlpha = 1; ctx.setLineDash([]);
      dot(ctx, pa.x, pa.y, col); dot(ctx, pb.x, pb.y, col);
    } else if (d.type === "fib") {
      pa = this.px(d.p1.t, d.p1.p); pb = this.px(d.p2.t, d.p2.p);
      if (!pa || !pb) return;
      var F = fibOf(d);
      var fw = this.wrapEl.clientWidth;
      /* horizontal extent of the level lines */
      var lx0, lx1;
      if (F.extend === "left") { lx0 = 0; lx1 = Math.max(pa.x, pb.x); }
      else if (F.extend === "both") { lx0 = 0; lx1 = fw; }
      else if (F.extend === "none") { lx0 = Math.min(pa.x, pb.x); lx1 = Math.max(pa.x, pb.x); }
      else { lx0 = Math.min(pa.x, pb.x); lx1 = fw; } /* "right" (default)*/
      /* optional trend line p1 -> p2 */
      if (F.trendLine) {
        applyDrawStroke(ctx, st, "#787b86", 0.8);
        ctx.setLineDash([5, 4]);
        ctx.beginPath(); ctx.moveTo(pa.x, pa.y); ctx.lineTo(pb.x, pb.y); ctx.stroke();
        ctx.setLineDash([]);
      }
      /* optional background between the 0 and 1 levels */
      if (F.bg) {
        var bg0 = this.series.priceToCoordinate(fibLevelPrice(d, F, 0));
        var bg1 = this.series.priceToCoordinate(fibLevelPrice(d, F, 1));
        if (bg0 !== null && bg0 !== undefined && bg1 !== null && bg1 !== undefined) {
          ctx.globalAlpha = F.bgOpacity;
          ctx.fillStyle = st.fillColor || "#2962ff";
          ctx.fillRect(lx0, Math.min(bg0, bg1), lx1 - lx0, Math.abs(bg1 - bg0));
          ctx.globalAlpha = 1;
        }
      }
      ctx.font = textFontOf({ fontSize: 11 });
      for (var fi = 0; fi < F.levels.length; fi++) {
        var FL = F.levels[fi];
        if (!FL.on) continue;
        var fpr = fibLevelPrice(d, F, FL.v);
        var fy = this.series.priceToCoordinate(fpr);
        if (fy === null || fy === undefined) continue;
        var fcol = F.useOneColor ? (F.oneColor || st.color) : (FL.color || st.color);
        ctx.strokeStyle = fcol;
        ctx.globalAlpha = (st.lineOpacity === undefined ? 1 : st.lineOpacity);
        ctx.lineWidth = Math.max(1, (st.width || 2) * 0.7);
        ctx.setLineDash(drawDashOf(F.lineStyle || "solid"));
        ctx.beginPath(); ctx.moveTo(lx0, fy); ctx.lineTo(lx1, fy); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 1;
        /* labels: level value and/or price, left or right of the zone */
        var parts = [];
        if (F.labels) parts.push(FL.v === 0 ? "0" : FL.v === 1 ? "1" : String(FL.v));
        if (F.prices) parts.push(fpr.toFixed(2));
        if (parts.length) {
          var flbl = parts.join("  ");
          var ftw = ctx.measureText(flbl).width;
          var bx = F.labelPos === "left" ? lx0 + 2 : lx1 - ftw - 10;
          ctx.fillStyle = "rgba(19,23,34,0.88)";
          ctx.fillRect(bx, fy - 14, ftw + 8, 14);
          ctx.fillStyle = fcol;
          ctx.fillText(flbl, bx + 4, fy - 3);
        }
      }
      ctx.globalAlpha = 1; ctx.setLineDash([]);
      dot(ctx, pa.x, pa.y, col); dot(ctx, pb.x, pb.y, col);
    } else if (d.type === "long" || d.type === "short") {
      this.drawLongShort(ctx, d, sel);
    }
    /* double-click text label attached to any drawing (rect/text draw their
       own text above)*/
    if (d.text && d.type !== "text" && d.type !== "rect") {
      var ap = this.px(d.p1.t, d.p1.p);
      if (ap) {
        ctx.font = textFontOf(d);
        ctx.globalAlpha = 1;
        var atw = ctx.measureText(d.text).width;
        ctx.fillStyle = "rgba(20,24,35,0.9)";
        ctx.fillRect(ap.x + 6, ap.y - 20, atw + 10, 20);
        ctx.fillStyle = d.textColor || col;
        ctx.fillText(d.text, ap.x + 11, ap.y - 6);
      }
    }
    ctx.setLineDash([]); ctx.globalAlpha = 1;
  };

  function dot(ctx, x, y, col) {
    ctx.fillStyle = col;
    ctx.beginPath(); ctx.arc(x, y, 3, 0, 7); ctx.fill();
  }

  function rrect(ctx, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  /* chart-style Long/Short position tool green reward box,
     red risk box, dashed blue entry line, and live-computed labels -
     "Target: 4.210 (0.099%) 421.0, Amount: 1060.92" (distance, %, points,
     distance x qty), "Stop: ...", "Open PnL: ..., Qty: ..." and
     "Risk/reward ratio: ...". Open PnL uses the latest bid while the pane is
     live. Works for any drawing object shaped {p1,p2,p3?,qty?}, so the
     creation rubber-band preview reuses it too.*/
  /* chart-style long/short position
     target/profit box is ALWAYS green, stop/risk box is ALWAYS red
       (a short position paints its target box green, not red);
     all text lives INSIDE the boxes / on the entry line - nothing floats
       over the candles;
     entry/stop/target resize independently: dragging one line only moves
       that line's price (see the mousemove drag handler).*/
  ChartPane.prototype.drawLongShort = function (ctx, d, sel) {
    var st = DrawStyle.of(d);
    var isLong = d.type === "long";
    var entryP = d.p1.p, stopP = d.p2.p, tgtP = this.lsTarget(d);
    var G = "#26a69a", R = "#ef5350", B = "#2962ff";
    var pa = this.px(d.p1.t, entryP), pb = this.px(d.p2.t, stopP);
    if (!pa || !pb) return;
    var ptg = this.series.priceToCoordinate(tgtP);
    if (ptg === null || ptg === undefined) return; /* target off the price scale*/
    var x0 = Math.min(pa.x, pb.x), x1 = Math.max(pa.x, pb.x) + 90;
    var entryY = pa.y, stopY = pb.y;
    var fillA = st.fillOpacity === undefined ? 0.12 : st.fillOpacity;
    /* profit box (entry -> target): always green */
    var ry0 = Math.min(entryY, ptg), ry1 = Math.max(entryY, ptg);
    ctx.globalAlpha = fillA; ctx.fillStyle = G;
    ctx.fillRect(x0, ry0, x1 - x0, Math.max(ry1 - ry0, 2));
    ctx.globalAlpha = 0.85; ctx.strokeStyle = G; ctx.lineWidth = 1;
    ctx.strokeRect(x0 + 0.5, ry0 + 0.5, x1 - x0 - 1, Math.max(ry1 - ry0, 2) - 1);
    /* risk box (entry -> stop): always red */
    var ky0 = Math.min(entryY, stopY), ky1 = Math.max(entryY, stopY);
    ctx.globalAlpha = fillA; ctx.fillStyle = R;
    ctx.fillRect(x0, ky0, x1 - x0, Math.max(ky1 - ky0, 2));
    ctx.globalAlpha = 0.85; ctx.strokeStyle = R;
    ctx.strokeRect(x0 + 0.5, ky0 + 0.5, x1 - x0 - 1, Math.max(ky1 - ky0, 2) - 1);
    ctx.globalAlpha = 1;
    /* lines: dashed blue entry, green target, red stop */
    ctx.setLineDash([5, 4]); ctx.strokeStyle = B; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x0, entryY); ctx.lineTo(x1, entryY); ctx.stroke();
    ctx.setLineDash([]);
    ctx.strokeStyle = G;
    ctx.beginPath(); ctx.moveTo(x0, ptg); ctx.lineTo(x1, ptg); ctx.stroke();
    ctx.strokeStyle = R;
    ctx.beginPath(); ctx.moveTo(x0, stopY); ctx.lineTo(x1, stopY); ctx.stroke();
    /* selected: dashed outline around the whole figure */
    if (sel) {
      ctx.setLineDash([4, 3]); ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 1;
      ctx.strokeRect(x0 - 4.5, Math.min(ry0, ky0) - 4.5, x1 - x0 + 9,
                     Math.max(ry1, ky1) - Math.min(ry0, ky0) + 9);
      ctx.setLineDash([]);
    }
    /* -- labels: inside the boxes, right-aligned, clipped to the box ---- */
    var risk = Math.abs(entryP - stopP), reward = Math.abs(tgtP - entryP);
    var rr = risk > 1e-9 ? reward / risk : 0;
    var qty = (d.qty && isFinite(d.qty) && d.qty > 0) ? d.qty : 1;
    function boxText(y0, y1, rows) {
      var h = y1 - y0;
      if (h < 15) return;
      ctx.save();
      ctx.beginPath();
      ctx.rect(x0 + 3, y0 + 2, Math.max(0, x1 - x0 - 6), Math.max(0, h - 4));
      ctx.clip();
      ctx.font = "600 11px sans-serif";
      ctx.textAlign = "right"; ctx.textBaseline = "top";
      ctx.shadowColor = "rgba(0,0,0,0.75)"; ctx.shadowBlur = 3;
      ctx.fillStyle = "#ffffff";
      var lh = 14, maxRows = Math.min(rows.length, Math.max(1, Math.floor((h - 6) / lh)));
      var sy = y0 + Math.max(3, (h - maxRows * lh) / 2);
      for (var i = 0; i < maxRows; i++) ctx.fillText(rows[i], x1 - 6, sy + i * lh);
      ctx.restore();
      ctx.shadowBlur = 0; ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
    }
    function moneyRows(title, price, dist) {
      var pct = (dist / Math.abs(entryP) * 100).toFixed(2);
      return [title, price.toFixed(2),
              dist.toFixed(1) + " pts (" + pct + "%)",
              "Amount: " + (dist * qty).toFixed(2)];
    }
    boxText(ry0, ry1, moneyRows("Target", tgtP, reward));
    boxText(ky0, ky1, moneyRows("Stop", stopP, risk));
    /* -- entry-line summary: centered pills on the entry line ---- */
    var liveP = (this._liveBid !== undefined && this._liveBid !== null) ? this._liveBid : null;
    var pnlTxt = "Open PnL: n/a";
    if (liveP !== null && isFinite(liveP)) {
      var pnl = (liveP - entryP) * (isLong ? 1 : -1) * qty;
      pnlTxt = "Open PnL: " + (pnl >= 0 ? "+" : "") + pnl.toFixed(2);
    }
    function centerPill(cy, text) {
      ctx.font = "11px sans-serif";
      var w = ctx.measureText(text).width + 16, cx = (x0 + x1) / 2;
      ctx.fillStyle = "rgba(19,23,34,0.92)";
      rrect(ctx, cx - w / 2, cy - 9, w, 18, 5); ctx.fill();
      ctx.fillStyle = "#ffffff"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(text, cx, cy + 0.5);
      ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
    }
    centerPill(entryY - 11, pnlTxt + "  -  Qty: " + qty);
    centerPill(entryY + 11, "Risk/reward ratio: " + rr.toFixed(2));
    ctx.globalAlpha = 1; ctx.setLineDash([]);
    dot(ctx, pa.x, pa.y, isLong ? G : R);
  };

  /* ----------------------------------------------------- indicator engine */
  ChartPane.prototype.barSec = function () {
    return TF_SEC[this.state.tf] || { D: 86400, W: 604800, M: 2592000 }[this.state.tf] || 60;
  };
  ChartPane.prototype.futureTime = function () {
    return (this.dataEnd || Math.floor(Date.now() / 1000)) + 60 * this.barSec();
  };

  ChartPane.prototype.loadIndicator = function (soft) {
    var self = this;
    /* Stale-while-revalidate: the overlay is NEVER blanked. Every enabled
       indicator fetches in the background and swaps only its own
       contribution when its response arrives - chart-style, no blink.
       A hidden indicator (eye off) still fetches: its sweeps keep arming
       alerts, only its visuals are skipped. Visibility never gates alerts.
       soft=true is the periodic resync refresh: it keeps the sweep-fire
       dedup (no refire attempts); a full reload restarts the dedup session.*/
    var active = (this.state.indicators || []).filter(function (i) {
      return i.enabled;
    });
    var wanted = {}, i, inst, uid;
    for (i = 0; i < active.length; i++) {
      inst = active[i]; uid = inst.id + "@" + this.id;
      wanted[uid] = inst;
    }
    if (!this.indParts) this.indParts = {};
    /* Drop contributions from indicators that are gone or disabled. */
    Object.keys(this.indParts).forEach(function (k) {
      if (!wanted[k]) delete self.indParts[k];
    });
    if (!soft) {
      this.firedSweeps = {};
      /* seed the soft-refresh signature from the current bars so
         the next resync doesn't redundantly refetch what just loaded.*/
      try { this._indSig = this._indSigFor(this.seriesData); }
      catch (e) { this._indSig = "full"; }
    }
    this._rebuildOverlay();
    this.drawDash(); this.draw();
    this.applyMarkers();   // clear stale markers immediately
    // Restore plain candles first; colored bars re-apply as results arrive.
    if (!soft && this.state.mode === "candles" && this.seriesData) {
      try { this.series.setData(this.seriesData); } catch (e) {}
      try { this.setVolumeData(this.seriesData); } catch (e) {}
      this.applyMarkers();
    }
    indLibrary(function () {
      self.renderIndLegend();   // names resolve once the library is loaded
      if (!active.length) return;
      var renkoMode = self.state.mode === "renko";
      /* Stamp every request with the pane's transition state: a response
         that resolves after a mode/TF/symbol/box change is stale and must
         never paint Renko-computed visuals onto a candle pane (or vice
         versa).*/
      var reqGen = self._gen || 0;
      var reqMode = self.state.mode, reqSym = self.state.symbol,
          reqTf = self.state.tf, reqBox = self.state.box;
      active.forEach(function (inst) {
        var uid = inst.id + "@" + self.id;
        var meta = indById(inst.id);
        if (!meta) { toast("Indicator '" + inst.id + "' not found in library"); return; }
        var merged = indDefaults(meta);
        for (var k in (inst.settings || {})) merged[k] = inst.settings[k];
        var url = "/api/indicator/" + inst.id +
          "?symbol=" + encodeURIComponent(self.state.symbol) +
          "&tf=" + encodeURIComponent(self.state.tf) +
          "&settings=" + encodeURIComponent(JSON.stringify(merged));
        /* Renko mode: the indicator must compute on bricks, not candles -
           the Renko engine runs on a Renko chart.*/
        if (renkoMode)
          url += "&mode=renko&box=" + encodeURIComponent(self.state.box);
        /* bar replay: the indicator recomputes on bars at/before the
           replay cursor - signals can never peek at future bars.*/
        var reqUpto = (typeof Replay !== "undefined" && Replay.active) ?
                      Replay.cursorT : 0;
        if (reqUpto) url += "&upto=" + reqUpto;
        var show = inst.visible !== false;
        /* track per-indicator state for the legend status dot. */
        if (!self._indStat) self._indStat = {};
        self._indStat[uid] = { s: "loading", t0: Date.now() };
        self._paintIndStat(uid);
        /* per-indicator abort + sequence guard. The 30s resync
           refires indicator fetches while older ones are still in flight;
           without this, overlapping responses resolve out of order and a
           STALE result (computed on older bars) overwrites the fresh
           overlay - indicator colors vanish for recent bars and markers
           appear "late". The newest request wins: older ones are aborted
           and their late responses are dropped.*/
        if (!self._indCtl) self._indCtl = {};
        if (!self._indSeq) self._indSeq = {};
        try { if (self._indCtl[uid]) self._indCtl[uid].abort(); } catch (e) {}
        var indCtl = null, indTimer = null;
        try { indCtl = new AbortController(); } catch (e) { indCtl = null; }
        if (indCtl) {
          self._indCtl[uid] = indCtl;
          indTimer = setTimeout(function () {
            try { indCtl.abort(); } catch (e) {}
          }, 130000);
        }
        var indSeq = (self._indSeq[uid] || 0) + 1;
        self._indSeq[uid] = indSeq;
        var indDone = function () {
          if (indTimer) clearTimeout(indTimer);
          if (self._indCtl && self._indCtl[uid] === indCtl) self._indCtl[uid] = null;
        };
        fetch(url, indCtl ? { signal: indCtl.signal } : undefined)
          .then(function (r) { indDone(); return r.json(); })
          .then(function (res) {
          /* Superseded responses never paint - not even their errors. */
          if (self._indSeq[uid] !== indSeq) return;
          /* Stale-response guard runs BEFORE any error toast: a Renko-mode
             request that fails after the user switched back to candles must
             not pop an irrelevant error, and a stale success must never
             paint.*/
          var stillThere = (self.state.indicators || []).some(function (x) {
            return x.enabled && (x.id + "@" + self.id) === uid;
          });
          if (!stillThere) return;
          if (reqGen !== (self._gen || 0)) return;
          if (self.state.mode !== reqMode || self.state.symbol !== reqSym ||
              self.state.tf !== reqTf || self.state.box !== reqBox) return;
          /* bar replay: a replay step taken while this was in flight
             makes the response stale - only the latest cursor's indicators
             may paint, so a signal can never leak in from another cursor.*/
          if (typeof Replay !== "undefined" && Replay.active &&
              reqUpto !== Replay.cursorT) return;
          if (res.error) {
            self._indStat[uid] = { s: "error", m: res.error };
            self._paintIndStat(uid);
            toast("Indicator: " + res.error);
            return;
          }
          /* the renko base is building in the background - skip
             silently; the next poll retries and paints when ready.*/
          if (res.loading) return;
          if (self.state.mode !== reqMode || self.state.symbol !== reqSym ||
              self.state.tf !== reqTf || self.state.box !== reqBox) return;
          /* Lines and boxes also render in Renko mode: drawIndLine/drawIndBox
             map their candle-epoch times through the chart time scale, which
             interpolates onto brick times. Only per-bar candle colors stay
             candles-only (bricks carry their own up/down color).*/
          var part = { lines: [], boxes: [], areas: [], barColors: {}, markers: [],
                       labels: [], sweeps: [], dashboard: null };
          if (show) {
            part.lines = res.lines || [];
            part.boxes = res.boxes || [];
            part.areas = res.areas || [];
            if (!renkoMode) {
              (res.bar_colors || []).forEach(function (bc) {
                if (bc && bc.t != null && bc.color) part.barColors[bc.t] = bc.color;
              });
            }
            (res.markers || []).forEach(function (m) {
              /* An indicator that ships script-style canvas labels replaces its
                 LWC markers with them (same info, faithful multiline label).*/
              if (res.labels && res.labels.length) return;
              part.markers.push(m);
            });
            part.labels = res.labels || [];
            /* an indicator may return multiple dashboards (array).
               Store as array; _rebuildOverlay flattens them into the stack.*/
            if (res.dashboard) {
              part.dashboard = Array.isArray(res.dashboard) ? res.dashboard : [res.dashboard];
            }
          }
          /* Sweeps ALWAYS arm - a hidden indicator keeps firing its alerts. */
          part.sweeps = res.sweeps || [];
          /* tag the part with the mode it was computed for. If a
             later fetch for the other mode FAILS, the stale part from this
             mode lingers in indParts - _rebuildOverlay skips cross-mode
             parts so Renko labels can never draw on a candle chart (and
             vice versa), even when a refetch fails.*/
          part._mode = renkoMode ? "renko" : "candles";
          self.indParts[uid] = part;
          self._indStat[uid] = { s: "ok" };
          self._paintIndStat(uid);
          self._rebuildOverlay();
          self.applyBarColors();
          self.applyMarkers();
          self.drawDash(); self.draw();
        }).catch(function (err) {
          indDone();
          /* A superseded (aborted) request stays silent - its replacement
             is already in flight.*/
          if (self._indSeq[uid] !== indSeq) return;
          /* transient error - mark the row so the user sees the real
             state instead of a silent stale plot. a stale (moved-on)
             pane must not get an error dot, and an abort gets the honest
             timeout message.*/
          var stillThere = (self.state.indicators || []).some(function (x) {
            return x.enabled && (x.id + "@" + self.id) === uid;
          });
          if (!stillThere) return;
          var aborted = err && err.name === "AbortError";
          self._indStat[uid] = { s: "error",
            m: aborted ? "timed out after 130s - MT5 is not responding"
                       : "request failed - will retry" };
          self._paintIndStat(uid);
        });
      });
    });
  };

  /* Concatenate the per-indicator parts into the overlay arrays draw()
     reads. Called after every part swap so the canvas always shows the
     latest complete picture - never an empty one.*/
  ChartPane.prototype._rebuildOverlay = function () {
    var lines = [], boxes = [], areas = [], barColors = {}, markers = [], labels = [],
        dashboards = [], sweeps = {}, self = this;
    /* never draw cross-mode parts. A part computed for Renko must
       not render on a candle pane even if it lingers in indParts because
       the candle-mode refetch failed - and vice versa.*/
    var curMode = this.state.mode;
    /* bar replay: indicator parts are recomputed per step with `upto`,
       but a still-in-flight response from an earlier cursor must never paint
       future-dated parts. Anything starting after the cursor is dropped.*/
    var rpT = (typeof Replay !== "undefined" && Replay.active) ? Replay.cursorT : 0;
    function rpFuture(x) {
      if (!rpT || !x) return false;
      var t = (x.t0 !== undefined) ? x.t0 :
              (x.t !== undefined) ? x.t :
              (x.time !== undefined ? x.time : 0);
      return t > rpT;
    }
    /* stack parts in legend order (state.indicators), so dragging a
       legend row up/down reorders what paints on top. Leftover uids that no
       longer map to an indicator (transient) keep their relative order.*/
    var order = [], seen = {};
    (this.state.indicators || []).forEach(function (inst) {
      var u = inst.id + "@" + self.id;
      if (self.indParts && self.indParts[u] && !seen[u]) { seen[u] = true; order.push(u); }
    });
    Object.keys(this.indParts || {}).forEach(function (u) {
      if (!seen[u]) { seen[u] = true; order.push(u); }
    });
    order.forEach(function (uid) {
      var p = self.indParts[uid];
      if (!p) return;
      if (p._mode && p._mode !== curMode) return;
      lines = lines.concat((p.lines || []).filter(function (l) { return !rpFuture(l); }));
      boxes = boxes.concat((p.boxes || []).filter(function (b) { return !rpFuture(b); }));
      /* areas (fills). Bar-replay truncates each area to points
         at/before the cursor so a fill can never leak in from the future.*/
      (p.areas || []).forEach(function (a) {
        if (!rpT) { areas.push(a); return; }
        var t = [], up = [], lo = [], i;
        for (i = 0; i < (a.t || []).length; i++) {
          if (a.t[i] <= rpT) { t.push(a.t[i]); up.push(a.upper[i]); lo.push(a.lower[i]); }
        }
        if (t.length > 1) areas.push({ t: t, upper: up, lower: lo,
          color: a.color, opacity: a.opacity });
      });
      for (var t in p.barColors) {
        if (!rpT || Number(t) <= rpT) barColors[t] = p.barColors[t];
      }
      markers = markers.concat((p.markers || []).filter(function (m) { return !rpFuture(m); }));
      labels = labels.concat((p.labels || []).filter(function (l) { return !rpFuture(l); }));
      if (p.dashboard) {
        if (Array.isArray(p.dashboard)) dashboards = dashboards.concat(p.dashboard);
        else dashboards.push(p.dashboard);
      }
      sweeps[uid] = p.sweeps || [];
    });
    this.indLines = lines;
    this.indBoxes = boxes;
    this.indAreas = areas;
    this.indBarColors = barColors;
    this.indMarkers = markers;
    this.indLabels = labels;
    this.indDashboards = dashboards;
    this.indSweeps = sweeps;
  };

  /* Indicator refresh signature: count + last CLOSED bar (time+close).
     Signals plot and alerts fire only on closed candles, so the forming
     bar's wiggling close must NOT trigger a recompute.keyed this
     on the forming bar's live close, which refired every indicator fetch on
     EVERY 30s resync - the overlapping slow requests then resolved out of
     order and stale overlays overwrote fresh ones.*/
  ChartPane.prototype._indSigFor = function (bars) {
    if (!bars || !bars.length) return "empty";
    var sec = (typeof TF_SEC !== "undefined" && TF_SEC[this.state.tf]) || 60;
    var nowS = Math.floor(Date.now() / 1000);
    var cb = null, i;
    for (i = bars.length - 1; i >= 0; i--) {
      /* Renko slot times are all far below `now`, so the time
         heuristic can't tell the forming brick apart - skip it explicitly,
         or every forming-brick wiggle refires the indicator fetch.*/
      if (this.state.mode === "renko" && bars[i].forming) continue;
      if (bars[i].time <= nowS - sec) { cb = bars[i]; break; }
    }
    return bars.length + ":" + (cb ? cb.time + ":" + cb.close : "noclose");
  };

  /* Resync-path indicator refresh: recompute only when the underlying bars
     actually changed (new bar / new close). Otherwise the 30s resync leaves
     the overlay - and the the host machine's CPU - alone.*/
  ChartPane.prototype.softIndicatorRefresh = function (bars) {
    var sig = this._indSigFor(bars);
    if (sig === this._indSig) return;
    this._indSig = sig;
    this.loadIndicator(true);
  };

  /* Merge indicator bar_colors into the candlestick series (per-bar color). */
  ChartPane.prototype.applyBarColors = function () {
    if (this.state.mode !== "candles" || !this.seriesData || !this.series) return;
    var cmap = this.indBarColors || {}, has = false, k;
    for (k in cmap) { has = true; break; }
    if (!has) return;  // plain restore already done in loadIndicator
    var data = this.seriesData.map(function (b) {
      var c = cmap[b.time];
      if (!c) return b;
      return { time: b.time, open: b.open, high: b.high, low: b.low,
               close: b.close, color: c };
    });
    try { this.series.setData(data); } catch (e) {}
    this.applyMarkers();   // setData is a resync point: re-apply markers
  };

  /* Indicator BUY/SELL markers, rendered with the Lightweight Charts v5
     markers plugin (LWC.createSeriesMarkers - v4 series.setMarkers
     does not exist on series in v5). BUY belowBar (green), SELL aboveBar
     (red).
     Backend contract: output["markers"] = [{t: epoch seconds,
     side: "buy"|"sell", entry: float, sl: float, label: "..."}].
     In Renko mode, marker times are snapped to the nearest brick (first brick
     closing at or after the signal time) so they render on the brick chart.
     setMarkers() on the plugin replaces the whole marker set, so re-applying
     on every data refresh is idempotent - markers survive resyncs and never
     duplicate.*/
  ChartPane.prototype.markersPlugin = function () {
    if (!this._markersPlugin) {
      this._markersPlugin = LWC.createSeriesMarkers(this.series, []);
    }
    return this._markersPlugin;
  };
  ChartPane.prototype.applyMarkers = function () {
    if (!this.series || !LWC.createSeriesMarkers) return;
    var plug;
    try { plug = this.markersPlugin(); } catch (e) { return; }
    if (!plug) return;
    var renkoMode = this.state.mode === "renko";
    var brickTimes = null;
    if (renkoMode && this.seriesData && this.seriesData.length) {
      brickTimes = [];
      /* bricks render at slot times but signals carry real times -
         search on rtime, plot at the slot.*/
      for (var bi = 0; bi < this.seriesData.length; bi++) {
        var bb = this.seriesData[bi];
        brickTimes.push({
          rt: (bb.rtime !== undefined && bb.rtime !== null) ? Number(bb.rtime) : Number(bb.time),
          slot: bb.time
        });
      }
    }
    // Binary search: first brick with real time >= t (the brick that
    // absorbed the signal); the marker is plotted at its slot time.
    function snapToBrick(t) {
      if (!brickTimes || !brickTimes.length) return null;
      if (t < 1e9) return t; // already a slot time
      var lo = 0, hi = brickTimes.length - 1, ans = brickTimes[hi].slot;
      while (lo <= hi) {
        var mid = (lo + hi) >> 1;
        if (brickTimes[mid].rt >= t) { ans = brickTimes[mid].slot; hi = mid - 1; }
        else lo = mid + 1;
      }
      return ans;
    }
    var seen = {}, out = [];
    var info = [];   // per-marker details for the hover tooltip (incl. Renko Logic text)
    (this.indMarkers || []).forEach(function (m) {
      if (!m || m.t === undefined || m.t === null) return;
      var t = Math.round(Number(m.t));   // epoch seconds, same scale as bar time
      if (!isFinite(t) || t <= 0) return;
      if (renkoMode) { t = snapToBrick(t); if (t === null) return; }
      var buy = String(m.side || "").toLowerCase() === "buy";
      var key = t + (buy ? "B" : "S");
      if (seen[key]) return;             // dedupe same-bar same-side markers
      seen[key] = true;
      var label = m.label ? String(m.label) : (buy ? "BUY" : "SELL");
      /* On-chart logic: what was swept and at which level, e.g.
         "SELL @ 4393.45 - Swept 1H High, 4H High @ 4464.55". The full
         alert text stays available on hover via m.explain.*/
      if (m.logic) label += " \u00B7 " + String(m.logic);
      out.push({
        time: t,
        position: buy ? "belowBar" : "aboveBar",
        color: buy ? "#26a69a" : "#ef5350",
        shape: buy ? "arrowUp" : "arrowDown",
        text: label
      });
      info.push({ t: t, side: buy ? "buy" : "sell", label: label,
                  explain: m.explain ? String(m.explain) : "" });
    });
    this._markerInfo = info;
    out.sort(function (a, b) { return a.time - b.time; });
    try { plug.setMarkers(out); } catch (e) {}
  };

  /* Snap a signal timestamp to the brick that absorbed it (Renko mode);
     in candle mode the timestamp is used as-is.*/
  /* contiguous Renko - the server serves bricks at slot times
     (sequence_index * 60) so the chart has no time gaps; each brick keeps
     its real formation time in `rtime`. These helpers translate between
     the two spaces. Slot times are tiny ints (< 1e9); real times are epoch
     seconds, so the two are unambiguous.*/
  ChartPane.prototype._brickRt = function (b) {
    if (!b) return 0;
    var rt = (b.rtime !== undefined && b.rtime !== null) ? Number(b.rtime) : Number(b.time);
    return isFinite(rt) ? rt : 0;
  };
  /* Real brick/signal time -> slot time for rendering. Already-slot values
     pass through untouched.*/
  ChartPane.prototype.renkoSlot = function (t) {
    t = Number(t);
    if (!isFinite(t) || t <= 0) return t;
    if (this.state.mode !== "renko" || !this.bricks || !this.bricks.length) return t;
    if (t < 1e9) return t; // already a slot time
    var br = this.bricks, lo = 0, hi = br.length - 1, ans = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1, rt = this._brickRt(br[mid]);
      if (rt >= t) { ans = mid; hi = mid - 1; }
      else lo = mid + 1;
    }
    if (ans < 0) ans = br.length - 1;
    return br[ans].time;
  };
  /* Slot time -> the brick's real formation time, for display (axis labels,
     crosshair). Falls back to the slot itself when unknown.*/
  ChartPane.prototype.renkoReal = function (slot) {
    slot = Number(slot);
    if (!isFinite(slot)) return slot;
    if (this.state.mode !== "renko" || !this.bricks || !this.bricks.length) return slot;
    var br = this.bricks, lo = 0, hi = br.length - 1, ans = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1, tm = br[mid].time;
      if (tm <= slot) { ans = mid; lo = mid + 1; }
      else hi = mid - 1;
    }
    if (ans < 0) return slot;
    return this._brickRt(br[ans]);
  };
  ChartPane.prototype.snapSignalTime = function (t) {
    if (this.state.mode !== "renko" || !this.seriesData || !this.seriesData.length) return t;
    t = Number(t);
    if (!isFinite(t) || t <= 0) return t;
    if (t < 1e9) return t; // already a slot time
    /* search on real formation time (rtime), return the slot. */
    var sd = this.seriesData, lo = 0, hi = sd.length - 1, ans = sd[hi].time;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1, tm = this._brickRt(sd[mid]);
      if (tm >= t) { ans = sd[mid].time; hi = mid - 1; }
      else lo = mid + 1;
    }
    return ans;
  };

  /* Exact bar/brick lookup by epoch-second timestamp. */
  ChartPane.prototype.barAt = function (t) {
    var d = this.seriesData;
    if (!d || !d.length) return null;
    var lo = 0, hi = d.length - 1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1, tm = d[mid].time;
      if (tm === t) return d[mid];
      if (tm < t) lo = mid + 1; else hi = mid - 1;
    }
    return null;
  };

  /* script-style BUY/SELL signal labels, drawn as boxes on the canvas overlay:
     SELL above the bar, BUY below - multiline "SIDE / Entry: / SL: /
     Logic: Swept <tfs> / @ <level>", exactly like the script's label.
     Redrawn on every draw() (pan/zoom/resize/indicator load). Hit boxes are
     kept in _labelBoxes for the explanation tooltip.*/
  ChartPane.prototype.drawIndLabels = function (vt0, vt1) {
    var ctx = this.ctx, self = this;
    this._labelBoxes = [];
    var list = this.indLabels || [];
    if (!list.length || !this.series) return;
    var w = this.wrapEl.clientWidth, h = this.wrapEl.clientHeight;
    if (!w || !h) return;
    ctx.save();
    ctx.font = "11px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    var padX = 7, padY = 5, lh = 15, gap = 7;
    list.forEach(function (L) {
      if (!L || L.t === undefined || L.t === null || !L.lines || !L.lines.length) return;
      /* snap to the slot time FIRST (Renko), then cull on it -
         L.t is a real time while the viewport is in slot space.*/
      var t = self.snapSignalTime(Math.round(Number(L.t)));
      if (!isFinite(t) || t <= 0) return;
      if (vt0 !== undefined && (t < vt0 || t > vt1)) return;
      var bar = self.barAt(t);
      if (!bar) return;
      var sell = String(L.side || "").toLowerCase() !== "buy";
      var x = self.chart.timeScale().timeToCoordinate(t);
      if (x === null || x === undefined) return;
      var anchorP = sell ? bar.high : bar.low;
      var ay = self.series.priceToCoordinate(anchorP);
      if (ay === null || ay === undefined) return;
      var maxW = 0, li;
      for (li = 0; li < L.lines.length; li++) {
        maxW = Math.max(maxW, ctx.measureText(L.lines[li]).width);
      }
      var bw = Math.ceil(maxW) + padX * 2;
      var bh = L.lines.length * lh + padY * 2;
      var bx = Math.max(2, Math.min(w - bw - 2, x - bw / 2));
      var by = sell ? ay - gap - bh : ay + gap;
      if (by + bh < 0 || by > h) return;
      ctx.fillStyle = sell ? "rgba(239,83,80,0.95)" : "rgba(38,166,154,0.95)";
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(bx, by, bw, bh, 3);
      else ctx.rect(bx, by, bw, bh);
      ctx.fill();
      // pointer triangle from the box edge to the bar/brick
      ctx.beginPath();
      if (sell) { ctx.moveTo(x - 5, by + bh); ctx.lineTo(x + 5, by + bh); ctx.lineTo(x, ay - 1); }
      else { ctx.moveTo(x - 5, by); ctx.lineTo(x + 5, by); ctx.lineTo(x, ay + 1); }
      ctx.closePath();
      ctx.fill();
      ctx.fillStyle = "#ffffff";
      for (li = 0; li < L.lines.length; li++) {
        ctx.fillText(L.lines[li], bx + bw / 2, by + padY + li * lh);
      }
      self._labelBoxes.push({ x0: bx, y0: by, x1: bx + bw, y1: by + bh,
                              side: sell ? "sell" : "buy",
                              label: String(L.lines[0]),
                              explain: L.explain ? String(L.explain) : "" });
    });
    ctx.restore();
  };

  /* Per-pane indicator legend: name + show/hide eye + settings + remove. */
  ChartPane.prototype.renderIndLegend = function () {
    var self = this, el = this.indLegendEl;
    if (!el) return;
    var list = (this.state.indicators || []).filter(function (i) { return i.enabled; });
    this.refreshFxBtn();
    if (!list.length) { el.innerHTML = ""; el.style.display = "none"; return; }
    el.style.display = "block";
    el.innerHTML = "";
    /* drag-and-drop legend reorder. Container listeners attach once;
       rows are rebuilt on every render.*/
    if (!el._dndInit) {
      el._dndInit = true;
      el.addEventListener("dragover", function (ev) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        var dragging = el.querySelector(".indrow.dragging");
        if (!dragging) return;
        var rows = el.querySelectorAll(".indrow:not(.dragging)");
        var after = null;
        for (var i = 0; i < rows.length; i++) {
          var b = rows[i].getBoundingClientRect();
          if (ev.clientY > b.top + b.height / 2) after = rows[i];
          else break;
        }
        if (after) el.insertBefore(dragging, after.nextSibling);
        else if (rows[0]) el.insertBefore(dragging, rows[0]);
        else el.appendChild(dragging);
      });
      el.addEventListener("drop", function (ev) {
        ev.preventDefault();
        var from = parseInt(ev.dataTransfer.getData("text/plain"), 10);
        var rows = el.querySelectorAll(".indrow"), order = [], i;
        for (i = 0; i < rows.length; i++) order.push(parseInt(rows[i].dataset.idx, 10));
        if (!isFinite(from) || order.indexOf(from) < 0) return;
        /* Rebuild state.indicators: visible rows in their new DOM order,
           everything else keeps its place. The order persists via saveState
           and _rebuildOverlay stacks plots in this order.*/
        var st = self.state.indicators || [], visSet = {}, out = [], j, k;
        for (j = 0; j < order.length; j++) visSet[order[j]] = true;
        var first = Math.min.apply(null, order);
        for (j = 0; j < st.length; j++) {
          if (j === first) { for (k = 0; k < order.length; k++) out.push(st[order[k]]); }
          else if (!visSet[j]) out.push(st[j]);
        }
        self.state.indicators = out;
        self.saveState();
        self.renderIndLegend();
        self._rebuildOverlay();
        self.draw();
      });
    }
    list.forEach(function (inst) {
      var meta = indById(inst.id) || { name: inst.id };
      var uid = inst.id + "@" + self.id;
      var row = document.createElement("div");
      row.className = "indrow" + (inst.visible === false ? " hidden" : "");
      row.draggable = true;
      row.dataset.idx = String(self.state.indicators.indexOf(inst));
      row.dataset.uid = uid;
      row.title = meta.name + " - drag to reorder";
      /* keep the plain name - _paintIndStat appends the live
         status/error here so hovering anywhere on the row (not just the
         7px dot) reveals why an indicator failed.*/
      row.dataset.iname = meta.name;
      var dot = document.createElement("span");
      dot.className = "inddot"; dot.style.display = "none";
      row.appendChild(dot);
      row.addEventListener("dragstart", function (ev) {
        ev.dataTransfer.setData("text/plain", row.dataset.idx);
        ev.dataTransfer.effectAllowed = "move";
        row.classList.add("dragging");
      });
      row.addEventListener("dragend", function () {
        row.classList.remove("dragging");
      });
      var nm = document.createElement("span");
      nm.className = "indname"; nm.textContent = meta.name; nm.title = meta.name;
      var eye = document.createElement("button");
      eye.className = "indbtn"; eye.title = inst.visible === false ? "Show" : "Hide";
      eye.textContent = inst.visible === false ? "\uD83D\uDC41" : "\uD83D\uDC41\uFE0F";
      eye.style.opacity = inst.visible === false ? 0.45 : 1;
      var gear = document.createElement("button");
      gear.className = "indbtn"; gear.title = "Settings"; gear.textContent = "\u2699";
      var dots = document.createElement("button");
      dots.className = "indbtn"; dots.title = "More"; dots.textContent = "\u22EF";
      var del = document.createElement("button");
      del.className = "indbtn"; del.title = "Remove"; del.textContent = "\u00D7";
      eye.addEventListener("click", function (ev) {
        ev.stopPropagation();
        inst.visible = inst.visible === false ? true : false;
        self.saveState(); self.renderIndLegend(); self.loadIndicator(false);
        /* Hiding only hides the plots - the indicator keeps calculating and
           its alerts keep firing. Use the Alerts panel to stop alerts.*/
      });
      gear.addEventListener("click", function (ev) {
        ev.stopPropagation(); IndUI.openSettings(self, inst);
      });
      dots.addEventListener("click", function (ev) {
        ev.stopPropagation();
        CtxMenu.show(ev.clientX, ev.clientY, [
          { label: IndAlerts.findAlert(self, inst) ? "Edit alert..." : "Add alert...",
            fn: function () { IndAlerts.open(self, inst, meta); } },
          { label: "Settings", fn: function () { IndUI.openSettings(self, inst); } },
          { label: inst.visible === false ? "Show" : "Hide",
            fn: function () { eye.click(); } },
          { label: "Remove", fn: function () { del.click(); } }
        ]);
      });
      del.addEventListener("click", function (ev) {
        ev.stopPropagation();
        self.state.indicators = self.state.indicators.filter(function (x) { return x !== inst; });
        self.saveState(); self.renderIndLegend(); self.loadIndicator();
        /* removing an indicator stops its alerts server-side */
        IndAlerts.setStopped(self, inst, true);
        toast("Removed " + meta.name);
      });
      row.appendChild(nm); row.appendChild(eye); row.appendChild(gear);
      row.appendChild(dots); row.appendChild(del);
      el.appendChild(row);
      self._paintIndStat(uid);
    });
  };

  /* paint one indicator row's status dot - green calculated,
     amber calculating (with elapsed time in the tooltip), red + real error.
     Targeted update: never rebuilds the row, so drag-reorder is unaffected.*/
  ChartPane.prototype._paintIndStat = function (uid) {
    var el = this.indLegendEl;
    if (!el) return;
    var row = el.querySelector('.indrow[data-uid="' + uid + '"]');
    if (!row) return;
    var dot = row.querySelector(".inddot");
    if (!dot) return;
    var st = (this._indStat || {})[uid];
    if (!st) { dot.style.display = "none"; return; }
    dot.style.display = "";
    dot.className = "inddot" + (st.s === "loading" ? " busy" : "");
    /* the error also goes on the whole ROW's hover title - the
       7px dot is too small a hover target to discover on its own.*/
    var baseName = row.dataset.iname || "";
    function rowTitle(extra) {
      row.title = baseName + (extra ? " - " + extra : " - drag to reorder");
    }
    if (st.s === "ok") {
      dot.style.background = "#0c8f4e";
      dot.title = "Calculated [ok] - plots are up to date";
      rowTitle("");
    } else if (st.s === "error") {
      dot.style.background = "#f23645";
      var emsg = "Error: " + (st.m || "calculation failed");
      dot.title = emsg;
      rowTitle(emsg);
    } else {
      dot.style.background = "#e8a33d";
      var cmsg = "Calculating... " + Math.round((Date.now() - st.t0) / 1000) + "s elapsed";
      dot.title = cmsg;
      rowTitle(cmsg);
    }
  };

  ChartPane.prototype.refreshFxBtn = function () {
    var b = this.el.querySelector(".fxbtn");
    if (b) b.classList.toggle("on", anyIndOn(this.state.indicators));
  };

  ChartPane.prototype.drawIndLine = function (L) {
    /* indicator lines carry real times; map to slot times on Renko. */
    var x0 = this.chart.timeScale().timeToCoordinate(this.renkoSlot(L.t0));
    if (x0 === null || x0 === undefined) return;
    var w = this.wrapEl.clientWidth;
    var x1;
    if (L.t1) {
      /* fixed historical segment: draw exactly t0 -> t1 */
      x1 = this.chart.timeScale().timeToCoordinate(this.renkoSlot(L.t1));
      if (x1 === null || x1 === undefined) x1 = w;
    } else {
      /* Open-ended level line (support/resistance ray): extend a short way
         past the latest bar - was the full viewport width (w-2),
         which drew lines across the entire empty chart area whenever zoomed
         out. Now ends 80px past the last bar (chart-like), capped at
         the viewport edge.*/
      var lastT = (this.cur && this.cur.time) ? this.cur.time : null;
      var lastX = lastT ? this.chart.timeScale().timeToCoordinate(lastT) : null;
      x1 = (lastX !== null && lastX !== undefined) ? lastX + 80 : w - 2;
    }
    x1 = Math.min(x1, w - 2);
    var p0 = (L.p0 !== undefined && L.p0 !== null) ? L.p0 : L.price;
    var p1 = (L.p1 !== undefined && L.p1 !== null) ? L.p1 : p0;
    var y0 = this.series.priceToCoordinate(p0), y1 = this.series.priceToCoordinate(p1);
    if (y0 === null || y0 === undefined || y1 === null || y1 === undefined) return;
    var ctx = this.ctx;
    ctx.strokeStyle = L.color; ctx.lineWidth = L.width || 1;
    ctx.setLineDash(dashOf(L.style));
    ctx.beginPath(); ctx.moveTo(Math.max(x0, 0), y0); ctx.lineTo(x1, y1); ctx.stroke();
    ctx.setLineDash([]);
    if (L.label) {
      ctx.font = labelPx(L.label_size) + "px sans-serif";
      var tw = ctx.measureText(L.label).width;
      var bx = Math.max(x1 - tw - 12, 2), by = Math.min(y0, y1) - 18;
      ctx.fillStyle = "rgba(19,23,34,0.88)";
      ctx.fillRect(bx, by, tw + 8, 15);
      ctx.fillStyle = L.color;
      ctx.fillText(L.label, bx + 4, by + 11);
    }
  };

  /* 
     News markers - DISPLAY ONLY. Draws each USD High (red) / Medium (gold)
     event as a vertical dashed line spanning the pane, with a small flag at
     the top. Event times are real unix seconds; Renko panes map them to slot
     times via renkoSlot(). Nothing here touches signals, alerts, replay, or
     the alert engine - pure canvas visuals.*/
  ChartPane.prototype.drawNews = function (vt0, vt1) {
    if (typeof NewsMarks === "undefined" || !NewsMarks.on()) { this._newsDrawn = null; return; }
    var evs = NewsMarks.events;
    if (!evs || !evs.length) { NewsMarks.maybeRefresh(vt0, vt1); this._newsDrawn = null; return; }
    var ctx = this.ctx, h = this.wrapEl.clientHeight;
    if (!h) return;
    var tsa = this.chart.timeScale(), self = this, drawn = [];
    for (var i = 0; i < evs.length; i++) {
      var ev = evs[i];
      var t = (this.state.mode === "renko") ? this.renkoSlot(ev.ts) : ev.ts;
      if (t < vt0 || t > vt1) continue;
      var x = tsa.timeToCoordinate(t);
      if (x === null || x === undefined) continue;
      var col = NewsMarks.COLORS[ev.impact] || "#ff9800";
      ctx.strokeStyle = col; ctx.lineWidth = 1;
      ctx.setLineDash([5, 4]);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = col;
      ctx.beginPath();
      ctx.moveTo(x - 5, 2); ctx.lineTo(x + 5, 2); ctx.lineTo(x, 10); ctx.closePath();
      ctx.fill();
      drawn.push({ ev: ev, x: x });
    }
    this._newsDrawn = drawn;  // hit-test list for the hover tooltip
    NewsMarks.maybeRefresh(vt0, vt1);
  };

  /* News hover tooltip (title + impact/time/forecast/previous; forecast and
     previous are blank pre-release and hidden gracefully). Pure visual.*/
  ChartPane.prototype.newsTipEl = function () {
    if (!this._newsTip) {
      var el = document.createElement("div");
      el.className = "newstip";
      el.style.display = "none";
      this.wrapEl.appendChild(el);
      this._newsTip = el;
    }
    return this._newsTip;
  };
  ChartPane.prototype.hideNewsTip = function () {
    if (this._newsTip) this._newsTip.style.display = "none";
  };
  ChartPane.prototype.showNewsTip = function (p) {
    var list = this._newsDrawn || [];
    if (!p || !p.point || !list.length ||
        typeof NewsMarks === "undefined" || !NewsMarks.on()) {
      this.hideNewsTip(); return;
    }
    var px = p.point.x, best = null, bd = 10, i, d;
    for (i = 0; i < list.length; i++) {
      d = Math.abs(list[i].x - px);
      if (d < bd) { bd = d; best = list[i]; }
    }
    if (!best) { this.hideNewsTip(); return; }
    var ev = best.ev, when = "";
    try { when = new Date(ev.ts * 1000).toLocaleString(); } catch (e) {}
    function row(k, v) {
      if (v === null || v === undefined || v === "") return "";
      return '<div><span class="k">' + k + ':</span> ' + escHtml(String(v)) + "</div>";
    }
    var el = this.newsTipEl();
    el.innerHTML = '<div class="nt-head">' + escHtml(ev.title || "News event") + "</div>" +
      '<div class="nt-body">' +
      '<div><span class="k">Impact:</span> ' + escHtml(ev.impact || "") +
      ' &nbsp;<span class="k">Time:</span> ' + escHtml(when) + "</div>" +
      row("Forecast", ev.forecast) + row("Previous", ev.previous) +
      "</div>";
    el.style.display = "block";
    var wrap = this.wrapEl;
    el.style.left = Math.min(Math.max(p.point.x + 14, 4),
                             Math.max(wrap.clientWidth - 310, 4)) + "px";
    el.style.top = Math.max(p.point.y - 10, 4) + "px";
  };

  ChartPane.prototype.drawIndArea = function (A) {
    /* filled polygon between two series (Bollinger band fill,
       Ichimoku cloud). A = {t:[...], upper:[...], lower:[...], color,
       opacity}. Null values split the polygon into runs, like line gaps.*/
    var ts = A.t || [], up = A.upper || [], lo = A.lower || [];
    if (ts.length < 2) return;
    var ctx = this.ctx, self = this;
    function pt(t, v) {
      var x = self.chart.timeScale().timeToCoordinate(self.renkoSlot(t));
      var y = self.series.priceToCoordinate(v);
      if (x === null || x === undefined || y === null || y === undefined) return null;
      return [x, y];
    }
    ctx.save();
    ctx.globalAlpha = (A.opacity !== undefined ? A.opacity : 0.08);
    ctx.fillStyle = A.color || "#2962ff";
    var i = 0;
    while (i < ts.length) {
      var run = [];
      while (i < ts.length && up[i] !== null && up[i] !== undefined &&
             lo[i] !== null && lo[i] !== undefined) {
        run.push(i);
        i++;
      }
      if (run.length > 1) {
        ctx.beginPath();
        var ok = true, k, p;
        for (k = 0; k < run.length; k++) {
          p = pt(ts[run[k]], up[run[k]]);
          if (!p) { ok = false; break; }
          if (k === 0) ctx.moveTo(p[0], p[1]); else ctx.lineTo(p[0], p[1]);
        }
        if (ok) {
          for (k = run.length - 1; k >= 0; k--) {
            p = pt(ts[run[k]], lo[run[k]]);
            if (!p) { ok = false; break; }
            ctx.lineTo(p[0], p[1]);
          }
        }
        if (ok) { ctx.closePath(); ctx.fill(); }
      }
      i++;  /* skip the gap point*/
    }
    ctx.restore();
  };

  ChartPane.prototype.drawIndBox = function (B) {
    /* indicator boxes carry real times; map to slot times on Renko. */
    var x0 = this.chart.timeScale().timeToCoordinate(this.renkoSlot(B.t0));
    var x1 = this.chart.timeScale().timeToCoordinate(this.renkoSlot(B.t1));
    if (x0 === null || x0 === undefined || x1 === null || x1 === undefined) return;
    var y0 = this.series.priceToCoordinate(B.p0), y1 = this.series.priceToCoordinate(B.p1);
    if (y0 === null || y0 === undefined || y1 === null || y1 === undefined) return;
    var ctx = this.ctx;
    var rx = Math.min(x0, x1), rw = Math.abs(x1 - x0);
    var ry = Math.min(y0, y1), rh = Math.abs(y1 - y0);
    if (B.bgcolor && B.bgcolor !== "#00000000") {
      ctx.fillStyle = B.bgcolor;
      ctx.fillRect(rx, ry, rw, Math.max(rh, 1));
    }
    if (B.border && B.border !== "#00000000" && (B.border_width || 1) > 0) {
      ctx.strokeStyle = B.border; ctx.lineWidth = B.border_width || 1;
      ctx.setLineDash(dashOf(B.border_style));
      ctx.strokeRect(rx + 0.5, ry + 0.5, Math.max(rw - 1, 1), Math.max(rh - 1, 1));
      ctx.setLineDash([]);
    }
    if (B.label) {
      ctx.font = labelPx(B.label_size) + "px sans-serif";
      ctx.fillStyle = B.label_color || "#d1d4dc";
      ctx.fillText(B.label, rx + 4, ry + 13);
    }
  };

  ChartPane.prototype.drawAlertLines = function () {
    var c = ChartSet.cached(), self = this;
    Alerts.forSymbol(this.state.symbol).forEach(function (a) {
      if (a.kind !== "price" || a.status !== "active") return;
      var y = self.series.priceToCoordinate(a.price);
      if (y === null || y === undefined) return;
      var ctx = self.ctx, w = self.wrapEl.clientWidth;
      ctx.strokeStyle = c.alertColor; ctx.lineWidth = 1; ctx.setLineDash([6, 4]);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); ctx.setLineDash([]);
      var label = "\uD83D\uDD14 " + a.price;
      ctx.font = "11px sans-serif";
      var tw = ctx.measureText(label).width;
      ctx.fillStyle = "rgba(19,23,34,0.9)";
      ctx.fillRect(w - tw - 14, y - 18, tw + 8, 15);
      ctx.fillStyle = c.alertColor;
      ctx.fillText(label, w - tw - 10, y - 7);
    });
  };

  /* Indicator dashboards: one stacked block per indicator that provides one. */
  ChartPane.prototype.drawDash = function () {
    var el = this.dashEl, list = this.indDashboards || [];
    if (!el) return;
    if (!list.length) { el.style.display = "none"; el.innerHTML = ""; return; }
    el.style.cssText = "";
    el.style.position = "absolute"; el.style.zIndex = 7; el.style.display = "block";
    el.style.lineHeight = "1.5"; el.style.padding = "6px 9px"; el.style.borderRadius = "4px";
    el.style.pointerEvents = "none"; el.style.maxHeight = "45%"; el.style.overflow = "hidden";
    var pos = (list[0] && list[0].position) || "Bottom Right";
    if (pos.indexOf("Top") === 0) el.style.top = "34px"; else el.style.bottom = "26px";
    if (pos.indexOf("Left") > 0) el.style.left = "8px"; else el.style.right = "70px";
    var html = "";
    list.forEach(function (d, i) {
      var body = "";
      /* table dashboards render a real HTML table. */
      if (d.kind === "table" && d.headers) {
        var th = d.headers.map(function (h) {
          return '<th style="padding:2px 8px 2px 0;text-align:left;font-weight:600;opacity:0.75">' +
            escHtml(h) + "</th>";
        }).join("");
        var tr = (d.rows || []).map(function (r) {
          var tds = (r.cells || []).map(function (c) {
            return '<td style="padding:2px 8px 2px 0;white-space:nowrap;color:' +
              (r.color || "inherit") + '">' + escHtml(c) + "</td>";
          }).join("");
          return "<tr>" + tds + "</tr>";
        }).join("");
        body = '<table style="border-collapse:collapse"><thead><tr>' + th +
          "</tr></thead><tbody>" + tr + "</tbody></table>";
      } else {
        body = (d.rows || []).map(function (r) {
          return '<div style="color:' + r.color + '">' + escHtml(r.text) + "</div>";
        }).join("");
      }
      html += '<div class="dashblock" style="margin-top:' + (i ? "6px" : "0") +
        ";font-size:" + labelPx(d.size) + "px;background:" + (d.bg || "rgba(19,23,34,0.88)") +
        ";color:" + (d.fg || "#d1d4dc") + ";padding:6px 9px;border-radius:4px\">" +
        '<div class="dh">' + escHtml(d.title || "INDICATOR") + "</div>" + body + "</div>";
    });
    el.innerHTML = html;
  };

  /* Sweep alerts: live cross of any sweep level reported by enabled indicators. */
  ChartPane.prototype.checkSweeps = function (prevBid, bid) {
    if (prevBid === null || prevBid === undefined || !isFinite(bid)) return;
    var self = this;
    function crossed(price) {
      return (prevBid < price && bid >= price) || (prevBid > price && bid <= price);
    }
    function fire(key, text, uid, sw) {
      /* the dedup key is pane-independent (base indicator id only).
         The same indicator on two panes sweeping the same session level is
         ONE breach - "once per session breach per day" must hold across
         panes, not just within one. The full pane-scoped uid is still sent
         as indicator_id so the alert definition routes correctly.*/
      var instId = String(uid).split("@")[0];
      key = instId + ":" + String(key).split(":").slice(1).join(":");
      if (self.firedSweeps[key]) return;
      self.firedSweeps[key] = true;
      var meta = indById(instId) || {};
      /* single server fire path: lifecycle, dedup, log and Telegram/webhook
         delivery all happen server-side; popup + sound only on confirmation*/
      fetch("/api/alerts/fire", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: "indicator", indicator_id: uid,
          indicator_name: meta.name || instId,
          symbol: self.state.symbol, tf: self.state.tf,
          chart_mode: self.state.mode, box: self.state.box,
          text: text, price: sw.price,
          sweep_key: key, event: !!sw.event,
          settings: (function () {
            var inst = null;
            (self.state.indicators || []).forEach(function (x) {
              if (x.id + "@" + self.id === uid) inst = x;
            });
            return (inst && inst.settings) || {};
          })()
        })
      }).then(function (r) { return r.json(); }).then(function (j) {
        if (j && j.fired) {
          if (j.event_id) Alerts._seenFires[j.event_id] = 1;
          var n = j.notify || {};
          if (n.popup !== false)
            toast("\u26A1 " + self.state.symbol + " " + text);
          if (n.sound !== false) playAlertSound();
        }
        Alerts.refresh();
      }).catch(function () {});
    }
    Object.keys(this.indSweeps).forEach(function (uid) {
      (self.indSweeps[uid] || []).forEach(function (sw) {
        if (sw.alert && crossed(sw.price))
          /* event sweeps (e.g. Renko/ORB BUY/SELL) carry their complete alert
             text; level sweeps go out with their raw text*/
          fire(uid + ":" + sw.key, sw.text, uid, sw);
      });
    });
  };

  /* ================================================================= Layout */
  var Layout = {
    panes: [], activeId: 0, tool: "cursor", nPanes: 1, arrId: "full",
    savedVersion: "3.0", _dirty: false,
    /* Anything done on the chart (drawings, indicators, TF, symbol...) marks
       the layout dirty: an orange dot appears on the Save button. The Save
       button writes it silently (no popup); a 60s timer auto-saves; closing
       the tab saves too.*/
    markDirty: function () {
      if (this._dirty) return;
      this._dirty = true;
      var b = $("btn-savelayout");
      if (b) b.classList.add("dirty");
    },

    init: function (status) {
      var self = this;
      $("ver").textContent = "v" + (status.version || "3.0");
      var restored = this.restore();
      if (restored) {
        this.nPanes = restored.n;
        this.arrId = restored.arr || arrDef(restored.n, null).id;
        this.buildGrid();
        restored.panes.forEach(function (ps, i) {
          self.panes.push(new ChartPane(self.paneEl(i), i, ps, function () { self.markDirty(); }));
        });
        this.setActive(restored.active || 0);
      } else {
        /* default = two panes, both 1m candles, no indicators.
           (A saved layout in localStorage still takes precedence.)*/
        this.nPanes = 2;
        this.arrId = "cols";
        this.buildGrid();
        var defSym = status.symbol || "XAUUSD";
        this.panes.push(new ChartPane(this.paneEl(0), 0,
          { symbol: defSym, tf: "1m", mode: "candles", box: 2.5, drawings: [], indicators: [] },
          function () { self.markDirty(); }));
        this.panes.push(new ChartPane(this.paneEl(1), 1,
          { symbol: defSym, tf: "1m", mode: "candles", box: 2.5, drawings: [], indicators: [] },
          function () { self.markDirty(); }));
        this.setActive(0);
      }
      self.wireTopbar();
      AlertPanel.init();
      /* server-owned alerts: initial sync, one-time migration of the old
         browser-local price alerts, then a light 60s re-sync*/
      Alerts.refresh(function () { Alerts.migrate(); });
      setInterval(function () { Alerts.refresh(); }, 60000);
      setInterval(function () { Alerts.pollFires(); }, 5000);
    },
    wireTopbar: function () {
      var self = this;
      document.querySelectorAll("#drawbar button").forEach(function (b) {
        b.addEventListener("click", function () {
          document.querySelectorAll("#drawbar button").forEach(function (x) { x.classList.remove("on"); });
          b.classList.add("on");
          self.tool = b.dataset.tool;
          /* while a drawing tool is armed, the chart must not pan/zoom under
             the drag - otherwise the time scale shifts mid-draw and the object
             lands away from the pointer (the chart locks too)*/
          var interact = (self.tool === "cursor");
          self.panes.forEach(function (pn) {
            if (pn.chart) pn.chart.applyOptions({ handleScroll: interact, handleScale: interact });
          });
          /* a newly armed tool cancels any in-progress drawing; selecting the
             cursor clears the pending preview too*/
          var p = self.activePane();
          if (p) { p.pending = null; p.draw(); }
          self.refreshFmtBars();
        });
      });
      /* Profile dropdown replaces the Layout button.
         Profiles = named layouts, kept strictly separate. Switching profiles
         auto-saves the current profile's drawings first, so drawings never
         leak between profiles. The profile name is always visible in the
         topbar button.*/
      var ProfileUI = {
        current: null,  // name of active named profile, or null for unsaved working state
        init: function () {
          var self = this;
          // Restore last active profile name
          try { this.current = localStorage.getItem("stocksophist_current_profile") || null; } catch (e) {}
          this.updateLabel();
          $("btn-profile").addEventListener("click", function (e) {
            e.stopPropagation();
            self.toggle();
          });
          document.addEventListener("click", function () { self.hide(); });
          $("profile-dropdown").addEventListener("click", function (e) { e.stopPropagation(); });
          $("profile-create").addEventListener("click", function () { self.createNew(); });
          $("profile-newname").addEventListener("keydown", function (e) {
            if (e.key === "Enter") self.createNew();
          });
        },
        toggle: function () {
          var dd = $("profile-dropdown");
          if (dd.style.display === "none") { this.refresh(); dd.style.display = "block"; }
          else dd.style.display = "none";
        },
        hide: function () { $("profile-dropdown").style.display = "none"; },
        updateLabel: function () {
          var nm = this.current || "Default";
          $("profile-name").textContent = nm;
          var badge = $("profile-badge-name");
          if (badge) badge.textContent = nm;
        },
        namedList: function () {
          try { return JSON.parse(localStorage.getItem("stocksophist_named_layouts") || "{}"); }
          catch (e) { return {}; }
        },
        saveNamed: function (obj) {
          try { localStorage.setItem("stocksophist_named_layouts", JSON.stringify(obj)); } catch (e) {}
        },
        setCurrent: function (nm) {
          this.current = nm;
          try {
            if (nm) localStorage.setItem("stocksophist_current_profile", nm);
            else localStorage.removeItem("stocksophist_current_profile");
          } catch (e) {}
          this.updateLabel();
        },
        /* Save working state back to the current named profile (if any) */
        persistCurrent: function () {
          if (!this.current) return;
          var saved = this.namedList();
          saved[this.current] = {
            v: "3.0", n: Layout.nPanes, arr: Layout.arrId, active: Layout.activeId,
            panes: Layout.panes.map(function (p) { return p.serialize(); })
          };
          this.saveNamed(saved);
        },
        refresh: function () {
          var self = this;
          var saved = this.namedList(), names = Object.keys(saved).sort();
          var h = "";
          if (!names.length) h += '<div style="padding:10px;color:#888;font-size:12px">No profiles yet. Create one below.</div>';
          names.forEach(function (nm) {
            var s = saved[nm];
            var active = (nm === self.current) ? ' style="background:#1a2a4a"' : "";
            h += '<div class="profile-row"' + active + ' data-profile="' + escHtml(nm) + '"' +
                 ' style="padding:8px 12px;cursor:pointer;font-size:13px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #2a2e39">' +
                 '<span>' + escHtml(nm) + ' <i style="color:#666">(' + s.n + (s.n === 1 ? " chart" : " charts") + ")</i>" +
                 (nm === self.current ? ' <span style="color:#4caf50;font-size:11px">*</span>' : "") + "</span>" +
                 '<button class="btn sm ghost" data-pdel="' + escHtml(nm) + '" title="Delete profile" style="padding:2px 8px">&times;</button></div>';
          });
          $("profile-list").innerHTML = h;
          // Wire row clicks (switch profile)
          Array.prototype.forEach.call($("profile-list").querySelectorAll(".profile-row"), function (row) {
            row.addEventListener("click", function (e) {
              if (e.target.getAttribute("data-pdel")) return;  // handled below
              self.switchTo(row.getAttribute("data-profile"));
            });
          });
          // Wire delete buttons
          Array.prototype.forEach.call($("profile-list").querySelectorAll("[data-pdel]"), function (b) {
            b.addEventListener("click", function (e) {
              e.stopPropagation();
              var nm = b.getAttribute("data-pdel");
              if (!confirm("Delete profile '" + nm + "'?")) return;
              var s = self.namedList();
              delete s[nm];
              self.saveNamed(s);
              if (self.current === nm) self.setCurrent(null);
              self.refresh();
            });
          });
        },
        switchTo: function (nm) {
          if (nm === this.current) { this.hide(); return; }
          // Save current profile's state first (prevents drawing leakage)
          this.persistCurrent();
          Layout.save();  // also persist working layout
          var s = this.namedList()[nm];
          if (s) {
            LayoutPicker.applySnapshot(s);
            this.setCurrent(nm);
            toast("Switched to profile: " + nm);
          }
          this.hide();
        },
        createNew: function () {
          var nm = $("profile-newname").value.trim();
          if (!nm) { toast("Give the profile a name first"); return; }
          var saved = this.namedList();
          if (saved[nm] && !confirm("Profile '" + nm + "' exists. Overwrite?")) return;
          // Save current profile first, then snapshot working state as the new profile
          this.persistCurrent();
          Layout.save();
          saved[nm] = {
            v: "3.0", n: Layout.nPanes, arr: Layout.arrId, active: Layout.activeId,
            panes: Layout.panes.map(function (p) { return p.serialize(); })
          };
          this.saveNamed(saved);
          this.setCurrent(nm);
          $("profile-newname").value = "";
          this.refresh();
          toast("Profile created: " + nm);
        }
      };
      ProfileUI.init();
      /* Keep the old LayoutPicker modal accessible via double-click on profile button
         (for advanced layout management). Single click opens the dropdown.*/
      $("btn-profile").addEventListener("dblclick", function () { LayoutPicker.open(); });
      /* dedicated Layout button - the 1/2/3/4-chart grid picker
         is reachable without the double-click above.*/
      if ($("btn-layout")) $("btn-layout").addEventListener("click", function () { LayoutPicker.open(); });
      /* Save button: one click writes the current layout silently - no popup.
         The orange dot on the button means unsaved changes exist.*/
      $("btn-savelayout").addEventListener("click", function () { Layout.save(true); });
      /* autosave: every 60s, persist whatever changed on the charts */
      setInterval(function () {
        if (Layout._dirty) {
          Layout.save();
          /* also persist to the active named profile so drawings
             stay with their profile even without manual profile save*/
          try { if (typeof ProfileUI !== "undefined") ProfileUI.persistCurrent(); } catch (e) {}
        }
      }, 60000);
      /* never lose work on tab close */
      window.addEventListener("beforeunload", function () {
        if (Layout._dirty) {
          try { Layout.save(); } catch (e) {}
          try { if (typeof ProfileUI !== "undefined") ProfileUI.persistCurrent(); } catch (e) {}
        }
      });
      $("btn-watch").addEventListener("click", function () { Watchlist.toggle(); });
      $("btn-mt5").addEventListener("click", function () { MT5UI.open(); });
      $("btn-data").addEventListener("click", function () { DataUI.open(); });
      /* manual data-integrity fix with real progress. The
         server runs the heal as a background job; this dialog shows the
         live step, per-series progress, and a completion summary that
         stays up until closed - no more guessing whether it did anything.
         The server also heals automatically at startup and every 10 min.*/
      $("btn-heal").addEventListener("click", function () {
        var p = self.activePane();
        var payload = {};
        if (p && p.state) { payload.symbol = p.state.symbol; payload.tf = p.state.tf; }
        HealUI.start(payload, function () {
          self.panes.forEach(function (pn) { try { pn.load(false); } catch (e) {} });
        });
      });
      $("btn-tg").addEventListener("click", function () { TG.open(); });
      $("btn-chartset").addEventListener("click", function () { ChartSet.open(); });
      document.addEventListener("keydown", function (e) {
        if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) {
          e.preventDefault(); Layout.save(true); return;
        }
        if (e.key === "Escape") {
          var p = self.activePane();
          if (p) { p.commitTextEdit(false); p.pending = null; p.selectedId = null; p.axisPlus.style.display = "none"; p.draw(); p.refreshFmtBar(); }
          CtxMenu.hide();
          if (!document.getElementById("ied-code")) Modal.close(); // keep script editor on Esc
        } else if (e.key === "Delete" || e.key === "Backspace") {
          var p2 = self.activePane();
          if (p2 && p2.selectedId && document.activeElement.tagName !== "INPUT") {
            var del = p2.getDrawing(p2.selectedId);
            if (del && !del.locked) p2.removeDrawing(p2.selectedId);
          }
        } else if (e.altKey && (e.key === "r" || e.key === "R")) {
          /* chart-style Alt+R: reset the chart (reload data, reset view) */
          var p3 = self.activePane();
          if (p3 && document.activeElement.tagName !== "INPUT") {
            p3.load(true);
            toast("Chart reset");
          }
          e.preventDefault();
        } else if ((e.key === "v" || e.key === "V") && document.activeElement.tagName !== "INPUT") {
          document.querySelector('#drawbar button[data-tool="cursor"]').click();
        } else if ((e.key === "m" || e.key === "M") &&
                   !/^(INPUT|TEXTAREA|SELECT)$/.test(((document.activeElement || {}).tagName || ""))) {
          /* M: switch to the mouse/select tool - never steal a remapped trade hotkey */
          var tk = (typeof Trade !== "undefined") ? Trade.keys() : null;
          var stolen = !!(Trade && Trade.visible && tk &&
            (tk.buy === "m" || tk.sell === "m" || tk.flat === "m" || tk.reverse === "m"));
          if (!stolen) document.querySelector('#drawbar button[data-tool="cursor"]').click();
        }
      });
      $("reset-layout").addEventListener("click", function () {
        if (confirm("Clear saved layout and all drawings?")) {
          localStorage.removeItem(LS_KEY);
          location.reload();
        }
      });
      window.addEventListener("resize", function () {
        self.panes.forEach(function (p) { p.resize(); });
      });
      Watchlist.init();
      if (typeof HistUI !== "undefined") HistUI.init();  // deep-history pill
      if (typeof HealthUI !== "undefined") HealthUI.init();  // feed-health pill
      if (typeof NewsMarks !== "undefined") NewsMarks.init();  // news markers
      _serverHasTone(function () {});  // probe server custom tune once
      if (typeof Replay !== "undefined") Replay.init();
      /* paint inline-SVG icons into [data-ic] placeholders, then
         apply the saved UI theme (charts follow via ChartSet defaults).*/
      try {
        document.querySelectorAll("[data-ic]").forEach(function (el) {
          var ic = ICONS[el.getAttribute("data-ic")];
          if (ic) {
            if (el.classList.contains("icobtn"))
              el.insertAdjacentHTML("afterbegin", ic);
            else
              el.innerHTML = ic;
          }
          el.setAttribute("aria-label", el.getAttribute("aria-label") || el.title || "");
        });
      } catch (e) {}
      Theme.init();
      updateAlertBadge();
      this.markBadges("DEMO");
      this.pollMt5Badge();
      setInterval(function () { self.pollMt5Badge(); }, 15000);
      this.pollModeBadge();
      setInterval(function () { self.pollModeBadge(); }, 15000);
    },

    /* The DEMO/LIVE-MT5 badge must track the server's real feed mode -
       a hardcoded "DEMO" hid live connections.
       the terminal's connected flag flaps on a sick MT5, which
       made the badge flicker DEMO/LIVE every 15s poll. Debounce the DOWN
       transition: flip to LIVE-MT5 instantly, but require 2 consecutive
       DEMO polls before flipping down.*/
    pollModeBadge: function () {
      var self = this;
      fetch("/api/status").then(function (r) { return r.json(); }).then(function (j) {
        var m = j && j.mode;
        if (!m) return;
        var isLive = (m === "LIVE-MT5" || m === "LIVE-OANDA");
        if (isLive) { self._demoStreak = 0; self.markBadges(m); }
        else {
          self._demoStreak = (self._demoStreak || 0) + 1;
          if (self._demoStreak >= 2) self.markBadges(m);
        }
        /* half-dead MT5 (quotes live, bar history repeatedly
           failing) - flag it on the MT5 button, once per episode.*/
        var deg = !!(j && j.feed_degraded);
        if (deg && !self._feedDegraded)
          toast("MT5 history degraded - quotes are live but bar history keeps failing. Candles may freeze; check the terminal.");
        self._feedDegraded = deg;
        var b = $("btn-mt5");
        if (b) {
          b.classList.toggle("degraded", deg);
          b.title = deg ? "MT5 connected, but bar history is failing - candles may freeze"
                        : "MT5 connection settings";
        }
      }).catch(function () {});
    },

    pollMt5Badge: function () {
      fetch("/api/mt5/status").then(function (r) { return r.json(); }).then(function (j) {
        var b = $("btn-mt5");
        if (j.connected) {
          b.textContent = "MT5 [ok] " + (j.login || "");
          b.classList.add("on");
        } else {
          b.textContent = "MT5"; b.classList.remove("on");
        }
      }).catch(function () {});
    },

    paneEl: function (i) {
      var el = document.createElement("div");
      el.style.gridArea = AREA_LETTERS[i] || "a";
      $("grid").appendChild(el);
      return el;
    },

    buildGrid: function () {
      var grid = $("grid"), def = arrDef(this.nPanes, this.arrId);
      grid.style.gridTemplateColumns = def.cols;
      grid.style.gridTemplateRows = def.rows;
      grid.style.gridTemplateAreas = def.areas.map(function (r) { return '"' + r.join(" ") + '"'; }).join(" ");
      grid.innerHTML = "";
    },

    setLayout: function (n, arr) {
      var self = this;
      var old = this.panes.map(function (p) { return p.serialize(); });
      this.panes.forEach(function (p) { p.destroy(); });
      this.panes = [];
      this.nPanes = n;
      this.arrId = arr || arrDef(n, null).id;
      this.buildGrid();
      for (var i = 0; i < n; i++) {
        var ps = old[i % old.length] || old[0];
        var state = ps ? JSON.parse(JSON.stringify(ps)) : null;
        this.panes.push(new ChartPane(this.paneEl(i), i, state, function () { self.markDirty(); }));
      }
      this.setActive(0);
      this.save();
    },

    setActive: function (id) {
      this.activeId = id;
      this.panes.forEach(function (p) { p.el.classList.toggle("active", p.id === id); });
      this.refreshFmtBars();
    },

    refreshFmtBars: function () {
      this.panes.forEach(function (p) { p.refreshFmtBar(); });
    },
    /* programmatic tool switch (single-use tools call this after one drawing) */
    setTool: function (t) {
      var b = document.querySelector('#drawbar button[data-tool="' + t + '"]');
      if (b) b.click();
    },

    activePane: function () {
      for (var i = 0; i < this.panes.length; i++)
        if (this.panes[i].id === this.activeId) return this.panes[i];
      return this.panes[0] || null;
    },

    markBadges: function (mode) {
      var live = (mode === "LIVE-MT5" || mode === "LIVE-OANDA");
      this.panes.forEach(function (p) {
        p.modeBadge = mode;
        p.badgeEl.textContent = mode;
        p.badgeEl.className = "badge " + (live ? "live" : "demo");
      });
    },

    save: function (manual) {
      try {
        localStorage.setItem(LS_KEY, JSON.stringify({
          v: this.savedVersion, n: this.nPanes, arr: this.arrId, active: this.activeId,
          panes: this.panes.map(function (p) { return p.serialize(); })
        }));
        this._dirty = false;
        var b = $("btn-savelayout");
        if (b) b.classList.remove("dirty");
        var s = $("save-state");
        s.style.opacity = 1;
        clearTimeout(s._h);
        s._h = setTimeout(function () { s.style.opacity = 0; }, 1200);
        if (manual) toast("Layout saved");
      } catch (e) { /* storage full/blocked: drawings just won't persist*/ }
    },

    restore: function () {
      try {
        var raw = localStorage.getItem(LS_KEY);
        if (!raw) return null;
        var o = JSON.parse(raw);
        // accept v2.x layouts too: arrangement simply defaults to the first
        if (!o || (o.v !== "3.0" && o.v !== "2.0" && o.v !== "2.1") || !o.panes || !o.panes.length) return null;
        return o;
      } catch (e) { return null; }
    }
  };

  /* ----------------------------------------------------- layout picker */
  var LayoutPicker = {
    open: function () {
      var html = this.savedHtml();
      [1, 2, 3, 4].forEach(function (n) {
        html += '<div class="lpick-sec">' + n + (n === 1 ? " chart" : " charts") + "</div><div class='lpick-grid'>";
        ARRANGEMENTS[n].forEach(function (a) {
          var cells = "";
          a.areas.forEach(function (row) {
            row.forEach(function (cell) { cells += '<i style="grid-area:' + cell + '"></i>'; });
          });
          html += '<div class="lpick' + (Layout.nPanes === n && Layout.arrId === a.id ? " on" : "") +
            '" data-n="' + n + '" data-a="' + a.id + '">' +
            '<div class="thumb" style="grid-template-columns:' + a.cols +
            ";grid-template-rows:" + a.rows +
            ";grid-template-areas:" + a.areas.map(function (r) { return "'" + r.join(" ") + "'"; }).join(" ") + '">' +
            cells + '</div><div class="nm">' + a.name + "</div></div>";
        });
        html += "</div>";
      });
      Modal.open("Chart layout", html,
        '<button class="btn ghost" id="m-cancel">Close</button>');
      $("m-cancel").addEventListener("click", Modal.close);
      document.querySelectorAll(".lpick").forEach(function (el) {
        el.addEventListener("click", function () {
          Layout.setLayout(parseInt(el.dataset.n, 10), el.dataset.a);
          Modal.close();
        });
      });
      this.wireSaved();
    },

    namedList: function () {
      try { return JSON.parse(localStorage.getItem("stocksophist_named_layouts") || "{}"); }
      catch (e) { return {}; }
    },

    savedHtml: function () {
      var saved = this.namedList(), names = Object.keys(saved);
      var h = '<div class="lpick-sec">Saved layouts</div>' +
        '<div style="display:flex;gap:6px;margin:0 0 8px">' +
        '<input id="lpick-name" placeholder="Layout name..." style="flex:1">' +
        '<button class="btn primary" id="lpick-save">Save current</button></div>' +
        '<div id="lpick-saved">';
      if (!names.length)
        h += '<div class="note">No saved layouts yet. Arrange your charts, name it, press Save current.</div>';
      names.forEach(function (nm) {
        var s = saved[nm];
        h += '<div class="lpick-savedrow"><span>' + escHtml(nm) +
          " <i>(" + s.n + (s.n === 1 ? " chart" : " charts") + ")</i></span>" +
          '<button class="btn sm" data-load="' + escHtml(nm) + '">Load</button>' +
          '<button class="btn sm ghost" data-del="' + escHtml(nm) + '">&times;</button></div>';
      });
      return h + "</div>";
    },

    wireSaved: function () {
      var self = this;
      $("lpick-save").addEventListener("click", function () {
        var nm = $("lpick-name").value.trim();
        if (!nm) { toast("Give the layout a name first"); return; }
        var saved = self.namedList();
        saved[nm] = {
          v: "3.0", n: Layout.nPanes, arr: Layout.arrId, active: Layout.activeId,
          panes: Layout.panes.map(function (p) { return p.serialize(); })
        };
        try { localStorage.setItem("stocksophist_named_layouts", JSON.stringify(saved)); } catch (e) {}
        toast("Layout saved: " + nm);
        Modal.close(); self.open();
      });
      Array.prototype.forEach.call(document.querySelectorAll("[data-load]"), function (b) {
        b.addEventListener("click", function () {
          var s = self.namedList()[b.getAttribute("data-load")];
          if (s) self.applySnapshot(s);
          Modal.close();
        });
      });
      Array.prototype.forEach.call(document.querySelectorAll("[data-del]"), function (b) {
        b.addEventListener("click", function () {
          var saved = self.namedList();
          delete saved[b.getAttribute("data-del")];
          try { localStorage.setItem("stocksophist_named_layouts", JSON.stringify(saved)); } catch (e) {}
          Modal.close(); self.open();
        });
      });
    },

    applySnapshot: function (s) {
      Layout.panes.forEach(function (p) { p.destroy(); });
      Layout.panes = [];
      Layout.nPanes = s.n;
      Layout.arrId = s.arr || arrDef(s.n, null).id;
      Layout.buildGrid();
      for (var i = 0; i < s.n; i++) {
        var ps = s.panes[i % s.panes.length] || s.panes[0];
        Layout.panes.push(new ChartPane(Layout.paneEl(i), i,
          JSON.parse(JSON.stringify(ps)), function () { Layout.markDirty(); }));
      }
      Layout.setActive(Math.min(s.active || 0, s.n - 1));
      Layout.save();
    }
  };

  /* Named snapshots live in the Chart layout picker ("Save current"); the
     topbar Save button writes the working layout silently (see Layout.save).*/

    /* ================================================= IndUI: indicator library
     chart-style: Favorites / My scripts / Built-in, search, star,
     click to add to the pane, per-pane legend (show/hide/settings/remove),
     and a script editor to add your own Python indicators (script-style).*/
  var IndUI = {
    _pane: null, _tab: "fav", _q: "",

    openMenu: function (pane) {
      this._pane = pane; this._tab = "fav"; this._q = "";
      var self = this;
      indLibrary(function () { self.render(); }, true);
    },

    render: function () {
      var self = this, lib = IND_LIB || [];
      document.querySelector("#modal-root .mbox").classList.add("wide");
      var body =
        '<div class="ilib-search"><input id="ilib-q" placeholder="Search" value="' +
          escHtml(this._q) + '"><button id="ilib-refresh" title="Re-scan indicator folder"' +
          ' style="margin-left:6px;cursor:pointer;background:#1e2530;color:#d1d4dc;' +
          'border:1px solid #2a3240;border-radius:4px;padding:4px 10px;">&#x21bb;</button></div>' +
        '<div class="ilib-wrap"><div class="ilib-nav">' +
          this.navBtn("fav", "\u2605", "Favorites") +
          this.navBtn("mine", "\uD83D\uDC64", "My scripts") +
          this.navBtn("builtin", "\uD83D\uDCCA", "Built-in") +
          '<button class="ilib-new" id="ilib-new">+ New script</button>' +
        "</div>" +
        '<div class="ilib-list" id="ilib-list"></div></div>';
      Modal.open("Indicators, metrics, and strategies", body, "");
      $("ilib-q").addEventListener("input", function (e) {
        self._q = e.target.value; self.paintList();
      });
      var nav = document.querySelectorAll(".ilib-navbtn");
      for (var i = 0; i < nav.length; i++) (function (b) {
        b.addEventListener("click", function () { self._tab = b.dataset.tab; self.render(); });
      })(nav[i]);
      $("ilib-new").addEventListener("click", function () { self.openEditor(null); });
      $("ilib-refresh").addEventListener("click", function () {
        indLibrary(function () { self.paintList(); }, true);
      });
      this.paintList();
    },

    navBtn: function (tab, icon, label) {
      return '<button class="ilib-navbtn' + (this._tab === tab ? " on" : "") +
        '" data-tab="' + tab + '">' + icon + " " + label + "</button>";
    },

    filtered: function () {
      var self = this, q = this._q.trim().toLowerCase();
      return (IND_LIB || []).filter(function (m) {
        if (self._tab === "fav" && !m.favorite) return false;
        if (self._tab === "mine" && m.builtin) return false;
        if (self._tab === "builtin" && !m.builtin) return false;
        if (q && (m.name + " " + (m.author || "") + " " + (m.description || ""))
              .toLowerCase().indexOf(q) < 0) return false;
        return true;
      });
    },

    onPane: function (id) {
      var list = (this._pane && this._pane.state.indicators) || [];
      for (var i = 0; i < list.length; i++)
        if (list[i].id === id && list[i].enabled) return true;
      return false;
    },

    paintList: function () {
      var self = this, el = $("ilib-list");
      if (!el) return;
      /* show files that failed to load (bad paste, syntax error,
         missing META/INPUTS/compute) so testing doesn't silently confuse.*/
      var errHtml = (IND_ERRORS || []).map(function (e) {
        return '<div class="ilib-row ilib-err" style="border-left:3px solid #f44336;' +
          'padding:8px 10px;opacity:0.9"><div><b style="color:#f44336">\u26a0 ' +
          escHtml(e.file) + '</b><div style="font-size:11px;color:#a0a7b8">' +
          escHtml(e.error) + "</div></div></div>";
      }).join("");
      var items = this.filtered();
      if (!items.length) {
        el.innerHTML = '<div class="ilib-empty">' +
          (this._tab === "mine"
            ? 'No scripts yet. Click <b>+ New script</b> to write your own indicator in Python.'
            : "Nothing here.") + "</div>";
        return;
      }
      el.innerHTML = errHtml;
      items.forEach(function (m) {
        var row = document.createElement("div");
        row.className = "ilib-row" + (self.onPane(m.id) ? " added" : "");
        var star = document.createElement("button");
        star.className = "ilib-star" + (m.favorite ? " on" : "");
        star.textContent = "\u2605"; star.title = "Favorite";
        star.addEventListener("click", function (ev) {
          ev.stopPropagation(); self.toggleFav(m);
        });
        var meta = document.createElement("div");
        meta.className = "ilib-meta";
        var nm = document.createElement("div");
        nm.className = "ilib-name"; nm.textContent = m.name;
        var ds = document.createElement("div");
        ds.className = "ilib-desc";
        ds.textContent = (m.description || m.attribution || "").slice(0, 120);
        ds.title = m.description || m.attribution || "";
        meta.appendChild(nm);
        /* overlay vs separate-pane badge from the indicator's META. */
        var badge2 = document.createElement("span");
        badge2.className = "ilib-place";
        badge2.textContent = (m.overlay === false) ? "Separate pane" : "Overlay";
        badge2.title = (m.overlay === false)
          ? "Add to a dedicated pane below the chart"
          : "Draws on the price chart";
        meta.appendChild(badge2);
        meta.appendChild(ds);
        var au = document.createElement("span");
        au.className = "ilib-author"; au.textContent = m.author || "";
        row.appendChild(star); row.appendChild(meta); row.appendChild(au);
        if (!m.builtin) {
          var del = document.createElement("button");
          del.className = "ilib-del"; del.textContent = "\uD83D\uDDD1";
          del.title = "Delete script";
          del.addEventListener("click", function (ev) {
            ev.stopPropagation(); self.deleteScript(m);
          });
          row.appendChild(del);
        }
        if (self.onPane(m.id)) {
          var badge = document.createElement("span");
          badge.className = "ilib-added"; badge.textContent = "\u2713 on chart";
          row.appendChild(badge);
        }
        row.addEventListener("click", function () { self.addToPane(m); });
        el.appendChild(row);
      });
    },

    toggleFav: function (m) {
      var self = this;
      fetch("/api/indicators/favorite", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: m.id, favorite: !m.favorite })
      }).then(function () {
        m.favorite = !m.favorite; self.paintList();
      });
    },

    deleteScript: function (m) {
      var self = this;
      if (!confirm("Delete '" + m.name + "' from My scripts?")) return;
      fetch("/api/indicators/delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: m.id })
      }).then(function (r) { return r.json(); }).then(function (j) {
        if (!j.ok) { toast("Delete failed: " + j.message); return; }
        IND_LIB = null;
        Layout.panes.forEach(function (p) {
          p.state.indicators = (p.state.indicators || []).filter(function (x) { return x.id !== m.id; });
          p.saveState(); p.loadIndicator();
        });
        indLibrary(function () { self.paintList(); });
        toast("Deleted " + m.name);
      });
    },

    addToPane: function (m) {
      var pane = this._pane;
      if (!pane) return;
      var list = pane.state.indicators, ex = null;
      for (var i = 0; i < list.length; i++)
        if (list[i].id === m.id) ex = list[i];
      if (ex) { ex.enabled = true; ex.visible = true; }
      else list.push({ id: m.id, enabled: true, visible: true, settings: indDefaults(m) });
      pane.saveState(); pane.loadIndicator();
      toast(m.name + " added to chart");
      this.paintList();
    },

    /* ----------------------------------------------- script editor */
    template: function () {
      return '"""My indicator - describe what it draws."""\n\n' +
        'META = {\n    "id": "my_indicator",   # unique: a-z 0-9 _ (must match the ID field)\n' +
        '    "name": "Example Indicator",\n    "author": "StockSophist",\n' +
        '    "description": "What it draws and why.",\n}\n\n' +
        'INPUTS = [\n    {"key": "length", "label": "Length", "type": "int", "default": 14,\n' +
        '     "min": 1, "max": 500, "group": "Settings"},\n' +
        '    {"key": "show", "label": "Show", "type": "bool", "default": True,\n' +
        '     "group": "Settings"},\n' +
        '    # types: bool, int, float, string, color, options\n' +
        '    # {"key": "mode", "label": "Mode", "type": "options", "default": "A",\n' +
        '    #  "options": ["A", "B"], "group": "Settings"},\n]\n\n' +
        'def compute(ctx):\n' +
        '    """ctx: symbol, tf, bars (DataFrame time/open/high/low/close, ascending),\n' +
        '       inputs (merged settings), minutes, get_bars(api_tf) for other TFs.\n' +
        '       Return {"lines": [...], "dashboard": {...}|None, "sweeps": [...]}."""\n' +
        '    bars = ctx["bars"]\n    inp = ctx["inputs"]\n    n = max(1, int(inp.get("length", 14)))\n' +
        '    ma = bars["close"].rolling(n).mean()\n    lines = []\n' +
        '    times = bars["time"].tolist()\n    start = max(1, len(times) - 2000)\n' +
        '    prev_t, prev_v = None, None\n' +
        '    for t, v in zip(times[start:], ma.tolist()[start:]):\n' +
        '        if v != v:  # NaN guard\n            prev_t, prev_v = None, None\n            continue\n' +
        '        t, v = int(t.timestamp() if hasattr(t, "timestamp") else t), float(v)\n' +
        '        if prev_t is not None:\n' +
        '            lines.append({"t0": prev_t, "t1": t, "p0": prev_v, "p1": v,\n' +
        '                          "color": "#2962ff", "width": 2, "style": "Solid",\n' +
        '                          "label": "", "label_size": "Small"})\n' +
        '        prev_t, prev_v = t, v\n' +
        '    # sweeps: {"key": str, "text": str, "price": float, "alert": bool}\n' +
        '    # dashboard: {"title": str, "rows": [{"text","color"}], "position": str,\n' +
        '    #             "size": str, "bg": str, "fg": str}\n' +
        '    return {"lines": lines, "dashboard": None, "sweeps": []}\n';
    },

    openEditor: function (existing) {
      var self = this;
      var body =
        '<label class="f">Script ID (a-z, 0-9, _ - must match META id)</label>' +
        '<input id="ied-id" class="txt" value="' + escHtml(existing ? existing.id : "") + '">' +
        '<label class="f">Name</label>' +
        '<input id="ied-name" class="txt" value="' + escHtml(existing ? existing.name : "") + '">' +
        '<label class="f">Python code</label>' +
        '<textarea id="ied-code" class="code" spellcheck="false">' +
          escHtml(existing ? existing.code : this.template()) + "</textarea>" +
        '<div class="ied-note">Runs locally on this PC when the chart loads. ' +
        "Keep META id in sync with the Script ID field above.</div>";
      Modal.open("New indicator script", body,
        '<button class="btn" id="ied-cancel">Cancel</button>' +
        '<button class="btn primary" id="ied-save">Save to My scripts</button>');
      $("ied-cancel").addEventListener("click", function () { self.render(); });
      $("ied-save").addEventListener("click", function () {
        var id = $("ied-id").value.trim().toLowerCase(),
            name = $("ied-name").value.trim() || id,
            code = $("ied-code").value;
        $("ied-save").disabled = true; $("ied-save").textContent = "Validating...";
        fetch("/api/indicators/add", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: id, name: name, code: code })
        }).then(function (r) { return r.json(); }).then(function (j) {
          if (!j.ok) {
            toast("Not saved: " + j.message);
            $("ied-save").disabled = false; $("ied-save").textContent = "Save to My scripts";
            return;
          }
          IND_LIB = null;
          indLibrary(function () {
            self._tab = "mine"; self.render(); toast(j.message);
          });
        }).catch(function () {
          toast("Save failed (server unreachable)");
          $("ied-save").disabled = false; $("ied-save").textContent = "Save to My scripts";
        });
      });
    },

    /* ----------------------------------------------- settings form */
    openSettings: function (pane, inst) {
      var meta = indById(inst.id);
      if (!meta) { toast("Indicator '" + inst.id + "' not found in library"); return; }
      var merged = indDefaults(meta);
      for (var k in (inst.settings || {})) merged[k] = inst.settings[k];
      var groups = {}, order = [];
      (meta.inputs || []).forEach(function (inp) {
        var g = inp.group || "Settings";
        if (!groups[g]) { groups[g] = []; order.push(g); }
        groups[g].push(inp);
      });
      var body = '<div class="iset-sub">' + escHtml(meta.name) +
        (meta.attribution ? ' <span class="iset-attr">' + escHtml(meta.attribution) + "</span>" : "") +
        "</div>";
      var self = this;
      order.forEach(function (g) {
        body += '<div class="iset-group">' + escHtml(g) + "</div>";
        groups[g].forEach(function (inp) { body += self.fieldHtml(inp, merged[inp.key]); });
      });
      Modal.open(meta.name + " - settings", body,
        '<button class="btn ghost" id="iset-reset" style="margin-right:auto" title="Restore every setting to its default value">Reset to defaults</button>' +
        '<button class="btn" id="iset-cancel">Cancel</button>' +
        '<button class="btn primary" id="iset-save">OK</button>');
      document.querySelector("#modal-root .mbox").classList.add("isetw");
      $("iset-cancel").addEventListener("click", Modal.close);
      $("iset-reset").addEventListener("click", function () {
        /* One click restores every declared input to its factory default:
           clears the saved global override (server-side) AND the instance
           settings, then reloads the indicator - nothing dummy, it uses
           the same defaults the modal was built from.*/
        if (IND_SETTINGS) delete IND_SETTINGS[meta.id];
        fetch("/api/indicator_settings", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ indicator: meta.id, reset: true })
        }).catch(function () {
          toast("Global default NOT reset (server unreachable)");
        });
        inst.settings = indDefaults(meta);
        pane.saveState(); Modal.close(); pane.loadIndicator(false);
        toast(meta.name + ": settings reset to defaults");
      });
      document.querySelectorAll('#modal-root [data-calpha]').forEach(function (sl) {
        sl.addEventListener("input", function () {
          var pct = sl.closest(".colorrow").querySelector(".colorpct");
          if (pct) pct.textContent = sl.value + "%";
        });
      });
      $("iset-save").addEventListener("click", function () {
        var vals = {};
        (meta.inputs || []).forEach(function (inp) {
          vals[inp.key] = self.readField(inp);
        });
        inst.settings = vals;
        pane.saveState(); Modal.close(); pane.loadIndicator();
        /* the change also becomes the new global default, so
           new instances and restarts pick it up.*/
        saveIndDefault(meta.id, vals);
      });
    },

    fieldHtml: function (inp, val) {
      var k = escHtml(inp.key), label = escHtml(inp.label || inp.key);
      if (inp.type === "bool")
        return '<label class="ifield chk"><input type="checkbox" data-k="' + k + '"' +
          (val ? " checked" : "") + "><span>" + label + "</span></label>";
      if (inp.type === "options") {
        var opts = (inp.options || []).map(function (o) {
          return '<option value="' + escHtml(o) + '"' + (String(o) === String(val) ? " selected" : "") +
            ">" + escHtml(o) + "</option>";
        }).join("");
        return '<label class="ifield"><span>' + label + "</span><select data-k=\"" + k + "\">" +
          opts + "</select></label>";
      }
      if (inp.type === "int" || inp.type === "float") {
        return '<label class="ifield"><span>' + label + "</span><input type=\"number\" data-k=\"" + k +
          "\" value=\"" + escHtml(val) + "\"" +
          (inp.min !== undefined ? ' min="' + inp.min + '"' : "") +
          (inp.max !== undefined ? ' max="' + inp.max + '"' : "") +
          (inp.type === "float" ? ' step="any"' : ' step="1"') + "></label>";
      }
      if (inp.type === "color") {
        /* Native palette picker (the swatch always shows the selected color)
           plus an opacity slider, since script colors carry an alpha channel.*/
        return '<label class="ifield"><span>' + label + "</span>" +
          '<span class="colorrow"><input type="color" data-k="' + k + '" data-crgb="1" value="' +
          colorRgb6(val) + '" title="Pick a color">' +
          '<input type="range" min="0" max="100" step="1" data-k="' + k + '" data-calpha="1" value="' +
          colorAlphaPct(val) + '" title="Opacity %">' +
          '<span class="colorpct">' + colorAlphaPct(val) + '%</span></span></label>';
      }
      return '<label class="ifield"><span>' + label + "</span><input type=\"text\" data-k=\"" + k +
        "\" value=\"" + escHtml(val) + "\"></label>";
    },

    readField: function (inp) {
      if (inp.type === "color") {
        var rgb = document.querySelector('[data-k="' + inp.key + '"][data-crgb]');
        var al = document.querySelector('[data-k="' + inp.key + '"][data-calpha]');
        if (!rgb) return inp.default;
        return joinColor8(rgb.value, al ? al.value : 100);
      }
      var el = document.querySelector('[data-k="' + inp.key + '"]');
      if (!el) return inp.default;
      if (inp.type === "bool") return el.checked;
      if (inp.type === "int") return parseInt(el.value, 10) || 0;
      if (inp.type === "float") return parseFloat(el.value) || 0;
      return el.value;
    }
  };

  /* ================================================= CtxMenu: right-click menu */
  var CtxMenu = {
    show: function (x, y, items) {
      this.hide();
      var m = $("ctxmenu");
      m.innerHTML = "";
      items.forEach(function (it) {
        if (!it) return;
        var d = document.createElement("div");
        if (it.sep) { d.className = "sep"; m.appendChild(d); return; }
        d.className = "mi"; d.textContent = it.label;
        d.addEventListener("click", function (e) {
          e.stopPropagation(); CtxMenu.hide(); it.fn();
        });
        m.appendChild(d);
      });
      m.style.display = "block";
      m.style.left = Math.max(4, Math.min(x, window.innerWidth - 230)) + "px";
      m.style.top = Math.max(4, Math.min(y, window.innerHeight - items.length * 34 - 12)) + "px";
      setTimeout(function () { document.addEventListener("click", CtxMenu.hide); }, 0);
    },
    hide: function () {
      var m = $("ctxmenu");
      if (m) { m.style.display = "none"; m.innerHTML = ""; }
      document.removeEventListener("click", CtxMenu.hide);
    }
  };

  /* ================================================= MT5UI: connection dialog
  All settings persist server-side in app/mt5_connection.json (the "settings
  file"). The password is never sent back to the browser; leaving the field
  blank on save keeps the stored one.*/
  var MT5UI = {
    open: function () {
      var self = this;
      fetch("/api/mt5/status").then(function (r) { return r.json(); })
        .then(function (st) { self.render(st); })
        .catch(function (e) { toast("MT5 status failed: " + e.message); });
    },
    statusHtml: function (st) {
      if (st.connected)
        return '<div class="statusline ok">Connected - ' + escHtml(String(st.login || "")) +
          " @ " + escHtml(String(st.server || "")) + "</div>";
      if (st.busy)
        return '<div class="statusline busy">Connecting...</div>';
      if (st.error)
        return '<div class="statusline err">' + escHtml(st.error) + "</div>";
      if (!st.has_mt5)
        return '<div class="statusline err">MetaTrader5 Python package not found. ' +
          "Run <b>start_chart.bat</b> on Windows (it installs it).</div>";
      if (st.configured)
        return '<div class="statusline busy">Not connected. Press Connect.</div>';
      return '<div class="note">Fill in your terminal login below and press Connect. ' +
        "Settings are saved on the server so you only do this once.</div>";
    },
    render: function (st) {
      var self = this;
      var hasPw = st.configured; // password may be stored; never returned
      var body =
        '<div id="mt5-status">' + this.statusHtml(st) + "</div>" +
        '<label class="f">Terminal path (terminal64.exe) - blank = attach to the running terminal</label>' +
        '<div style="display:flex;gap:6px"><input type="text" id="mt5-path" style="flex:1" value="' +
          escHtml(st.path || "") + '" placeholder="auto-detect">' +
        '<button class="btn ghost" id="mt5-detect" type="button">Detect</button></div>' +
        '<label class="f">Login (account number)</label>' +
        '<input type="text" id="mt5-login" value="' + escHtml(st.login || "") + '">' +
        '<label class="f">Password' + (hasPw ? " (saved - leave blank to keep)" : "") + "</label>" +
        '<input type="password" id="mt5-password" value="" autocomplete="off">' +
        '<label class="f">Server (exact name, e.g. Exness-MT5Real)</label>' +
        '<input type="text" id="mt5-server" value="' + escHtml(st.server || "") + '">' +
        '<label class="f">Timeout (ms)</label>' +
        '<input type="number" id="mt5-timeout" value="60000" step="1000">' +
        '<label class="chk"><input type="checkbox" id="mt5-portable"> Portable mode</label>' +
        '<label class="chk"><input type="checkbox" id="mt5-remember" checked> Remember these settings on this PC</label>' +
        '<div class="note">Tip: keep the MT5 terminal itself open and logged in - ' +
        "the app attaches to it for live prices and history.</div>";
      Modal.open("MetaTrader 5 connection", body,
        '<button class="btn danger" id="mt5-disconnect">Disconnect</button>' +
        '<button class="btn ghost" id="mt5-close">Close</button>' +
        '<button class="btn" id="mt5-connect">Connect</button>');
      $("mt5-close").addEventListener("click", Modal.close);
      $("mt5-detect").addEventListener("click", function () {
        fetch("/api/mt5/detect_paths").then(function (r) { return r.json(); })
          .then(function (j) {
            var p = (j.paths || [])[0] || "";
            if (p) { $("mt5-path").value = p; toast("Terminal found"); }
            else toast("No terminal found - enter the path manually");
          }).catch(function (e) { toast(e.message); });
      });
      $("mt5-connect").addEventListener("click", function () {
        var cfg = {
          path: $("mt5-path").value.trim(),
          login: $("mt5-login").value.trim(),
          password: $("mt5-password").value,
          server: $("mt5-server").value.trim(),
          timeout: parseInt($("mt5-timeout").value, 10) || 60000,
          portable: $("mt5-portable").checked,
          remember: $("mt5-remember").checked
        };
        $("mt5-status").innerHTML = '<div class="statusline busy">Connecting...</div>';
        fetch("/api/mt5/connect", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(cfg)
        }).then(function (r) { return r.json(); })
          .then(function () { self.pollStatus(12); })
          .catch(function (e) { toast(e.message); });
      });
      $("mt5-disconnect").addEventListener("click", function () {
        fetch("/api/mt5/disconnect", { method: "POST" })
          .then(function () { return fetch("/api/mt5/status").then(function (r) { return r.json(); }); })
          .then(function (st2) {
            $("mt5-status").innerHTML = self.statusHtml(st2);
            Layout.pollMt5Badge();
          });
      });
    },
    pollStatus: function (n) {
      var self = this;
      if (n <= 0 || !$("mt5-status")) return;
      fetch("/api/mt5/status").then(function (r) { return r.json(); })
        .then(function (st) {
          if (!$("mt5-status")) return;
          $("mt5-status").innerHTML = self.statusHtml(st);
          Layout.pollMt5Badge();
          Layout.panes.forEach(function (p) { p.load(true); });
          if (!st.busy && (st.connected || st.error)) return;
          setTimeout(function () { self.pollStatus(n - 1); }, 2000);
        }).catch(function () {});
    }
  };

  /* ================================================= Data: backfill / backup / restore */
  var DataUI = {
    jobId: null, jobTimer: null,
    open: function () {
      var self = this;
      var body =
        '<div class="note" style="margin-bottom:6px"><b>Data feed</b> - where live prices and history come from. ' +
        'Currently active: <b id="data-feed-active">...</b></div>' +
        '<div class="drow" style="margin-bottom:10px">' +
          '<label class="chk"><input type="radio" name="data-feed-src" value="mt5"> MT5 terminal</label>' +
          '<label class="chk"><input type="radio" name="data-feed-src" value="oanda"> OANDA <span class="note">(free practice feed)</span></label>' +
        '</div>' +
        '<div id="data-feed-oanda" style="display:none;margin-bottom:10px">' +
          '<div class="note" style="margin-bottom:6px">OANDA practice account - free, no deposit, full API access. ' +
          'Sign up at oanda.com, then generate a token under <b>My Account -> My Services -> Manage API Access</b>.</div>' +
          '<div class="drow" style="margin-bottom:6px">' +
            '<label style="min-width:120px">API token</label>' +
            '<input id="data-oanda-token" type="password" class="txt" style="flex:1;margin-bottom:0" placeholder="paste token (blank = keep saved)">' +
          '</div>' +
          '<div class="drow" style="margin-bottom:6px">' +
            '<label style="min-width:120px">Account ID</label>' +
            '<input id="data-oanda-acct" class="txt" style="flex:1;margin-bottom:0" placeholder="e.g. 001-001-1234567-001">' +
          '</div>' +
        '</div>' +
        '<div class="drow" style="margin-bottom:10px">' +
          '<button class="btn primary" id="data-feed-save">(save) Save feed settings</button>' +
          '<span class="note" id="data-feed-msg" style="align-self:center"></span>' +
        '</div>' +
        '<div class="note" style="margin-bottom:10px">Switching the feed needs an <b>app restart</b> - ' +
        'each feed keeps its own history cache, so nothing is ever mixed.</div>' +
        '<hr style="border-color:#2a2e39;margin:12px 0">' +
        '<div class="note" style="margin-bottom:6px"><b>History database file</b> - paste the full .db path if auto-detect misses it (saved, survives restarts):</div>' +
        '<div style="display:flex;gap:8px;margin-bottom:6px">' +
          '<input id="data-dbpath" class="txt" style="flex:1;margin-bottom:0;font-family:Consolas,monospace" placeholder="e.g. C:\\StockSophist\\data\\stocksophist_xauusd_m1_2009-2026_20260925_064407.db">' +
          '<button class="btn" id="data-browse" style="flex:none">Browse...</button>' +
          '<button class="btn primary" id="data-dbsave" style="flex:none">Save path</button>' +
          '<button class="btn" id="data-rescan" style="flex:none">(refresh) Rescan</button>' +
        '</div>' +
        '<div id="data-dbstatus" style="margin-bottom:10px"><div class="statusline">Locating database...</div></div>' +
        '<div id="data-historydb" style="margin-bottom:10px"><div class="statusline">Checking history database...</div></div>' +
        '<hr style="border-color:#2a2e39;margin:12px 0">' +
        '<div class="note" style="margin-bottom:8px"><b>Tick data</b> - check for gaps, backfill from XM, save separate backups, merge them back.</div>' +
        '<div id="data-tickgap" style="margin-bottom:8px"><div class="statusline">Checking tick gap...</div></div>' +
        '<div class="drow" style="margin-bottom:10px">' +
          '<button class="btn primary" id="data-tickbackfill">(down) Backfill missing ticks</button>' +
          '<button class="btn" id="data-tickrefresh">(refresh) Check gap</button>' +
          '<button class="btn" id="data-diaggaps">(search) Diagnose chart gaps</button>' +
          '<button class="btn" id="data-repairgaps" title="Force-fill missing chart candles from MT5 now">(settings) Repair chart gaps</button>' +
        '</div>' +
        '<div id="data-tickjob" style="margin-bottom:8px"></div>' +
        '<div id="data-gapdiag" style="margin-bottom:8px"></div>' +
        '<div id="data-repairmsg" style="margin-bottom:8px"></div>' +
        '<div class="note" style="margin-bottom:6px"><b>Incremental backup</b> - fetch a date range from XM into its own database file:</div>' +
        '<div class="drow" style="margin-bottom:10px">' +
          '<input id="data-incr-start" type="date" class="txt" style="width:150px;flex:none;margin-bottom:0">' +
          '<span style="color:#787b86">-></span>' +
          '<input id="data-incr-end" type="date" class="txt" style="width:150px;flex:none;margin-bottom:0">' +
          '<button class="btn" id="data-incr-backup">(save) Save as separate DB</button>' +
        '</div>' +
        '<div class="note" style="margin-bottom:6px"><b>Import ticks</b> - load a .parquet file from fetch_ticks.py into the main database:</div>' +
        '<div class="drow" style="margin-bottom:10px">' +
          '<input type="file" id="data-tickimport-file" accept=".parquet" class="txt" style="flex:1;margin-bottom:0">' +
          '<button class="btn primary" id="data-tickimport">(up) Import ticks</button>' +
        '</div>' +
        '<div class="note" style="margin-bottom:6px"><b>Merge incrementals</b> - combine separate tick databases back into the main database (single file):</div>' +
        '<div id="data-incrlist" style="margin-bottom:8px"></div>' +
        '<div class="drow" style="margin-bottom:10px">' +
          '<button class="btn primary" id="data-merge-incr">(shuffle) Merge all into main DB</button>' +
        '</div>' +
        '<hr style="border-color:#2a2e39;margin:12px 0">' +
        '<div class="note" style="margin-bottom:10px">Pull full history from MT5 into the local database. ' +
        'M1 bars go back as far as your broker serves (1-2+ years); ticks cover the last 15-30 days. ' +
        'Higher timeframes are built from M1 automatically.</div>' +
        '<div class="drow" style="margin-bottom:10px">' +
          '<label style="min-width:120px">Tick history (days)</label>' +
          '<input id="data-tickdays" type="number" value="15" min="1" max="60" class="txt" style="width:70px">' +
          '<button class="btn primary" id="data-backfill">(down) Start Backfill</button>' +
        '</div>' +
        '<div id="data-job" style="margin-bottom:10px"></div>' +
        '<hr style="border-color:#2a2e39;margin:12px 0">' +
        '<div class="note" style="margin-bottom:8px"><b>Max-depth M1 archive</b> - fetch everything MT5 can give, ' +
        'back to the broker\u2019s oldest bar (may take a while on the first run - progress shows in the top bar). ' +
        'Later runs only fetch what\u2019s missing. Renko bricks are pre-built from the same data.</div>' +
        '<div class="drow" style="margin-bottom:10px">' +
          '<button class="btn primary" id="data-deepsync">(down) Sync max history now</button>' +
          '<span class="note" id="data-deepsync-msg" style="align-self:center"></span>' +
        '</div>' +
        '<div id="data-deepsync-status" style="margin-bottom:10px"></div>' +
        '<hr style="border-color:#2a2e39;margin:12px 0">' +
        '<div class="drow" style="margin-bottom:10px">' +
          '<button class="btn" id="data-backup">(save) Backup now</button>' +
          '<button class="btn" id="data-refresh-list">(refresh) Refresh list</button>' +
        '</div>' +
        '<div id="data-backups"></div>' +
        '<div class="note" style="margin-top:10px">Backups are single files named ' +
        '<b>stocksophist_backup_SYMBOL_YYYYMMDD_HHMMSS.db</b>. Restoring loads the file, then ' +
        'fills any missing ranges from MT5 and merges them in.</div>' +
        '<hr style="border-color:#2a2e39;margin:12px 0">' +
        '<div class="note" style="margin-bottom:8px"><b>Import history</b> - upload a CSV/TXT file ' +
        'with 1m bars (columns: datetime, open, high, low, close). Merges into the database.</div>' +
        '<div class="drow" style="margin-bottom:10px">' +
          '<input type="file" id="data-import-file" accept=".csv,.txt" class="txt" style="flex:1">' +
          '<select id="data-import-tz" class="txt">' +
            '<option value="UTC">UTC</option>' +
            '<option value="Asia/Kolkata">IST</option>' +
            '<option value="Europe/London">London</option>' +
            '<option value="America/New_York">New York</option>' +
          '</select>' +
          '<button class="btn primary" id="data-import">(up) Import</button>' +
        '</div>' +
        '<div id="data-incr" style="margin-top:8px"></div>';
      Modal.open("History Data - Backfill / Backup / Restore", body,
        '<button class="btn ghost" id="data-close">Close</button>');
      $("data-close").addEventListener("click", Modal.close);
      $("data-feed-save").addEventListener("click", function () { self.saveFeed(); });
      Array.prototype.forEach.call(document.querySelectorAll('input[name="data-feed-src"]'),
        function (r) { r.addEventListener("change", function () { self._toggleOanda(); }); });
      self.loadFeed();
      $("data-backfill").addEventListener("click", function () { self.startBackfill(); });
      $("data-deepsync").addEventListener("click", function () { self.deepSync(); });
      $("data-backup").addEventListener("click", function () { self.createBackup(); });
      $("data-refresh-list").addEventListener("click", function () { self.listBackups(); });
      $("data-import").addEventListener("click", function () { self.importHistory(); });
      $("data-tickbackfill").addEventListener("click", function () { self.tickBackfill(); });
      $("data-tickrefresh").addEventListener("click", function () { self.checkTickGap(); });
      $("data-diaggaps").addEventListener("click", function () { self.diagGaps(); });
      $("data-repairgaps").addEventListener("click", function () { self.repairGaps(); });
      $("data-incr-backup").addEventListener("click", function () { self.incrBackup(); });
      $("data-tickimport").addEventListener("click", function () { self.importTicks(); });
      $("data-merge-incr").addEventListener("click", function () { self.mergeIncr(); });
      $("data-rescan").addEventListener("click", function () { self.showDbPath(true); });
      $("data-dbsave").addEventListener("click", function () { self.saveDbPath(); });
      $("data-browse").addEventListener("click", function () { self.browseDb(); });
      this.showDbPath(false);
      this.showHistoryDB();
      this.checkTickGap();
      this.listIncrBackups();
      this.listBackups();
      this.listIncr();
    },
    _fetchT: function (url, opts, ms) {
      // fetch with timeout - never leave a "loading..." stuck forever.
      var ctrl = new AbortController();
      var t = setTimeout(function () { ctrl.abort(); }, ms || 15000);
      return fetch(url, Object.assign({}, opts, { signal: ctrl.signal }))
        .then(function (r) { clearTimeout(t); return r; },
              function (e) { clearTimeout(t); throw e; });
    },
    showDbPath: function (rescan) {
      var self = this;
      $("data-dbstatus").innerHTML = '<div class="statusline">Locating database...</div>';
      self._fetchT("/api/db_path" + (rescan ? "?refresh=1" : "")).then(function (r) { return r.json(); })
        .then(function (d) {
          // Show the found .db path, else the override, else the folder.
          var cur = d.db_path || ((d.debug && d.debug.override && d.debug.override.path) || "") || d.data_dir || "";
          if (document.activeElement !== $("data-dbpath")) $("data-dbpath").value = cur;
          var html;
          if (d.db_found) {
            html = '<div class="statusline ok">[ok] Found: <span style="font-family:Consolas,monospace">' +
              escHtml(d.db_path) + '</span></div>';
          } else {
            html = '<div class="statusline err">[!] No .db file found - paste the full path above and click <b>Save path</b>.</div>';
            if (d.debug && d.debug.candidates) {
              html += '<div class="statusline" style="margin-top:6px">Searched folders:</div>';
              d.debug.candidates.forEach(function (c) {
                var line = (c.exists ? "- " : "- [x] ") + c.dir;
                if (c.exists && c.files && c.files.length) {
                  line += " - found: " + c.files.map(function (f) {
                    return f.path.split(/[\\/]/).pop() + " (" + f.size_mb + " MB)";
                  }).join(", ");
                } else if (c.exists) {
                  line += " - no .db files";
                } else {
                  line += " - folder does not exist";
                }
                html += '<div class="statusline" style="margin-left:12px;font-family:Consolas,monospace;font-size:11px">' +
                  escHtml(line) + '</div>';
              });
            }
          }
          $("data-dbstatus").innerHTML = html;
        })
        .catch(function (e) {
          var why = (e && e.name === "AbortError") ?
            "Server took too long (>15s) - is the Python server stuck? Restart it." :
            "Could not reach server: " + (e.message || "network error");
          $("data-dbstatus").innerHTML = '<div class="statusline err">' + escHtml(why) + '</div>';
        });
    },
    browseDb: function () {
      // server-side folder browser - click through to the .db file.
      // starts at C:\stocksophist\data (or C:\); quick-links, no drive scan.
      var self = this;
      var body = '<div style="margin-bottom:8px">' +
          '<button class="btn" data-ql="C:\\stocksophist\\data" style="margin-right:6px">(folder) C:\\stocksophist\\data</button>' +
          '<button class="btn" data-ql="C:\\" style="margin-right:6px">(folder) C:\\</button>' +
          '<button class="btn" data-ql="D:\\">(folder) D:\\</button>' +
        '</div>' +
        '<div id="browse-crumb" style="font-family:Consolas,monospace;font-size:11px;margin-bottom:8px;color:#9aa0ae"></div>' +
        '<div id="browse-list" style="max-height:300px;overflow-y:auto;border:1px solid #2a2e39;border-radius:6px;padding:6px"></div>' +
        '<div class="statusline" style="margin-top:8px">Click a folder to open it, or click a <b>.db</b> file to pick it.</div>' +
        '<div style="margin-top:8px;display:flex;gap:8px">' +
          '<input id="browse-manual" class="txt" style="flex:1;margin-bottom:0;font-family:Consolas,monospace" placeholder="Or paste full path here, e.g. C:\\stocksophist\\data\\file.db">' +
          '<button class="btn" id="browse-use-manual">Use this path</button>' +
        '</div>';
      Modal.open("Choose history database", body,
        '<button class="btn" id="browse-cancel">Cancel</button>');
      $("browse-cancel").addEventListener("click", Modal.close);
      // manual path fallback - works even if the folder listing fails.
      $("browse-use-manual").addEventListener("click", function () {
        var p = $("browse-manual").value.trim();
        if (!p) { toast("Paste the .db path first"); return; }
        self.open();  // reopen DATA panel first (browser replaced it)
        $("data-dbpath").value = p;
        toast("Path picked - click Save path");
      });
      // Quick-link buttons.
      Modal.open && document.querySelectorAll("#modal-root [data-ql]").forEach(function (b) {
        b.addEventListener("click", function () { load(b.getAttribute("data-ql")); });
      });
      function load(path) {
        var list = $("browse-list"), crumb = $("browse-crumb");
        if (!list) return;
        list.innerHTML = '<div class="statusline busy">Loading...</div>';
        // check backend version FIRST - a stale backend is the
        // #1 cause of browse hanging. Fail fast with a clear message.
        self._fetchT("/api/version", {}, 5000).then(function (r) { return r.json(); })
          .then(function (v) {
            var bv = (v && v.version) || "?";
            if (bv !== APP_VERSION) {
              list.innerHTML = '<div class="statusline err">' +
                'Backend is v' + escHtml(bv) + ' but this page is v' + escHtml(APP_VERSION) + '.<br>' +
                'Run <b>stop_chart.bat</b>, wait for "done", then <b>start_chart.bat</b>, then try Browse again.</div>';
              return null;
            }
            // timeout so a stuck drive can't freeze the dialog forever.
            return self._fetchT("/api/browse?path=" + encodeURIComponent(path), {}, 15000)
              .then(function (r) { return r.json(); });
          })
          .then(function (d) {
            if (!d) return;  // version mismatch already shown
            if (d.error) { list.innerHTML = '<div class="statusline err">' + escHtml(d.error) + '</div>'; return; }
            crumb.textContent = d.path;
            var html = "";
            if (d.parent) html += '<div class="brow-row" data-p="' + escHtml(d.parent) + '" style="cursor:pointer;padding:6px;color:#7ea6ff">(up) .. (up)</div>';
            d.dirs.forEach(function (nm) {
              var full = d.path.replace(/[\\/]$/, "") + "\\" + nm;
              html += '<div class="brow-row" data-p="' + escHtml(full) + '" style="cursor:pointer;padding:6px">(folder) ' + escHtml(nm) + '</div>';
            });
            d.files.forEach(function (f) {
              var full = d.path.replace(/[\\/]$/, "") + "\\" + f.name;
              html += '<div class="brow-row" data-f="' + escHtml(full) + '" style="cursor:pointer;padding:6px;color:#8be28b">(archive) ' +
                escHtml(f.name) + ' <span style="color:#787b86">(' + f.size_mb + ' MB)</span></div>';
            });
            if (!html) html = '<div class="statusline">Empty folder</div>';
            list.innerHTML = html;
            list.querySelectorAll(".brow-row").forEach(function (row) {
              row.addEventListener("click", function () {
                if (row.getAttribute("data-f")) {
                  var picked = row.getAttribute("data-f");
                  self.open();  // reopen DATA panel first (browser replaced it)
                  $("data-dbpath").value = picked;
                  toast("Path picked - click Save path");
                } else {
                  load(row.getAttribute("data-p"));
                }
              });
              row.addEventListener("mouseover", function () { row.style.background = "#2a2e39"; });
              row.addEventListener("mouseout", function () { row.style.background = ""; });
            });
          })
          .catch(function (e) {
            var why = (e && e.name === "AbortError") ?
              "Timed out listing drives - a disconnected network drive may be stalling. Type the path manually instead." :
              escHtml(e.message || "failed to list folders");
            list.innerHTML = '<div class="statusline err">' + why + '</div>';
          });
      }
      load("");
    },
    saveDbPath: function () {
      var self = this;
      var p = $("data-dbpath").value.trim();
      $("data-dbstatus").innerHTML = '<div class="statusline busy">Saving path...</div>';
      fetch("/api/db_path", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: p })
      }).then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.ok && d.db_found) {
            $("data-dbstatus").innerHTML = '<div class="statusline ok">[ok] Saved and found: <span style="font-family:Consolas,monospace">' +
              escHtml(d.db_path) + '</span></div>';
          } else if (d.ok) {
            $("data-dbstatus").innerHTML = '<div class="statusline err">Saved, but file not found there.</div>';
          } else {
            $("data-dbstatus").innerHTML = '<div class="statusline err">' + escHtml(d.error || "Save failed") + '</div>';
          }
          self.showHistoryDB();
          self.checkTickGap();
        })
        .catch(function (e) {
          $("data-dbstatus").innerHTML = '<div class="statusline err">Save failed: ' + escHtml(e.message) + '</div>';
        });
    },
    checkTickGap: function () {
      $("data-tickgap").innerHTML = '<div class="statusline">Checking tick gap...</div>';
      fetch("/api/ticks/gap").then(function (r) { return r.json(); })
        .then(function (d) {
          var html;
          if (!d.available) {
            html = '<div class="statusline err">Tick gap: main database not found - place the .db file in C:\\stocksophist\\data first.</div>';
          } else if (d.gap_days === 0) {
            html = '<div class="statusline ok">[ok] Tick data is up to date (newest: ' + d.db_newest + ').</div>';
          } else {
            html = '<div class="statusline err">[!] ' + d.gap_days + ' trading day(s) missing ticks: ' +
              d.missing_dates.join(", ") + '<br>Database newest: ' + d.db_newest +
              ' - click "(down) Backfill missing ticks" to fetch from XM.</div>';
          }
          $("data-tickgap").innerHTML = html;
        })
        .catch(function () {
          $("data-tickgap").innerHTML = '<div class="statusline err">Gap check failed</div>';
        });
    },
    repairGaps: function () {
      // force the MT5 gap-fill now (bypasses the hourly throttle).
      var el = $("data-repairmsg");
      el.innerHTML = '<div class="statusline busy">Repairing chart gaps from MT5...</div>';
      var tf = (window.stocksophistTF || "1m");
      this._fetchT("/api/repair_gaps", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tf: tf })
      }, 60000).then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.ok) {
            el.innerHTML = '<div class="statusline err">Repair failed: ' +
              escHtml(d.error || "unknown") + '</div>';
            return;
          }
          if (d.new_bars > 0) {
            el.innerHTML = '<div class="statusline ok">[ok] Repaired: +' + d.new_bars +
              ' bars filled from MT5. Reload the chart to see them.</div>';
          } else if (d.still_has_gaps) {
            el.innerHTML = '<div class="statusline err">MT5 did not return the missing bars ' +
              '(still has gaps). Try again in a minute - the terminal may still be syncing.</div>';
          } else {
            el.innerHTML = '<div class="statusline ok">[ok] No gaps found - chart data is complete.</div>';
          }
        })
        .catch(function (e) {
          el.innerHTML = '<div class="statusline err">Repair failed: ' +
            escHtml(e.message || "network error") + '</div>';
        });
    },
    diagGaps: function () {
      var el = $("data-gapdiag");
      el.innerHTML = '<div class="statusline busy">Diagnosing chart gaps...</div>';
      var tf = (window.stocksophistTF || "1m");
      fetch("/api/diag/gaps?tf=" + encodeURIComponent(tf) + "&bars=600")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.error) { el.innerHTML = '<div class="statusline err">' + escHtml(d.error) + '</div>'; return; }
          function fmt(t) {
            var dt = new Date(t * 1000);
            return dt.toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit",
              month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
          }
          var html = '<div class="statusline">Chart bars: <b>' + d.bars + '</b> (' + tf + ')' +
            ' - vault-healed: <b>' + d.vault_healed_bars + '</b>' +
            ' - ticks recorded today: <b>' + (d.vault.ticks_today == null ? "?" : d.vault.ticks_today) + '</b>' +
            ' - MT5 live: <b>' + (d.feed_live ? "yes" : "NO") + '</b></div>';
          if (d.served_gap_count === 0) {
            html += '<div class="statusline ok">[ok] No gaps in the served chart data.</div>';
          } else {
            html += '<div class="statusline err">[!] ' + d.served_gap_count + ' gap(s) in chart data:</div>';
            d.served_gaps.forEach(function (g) {
              html += '<div class="statusline err" style="margin-left:12px">- ' +
                fmt(g.from) + ' -> ' + fmt(g.to) + ' IST (' + g.slots + ' missing ' + tf + ' bars)</div>';
            });
          }
          if (d.mt5_gap_count > 0 && !d.mt5_gaps[0].error) {
            html += '<div class="statusline">MT5 itself is missing ' + d.mt5_gap_count +
              ' gap(s) in the same window - the holes come from the broker feed, not the app.</div>';
          } else if (d.mt5_gap_count === 0) {
            html += '<div class="statusline ok">MT5 serves complete data here - gaps (if any) are in the app cache/vault.</div>';
          }
          el.innerHTML = html;
        })
        .catch(function () {
          el.innerHTML = '<div class="statusline err">Gap diagnosis failed</div>';
        });
    },
    _pollTickJob: function (jobId, doneMsg) {
      var self = this;
      var el = $("data-tickjob");
      var t0 = Date.now();
      var polls = 0;
      var timer = setInterval(function () {
        polls++;
        fetch("/api/ticks/job/" + jobId).then(function (r) { return r.json(); })
          .then(function (d) {
            var elapsed = Math.round((Date.now() - t0) / 1000);
            var msg = d.message || ("working... (" + elapsed + "s)");
            el.innerHTML = '<div class="statusline busy">' + escHtml(msg) +
              (d.progress ? " (" + d.progress + "%)" : "") + '</div>';
            if (d.done) {
              clearInterval(timer);
              el.innerHTML = '<div class="statusline ' + (d.message && d.message.indexOf("[ok]") === 0 ? "ok" : "") + '">' +
                escHtml(d.message || "done") + '</div>';
              self.checkTickGap();
              self.showHistoryDB();
              self.listIncrBackups();
            }
          })
          .catch(function (e) {
            // don't silently freeze - show the failure.
            clearInterval(timer);
            el.innerHTML = '<div class="statusline err">Job status unreachable: ' +
              escHtml(e.message || "network error") + ' - is the server still running?</div>';
          });
      }, 2000);
    },
    tickBackfill: function () {
      var self = this;
      var sym = (typeof Layout !== "undefined" && Layout.symbol) ? Layout.symbol : "XAUUSD";
      $("data-tickjob").innerHTML = '<div class="statusline busy">Starting tick backfill...</div>';
      fetch("/api/ticks/backfill", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: sym })
      }).then(function (r) { return r.json(); })
        .then(function (d) { self._pollTickJob(d.job_id); })
        .catch(function (e) {
          $("data-tickjob").innerHTML = '<div class="statusline err">Failed: ' + escHtml(e.message) + '</div>';
        });
    },
    incrBackup: function () {
      var self = this;
      var s = $("data-incr-start").value, e = $("data-incr-end").value;
      if (!s || !e) { toast("Pick start and end dates first"); return; }
      var sym = (typeof Layout !== "undefined" && Layout.symbol) ? Layout.symbol : "XAUUSD";
      $("data-tickjob").innerHTML = '<div class="statusline busy">Starting incremental backup...</div>';
      fetch("/api/ticks/backup_incr", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: sym, start_date: s, end_date: e })
      }).then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.error) { $("data-tickjob").innerHTML = '<div class="statusline err">' + escHtml(d.error) + '</div>'; return; }
          self._pollTickJob(d.job_id);
        })
        .catch(function (e) {
          $("data-tickjob").innerHTML = '<div class="statusline err">Failed: ' + escHtml(e.message) + '</div>';
        });
    },
    importTicks: function () {
      var self = this;
      var fi = $("data-tickimport-file");
      if (!fi.files.length) { toast("Choose a .parquet file first"); return; }
      var fd = new FormData();
      fd.append("file", fi.files[0]);
      $("data-tickjob").innerHTML = '<div class="statusline busy">Uploading and importing ticks...</div>';
      fetch("/api/ticks/import_parquet", { method: "POST", body: fd })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.error) {
            $("data-tickjob").innerHTML = '<div class="statusline err">' + escHtml(d.error) + '</div>';
          } else {
            $("data-tickjob").innerHTML = '<div class="statusline ok">' + escHtml(d.message) + '</div>';
            self.checkTickGap();
            self.showHistoryDB();
          }
        })
        .catch(function (e) {
          $("data-tickjob").innerHTML = '<div class="statusline err">Failed: ' + escHtml(e.message) + '</div>';
        });
    },
    listIncrBackups: function () {
      fetch("/api/ticks/incr_list").then(function (r) { return r.json(); })
        .then(function (d) {
          var html;
          if (!d.files || !d.files.length) {
            html = '<div class="note">No separate tick backup files in the data folder yet.</div>';
          } else {
            html = d.files.map(function (f) {
              return '<div class="statusline">(package) ' + escHtml(f.file) + ' - ' +
                Number(f.ticks).toLocaleString() + ' ticks (' + escHtml(f.range) + '), ' +
                f.size_mb + ' MB</div>';
            }).join("");
          }
          $("data-incrlist").innerHTML = html;
        })
        .catch(function () {
          $("data-incrlist").innerHTML = '<div class="note">Could not list incremental files.</div>';
        });
    },
    mergeIncr: function () {
      var self = this;
      if (!confirm("Merge ALL incremental tick DBs into the main database? The separate files will be deleted after merging.")) return;
      $("data-tickjob").innerHTML = '<div class="statusline busy">Merging incremental DBs...</div>';
      fetch("/api/ticks/merge_incr", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
        .then(function (r) { return r.json(); })
        .then(function (d) { self._pollTickJob(d.job_id); })
        .catch(function (e) {
          $("data-tickjob").innerHTML = '<div class="statusline err">Failed: ' + escHtml(e.message) + '</div>';
        });
    },
    showHistoryDB: function () {
      fetch("/api/history_status").then(function (r) { return r.json(); })
        .then(function (d) {
          var html;
          if (d.available) {
            html = '<div class="statusline ok">(disk) History DB: <b>' +
              Number(d.m1_bars).toLocaleString() + '</b> M1 bars (' + d.m1_start + ' -> ' + d.m1_end + '), <b>' +
              Number(d.tick_count).toLocaleString() + '</b> ticks (' + d.tick_start + ' -> ' + d.tick_end + ') - ' +
              d.size_mb + ' MB<br><span style="opacity:.7">' + escHtml(d.path) + '</span></div>';
          } else {
            html = '<div class="statusline err">[!] History database not found.<br>' +
              'Place the <b>.db</b> file in <span style="color:#9aa0ae">C:\\stocksophist\\data</span> ' +
              '(the <b>data</b> folder next to the app folder), then reopen this panel.<br>' +
              'Charts will use MT5 only until the file is found.</div>';
          }
          $("data-historydb").innerHTML = html;
        })
        .catch(function () {
          $("data-historydb").innerHTML = '<div class="statusline err">History DB check failed</div>';
        });
    },
    importHistory: function () {
      var self = this;
      var fi = $("data-import-file");
      if (!fi.files.length) { toast("Choose a CSV/TXT file first"); return; }
      var tz = $("data-import-tz").value;
      var sym = (typeof Layout !== "undefined" && Layout.symbol) ? Layout.symbol : "XAUUSD";
      var fd = new FormData();
      fd.append("file", fi.files[0]);
      fd.append("symbol", sym);
      fd.append("tz", tz);
      $("data-import").disabled = true;
      $("data-job").innerHTML = '<div class="statusline busy">Uploading & importing...</div>';
      fetch("/api/import/history", {method: "POST", body: fd})
        .then(function (r) { return r.json(); })
        .then(function (d) {
          self.jobId = d.job_id;
          self.pollJob();
          $("data-import").disabled = false;
        })
        .catch(function (e) {
          toast("Import failed: " + e.message);
          $("data-import").disabled = false;
        });
    },
    listIncr: function () {
      var sym = (typeof Layout !== "undefined" && Layout.symbol) ? Layout.symbol : "XAUUSD";
      fetch("/api/incr/status?symbol=" + encodeURIComponent(sym))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var incrs = d.incrementals || [];
          if (!incrs.length) {
            $("data-incr").innerHTML = '<div class="note">No pending incremental files.</div>';
            return;
          }
          var html = '<div class="note" style="margin-bottom:6px"><b>Pending incrementals</b> ' +
            '(merge into main DB on app close):</div>';
          incrs.forEach(function (f) {
            html += '<div class="drow" style="margin-bottom:4px"><span style="flex:1">' +
              escHtml(f.file) + ' <span class="dim">(' + f.bars + " bars, " + f.size_kb + " KB)</span></span></div>";
          });
          html += '<button class="btn sm" id="data-merge-incr" style="margin-top:6px">Merge now</button>';
          $("data-incr").innerHTML = html;
          $("data-merge-incr").addEventListener("click", function () {
            fetch("/api/incr/merge", {
              method: "POST", headers: {"Content-Type": "application/json"},
              body: JSON.stringify({symbol: sym})
            }).then(function (r) { return r.json(); })
              .then(function (d) {
                toast("Merged " + d.merged_bars + " bars");
                DataUI.listIncr();
              });
          });
        });
    },
    deepSync: function () {
      // start the max-depth M1 sync for the active symbol and show
      // its live status. The sync itself runs on the server in the
      // background; later runs only fetch what's missing.
      var self = this;
      var sym = "XAUUSD";
      try {
        var p = (typeof Layout !== "undefined" && Layout.activePane) ? Layout.activePane() : null;
        if (p && p.state && p.state.symbol) sym = p.state.symbol;
      } catch (e) {}
      var msg = $("data-deepsync-msg"), st = $("data-deepsync-status");
      if (msg) msg.textContent = "Starting...";
      fetch("/api/history/deep", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: sym }) })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (msg) msg.textContent = j.note || "Sync running in the background.";
          self.pollDeepSync(sym);
        })
        .catch(function (e) { if (msg) msg.textContent = "Error: " + e.message; });
    },
    pollDeepSync: function (sym) {
      var st = $("data-deepsync-status");
      if (!st) return;
      var short = function (n) {
        n = n || 0;
        if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
        if (n >= 1000) return (n / 1000).toFixed(0) + "k";
        return "" + n;
      };
      var fmtD = function (t) {
        try {
          var d = new Date(t * 1000);
          return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2);
        } catch (e) { return "?"; }
      };
      fetch("/api/history/deep/status?symbol=" + encodeURIComponent(sym))
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!$("data-deepsync-status")) return;
          var arch = j.archive, op = j.op || {}, html = "";
          if (arch && arch.bars)
            html += '<div class="statusline ok">' + short(arch.bars) + " bars - " +
                    fmtD(arch.first) + " -> " + fmtD(arch.last) + "</div>";
          if (op.status === "running")
            html += '<div class="statusline">(wait) ' + escHtml(op.stage || "syncing") +
                    (op.detail ? " - " + escHtml(op.detail) : "") + "</div>";
          else if (op.status === "failed")
            html += '<div class="statusline err">[!] ' + escHtml(op.error || op.message || "failed") + "</div>";
          else if (op.status === "done" && op.message)
            html += '<div class="statusline ok">[ok] ' + escHtml(op.message) + "</div>";
          st.innerHTML = html || '<div class="statusline">No archive yet.</div>';
          if (op.status === "running")
            setTimeout(function () { self.pollDeepSync(sym); }, 3000);
        })
        .catch(function () {});
    },
    /* data-feed switch (MT5 terminal <-> OANDA practice feed) */
    loadFeed: function () {
      var self = this;
      fetch("/api/feed/config").then(function (r) { return r.json(); })
        .then(function (j) {
          var ds = (j && j.data_source) || "mt5";
          var act = (j && j.active_source) || "mt5";
          var lbl = $("data-feed-active");
          if (lbl) lbl.textContent = act === "oanda" ? "OANDA" : "MT5 terminal";
          Array.prototype.forEach.call(
            document.querySelectorAll('input[name="data-feed-src"]'),
            function (r) { r.checked = (r.value === ds); });
          var acct = $("data-oanda-acct");
          if (acct && j && j.oanda) acct.value = j.oanda.account_id || "";
          self._toggleOanda();
        }).catch(function () {});
    },
    _toggleOanda: function () {
      var sel = document.querySelector('input[name="data-feed-src"]:checked');
      var box = $("data-feed-oanda");
      if (box) box.style.display = (sel && sel.value === "oanda") ? "" : "none";
    },
    saveFeed: function () {
      var sel = document.querySelector('input[name="data-feed-src"]:checked');
      var msg = $("data-feed-msg");
      var body = {
        data_source: sel ? sel.value : "mt5",
        token: $("data-oanda-token") ? $("data-oanda-token").value : "",
        account_id: $("data-oanda-acct") ? $("data-oanda-acct").value : ""
      };
      if (msg) msg.textContent = "Saving...";
      fetch("/api/feed/config", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (msg) msg.textContent = (j && j.message) ||
            ((j && j.error) ? ("Error: " + j.error) : "Saved.");
          if ($("data-oanda-token")) $("data-oanda-token").value = "";
        })
        .catch(function (e) { if (msg) msg.textContent = "Error: " + e.message; });
    },
    startBackfill: function () {
      var self = this;
      var days = parseInt($("data-tickdays").value, 10) || 15;
      var sym = (typeof Layout !== "undefined" && Layout.symbol) ? Layout.symbol : "XAUUSD";
      $("data-backfill").disabled = true;
      $("data-job").innerHTML = '<div class="statusline busy">Starting backfill...</div>';
      fetch("/api/backfill/start", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({symbol: sym, tick_days: days})
      }).then(function (r) { return r.json(); })
        .then(function (d) {
          self.jobId = d.job_id;
          self.pollJob();
        })
        .catch(function (e) {
          $("data-job").innerHTML = '<div class="statusline err">Failed: ' + escHtml(e.message) + "</div>";
          $("data-backfill").disabled = false;
        });
    },
    pollJob: function () {
      var self = this;
      if (!self.jobId) return;
      fetch("/api/backfill/status?job_id=" + self.jobId)
        .then(function (r) { return r.json(); })
        .then(function (st) {
          var pct = st.progress || 0;
          var bar = '<div style="background:#0f1420;border-radius:4px;height:18px;overflow:hidden;margin:6px 0">' +
            '<div style="background:#2962ff;height:100%;width:' + pct + '%;transition:width .3s"></div></div>';
          $("data-job").innerHTML = bar +
            '<div class="statusline ' + (st.status === "error" ? "err" : "busy") + '">' +
            escHtml(st.message || "") + "</div>";
          if (st.done) {
            $("data-backfill").disabled = false;
            self.jobId = null;
            if (st.status === "done") {
              toast("Backfill complete");
              // Reload candles to show the new history.
              if (typeof Layout !== "undefined")
                Layout.panes.forEach(function (p) { p.load(true); });
            }
          } else {
            self.jobTimer = setTimeout(function () { self.pollJob(); }, 1500);
          }
        })
        .catch(function () {
          self.jobTimer = setTimeout(function () { self.pollJob(); }, 3000);
        });
    },
    createBackup: function () {
      var sym = (typeof Layout !== "undefined" && Layout.symbol) ? Layout.symbol : "XAUUSD";
      toast("Creating backup...");
      fetch("/api/backup/create", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({symbol: sym})
      }).then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.ok) {
            toast("Backup saved: " + d.file + " (" + d.size_mb + " MB)");
            DataUI.listBackups();
          } else {
            toast("Backup failed: " + (d.error || "unknown"));
          }
        })
        .catch(function (e) { toast("Backup failed: " + e.message); });
    },
    listBackups: function () {
      fetch("/api/backup/list").then(function (r) { return r.json(); })
        .then(function (d) {
          var html = "";
          (d.backups || []).forEach(function (b) {
            html += '<div class="drow" style="margin-bottom:6px">' +
              '<span style="flex:1">' + escHtml(b.file) +
              ' <span class="dim">(' + b.size_mb + " MB, " + escHtml(b.mtime) + ")</span></span>" +
              '<button class="btn sm" data-restore="' + escHtml(b.file) + '">Restore</button>' +
              "</div>";
          });
          if (!html) html = '<div class="note">No backups yet.</div>';
          $("data-backups").innerHTML = html;
          var btns = $("data-backups").querySelectorAll("[data-restore]");
          btns.forEach(function (btn) {
            btn.addEventListener("click", function () {
              DataUI.restoreBackup(btn.getAttribute("data-restore"));
            });
          });
        });
    },
    restoreBackup: function (fname) {
      var self = this;
      if (!confirm("Restore " + fname + "?\n\nCurrent cache will be replaced, then missing data filled from MT5.")) return;
      var sym = (typeof Layout !== "undefined" && Layout.symbol) ? Layout.symbol : "XAUUSD";
      $("data-job").innerHTML = '<div class="statusline busy">Restoring...</div>';
      fetch("/api/backup/restore", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({symbol: sym, file: fname})
      }).then(function (r) { return r.json(); })
        .then(function (d) {
          self.jobId = d.job_id;
          self.pollJob();
        })
        .catch(function (e) { toast("Restore failed: " + e.message); });
    }
  };

  /* ================================================= Watchlist: broker Market Watch */
  /* ----------------------------------------------- watchlist sidebar
     Persistent left panel like a broker Market Watch: always
     visible (hide via the top-bar Watchlist button), live prices every 2s,
     click a symbol to load it on the active chart pane.*/
  var Watchlist = {
    _timer: null,
    init: function () {
      var self = this, panel = $("watchlist");
      var saved = null;
      try { saved = localStorage.getItem("stocksophist_wl_open"); } catch (e) {}
      if (saved === null || saved === "1") panel.classList.add("open");
      $("wl-add").addEventListener("click", function () { self.addPrompt(); });
      $("wl-sync").addEventListener("click", function () { self.syncDialog(); });
      this.applySizes();
      this.wireResize(panel);
      this.refresh();
      this._timer = setInterval(function () {
        if (panel.classList.contains("open")) self.refresh();
      }, 2000);
    },
    /* -------------- resizable window + resizable columns (persisted) */
    LS_W: "stocksophist_wl_width",
    LS_COLS: "stocksophist_wl_cols",
    applySizes: function () {
      var panel = $("watchlist");
      try {
        var w = parseFloat(localStorage.getItem(this.LS_W));
        if (isFinite(w)) panel.style.width = Math.min(560, Math.max(200, w)) + "px";
        var c = JSON.parse(localStorage.getItem(this.LS_COLS) || "null");
        if (c) {
          if (isFinite(c.w0)) panel.style.setProperty("--wlw0", Math.min(400, Math.max(50, c.w0)) + "px");
          if (isFinite(c.w1)) panel.style.setProperty("--wlw1", Math.min(200, Math.max(44, c.w1)) + "px");
          if (isFinite(c.w2)) panel.style.setProperty("--wlw2", Math.min(200, Math.max(44, c.w2)) + "px");
        }
      } catch (e) {}
    },
    saveCols: function () {
      var panel = $("watchlist"), self = this;
      function num(v, d) { var n = parseFloat(v); return isFinite(n) ? n : d; }
      try {
        localStorage.setItem(self.LS_COLS, JSON.stringify({
          w0: num(panel.style.getPropertyValue("--wlw0"), 90),
          w1: num(panel.style.getPropertyValue("--wlw1"), 64),
          w2: num(panel.style.getPropertyValue("--wlw2"), 64)
        }));
      } catch (e) {}
    },
    wireResize: function (panel) {
      var self = this;
      /* right-edge handle: resize the whole watchlist window */
      var edge = document.createElement("div");
      edge.id = "wl-resize"; edge.title = "Drag to resize watchlist";
      panel.appendChild(edge);
      var sx = 0, sw = 0, on = false;
      edge.addEventListener("mousedown", function (e) {
        on = true; sx = e.clientX; sw = panel.getBoundingClientRect().width;
        edge.classList.add("on");
        e.preventDefault(); e.stopPropagation();
      });
      window.addEventListener("mousemove", function (e) {
        if (!on) return;
        panel.style.width = Math.min(560, Math.max(200, sw + e.clientX - sx)) + "px";
      });
      window.addEventListener("mouseup", function () {
        if (!on) return; on = false; edge.classList.remove("on");
        try { localStorage.setItem(self.LS_W, String(panel.getBoundingClientRect().width)); } catch (e) {}
        Layout.panes.forEach(function (p) { p.resize(); });
      });
      /* header dividers: resize the Symbol / Last / Chg columns */
      var keys = ["--wlw0", "--wlw1", "--wlw2"];
      var mins = [50, 44, 44], maxs = [400, 200, 200], defs = [90, 64, 64];
      var heads = panel.querySelectorAll(".wl-cols > span");
      Array.prototype.forEach.call(heads, function (cell, i) {
        if (i > 2) return;
        var dv = document.createElement("i");
        dv.className = "wl-div"; dv.title = "Drag to resize column";
        cell.appendChild(dv);
        dv.addEventListener("mousedown", function (e) {
          var startX = e.clientX;
          var cur = parseFloat(panel.style.getPropertyValue(keys[i])) || defs[i];
          dv.classList.add("on");
          e.preventDefault(); e.stopPropagation();
          var mv = function (ev) {
            var mx = maxs[i];
            if (i === 0) {
              /* symbol column shares the row with the numeric columns: never let
                 the minimums overflow the window width*/
              mx = Math.min(mx, Math.max(mins[i],
                panel.getBoundingClientRect().width - 44 - 44 - 56 - 40));
            }
            var w = Math.min(mx, Math.max(mins[i], cur + ev.clientX - startX));
            panel.style.setProperty(keys[i], w + "px");
          };
          var up = function () {
            window.removeEventListener("mousemove", mv);
            window.removeEventListener("mouseup", up);
            dv.classList.remove("on");
            self.saveCols();
          };
          window.addEventListener("mousemove", mv);
          window.addEventListener("mouseup", up);
        });
      });
    },
    toggle: function () {
      var panel = $("watchlist");
      panel.classList.toggle("open");
      try { localStorage.setItem("stocksophist_wl_open", panel.classList.contains("open") ? "1" : "0"); } catch (e) {}
      if (panel.classList.contains("open")) this.refresh();
      setTimeout(function () {
        Layout.panes.forEach(function (p) { p.resize(); });
      }, 30);
    },
    isOpen: function () { return $("watchlist").classList.contains("open"); },
    addPrompt: function () { this.addDialog(); },
    /* Broker-driven symbol picker: search the broker's real symbol list and
       tap to add - no more typing exact names from memory, so no more
       "terminal call failed" from typos and no GOLD vs GOLD# confusion.
       Adding here also adds the symbol to MT5's own Market Watch.*/
    /* -------------- MT5 watchlist sync
       MT5's Market Watch is the source of truth. Sync re-reads it, starts
       history backfill for symbols the app has never cached, and can prune
       everything except a keep-list (pruning also removes from MT5).*/
    /* sticky dialog (backdrop clicks do not swallow it), shows
       which MT5 terminal/account the app is attached to (same quotes but a
       different terminal install = different Market Watch list), per-row
       remove, and prune that may empty the list entirely.*/
    syncDialog: function () {
      var self = this;
      Modal.open("Sync watchlist with MT5",
        '<div class="note" style="margin-bottom:8px">MT5\u2019s Market Watch is the source of truth. ' +
        "Sync re-reads it and fetches history for newcomers. Removing here also removes from MT5.</div>" +
        '<div id="wl-sync-id" class="note" style="margin-bottom:8px">Checking MT5 connection\u2026</div>' +
        '<div id="wl-sync-list" style="max-height:240px;overflow:auto;margin-bottom:10px">' +
        '<div class="note">Reading MT5\u2026</div></div>' +
        '<div class="note" style="margin-bottom:4px">Prune keeps <b>only</b> these symbols ' +
        '(defaults to exactly what is in MT5\u2019s Market Watch right now). ' +
        'Pruning also removes the rest from MT5.</div>' +
        '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">' +
        '<input id="wl-keep" value="" placeholder="reading MT5\u2026" style="flex:1;min-width:170px;box-sizing:border-box" ' +
        'title="Comma-separated symbols to keep (empty = remove all)">' +
        '<button class="btn" id="wl-prune">Prune to kept</button>' +
        '<button class="btn ghost" id="wl-sync-go">Sync now</button></div>' +
        '<div id="wl-sync-prog" style="margin-top:8px"></div>' +
        '<div id="wl-sync-msg" class="note" style="margin-top:8px"></div>',
        '<button class="btn ghost" id="wl-sync-close">Close</button>',
        { sticky: true });
      $("wl-sync-close").addEventListener("click", function () {
        /* stop the background-op poll when the dialog closes. */
        if (self._syncTimer) { clearInterval(self._syncTimer); self._syncTimer = null; }
        Modal.close();
      });
      /* which terminal are we actually talking to? */
      fetch("/api/status").then(function (r) { return r.json(); })
        .then(function (s) {
          var m = (s && s.mt5) || {};
          var idEl = $("wl-sync-id");
          if (!idEl) return;
          if (!m.connected) {
            idEl.innerHTML = '<span style="color:var(--warn)">MT5 is not connected.</span>';
            return;
          }
          idEl.innerHTML = "<b>Connected MT5:</b> account " + escHtml(String(m.login || "?")) +
            " @ " + escHtml(String(m.server || "?")) +
            (m.terminal_path ? " \u2014 " + escHtml(String(m.terminal_path)) : "") +
            '<br><span style="color:var(--warn)">If this is NOT the terminal on your screen, ' +
            "the list below will NOT match it - the app is attached to a different MT5. " +
            'Use the topbar MT5 button to point it at the right terminal.</span>';
        })
        .catch(function () {});
      function renderList(symbols) {
        if (!symbols.length) {
          $("wl-sync-list").innerHTML = '<div class="note">Market Watch is empty.</div>';
          return;
        }
        $("wl-sync-list").innerHTML = symbols.map(function (x) {
          return '<div class="wl-pick" data-s="' + escHtml(x) + '">' +
            '<span style="flex:1">' + escHtml(x) + "</span>" +
            '<span class="rm" data-rms="' + escHtml(x) + '" title="Remove from MT5 Market Watch" ' +
            'style="cursor:pointer;padding:0 6px">&times;</span></div>';
        }).join("");
        Array.prototype.forEach.call(
          $("wl-sync-list").querySelectorAll("[data-rms]"), function (el) {
            el.addEventListener("click", function (e) {
              e.stopPropagation();
              var sym = el.getAttribute("data-rms");
              if (!window.confirm("Remove " + sym + " from MT5's Market Watch?")) return;
              msg("Removing " + sym + "\u2026");
              fetch("/api/watchlist/remove", { method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ symbol: sym }) })
                .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
                .then(function (res) {
                  if (!res.ok) throw new Error((res.j && res.j.error) || "remove failed");
                  run(null);
                })
                .catch(function (err) {
                  var m = err.message || "remove failed";
                  if (/refused/i.test(m))
                    m += " \u2014 close its chart and any open positions in MT5, then retry.";
                  msg('<span style="color:var(--warn)">' + escHtml(m) + "</span>");
                });
            });
          });
      }
      function msg(t) { $("wl-sync-msg").innerHTML = t; }
      /* default keep = exactly what's in MT5's Market Watch now -
         "remove all remaining and keep which are there in meta trader".
         Never clobbers the user's own edits.*/
      var keepTouched = false;
      /* keep === null means a plain authoritative re-read (sync now,
         initial dialog load, after per-row removal) - send NO keep key so
         the backend prunes nothing. An explicit array (even []) prunes MT5's
         Market Watch down to exactly those symbols.*/
      function run(keep) {
        /* the sync runs as a server-side background op (it can
           block for a minute+ on a sick terminal) - poll its live progress
           instead of hanging on one request.*/
        if (self._syncTimer) { clearInterval(self._syncTimer); self._syncTimer = null; }
        msg("");
        OpsUI.render("wl-sync-prog", null);
        function stopPoll() {
          if (self._syncTimer) { clearInterval(self._syncTimer); self._syncTimer = null; }
        }
        function finishFromOp(op) {
          /* op.status is done/error; the payload rides in op.result. */
          stopPoll();
          OpsUI.render("wl-sync-prog", op);
          if (op.status === "error") {
            msg('<span style="color:var(--warn)">Sync failed - the real error ' +
                'is shown above. You can retry with "Sync now".</span>');
            return;
          }
          var j = op.result || {};
          renderList(j.symbols || []);
          if (!keepTouched) {
            var ki = $("wl-keep");
            if (ki) ki.value = (j.symbols || []).join(",");
          }
          var bits = ["\u2713 App watchlist now matches MT5: " + (j.count || 0) +
            " symbol" + (j.count === 1 ? "" : "s") + "."];
          if (j.removed && j.removed.length)
            bits.push("Removed: " + j.removed.map(escHtml).join(", ") + ".");
          if (j.backfilled && j.backfilled.length)
            bits.push("Fetching history for: " + j.backfilled.map(escHtml).join(", ") + "\u2026");
          if (j.errors && j.errors.length)
            bits.push('<span style="color:var(--warn)">' + j.errors.map(escHtml).join("<br>") + "</span>");
          msg(bits.join(" "));
          self.refresh();
        }
        function pollOp() {
          if (!$("wl-sync-prog")) { stopPoll(); return; } /* dialog closed*/
          fetch("/api/ops/watchlist_sync", { cache: "no-store" })
            .then(function (r) { return r.json(); })
            .then(function (op) {
              if (!$("wl-sync-prog")) { stopPoll(); return; }
              if (!op || op.missing) { stopPoll(); return; }
              OpsUI.render("wl-sync-prog", op);
              if (op.status === "done" || op.status === "error") finishFromOp(op);
            })
            .catch(function () { /* keep polling; the next tick retries*/ });
        }
        fetch("/api/watchlist/sync", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(keep === null ? {} : { keep: keep }) })
          .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
          .then(function (res) {
            var j = res.j;
            if (!res.ok || !j.ok) throw new Error((j && j.error) || "sync failed");
            /* Server started (or re-attached to) the background op. */
            pollOp();
            self._syncTimer = setInterval(pollOp, 2000);
          })
          .catch(function (e) {
            stopPoll();
            OpsUI.render("wl-sync-prog", null);
            msg('<span style="color:var(--warn)">' + escHtml(e.message) + "</span>");
          });
      }
      $("wl-keep").addEventListener("input", function () { keepTouched = true; });
      $("wl-sync-go").addEventListener("click", function () { run(null); });
      $("wl-prune").addEventListener("click", function () {
        var keep = $("wl-keep").value.split(",").map(function (x) { return x.trim(); })
          .filter(function (x) { return !!x; });
        var what = keep.length ? "every Market Watch symbol except " + keep.join(", ")
                               : "EVERY symbol in Market Watch";
        if (!window.confirm("Remove " + what + "?\n(This also removes them from MT5's Market Watch.)")) return;
        run(keep);
      });
      run(null);
    },
    addDialog: function () {
      var self = this;
      Modal.open("Add symbol",
        '<input id="wl-search" placeholder="Type to search your broker\u2019s symbols\u2026" ' +
        'style="width:100%;box-sizing:border-box">' +
        '<div id="wl-results" style="max-height:320px;overflow:auto;margin-top:8px"></div>' +
        '<div class="note" id="wl-add-note">Symbols come straight from your MT5 terminal. ' +
        "Adding one also puts it in MT5's Market Watch.</div>",
        '<button class="btn ghost" id="wl-cancel">Cancel</button>');
      $("wl-cancel").addEventListener("click", Modal.close);
      /* caption is feed-aware - in OANDA mode the symbols come from
         the OANDA account's instrument list, not the MT5 terminal.*/
      fetch("/api/status").then(function (r) { return r.json(); }).then(function (st) {
        var n = $("wl-add-note");
        if (n && st && (st.feed_source === "oanda" || st.mode === "LIVE-OANDA"))
          n.textContent = "Symbols come from your OANDA account's instrument list.";
      }).catch(function () {});
      var box = $("wl-search"), res = $("wl-results"), timer = null, cur = [];
      function have(sym) {
        for (var i = 0; i < cur.length; i++) if (cur[i].symbol === sym) return true;
        return false;
      }
      function run() {
        var q = box.value.trim();
        fetch("/api/symbols/search?q=" + encodeURIComponent(q))
          .then(function (r) { return r.json(); })
          .then(function (j) {
            var list = j.symbols || [];
            res.innerHTML = list.length ? list.map(function (s) {
              var dis = have(s) ? " disabled" : "";
              return '<div class="wl-pick' + dis + '" data-s="' + escHtml(s) + '">' +
                escHtml(s) + (have(s) ? ' <span class="note">- in watchlist</span>' : "") + "</div>";
            }).join("") : '<div class="note">No matches on your broker.</div>';
            Array.prototype.forEach.call(res.querySelectorAll(".wl-pick:not(.disabled)"),
              function (el) {
                el.addEventListener("click", function () { self.addSymbol(el.getAttribute("data-s")); });
              });
          })
          .catch(function () { res.innerHTML = '<div class="note">Search unavailable.</div>'; });
      }
      fetch("/api/watchlist").then(function (r) { return r.json(); })
        .then(function (j) { cur = j.watchlist || []; run(); })
        .catch(function () { run(); });
      box.addEventListener("input", function () {
        clearTimeout(timer); timer = setTimeout(run, 250);
      });
      box.focus();
    },
    addSymbol: function (sym) {
      var self = this;
      fetch("/api/watchlist/add", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: sym })
      }).then(function (r) { return r.json().then(function (j) {
          if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
          return j;
        }); })
        .then(function () { Modal.close(); toast("Added " + sym + " to Market Watch"); self.refresh(); })
        .catch(function (e) { toast(e.message); });
    },
    remove: function (sym) {
      var self = this;
      if (!confirm("Remove " + sym + " from Market Watch?")) return;
      fetch("/api/watchlist/remove", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: sym })
      }).then(function (r) { return r.json().then(function (j) {
          if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
          return j;
        }); })
        .then(function () { toast("Removed " + sym); self.refresh(); })
        .catch(function (e) {
          var m = e.message || "remove failed";
          if (/refused/i.test(m))
            m += " \u2014 close its chart and any open positions in MT5, then retry.";
          toast(m);
        });
    },
    refresh: function () {
      var self = this, body = $("wl-body"), foot = $("wl-foot");
      fetch("/api/watchlist").then(function (r) { return r.json(); })
        .then(function (j) {
          var list = j.watchlist || [], html = "";
          var activeSym = null;
          try { activeSym = Layout.activePane().state.symbol; } catch (e) {}
          list.forEach(function (q) {
            var cls = q.change > 0 ? "up" : (q.change < 0 ? "dn" : "");
            var chg = (q.change > 0 ? "+" : "") + q.change;
            var chgp = (q.change_pct > 0 ? "+" : "") + q.change_pct + "%";
            /* a Market Watch symbol with no tick yet still lists -
               quotes render as em-dashes instead of "null".*/
            var lastTxt = (q.bid === null || q.bid === undefined) ? "\u2014" : q.bid;
            var askTxt = (q.ask === null || q.ask === undefined) ? "\u2014" : q.ask;
            if (q.no_quote) { cls = ""; chg = "\u2014"; chgp = "\u2014"; }
            html += '<div class="wl-row' + (q.symbol === activeSym ? " wl-active" : "") +
              '" data-sym="' + escHtml(q.symbol) + '" title="Bid ' + lastTxt + " / Ask " + askTxt + '">' +
              '<span class="sym">' + escHtml(q.symbol) + "</span>" +
              '<span class="last">' + lastTxt + "</span>" +
              '<span class="' + cls + '">' + chg + "</span>" +
              '<span class="' + cls + '">' + chgp + "</span>" +
              '<span class="rm" data-rm="' + escHtml(q.symbol) + '" title="Remove">&times;</span></div>';
          });
          if (!list.length)
            html = '<div class="note" style="padding:10px">No symbols in Market Watch.<br>' +
              "Connect MT5, then press + to add.</div>";
          body.innerHTML = html;
          foot.textContent = list.length + " symbols - " +
            (j.live ? "LIVE " + (j.feed_label || "MT5") : "DEMO");
          Array.prototype.forEach.call(body.querySelectorAll(".wl-row"), function (row) {
            row.addEventListener("click", function (e) {
              if (e.target.getAttribute("data-rm")) return;
              var p = Layout.activePane();
              if (p) p.setSymbol(row.getAttribute("data-sym"));
            });
          });
          Array.prototype.forEach.call(body.querySelectorAll("[data-rm]"), function (x) {
            x.addEventListener("click", function (e) {
              e.stopPropagation();
              self.remove(x.getAttribute("data-rm"));
            });
          });
        })
        .catch(function () {
          foot.textContent = "watchlist unavailable";
        });
    }
  };

  /* ----------------------------------------- Alerts: right-side panel
     The old price-alert list modal is retired - the topbar "Alerts" button
     now toggles the chart-style Alerts / Log panel (AlertPanel).*/
  Alerts.open = function () { AlertPanel.toggle(); };
  /* ----------------------------------------- TG: telegram settings dialog
  Token lives in app/settings.json on the server now - never in the browser.*/
  TG.send = function (text, kind, skipWebhook, symbol, price) {
    fetch("/api/telegram/send", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, kind: kind || "", skip_webhook: !!skipWebhook,
                             symbol: symbol || "", price: (price === undefined ? null : price) })
    }).catch(function () {});
  };
  /* -------------- Telegram Alerts: this app's own alerts, direct
     Simple settings for the app's OWN alert delivery: bot token, target
     chat, enable/disable, per-kind toggles. Alerts go DIRECTLY to this
     one Telegram chat - no relay, no channels, no intermediate hop.*/
  TG.open = function () {
    function api(path, opts) {
      return fetch(path, opts).then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
          return j;
        });
      });
    }
    Modal.open("Telegram Alerts",
      '<div class="note" style="padding:18px 4px">Loading Telegram settings\u2026</div>',
      '<button class="btn ghost" id="tg-close">Close</button>');
    $("tg-close").addEventListener("click", Modal.close);
    api("/api/telegram/config").then(function (c) {
      var body =
        '<div class="note" style="margin:0 0 12px">Your own alerts go ' +
        "directly to this one Telegram chat. No relay, no channels.</div>" +
        '<label class="chk" style="font-weight:600"><input type="checkbox" id="tg-enabled"' +
          (c.enabled ? " checked" : "") + "> Enable Telegram alerts</label>" +
        '<label class="f">Bot token' +
          " (saved on server - leave blank to keep)</label>" +
        '<input type="password" id="tg-token" autocomplete="off" placeholder="' +
          (c.has_token ? "------" : "paste bot token") + '">' +
        '<label class="f">Target chat ID</label>' +
        '<input type="text" id="tg-chat" value="' + escHtml(c.chat_id || "") +
          '" autocomplete="off" spellcheck="false" ' +
          'placeholder="e.g. channel ID or your user id">' +
        '<div class="note" style="margin:4px 0 12px">The chat (or channel) ' +
        "that receives your alerts. The bot must be a member of it.</div>" +
        '<label class="chk"><input type="checkbox" id="tg-on-price"' +
          (c.on_price ? " checked" : "") + '> Price alerts</label>' +
        '<label class="chk"><input type="checkbox" id="tg-on-sweep"' +
          (c.on_sweep ? " checked" : "") + '> Sweep / indicator alerts</label>' +
        '<div id="tg-status"></div>';
      Modal.open("Telegram Alerts", body,
        '<button class="btn ghost" id="tg-test">Send test</button>' +
        '<button class="btn ghost" id="tg-close">Close</button>' +
        '<button class="btn" id="tg-save">Save</button>');
      $("tg-close").addEventListener("click", Modal.close);
      $("tg-test").addEventListener("click", function () {
        api("/api/telegram/send", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: "StockSophist Charts test - your own alerts will arrive here.",
                                 kind: "", skip_webhook: true }) })
          .then(function (j) {
            var t = j.telegram || {};
            $("tg-status").innerHTML = '<div class="statusline ' +
              (t.sent ? "ok" : "err") + '">Test ' +
              (t.sent ? "sent - check Telegram." :
                "not sent: " + escHtml(t.skipped || t.error || "unknown")) + "</div>";
          })
          .catch(function (e) {
            $("tg-status").innerHTML = '<div class="statusline err">' + escHtml(e.message) + "</div>";
          });
      });
      $("tg-save").addEventListener("click", function () {
        var payload = {
          enabled: $("tg-enabled").checked,
          token: $("tg-token").value.trim(),
          chat_id: $("tg-chat").value.trim(),
          on_price: $("tg-on-price").checked,
          on_sweep: $("tg-on-sweep").checked
        };
        api("/api/telegram/config", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload) })
          .then(function () {
            $("tg-status").innerHTML = '<div class="statusline ok">Saved.</div>';
          })
          .catch(function (e) {
            $("tg-status").innerHTML = '<div class="statusline err">' + escHtml(e.message) + "</div>";
          });
      });
    }).catch(function (e) {
      /* config never arrived - show the error inside the already-open dialog */
      $("m-body").innerHTML =
        '<div class="note" style="color:var(--warn);padding:18px 4px">' +
        "Telegram settings unavailable: " + escHtml(e.message) + "</div>";
    });
  };

  /* ----------------------------------------- ChartSet: settings dialog */
  ChartSet.open = function () {
    /* refresh the server-tone probe so Test/play use the live answer. */
    _serverToneKnown = null;
    _serverHasTone(function () {});
    var c = this.cfg();
    var schema = [
      ["Time scale", [
        ["timezone", "Timezone", "tzselect"],
        ["dateFormat", "Date format", "text"], ["time12h", "12-hour clock", "bool"]
      ]],
      ["Canvas", [
        ["bg", "Background", "color"], ["text", "Text", "color"],
        ["fontSize", "Font size", "number"],
        ["grid", "Grid lines", "bool"], ["gridColor", "Grid color", "color"],
        ["up", "Up candle", "color"], ["down", "Down candle", "color"],
        ["wickUp", "Up wick", "color"], ["wickDown", "Down wick", "color"],
        ["borderVisible", "Candle borders", "bool"],
        ["crossMode", "Crosshair", "select", ["normal", "magnet", "hidden"]],
        ["crossColor", "Crosshair color", "color"]
      ]],
      ["Scales & lines", [
        ["scaleSide", "Price scale side", "select", ["right", "left"]],
        ["scaleMode", "Scale mode", "select", ["normal", "log", "percentage"]],
        ["lastValue", "Last value line", "bool"],
        ["priceLine", "Live price line", "bool"],
        ["priceLineColor", "Price line color", "color"],
        ["bidAsk", "Bid / ask lines", "bool"],
        ["prevClose", "Previous close line", "bool"],
        ["prevCloseColor", "Prev-close color", "color"],
        ["dayHL", "Day high / low lines", "bool"],
        ["dayHLColor", "Day H/L color", "color"],
        ["countdown", "Bar-close countdown", "bool"],
        ["plusButton", "'+' on price-axis hover", "bool"],
        ["showVolume", "Show volume", "bool"]
      ]],
      ["Status line", [["legend", "OHLC legend", "bool"]]],
      ["Alerts", [
        ["alertColor", "Alert line color", "color"], ["alertSound", "Alert sound", "bool"],
        ["alertTune", "Alert tune", "select", ALERT_TUNES]
      ]]
    ];
    var body = "";
    schema.forEach(function (g) {
      body += '<div class="igroup">' + g[0] + "</div>";
      g[1].forEach(function (f) {
        var key = f[0], label = f[1], type = f[2], val = c[key];
        var ctl;
        if (type === "bool") {
          ctl = '<input type="checkbox" data-csk="' + key + '"' + (val ? " checked" : "") + ">";
        } else if (type === "color") {
          /* Same palette swatch + opacity slider as indicator settings:
             the swatch always shows the selected color, never a hex code.*/
          ctl = '<span class="colorrow"><input type="color" data-csk="' + key + '" data-crgb="1" value="' +
            colorRgb6(val) + '" title="Pick a color">' +
            '<input type="range" min="0" max="100" step="1" data-csk="' + key + '" data-calpha="1" value="' +
            colorAlphaPct(val) + '" title="Opacity %">' +
            '<span class="colorpct">' + colorAlphaPct(val) + '%</span></span>';
        } else if (type === "select") {
          ctl = "<select data-csk=\"" + key + "\">" + f[3].map(function (o) {
            return '<option value="' + o + '"' + (String(o) === String(val) ? " selected" : "") +
              ">" + o + "</option>";
          }).join("") + "</select>";
        } else if (type === "tzselect") {
          ctl = "<select data-csk=\"" + key + "\">" + allTimezones().map(function (z) {
            return '<option value="' + z + '"' + (z === String(val) ? " selected" : "") +
              ">" + z + "</option>";
          }).join("") + "</select>";
        } else if (type === "number") {
          ctl = '<input type="number" data-csk="' + key + '" value="' + escHtml(val) + '">';
        } else {
          ctl = '<input type="text" data-csk="' + key + '" value="' + escHtml(val) + '">';
        }
        body += '<div class="irow"><span class="lbl">' + label +
          '</span><span class="ctl">' + ctl + "</span></div>";
      });
    });
    Modal.open("Chart settings", body,
      '<button class="btn ghost" id="cs-reset">Defaults</button>' +
      '<button class="btn ghost" id="cs-close">Close</button>' +
      '<button class="btn" id="cs-save">Apply</button>');
    $("cs-close").addEventListener("click", Modal.close);
    /* Alert-tune upload + preview controls under the "Alert tune" row */
    (function () {
      var sel = document.querySelector('select[data-csk="alertTune"]');
      if (!sel || !sel.closest) return;
      var row = document.createElement("div");
      row.className = "irow";
      row.innerHTML = '<span class="lbl">Custom tune</span><span class="ctl">' +
        '<button class="btn ghost" id="cs-tune-upload" style="margin-right:6px">Upload...</button>' +
        '<button class="btn ghost" id="cs-tune-test">Test</button>' +
        '<input type="file" id="cs-tune-file" accept="audio/*" style="display:none"></span>';
      sel.closest(".irow").after(row);
      var file = row.querySelector("#cs-tune-file");
      row.querySelector("#cs-tune-upload").addEventListener("click", function () { file.click(); });
      file.addEventListener("change", function () {
        var f = file.files && file.files[0];
        if (!f) return;
        file.value = "";
        /* upload to the server - it validates and keeps the file in
           the app data folder (survives updates); the browser copy is legacy.*/
        var ext = (f.name.split(".").pop() || "").toLowerCase();
        if (["mp3", "wav", "ogg", "m4a", "flac"].indexOf(ext) < 0) {
          toast("Unsupported file type - use mp3, wav, ogg, m4a or flac"); return;
        }
        if (f.size > 10 * 1024 * 1024) { toast("File too large - 10 MB max"); return; }
        var fd = new FormData();
        fd.append("file", f, f.name);
        toast("Uploading tune...");
        fetch("/api/alerts/tone", { method: "POST", body: fd })
          .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
          .then(function (res) {
            if (!res.ok || !res.j.ok) throw new Error((res.j && res.j.error) || "upload failed");
            try { localStorage.removeItem("stocksophist_custom_alert_tune"); } catch (e) {}
            _serverToneKnown = true;
            _customAudio = null;
            sel.value = "Custom (uploaded)";
            toast("Custom tune saved - it now lives in the app data folder and survives updates");
          })
          .catch(function (e) { toast("Could not save tune: " + (e.message || e)); });
      });
      row.querySelector("#cs-tune-test").addEventListener("click", function () {
        try {
          if (sel.value === "Custom (uploaded)") {
            /* exercise the same server-first path as real alerts. */
            _serverHasTone(function (has) {
              if (has) { playTuneByName("Custom (uploaded)"); return; }
              var legacy = null;
              try { legacy = localStorage.getItem("stocksophist_custom_alert_tune"); } catch (e) {}
              if (!legacy) { toast("Upload a custom tune first"); return; }
              playTuneByName("Custom (uploaded)");
            });
            return;
          }
          playTuneByName(sel.value);
        } catch (e) {}
      });
    })();
    $("cs-reset").addEventListener("click", function () {
      ChartSet.save(ChartSet.defaults()); ChartSet.applyAll(); ChartSet.open();
    });
    $("cs-save").addEventListener("click", function () {
      var out = ChartSet.cfg();
      var prevTz = out.timezone;
      document.querySelectorAll("[data-csk]").forEach(function (el) {
        var k = el.getAttribute("data-csk");
        if (el.hasAttribute("data-calpha")) return; // paired with its color input
        if (el.hasAttribute("data-crgb")) {
          var al = document.querySelector('[data-csk="' + k + '"][data-calpha]');
          out[k] = joinColor8(el.value, al ? al.value : 100);
          return;
        }
        if (el.type === "checkbox") out[k] = el.checked;
        else if (el.type === "number") out[k] = parseFloat(el.value) || 0;
        else out[k] = el.value;
      });
      ChartSet.save(out); ChartSet.applyAll(); Modal.close();
      if (out.timezone !== prevTz) {
        /* every displayed timestamp follows the selected zone: repaint the
           panels that cache rendered times (alert log, "Hist" dates)*/
        try { Alerts.refresh(); } catch (e) {}
        Layout.panes.forEach(function (p) {
          if (p.histEl && p._histEarliest)
            p.histEl.textContent = "Hist " + fmtTZ_YMD(p._histEarliest * 1000);
          p.draw();
        });
      }
      toast("Chart settings applied");
    });
  };


  /* ================================================= Trade: direct MT5 chart trading
  DHAN-style floating scalp panel + draggable SL/TP/pending lines.
  Safety: every send is blocked unless MT5 is live AND the user explicitly
  enabled trading for the current account (per-account opt-in persisted in
  localStorage). DEMO mode can never reach the server trade endpoints.*/
  /* ================================================================ news
     economic calendar - floating panel like Trade (drag by header,
     position persisted, closes ONLY via the x). Shows one IST day's events
     from the server's 7-day ForexFactory cache (refreshed every 4h, so the
     feed never sees bulk requests). Past events are grayed out; upcoming
     show "in Xh Ym"; inside 30 minutes a live ticking countdown starts.*/
  var NewsUI = {
    LS_POS: "stocksophist_news_pos",
    panel: null, visible: false, tickTimer: null,
    dayOffset: 0, events: [], fetchedAt: 0, feedOk: true, feedErr: "",

    istToday: function () {
      try {
        return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata",
          year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
      } catch (e) {
        var d = new Date();
        return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) +
               "-" + ("0" + d.getDate()).slice(-2);
      }
    },
    targetDate: function () {
      var t = this.istToday().split("-");
      var ms = Date.UTC(+t[0], +t[1] - 1, +t[2], 12) + this.dayOffset * 864e5;
      var d = new Date(ms);
      return d.getUTCFullYear() + "-" + ("0" + (d.getUTCMonth() + 1)).slice(-2) +
             "-" + ("0" + d.getUTCDate()).slice(-2);
    },
    dayLabel: function () {
      if (this.dayOffset === 0) return "Today";
      var p = this.targetDate().split("-");
      var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
      var wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()];
      var mo = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()];
      return wd + " " + (+p[2]) + " " + mo;
    },

    toggle: function () { this.visible ? this.hide() : this.show(); },
    show: function () {
      this.visible = true;
      var b = $("btn-news"); if (b) b.classList.add("on");
      if (!this.panel) this.buildPanel();
      this.panel.style.display = "block";
      this.dayOffset = 0;
      this.load();
      var self = this;
      if (this.tickTimer) clearInterval(this.tickTimer);
      this.tickTimer = setInterval(function () { self.tick(); }, 1000);
    },
    hide: function () {
      this.visible = false;
      var b = $("btn-news"); if (b) b.classList.remove("on");
      if (this.panel) this.panel.style.display = "none";
      if (this.tickTimer) { clearInterval(this.tickTimer); this.tickTimer = null; }
    },

    buildPanel: function () {
      var self = this;
      var el = document.createElement("div");
      el.id = "news-panel";
      el.innerHTML =
        '<div class="nw-head"><span>\uD83D\uDCF0</span><span class="t">News</span>' +
        '<span class="nw-nav"><button class="nw-prev" title="Previous day">\u2039</button>' +
        '<span class="nw-day">Today</span>' +
        '<button class="nw-next" title="Next day">\u203A</button></span>' +
        '<button class="nw-rf" title="Refresh calendar now">\u21BB</button>' +
        '<span class="x" title="Close">\u00D7</span></div>' +
        '<div class="nw-status"><span class="nw-upd">Loading\u2026</span><span class="nw-cnt"></span></div>' +
        '<div class="nw-list"></div>' +
        '<div class="nw-foot">' +
        '<span class="lg"><span class="sw" style="background:#ef5350"></span>High</span>' +
        '<span class="lg"><span class="sw" style="background:#ff9800"></span>Med</span>' +
        '<span class="lg"><span class="sw" style="background:#9e9e9e"></span>Low</span>' +
        '<span style="margin-left:auto">Times in IST</span></div>';
      document.body.appendChild(el);
      this.panel = el;
      // drag by header (same pattern as the Trade panel)
      var head = el.querySelector(".nw-head"), sx = 0, sy = 0, ox = 0, oy = 0, drag = false;
      try {
        var pp = JSON.parse(localStorage.getItem(this.LS_POS) || "null");
        if (pp) { el.style.left = pp.x + "px"; el.style.top = pp.y + "px"; }
        else { el.style.right = "14px"; el.style.top = "64px"; }
      } catch (e) { el.style.right = "14px"; el.style.top = "64px"; }
      head.addEventListener("mousedown", function (e) {
        if (e.target.classList.contains("x") || e.target.closest("button")) return;
        drag = true; sx = e.clientX; sy = e.clientY;
        var r = el.getBoundingClientRect(); ox = r.left; oy = r.top;
        el.style.right = "auto";
        e.preventDefault();
      });
      window.addEventListener("mousemove", function (e) {
        if (!drag) return;
        el.style.left = (ox + e.clientX - sx) + "px";
        el.style.top = Math.max(0, oy + e.clientY - sy) + "px";
      });
      window.addEventListener("mouseup", function () {
        if (!drag) return; drag = false;
        try {
          var r = el.getBoundingClientRect();
          localStorage.setItem(self.LS_POS, JSON.stringify({ x: r.left, y: r.top }));
        } catch (e) {}
      });
      el.querySelector(".x").addEventListener("click", function () { self.hide(); });
      el.querySelector(".nw-prev").addEventListener("click", function () {
        self.dayOffset = Math.max(-7, self.dayOffset - 1); self.load();
      });
      el.querySelector(".nw-next").addEventListener("click", function () {
        self.dayOffset = Math.min(7, self.dayOffset + 1); self.load();
      });
      el.querySelector(".nw-rf").addEventListener("click", function () {
        var rf = el.querySelector(".nw-rf");
        rf.classList.add("spin");
        fetch("/api/news/refresh", { method: "POST" })
          .then(function (r) { return r.json(); })
          .then(function () { self.load(); })
          .catch(function () { self.load(); })
          .then(function () { rf.classList.remove("spin"); });
      });
    },

    load: function () {
      var self = this;
      var day = this.targetDate();
      this.panel.querySelector(".nw-day").textContent = this.dayLabel();
      this.panel.querySelector(".nw-upd").textContent = "Loading\u2026";
      fetch("/api/news?date=" + encodeURIComponent(day))
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (res) {
          if (!self.visible) return;
          var j = res.j;
          if (!res.ok || !j.ok) throw new Error(j.error || "news failed");
          self.events = j.events || [];
          self.fetchedAt = j.fetched_at || 0;
          self.feedOk = !!j.feed_ok;
          self.feedErr = j.feed_error || "";
          self.render();
          self.tick();
        })
        .catch(function (e) {
          if (!self.visible) return;
          self.panel.querySelector(".nw-upd").textContent = "Could not load: " + e.message;
        });
    },

    render: function () {
      var list = this.panel.querySelector(".nw-list");
      var html = "";
      this.events.forEach(function (e) {
        var sub = [];
        if (e.forecast) sub.push("Fcst " + e.forecast);
        if (e.previous) sub.push("Prev " + e.previous);
        html += '<div class="nw-row" data-ts="' + e.ts + '">' +
          '<span class="nw-time">' + e.time + '</span>' +
          '<span class="nw-ccy">' + escHtml(e.country) + '</span>' +
          '<span class="nw-dot ' + escHtml(e.impact) + '" title="' + escHtml(e.impact) + ' impact"></span>' +
          '<span class="nw-title">' + escHtml(e.title) +
          (sub.length ? "<small>" + escHtml(sub.join(" \u00B7 ")) + "</small>" : "") + "</span>" +
          '<span class="nw-cd"></span></div>';
      });
      list.innerHTML = html ||
        '<div class="nw-empty">No events scheduled for this day.</div>';
      this.panel.querySelector(".nw-cnt").textContent =
        this.events.length ? (this.events.length + " events") : "";
    },

    tick: function () {
      if (!this.visible || !this.panel) return;
      var now = Date.now(), nextTs = Infinity;
      var rows = this.panel.querySelectorAll(".nw-row");
      rows.forEach(function (r) {
        var ts = +r.dataset.ts, diff = ts * 1000 - now;
        var cd = r.querySelector(".nw-cd");
        r.classList.remove("past", "next"); cd.classList.remove("soon");
        if (diff <= 0) {
          r.classList.add("past"); cd.textContent = "done";
        } else {
          if (ts < nextTs) nextTs = ts;
          var m = Math.floor(diff / 60000), s = Math.floor((diff % 60000) / 1000);
          if (diff < 30 * 60000) {
            cd.textContent = "in " + m + "m " + ("0" + s).slice(-2) + "s";
            cd.classList.add("soon");
          } else {
            var h = Math.floor(m / 60);
            cd.textContent = h > 0 ? ("in " + h + "h " + (m % 60) + "m") : ("in " + m + "m");
          }
        }
      });
      rows.forEach(function (r) {
        if (+r.dataset.ts === nextTs) r.classList.add("next");
      });
      // status line: how fresh is the server's cache?
      var upd = this.panel.querySelector(".nw-upd");
      if (this.fetchedAt) {
        var mins = Math.max(0, Math.round((Date.now() / 1000 - this.fetchedAt) / 60));
        var txt = "Updated " + (mins < 1 ? "just now" : mins < 60 ? mins + "m ago"
          : Math.floor(mins / 60) + "h " + (mins % 60) + "m ago");
        if (!this.feedOk && this.feedErr) txt = "Feed error \u2014 showing last data";
        upd.textContent = txt;
        upd.className = "nw-upd" + ((!this.feedOk && this.feedErr) ? " warn" : "");
      }
    }
  };

  var Trade = {
    LS_LOTS: "stocksophist_trade_lots",
    LS_CFG: "stocksophist_trade_cfg",
    LS_KEYS: "stocksophist_trade_keys",
    LS_EN: "stocksophist_trade_enabled",
    LS_POS: "stocksophist_trade_pos",
    live: false, account: null, symInfo: {},
    positions: [], orders: [],
    panel: null, visible: false, pollTimer: null, quoteTimer: null,
    busy: false,

    /* -------------- persistence */
    lots: function () {
      try { return JSON.parse(localStorage.getItem(this.LS_LOTS) || "{}"); } catch (e) { return {}; }
    },
    saveLots: function (m) { try { localStorage.setItem(this.LS_LOTS, JSON.stringify(m)); } catch (e) {} },
    cfg: function () {
      try {
        return Object.assign({ slPts: 200, tpPts: 300, otype: "market", scalp: false },
          JSON.parse(localStorage.getItem(this.LS_CFG) || "{}"));
      } catch (e) { return { slPts: 200, tpPts: 300, otype: "market", scalp: false }; }
    },
    saveCfg: function (c) { try { localStorage.setItem(this.LS_CFG, JSON.stringify(c)); } catch (e) {} },
    keys: function () {
      try {
        return Object.assign({ buy: "b", sell: "s", flat: "x", reverse: "r" },
          JSON.parse(localStorage.getItem(this.LS_KEYS) || "{}"));
      } catch (e) { return { buy: "b", sell: "s", flat: "x", reverse: "r" }; }
    },
    enabledFor: function (login) {
      try { return !!JSON.parse(localStorage.getItem(this.LS_EN) || "{}")[String(login)]; }
      catch (e) { return false; }
    },
    setEnabled: function (login, on) {
      try {
        var m = JSON.parse(localStorage.getItem(this.LS_EN) || "{}");
        m[String(login)] = !!on;
        localStorage.setItem(this.LS_EN, JSON.stringify(m));
      } catch (e) {}
    },

    /* -------------- api */
    api: function (path, body) {
      /* a hung order request must not leave `busy` true forever -
         every later BUY/SELL click then dies silently (`if (busy) return`).
         30s timeout so a stuck request always resolves and `busy` resets.*/
      var controller = null, timer = null;
      try {
        controller = new AbortController();
        timer = setTimeout(function () { controller.abort(); }, 30000);
      } catch (e) { controller = null; }
      var opts = body === undefined
        ? {}
        : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
      if (controller) opts.signal = controller.signal;
      return fetch(path, opts).then(function (r) {
        if (timer) clearTimeout(timer);
        return r.text().then(function (t) {
          var j = null;
          try { j = JSON.parse(t); } catch (e) {}
          if (!r.ok) throw new Error((j && (j.message || j.error)) || ("HTTP " + r.status));
          if (!j) throw new Error("empty response");
          return j;
        });
      }).catch(function (e) {
        if (timer) clearTimeout(timer);
        if (e && e.name === "AbortError") throw new Error("Request timed out after 30s - try again");
        throw e;
      });
    },

    activeSymbol: function () {
      try { return Layout.activePane().state.symbol; } catch (e) { return "XAUUSD"; }
    },
    activePane: function () {
      try { return Layout.activePane(); } catch (e) { return null; }
    },
    quote: function () {
      var p = this.activePane();
      return (p && p.lastQuote) || null;
    },
    canTrade: function () {
      return this.live && this.account && this.enabledFor(this.account.login);
    },

    /* -------------- live state */
    refreshLive: function (cb) {
      var self = this;
      fetch("/api/status").then(function (r) { return r.json(); }).then(function (st) {
        /* execution runs through the MT5 terminal whenever it is
           connected - even when the price feed is OANDA.*/
        self.live = (st.mode === "LIVE-MT5" || st.mode === "LIVE-OANDA");
        if (self.live) {
          self.api("/api/trade/account").then(function (r) {
            self.account = r.account;
            self.afterLive(cb);
          }).catch(function () { self.live = false; self.account = null; self.afterLive(cb); });
        } else { self.account = null; self.afterLive(cb); }
      }).catch(function () { self.live = false; self.account = null; self.afterLive(cb); });
    },
    afterLive: function (cb) {
      var sym = this.activeSymbol();
      if (this.live && !this.symInfo[sym]) this.loadSymInfo(sym);
      this.renderHead();
      if (cb) cb();
    },
    loadSymInfo: function (sym) {
      var self = this;
      this.api("/api/trade/symbol_info?symbol=" + encodeURIComponent(sym)).then(function (r) {
        if (r.ok) self.symInfo[sym] = r;
      }).catch(function () {});
    },
    poll: function () {
      var self = this;
      if (!this.visible || !this.live) { this.clearOverlays(); return; }
      var syms = [];
      try {
        Layout.panes.forEach(function (p) {
          if (p && p.state && p.state.symbol && syms.indexOf(p.state.symbol) < 0) syms.push(p.state.symbol);
        });
      } catch (e) {}
      var sym = syms[0] || this.activeSymbol();
      Promise.all([
        self.api("/api/trade/positions?symbol=" + encodeURIComponent(sym)).catch(function () { return { positions: [] }; }),
        self.api("/api/trade/orders?symbol=" + encodeURIComponent(sym)).catch(function () { return { orders: [] }; }),
        self.api("/api/trade/account").catch(function () { return { account: null }; })
      ]).then(function (rs) {
        self.positions = rs[0].positions || [];
        self.orders = rs[1].orders || [];
        if (rs[2].account) self.account = rs[2].account;
        try {
          Layout.panes.forEach(function (p) {
            if (!p || !p.state) return;
            p.tradePos = self.positions.filter(function (x) { return x.symbol === p.state.symbol; });
            p.tradeOrd = self.orders.filter(function (x) { return x.symbol === p.state.symbol; });
            p.draw();
          });
        } catch (e) {}
        self.renderPnl();
      });
    },
    clearOverlays: function () {
      try {
        Layout.panes.forEach(function (p) {
          if (p) { p.tradePos = []; p.tradeOrd = []; p.draw(); }
        });
      } catch (e) {}
    },

    /* -------------- panel */
    toggle: function () {
      var self = this;
      if (this.visible) { this.hide(); return; }
      /* show the panel IMMEDIATELY with a "connecting..." state, then
         fill account data when the server answers. (The data fills
         when /api/status + /api/trade/account both resolve.)*/
      this.live = true;  /* optimistic - corrected by refreshLive below*/
      this.show();
      this.refreshLive(function () {
        if (!self.visible) return;  /* user closed it while loading*/
        self.renderHead(); self.renderPnl(); self.renderButtons();
      });
    },
    show: function () {
      this.visible = true;
      var b = $("btn-trade");
      if (b) b.classList.add("on");
      if (!this.panel) this.buildPanel();
      this.panel.style.display = "block";
      this.renderHead(); this.renderPnl(); this.renderButtons();
      var self = this;
      if (this.pollTimer) clearInterval(this.pollTimer);
      this.pollTimer = setInterval(function () { self.poll(); }, 2500);
      if (this.quoteTimer) clearInterval(this.quoteTimer);
      this.quoteTimer = setInterval(function () { self.renderButtons(); }, 1000);
      this.poll();
    },
    hide: function () {
      this.visible = false;
      var b = $("btn-trade");
      if (b) b.classList.remove("on");
      if (this.panel) this.panel.style.display = "none";
      if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
      if (this.quoteTimer) { clearInterval(this.quoteTimer); this.quoteTimer = null; }
      this.clearOverlays();
    },
    buildPanel: function () {
      var self = this;
      var el = document.createElement("div");
      el.id = "trade-panel";
      el.innerHTML =
        '<div class="tp-head"><span>&#9889;</span><span class="sym"></span><span class="sprd"></span><span class="x" title="Close">&times;</span></div>' +
        '<div class="tp-demo">DEMO - MT5 not connected. Trading is disabled.</div>' +
        '<div class="tp-body">' +
        '<div class="tp-acct"></div>' +
        '<label class="tp-enable"><input type="checkbox" id="tp-enable"><span>Enable live trading on this account</span></label>' +
        '<div class="tp-row tp-lots wrap">' +
        '<button class="stepbtn" data-step="-1">&minus;</button>' +
        '<input id="tp-vol" type="number" step="0.01" min="0.01" value="0.01">' +
        '<button class="stepbtn" data-step="1">+</button>' +
        '<span class="chip" data-lot="0.10">0.10</span><span class="chip" data-lot="0.25">0.25</span>' +
        '<span class="chip" data-lot="0.50">0.50</span><span class="chip" data-lot="1.00">1.00</span>' +
        '</div>' +
        '<div class="tp-row">' +
        '<button class="tp-buy">BUY<small class="tp-buy-p">&mdash;</small></button>' +
        '<button class="tp-sell">SELL<small class="tp-sell-p">&mdash;</small></button>' +
        '</div>' +
        '<div class="tp-tabs">' +
        '<span class="chip" data-ot="market">Market</span><span class="chip" data-ot="limit">Limit</span><span class="chip" data-ot="stop">Stop</span>' +
        '</div>' +
        '<div class="tp-row tp-price-row" style="display:none">' +
        '<span class="tp-lab">Price</span><input class="tp-num" id="tp-price" style="flex:1">' +
        '<button class="tp-act" id="tp-place">Place</button>' +
        '</div>' +
        '<div class="tp-row tp-slrow"><div style="flex:1"><div class="tp-lab">SL pts (0=none)</div><input class="tp-num" id="tp-sl"></div>' +
        '<div style="flex:1"><div class="tp-lab">TP pts (0=none)</div><input class="tp-num" id="tp-tp"></div></div>' +
        '<div class="tp-row tp-slrow wrap"><span class="chip" data-sl="100">SL 100</span><span class="chip" data-sl="200">SL 200</span>' +
        '<span class="chip" data-tp="200">TP 200</span><span class="chip" data-tp="300">TP 300</span></div>' +
        '<div class="tp-row"><label class="tp-lab" style="display:flex;gap:5px;align-items:center"><input type="checkbox" id="tp-scalp"> Scalp mode (one-tap)</label>' +
        '<button class="tp-act" id="tp-keys" title="Remap hotkeys" style="flex:0 0 auto">&#9000;</button></div>' +
        '<div class="tp-row"><span class="tp-lab">Open P&amp;L</span><span class="tp-pnl" id="tp-pnl">$0.00</span>' +
        '<span class="tp-lab" style="margin-left:auto" id="tp-posn"></span></div>' +
        '<div class="tp-row"><button class="tp-act" id="tp-half">Close 50%</button>' +
        '<button class="tp-act" id="tp-rev">Reverse</button></div>' +
        '<div class="tp-row"><button class="tp-act warn" id="tp-flat" style="flex:1">FLATTEN ALL</button></div>' +
        '</div>';
      document.body.appendChild(el);
      this.panel = el;
      // drag by header
      var head = el.querySelector(".tp-head"), sx = 0, sy = 0, ox = 0, oy = 0, drag = false;
      try {
        var pp = JSON.parse(localStorage.getItem(this.LS_POS) || "null");
        if (pp) { el.style.left = pp.x + "px"; el.style.top = pp.y + "px"; }
        else { el.style.right = "14px"; el.style.top = "64px"; }
      } catch (e) { el.style.right = "14px"; el.style.top = "64px"; }
      head.addEventListener("mousedown", function (e) {
        if (e.target.classList.contains("x")) return;
        drag = true; sx = e.clientX; sy = e.clientY;
        var r = el.getBoundingClientRect(); ox = r.left; oy = r.top;
        el.style.right = "auto";
        e.preventDefault();
      });
      window.addEventListener("mousemove", function (e) {
        if (!drag) return;
        el.style.left = (ox + e.clientX - sx) + "px";
        el.style.top = Math.max(0, oy + e.clientY - sy) + "px";
      });
      window.addEventListener("mouseup", function () {
        if (!drag) return; drag = false;
        try {
          var r = el.getBoundingClientRect();
          localStorage.setItem(self.LS_POS, JSON.stringify({ x: r.left, y: r.top }));
        } catch (e) {}
      });
      el.querySelector(".x").addEventListener("click", function () { self.hide(); });
      // lots
      var volEl = el.querySelector("#tp-vol");
      var lots = this.lots();
      volEl.value = lots[this.activeSymbol()] || "0.01";
      var setVol = function (v) {
        v = Math.max(0.01, Math.round(v * 100) / 100);
        volEl.value = v.toFixed(2);
        var m = self.lots(); m[self.activeSymbol()] = v.toFixed(2); self.saveLots(m);
      };
      el.querySelectorAll(".stepbtn").forEach(function (btn) {
        btn.addEventListener("click", function () {
          setVol((parseFloat(volEl.value) || 0.01) + 0.01 * parseInt(btn.getAttribute("data-step"), 10));
        });
      });
      el.querySelectorAll("[data-lot]").forEach(function (c) {
        c.addEventListener("click", function () { setVol(parseFloat(c.getAttribute("data-lot"))); });
      });
      volEl.addEventListener("change", function () { setVol(parseFloat(volEl.value) || 0.01); });
      // order type tabs
      var cfg = this.cfg();
      el.querySelector("#tp-sl").value = cfg.slPts;
      el.querySelector("#tp-tp").value = cfg.tpPts;
      el.querySelector("#tp-scalp").checked = !!cfg.scalp;
      var setOt = function (ot) {
        cfg.otype = ot; self.saveCfg(cfg);
        el.querySelectorAll("[data-ot]").forEach(function (c) {
          c.classList.toggle("on", c.getAttribute("data-ot") === ot);
        });
        var pr = el.querySelector(".tp-price-row");
        pr.style.display = (ot === "market") ? "none" : "flex";
        var slrows = el.querySelectorAll(".tp-slrow");
        slrows.forEach(function (r) { r.style.display = cfg.scalp ? "none" : "flex"; });
        if (ot !== "market") {
          var q = self.quote();
          if (q) el.querySelector("#tp-price").value = (ot === "limit" ? q.bid : q.ask).toFixed(2);
        }
      };
      el.querySelectorAll("[data-ot]").forEach(function (c) {
        c.addEventListener("click", function () { setOt(c.getAttribute("data-ot")); });
      });
      setOt(cfg.otype);
      el.querySelector("#tp-sl").addEventListener("change", function (e) {
        cfg.slPts = Math.max(0, parseFloat(e.target.value) || 0); self.saveCfg(cfg);
      });
      el.querySelector("#tp-tp").addEventListener("change", function (e) {
        cfg.tpPts = Math.max(0, parseFloat(e.target.value) || 0); self.saveCfg(cfg);
      });
      el.querySelectorAll("[data-sl]").forEach(function (c) {
        c.addEventListener("click", function () {
          cfg.slPts = parseFloat(c.getAttribute("data-sl")); self.saveCfg(cfg);
          el.querySelector("#tp-sl").value = cfg.slPts;
        });
      });
      el.querySelectorAll("[data-tp]").forEach(function (c) {
        c.addEventListener("click", function () {
          cfg.tpPts = parseFloat(c.getAttribute("data-tp")); self.saveCfg(cfg);
          el.querySelector("#tp-tp").value = cfg.tpPts;
        });
      });
      el.querySelector("#tp-scalp").addEventListener("change", function (e) {
        cfg.scalp = e.target.checked; self.saveCfg(cfg); setOt(cfg.otype);
        toast(cfg.scalp ? "Scalp mode ON - BUY/SELL fire with one tap" : "Scalp mode OFF - orders ask for confirmation");
      });
      el.querySelector("#tp-place").addEventListener("click", function () { self.placePending(); });
      el.querySelector(".tp-buy").addEventListener("click", function () { self.buySell("buy"); });
      el.querySelector(".tp-sell").addEventListener("click", function () { self.buySell("sell"); });
      el.querySelector("#tp-flat").addEventListener("click", function () { self.flattenAll(); });
      el.querySelector("#tp-rev").addEventListener("click", function () { self.reverse(); });
      el.querySelector("#tp-half").addEventListener("click", function () { self.closeHalf(); });
      el.querySelector("#tp-keys").addEventListener("click", function () { self.remapKeys(); });
      el.querySelector("#tp-enable").addEventListener("change", function (e) {
        if (!self.account) return;
        if (e.target.checked) {
          Modal.open("Enable live trading",
            "<p>Allow <b>market orders, pending orders, modifies and closes</b> from this chart on:</p>" +
            "<p style='margin:8px 0'>Account <b>" + self.account.login + "</b> (" + self.account.name + ")<br>" +
            "Server <b>" + self.account.server + "</b><br>" +
            "Symbol <b>" + self.activeSymbol() + "</b></p>" +
            "<p style='color:#ffb74d'>Real money. Orders go straight to the broker.</p>",
            '<button class="btn ghost" id="te-no">Cancel</button>' +
            '<button class="btn" id="te-yes">Enable trading</button>');
          $("te-no").addEventListener("click", function () { Modal.close(); e.target.checked = false; self.renderHead(); });
          $("te-yes").addEventListener("click", function () {
            self.setEnabled(self.account.login, true); Modal.close(); self.renderHead();
            toast("Live trading enabled for account " + self.account.login);
          });
        } else {
          self.setEnabled(self.account.login, false); self.renderHead();
        }
      });
    },
    renderHead: function () {
      if (!this.panel) return;
      var sym = this.activeSymbol();
      this.panel.querySelector(".sym").textContent = sym;
      this.panel.classList.toggle("demo", !this.live);
      var acct = this.panel.querySelector(".tp-acct");
      if (this.live && this.account) {
        acct.innerHTML = "Acct <b style='color:#fff'>" + this.account.login + "</b> @ " +
          this.account.server + " &nbsp; Bal $" + this.account.balance.toFixed(2);
        var en = this.panel.querySelector("#tp-enable");
        en.checked = this.enabledFor(this.account.login);
        en.parentElement.style.display = "flex";
      } else {
        /* when we're live but MT5 returns no trade account, say so
           honestly instead of "connecting..." forever - the terminal's trade
           login (not the price feed) is what's missing.*/
        acct.textContent = this.live ? "Waiting for MT5 trade account..." : "Not connected to MT5";
        this.panel.querySelector(".tp-enable").style.display = "none";
      }
      var lots = this.lots();
      var v = this.panel.querySelector("#tp-vol");
      if (v && document.activeElement !== v) v.value = lots[sym] || "0.01";
      this.renderButtons();
    },
    renderButtons: function () {
      if (!this.panel) return;
      var q = this.quote();
      var bb = this.panel.querySelector(".tp-buy-p"), sb = this.panel.querySelector(".tp-sell-p");
      var sprd = this.panel.querySelector(".sprd");
      if (q) {
        bb.textContent = "@ " + q.ask.toFixed(2);
        sb.textContent = "@ " + q.bid.toFixed(2);
        sprd.textContent = "spread " + Math.round((q.ask - q.bid) * 100) + " pts";
      } else { bb.textContent = "-"; sb.textContent = "-"; sprd.textContent = ""; }
      var ok = this.canTrade();
      this.panel.querySelector(".tp-buy").disabled = !ok;
      this.panel.querySelector(".tp-sell").disabled = !ok;
      this.panel.querySelector("#tp-place").disabled = !ok;
    },
    renderPnl: function () {
      if (!this.panel) return;
      var sym = this.activeSymbol(), pnl = 0, n = 0;
      this.positions.forEach(function (p) {
        if (p.symbol === sym) { pnl += (p.pnl || 0); n++; }
      });
      var el = this.panel.querySelector("#tp-pnl");
      el.textContent = (pnl >= 0 ? "+$" : "-$") + Math.abs(pnl).toFixed(2);
      el.className = "tp-pnl " + (pnl >= 0 ? "pos" : "neg");
      this.panel.querySelector("#tp-posn").textContent = n ? (n + " open") : "";
    },

    /* -------------- order actions */
    guard: function () {
      if (!this.live) { toast("DEMO - connect MT5 to trade"); return false; }
      if (!this.account) { toast("Waiting for account info..."); return false; }
      if (!this.enabledFor(this.account.login)) { toast("Tick 'Enable live trading' first"); return false; }
      return true;
    },
    vol: function () {
      var v = parseFloat(this.panel.querySelector("#tp-vol").value);
      return (isFinite(v) && v > 0) ? v : 0.01;
    },
    buySell: function (side) {
      var self = this;
      if (!this.guard()) return;
      /* never die silently - a stuck `busy` must not swallow every
         click with zero feedback.*/
      if (this.busy) { toast("Order in progress - please wait..."); return; }
      var cfg = this.cfg(), sym = this.activeSymbol(), vol = this.vol();
      var q = this.quote();
      var px = q ? (side === "buy" ? q.ask : q.bid) : null;
      var doSend = function () {
        self.busy = true;
        self.api("/api/trade/market", {
          symbol: sym, side: side, volume: vol,
          sl_pts: cfg.scalp ? 0 : cfg.slPts, tp_pts: cfg.scalp ? 0 : cfg.tpPts,
          comment: "chart" + (cfg.scalp ? "_scalp" : "")
        }).then(function (r) {
          self.busy = false;
          toast(r.ok ? r.message : ("Order failed: " + r.message), !r.ok);
          if (r.ok) playAlertSound();
          self.poll();
        }).catch(function (e) { self.busy = false; toast("Order failed: " + e.message, true); });
      };
      if (cfg.scalp) { doSend(); return; }  // explicit opt-in: one tap
      var slP = cfg.slPts ? ("<br>SL " + cfg.slPts + " pts") : "";
      var tpP = cfg.tpPts ? ("<br>TP " + cfg.tpPts + " pts") : "";
      Modal.open(side === "buy" ? "Confirm BUY" : "Confirm SELL",
        "<p><b style='color:" + (side === "buy" ? "#26a69a" : "#ef5350") + "'>" +
        (side === "buy" ? "BUY " : "SELL ") + vol.toFixed(2) + " " + sym + "</b><br>" +
        "Price ~ " + (px ? px.toFixed(2) : "market") + slP + tpP + "<br>" +
        "Account " + this.account.login + " @ " + this.account.server + "</p>",
        '<button class="btn ghost" id="cf-no">Cancel</button>' +
        '<button class="btn" id="cf-yes">' + (side === "buy" ? "BUY" : "SELL") + "</button>");
      $("cf-no").addEventListener("click", Modal.close);
      $("cf-yes").addEventListener("click", function () { Modal.close(); doSend(); });
    },
    placePending: function () {
      var self = this;
      if (!this.guard()) return;
      if (this.busy) { toast("Order in progress - please wait..."); return; }
      var cfg = this.cfg(), sym = this.activeSymbol(), vol = this.vol();
      var price = parseFloat(this.panel.querySelector("#tp-price").value);
      if (!isFinite(price) || price <= 0) { toast("Enter a trigger price", true); return; }
      var q = this.quote();
      var otype;
      if (cfg.otype === "limit") otype = price < (q ? q.ask : price) ? "buy_limit" : "sell_limit";
      else otype = price > (q ? q.ask : price) ? "buy_stop" : "sell_stop";
      this.busy = true;
      this.api("/api/trade/pending", {
        symbol: sym, otype: otype, volume: vol, price: price,
        sl_pts: cfg.slPts, tp_pts: cfg.tpPts, comment: "chart"
      }).then(function (r) {
        self.busy = false;
        toast(r.ok ? r.message : ("Pending failed: " + r.message), !r.ok);
        self.poll();
      }).catch(function (e) { self.busy = false; toast("Pending failed: " + e.message, true); });
    },
    closeTicket: function (ticket, volume, silent) {
      var self = this;
      if (!this.guard()) return;
      this.api("/api/trade/close", { ticket: ticket, volume: volume || 0 }).then(function (r) {
        if (!silent) toast(r.ok ? r.message : ("Close failed: " + r.message), !r.ok);
        self.poll();
      }).catch(function (e) { if (!silent) toast("Close failed: " + e.message, true); });
    },
    closeHalf: function () {
      var sym = this.activeSymbol();
      var ps = this.positions.filter(function (p) { return p.symbol === sym; });
      if (!ps.length) { toast("No open positions on " + sym); return; }
      var self = this, done = 0;
      ps.forEach(function (p) {
        self.api("/api/trade/close", { ticket: p.ticket, volume: p.volume / 2 }).then(function () {
          if (++done === ps.length) { toast("Closed 50% of " + ps.length + " position(s)"); self.poll(); }
        }).catch(function (e) { toast("Close failed: " + e.message, true); });
      });
    },
    flattenAll: function () {
      var self = this;
      if (!this.guard()) return;
      var sym = this.activeSymbol();
      var np = this.positions.filter(function (p) { return p.symbol === sym; }).length;
      var no = this.orders.filter(function (o) { return o.symbol === sym; }).length;
      if (!np && !no) { toast("Nothing to flatten on " + sym); return; }
      Modal.open("Flatten all",
        "<p>Close <b>" + np + "</b> position(s) and cancel <b>" + no + "</b> pending order(s) on <b>" + sym + "</b>?</p>",
        '<button class="btn ghost" id="fl-no">Cancel</button>' +
        '<button class="btn" id="fl-yes" style="background:#c62828">FLATTEN</button>');
      $("fl-no").addEventListener("click", Modal.close);
      $("fl-yes").addEventListener("click", function () {
        Modal.close();
        self.api("/api/trade/close_all", { symbol: sym }).then(function (r) {
          toast(r.message, !r.ok); self.poll();
        }).catch(function (e) { toast("Flatten failed: " + e.message, true); });
      });
    },
    reverse: function () {
      var self = this;
      if (!this.guard()) return;
      if (this.busy) { toast("Order in progress - please wait..."); return; }
      var sym = this.activeSymbol();
      var ps = this.positions.filter(function (p) { return p.symbol === sym; });
      if (!ps.length) { toast("No open positions to reverse on " + sym); return; }
      var net = 0;
      ps.forEach(function (p) { net += (p.type === "buy" ? p.volume : -p.volume); });
      if (Math.abs(net) < 1e-9) { toast("Net flat - nothing to reverse"); return; }
      var side = net > 0 ? "sell" : "buy", vol = Math.abs(net);
      Modal.open("Reverse position",
        "<p>Close " + ps.length + " position(s) and open <b>" + side.toUpperCase() + " " +
        vol.toFixed(2) + " " + sym + "</b>?</p>",
        '<button class="btn ghost" id="rv-no">Cancel</button>' +
        '<button class="btn" id="rv-yes">REVERSE</button>');
      $("rv-no").addEventListener("click", Modal.close);
      $("rv-yes").addEventListener("click", function () {
        Modal.close(); self.busy = true;
        self.api("/api/trade/close_all", { symbol: sym }).then(function () {
          return self.api("/api/trade/market", {
            symbol: sym, side: side, volume: vol,
            sl_pts: 0, tp_pts: 0, comment: "chart_rev"
          });
        }).then(function (r) {
          self.busy = false;
          toast(r.ok ? ("Reversed: " + r.message) : ("Reverse leg failed: " + r.message), !r.ok);
          self.poll();
        }).catch(function (e) { self.busy = false; toast("Reverse failed: " + e.message, true); });
      });
    },
    beMove: function (ticket) {
      var self = this;
      var p = null;
      this.positions.forEach(function (x) { if (x.ticket === ticket) p = x; });
      if (!p) { toast("Position not found", true); return; }
      this.api("/api/trade/modify_position", { ticket: ticket, sl: p.price_open, tp: p.tp })
        .then(function (r) { toast(r.ok ? "SL -> breakeven" : ("Failed: " + r.message), !r.ok); self.poll(); })
        .catch(function (e) { toast("Failed: " + e.message, true); });
    },
    remapKeys: function () {
      var k = this.keys();
      var v = prompt("Hotkeys as buy,sell,flatten,reverse (single keys):",
        [k.buy, k.sell, k.flat, k.reverse].join(","));
      if (!v) return;
      var parts = v.split(",").map(function (s) { return (s || "").trim().toLowerCase().slice(0, 1); });
      if (parts.length !== 4 || parts.some(function (x) { return !x; })) { toast("Need 4 keys", true); return; }
      try { localStorage.setItem(this.LS_KEYS, JSON.stringify({ buy: parts[0], sell: parts[1], flat: parts[2], reverse: parts[3] })); } catch (e) {}
      toast("Hotkeys updated: " + parts.join(" / ").toUpperCase());
    },

    /* -------------- chart overlays + drag */
    drawOn: function (pane) {
      var ctx = pane.ctx, self = this;
      (pane.tradePos || []).forEach(function (p) { self.drawPosition(pane, ctx, p); });
      (pane.tradeOrd || []).forEach(function (o) { self.drawPending(pane, ctx, o); });
      if (pane.tradeDrag) self.drawDragGhost(pane, ctx);
    },
    yOf: function (pane, price) { return pane.series.priceToCoordinate(price); },
    drawPosition: function (pane, ctx, p) {
      var yE = this.yOf(pane, p.price_open);
      if (yE === null || yE === undefined) return;
      var w = pane.wrapEl.clientWidth;
      var col = p.type === "buy" ? "#2962ff" : "#ef5350";
      ctx.save();
      ctx.strokeStyle = col; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(0, yE); ctx.lineTo(w, yE); ctx.stroke();
      // SL / TP dashed
      if (p.sl) this.hline(ctx, w, this.yOf(pane, p.sl), "#ef5350", [5, 4]);
      if (p.tp) this.hline(ctx, w, this.yOf(pane, p.tp), "#26a69a", [5, 4]);
      // label: ticket + live P&L
      var pnl = p.pnl || 0;
      var txt = "#" + p.ticket + " " + p.type.toUpperCase() + " " + p.volume.toFixed(2) +
        "  " + (pnl >= 0 ? "+" : "-") + "$" + Math.abs(pnl).toFixed(2);
      ctx.font = "11px sans-serif";
      var tw = ctx.measureText(txt).width + 12;
      ctx.fillStyle = col;
      ctx.fillRect(w - tw - 62, yE - 10, tw, 18);
      ctx.fillStyle = "#fff";
      ctx.fillText(txt, w - tw - 56, yE + 3);
      ctx.restore();
    },
    drawPending: function (pane, ctx, o) {
      var y = this.yOf(pane, o.price);
      if (y === null || y === undefined) return;
      var w = pane.wrapEl.clientWidth;
      ctx.save();
      this.hline(ctx, w, y, "#ff9800", [3, 3]);
      var txt = "#" + o.ticket + " " + o.type.replace(/_/g, " ").toUpperCase() + " @ " + o.price.toFixed(2);
      ctx.font = "11px sans-serif";
      var tw = ctx.measureText(txt).width + 12;
      ctx.fillStyle = "#ff9800";
      ctx.fillRect(w - tw - 62, y - 10, tw, 18);
      ctx.fillStyle = "#000";
      ctx.fillText(txt, w - tw - 56, y + 3);
      ctx.restore();
    },
    hline: function (ctx, w, y, color, dash) {
      if (y === null || y === undefined) return;
      ctx.strokeStyle = color; ctx.lineWidth = 1;
      ctx.setLineDash(dash || []);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
      ctx.setLineDash([]);
    },
    drawDragGhost: function (pane, ctx) {
      var d = pane.tradeDrag, y = this.yOf(pane, d.price);
      if (y === null || y === undefined) return;
      var w = pane.wrapEl.clientWidth;
      ctx.save();
      this.hline(ctx, w, y, "#ffffff", [4, 3]);
      ctx.restore();
      // $ risk / $ reward tooltip
      var tip = $("tp-dragtip"), info = this.symInfo[pane.state.symbol];
      if (tip && info && d.pos) {
        var perPt = (info.tick_value / info.tick_size) * d.pos.volume;
        var diff = d.kind === "sl"
          ? (d.pos.type === "buy" ? d.pos.price_open - d.price : d.price - d.pos.price_open)
          : (d.pos.type === "buy" ? d.price - d.pos.price_open : d.pos.price_open - d.price);
        var usd = diff * perPt;
        tip.style.display = "block";
        tip.style.left = (d.mx + 14) + "px";
        tip.style.top = (d.my - 10) + "px";
        tip.innerHTML = (d.kind === "sl" ? "Risk " : "Reward ") +
          (usd >= 0 ? "+$" : "-$") + Math.abs(usd).toFixed(2) +
          " @ " + d.price.toFixed(info.digits);
      }
    },
    hideDragTip: function () {
      var tip = $("tp-dragtip");
      if (tip) tip.style.display = "none";
    },
    /* mouseup after an SL/TP/pending drag: push to the broker; on failure the
    ghost simply vanishes and the line redraws from server state (clear revert).*/
    finishDrag: function (pane, td) {
      var self = this, hit = td.hit;
      if (!this.guard()) { pane.draw(); return; }
      var done = function (r) {
        toast(r.ok ? r.message : ("Modify failed: " + r.message));
        self.poll();
      };
      var fail = function (e) { toast("Modify failed: " + e.message); pane.draw(); };
      if (hit.kind === "sl" || hit.kind === "tp") {
        var body = { ticket: hit.pos.ticket };
        body[hit.kind] = Math.round(td.price * 100) / 100;
        this.api("/api/trade/modify_position", body).then(done).catch(fail);
      } else if (hit.kind === "pend") {
        this.api("/api/trade/modify_pending",
          { order: hit.ord.ticket, price: Math.round(td.price * 100) / 100 }).then(done).catch(fail);
      }
    },
    /* hit test: {kind:'entry'|'sl'|'tp'|'pend', ref} or null */
    hitTest: function (pane, x, y) {
      var self = this, best = null, bestD = 7;
      (pane.tradePos || []).forEach(function (p) {
        [["entry", p.price_open], ["sl", p.sl], ["tp", p.tp]].forEach(function (kv) {
          if (!kv[1]) return;
          var yy = self.yOf(pane, kv[1]);
          if (yy === null || yy === undefined) return;
          var d = Math.abs(y - yy);
          if (d < bestD) { bestD = d; best = { kind: kv[0], pos: p }; }
        });
      });
      (pane.tradeOrd || []).forEach(function (o) {
        var yy = self.yOf(pane, o.price);
        if (yy === null || yy === undefined) return;
        var d = Math.abs(y - yy);
        if (d < bestD) { bestD = d; best = { kind: "pend", ord: o }; }
      });
      return best;
    },
    popupFor: function (pane, hit, x, y) {
      var self = this;
      if (hit.kind === "entry") {
        var p = hit.pos;
        CtxMenu.show(x, y, [
          { label: "Close #" + p.ticket, fn: function () { self.closeTicket(p.ticket); } },
          { label: "Close 50%", fn: function () { self.closeTicket(p.ticket, p.volume / 2); } },
          { label: "SL -> breakeven", fn: function () { self.beMove(p.ticket); } },
          {
            label: "Reverse", fn: function () {
              self.api("/api/trade/close", { ticket: p.ticket }).then(function () {
                return self.api("/api/trade/market", {
                  symbol: p.symbol, side: p.type === "buy" ? "sell" : "buy",
                  volume: p.volume, sl_pts: 0, tp_pts: 0, comment: "chart_rev"
                });
              }).then(function (r) {
                toast(r.ok ? ("Reversed: " + r.message) : ("Reverse leg failed: " + r.message), !r.ok);
                self.poll();
              }).catch(function (e) { toast("Reverse failed: " + e.message, true); });
            }
          }
        ]);
      } else if (hit.kind === "pend") {
        var o = hit.ord;
        CtxMenu.show(x, y, [
          {
            label: "Move trigger...", fn: function () {
              var v = prompt("New trigger price for #" + o.ticket + ":", o.price);
              if (v === null) return;
              v = parseFloat(v);
              if (!isFinite(v) || v <= 0) { toast("Bad price", true); return; }
              self.api("/api/trade/modify_pending", { order: o.ticket, price: v }).then(function (r) {
                toast(r.ok ? r.message : ("Failed: " + r.message), !r.ok); self.poll();
              }).catch(function (e) { toast("Failed: " + e.message, true); });
            }
          },
          {
            label: "Delete #" + o.ticket, fn: function () {
              self.api("/api/trade/delete_pending", { order: o.ticket }).then(function (r) {
                toast(r.ok ? r.message : ("Failed: " + r.message), !r.ok); self.poll();
              }).catch(function (e) { toast("Failed: " + e.message, true); });
            }
          }
        ]);
      }
    }
  };

  /* drag tip element (shared) */
  (function () {
    var t = document.createElement("div");
    t.className = "tp-dragtip"; t.id = "tp-dragtip";
    document.body.appendChild(t);
  })();

/* ================================================================== boot */
  $("m-close").addEventListener("click", Modal.close);
  document.querySelector("#modal-root .mback").addEventListener("click", function () {
    if (!Modal._sticky) Modal.close();
  });
  $("btn-trade").addEventListener("click", function () { Trade.toggle(); });
  $("btn-news").addEventListener("click", function () { NewsUI.toggle(); });
  /* trade hotkeys - skipped while typing or when a modal is open */
  document.addEventListener("keydown", function (e) {
    if (!Trade.visible) return;
    var t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
    if ($("modal-root").classList.contains("open")) return;
    var k = (e.key || "").toLowerCase(), keys = Trade.keys();
    if (k === keys.buy) Trade.buySell("buy");
    else if (k === keys.sell) Trade.buySell("sell");
    else if (k === keys.flat) Trade.flattenAll();
    else if (k === keys.reverse) Trade.reverse();
  });
  /* version check FIRST - if the Python process is older than this
     page, every feature misbehaves silently. Show an unmissable banner.*/
  (function versionCheck() {
    fetch("/api/version").then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.version && d.version !== APP_VERSION) {
          var b = document.createElement("div");
          b.id = "ver-mismatch";
          b.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:9999;" +
            "background:#b71c1c;color:#fff;font:700 13px/1.5 sans-serif;" +
            "padding:10px 16px;text-align:center;";
          b.innerHTML = "[!] VERSION MISMATCH - this page is v" + APP_VERSION +
            " but the server is v" + d.version +
            ". Stop the Python server completely and restart it from the new folder, then reload.";
          document.body.appendChild(b);
        }
      }).catch(function () {});
  })();

  /* OpsUI - one honest progress renderer for every long-running
     operation. Shows: what it's doing, how long it's been running, whether
     it's progressing or stuck, and the real error when it fails. Never a
     fake percentage: pct is rendered only when the server measured real
     work, otherwise an honest indeterminate spinner.*/
  var OpsUI = {
    esc: function (s) {
      return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
      });
    },
    render: function (container, op) {
      /* container: DOM element (or id). op: {id,label,stage,detail,pct,
         status,error,elapsed_s}. Clears the container when op is null. */
      var el = typeof container === "string" ? document.getElementById(container) : container;
      if (!el) return;
      if (!op) { el.innerHTML = ""; return; }
      var esc = OpsUI.esc;
      var icon, col;
      if (op.status === "error") { icon = "x"; col = "#f23645"; }
      else if (op.status === "done") { icon = "[ok]"; col = "#0c8f4e"; }
      else if (op.stuck) { icon = "[!]"; col = "#e8a33d"; }
      else { icon = "o"; col = "var(--accent)"; }
      var el3 = Math.round(op.elapsed_s || 0);
      var line = '<div style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-2)">' +
        '<span style="color:' + col + ';font-weight:700">' + icon + '</span>' +
        '<span style="flex:1">' + esc(op.label || "") + '</span>' +
        '<span style="color:#5a5e6a;font-size:11px">' + el3 + 's</span></div>';
      var stage = (op.stage || "") + (op.detail ? " - " + op.detail : "");
      if (stage) line += '<div style="font-size:11px;color:#8b98ab;margin:2px 0 0 22px">' +
        esc(stage) + '</div>';
      if (op.status === "error" && op.error)
        line += '<div style="font-size:11px;color:#f23645;margin:2px 0 0 22px">' +
          esc(String(op.error).slice(0, 220)) + '</div>';
      if (op.status === "running") {
        if (typeof op.pct === "number")
          line += '<div style="height:4px;background:var(--border);border-radius:2px;margin:6px 0 0 22px">' +
            '<div style="height:100%;width:' + Math.min(100, op.pct) +
            '%;background:var(--accent);border-radius:2px;transition:width .3s"></div></div>';
        else
          line += '<div style="height:4px;background:var(--border);border-radius:2px;margin:6px 0 0 22px;' +
            'overflow:hidden"><div class="opbar" style="height:100%;width:30%;background:var(--accent);' +
            'border-radius:2px"></div></div>';
      }
      el.innerHTML = line;
    }
  };

  /* startup sync - poll the server's warmup status and show
     what it's doing, instead of a blank chart. Hides once history is
     synced (or after 90s regardless - the chart loads anyway). */
  (function startupSync() {
    var overlay = document.getElementById("sync-overlay");
    var msg = document.getElementById("sync-msg");
    var steps = document.getElementById("sync-steps");
    var sub = document.getElementById("sync-sub");
    if (!overlay) return;
    var done = false;
    var t0 = Date.now();
    function hide() {
      if (done) return; done = true;
      overlay.style.transition = "opacity .4s"; overlay.style.opacity = "0";
      setTimeout(function () { overlay.style.display = "none"; }, 450);
    }
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      /* the 90s timeout lives OUTSIDE the fetch promise. If the
         server hangs the request (sick MT5), the old code never hid -
         the tries>45 check was inside .then/.catch, which never run when
         a fetch neither resolves nor rejects. Now the overlay ALWAYS dies
         on schedule instead of sitting at "Connecting to server..." forever. */
      if (tries > 45) { clearInterval(timer); hide(); return; }
      /* hung fetches are aborted after 10s so they can't pile up
         - each poll is independent. */
      var ctl = null, killer = null;
      try {
        ctl = new AbortController();
        killer = setTimeout(function () { try { ctl.abort(); } catch (e) {} }, 10000);
      } catch (e) { /* very old browser: no abort, fetch still works */ }
      var fopts = ctl ? { signal: ctl.signal } : undefined;
      fetch("/api/startup_status", fopts).then(function (r) { return r.json(); })
        .then(function (d) {
          if (killer) clearTimeout(killer);
          /* real-time step list instead of a static message -
             the user sees exactly which stage the startup is at, live. */
          msg.textContent = d.message || "Syncing...";
          var st = [];
          st.push("Server [ok]");
          /* feed-aware label - "OANDA [ok]", not "MT5 [ok]", in OANDA mode. */
          var fl = d.feed_label || "MT5";
          st.push(d.mt5_live ? fl + " [ok]" : fl + " ...");
          if (d.warmed) st.push("History [ok]");
          else if (d.hist_circuit === "open" || d.hist_circuit === "half-open")
            st.push("History [!] cached (MT5 unreachable)");
          else st.push("History ...");
          steps.textContent = st.join("   -   ");
          var bits = [];
          if (d.warmed_bars) bits.push(d.warmed_bars.toLocaleString("en-IN") + " bars cached");
          if (d.ticks_today >= 0) bits.push(d.ticks_today.toLocaleString("en-IN") + " ticks today");
          bits.push(Math.round((Date.now() - t0) / 1000) + "s");
          sub.textContent = bits.join(" - ");
          /* live per-operation progress - what / how long /
             stuck? / real error - instead of one static message. */
          var opsBox = document.getElementById("sync-ops");
          if (opsBox) {
            opsBox.innerHTML = "";
            [d.startup_op, d.tickgap_op].forEach(function (op) {
              if (!op) return;
              var row = document.createElement("div");
              OpsUI.render(row, op);
              opsBox.appendChild(row);
            });
          }
          if (d.warmed) { clearInterval(timer); hide(); }
        })
        .catch(function () {
          if (killer) clearTimeout(killer);
          /* never a bare "Waiting for server..." - say how long and
             what to check. */
          var s = Math.round((Date.now() - t0) / 1000);
          msg.textContent = "Server is not answering (" + s + "s) - is the Python window still running?";
          steps.textContent = "Server ...";
          sub.textContent = s + "s elapsed - the chart loads anyway when the server responds";
          var opsBox2 = document.getElementById("sync-ops");
          if (opsBox2) opsBox2.innerHTML = "";
        });
    }, 2000);
    msg.textContent = "Connecting to server...";
  })();
  fetch("/api/status")
    .then(function (r) { return r.json(); })
    .then(function (st) { Layout.init(st); })
    .catch(function () { Layout.init({ mode: "DEMO", symbol: "XAUUSD", version: APP_VERSION }); });
  try { document.title = "StockSophist Charts v" + APP_VERSION; } catch (e) {}
  /* exposed for automated testing / debugging consoles */
  window.StockSophistLayout = Layout;
})();
