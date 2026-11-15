/* protonfs du: page shell. Exposes a global PFS; the charts (PFS.charts) read only PFS.state. */
(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var meta = $('meta[name="pfs-token"]');
  var TOKEN = meta ? meta.getAttribute("content") : "";
  var DEBOUNCE_MS = 300;
  var SPLIT_KEY = "pfs.split";

  var PFS = window.PFS = {
    state: {
      path: null, rows: [], breakdowns: {}, selected: null, mode: "abs",
      settings: null, pending: [], trash: null, sort: { key: "size", dir: "desc" },
      progress: null, tab: "treemap", icicle: false
    },
    charts: {}
  };
  var S = PFS.state;

  // ------------------------------------------------------------------ helpers
  function humanSize(n) {
    var units = ["B", "KiB", "MiB", "GiB", "TiB"], v = n, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return i === 0 ? String(Math.round(v)) + " B" : v.toFixed(1) + " " + units[i];
  }
  function formatAge(s) {
    var units = [["d", 86400], ["h", 3600], ["m", 60]];
    for (var i = 0; i < units.length; i++) {
      if (s >= units[i][1]) {
        var v = s / units[i][1];
        return (v >= 10 || Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1)) + units[i][0];
      }
    }
    return s.toFixed(0) + "s";
  }
  function parseDuration(text) {
    var m = /^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$/i.exec(text || "");
    if (!m) return null;
    var mult = { "": 1, s: 1, m: 60, h: 3600, d: 86400, w: 604800 }[m[2].toLowerCase()];
    return parseFloat(m[1]) * mult;
  }
  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text != null) e.textContent = text;
    return e;
  }
  function api(method, url, body) {
    var opts = { method: method, headers: {}, credentials: "same-origin" };
    if (method === "POST") {
      opts.headers["X-Protonfs-Token"] = TOKEN;
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body || {});
    }
    return fetch(url, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var err = new Error(data.error || ("HTTP " + r.status));
          err.status = r.status;
          throw err;
        }
        return data;
      });
    });
  }
  function setStatus(text) { $("#status").textContent = text || ""; }
  function parentOf(path) {
    var i = path.lastIndexOf("/");
    return i <= 0 ? "/" : path.slice(0, i);
  }
  function atTarget() { return !S.settings || S.path === S.settings.target; }

  // ------------------------------------------------------------------ loading
  var loadSeq = 0;
  function load(queue) {
    if (S.path == null || !S.settings) return Promise.resolve();
    var seq = ++loadSeq;
    var url = (atTarget() ? "/api/root?" : "/api/tree?path=" + encodeURIComponent(S.path) +
      "&depth=" + S.settings.maxdepth + "&") + "queue=" + queue;
    return api("GET", url).then(function (data) {
      if (seq !== loadSeq) return; // a newer navigation superseded this answer
      S.rows = data.rows || [];
      S.breakdowns = data.breakdowns || {};
      S.pending = data.pending || [];
      if (data.trash !== undefined) S.trash = data.trash;
      if (S.selected && !S.rows.some(function (r) { return r.path === S.selected; })) S.selected = null;
      setStatus("");
      PFS.render();
    }).catch(function (e) { setStatus("Could not load " + S.path + ": " + e.message); });
  }

  // trailing debounce, but never starved: under a steady stream of events the table
  // is still refetched at least once per MAX_WAIT_MS
  var timer = null, firstPending = 0, MAX_WAIT_MS = 1000;
  function scheduleRefetch() {
    var now = Date.now();
    if (!firstPending) firstPending = now;
    clearTimeout(timer);
    var wait = Math.max(0, Math.min(DEBOUNCE_MS, firstPending + MAX_WAIT_MS - now));
    timer = setTimeout(function () { firstPending = 0; load("new"); }, wait);
  }
  function parseEvent(ev) {
    try { return JSON.parse(ev.data); } catch (e) {
      console.warn("ignoring malformed event", e);
      return null;
    }
  }

  PFS.navigate = function (path) {
    S.path = path;
    S.selected = null;
    return load("stale");
  };
  PFS.up = function () {
    if (atTarget() || S.path == null) return Promise.resolve();
    return PFS.navigate(parentOf(S.path));
  };
  PFS.select = function (path) {
    S.selected = path;
    // restyle in place: rebuilding the rows between the clicks of a double click would
    // lose the dblclick target
    document.querySelectorAll("#rows tr").forEach(function (tr) {
      var on = tr.dataset.path === path && tr.dataset.kind !== "gap";
      tr.classList.toggle("selected", on);
      if (on && tr.scrollIntoView) tr.scrollIntoView({ block: "nearest" });
    });
    if (PFS.charts && typeof PFS.charts.render === "function") PFS.charts.render();
  };

  // ------------------------------------------------------------------ render
  function sortedRows() {
    // keep the tree order; sort siblings (rows of one parent) by the chosen column
    var rows = S.rows, out = [], key = S.sort.key, sign = S.sort.dir === "asc" ? 1 : -1;
    function val(r) {
      var v = r[key];
      if (key === "name") return String(v).toLowerCase();
      return v == null ? -Infinity : v;
    }
    function cmp(a, b) {
      if (a.kind === "gap" !== (b.kind === "gap")) return a.kind === "gap" ? 1 : -1;
      var x = val(a), y = val(b);
      return (x < y ? -1 : x > y ? 1 : 0) * sign;
    }
    function build(i, depth) {
      var group = [];
      while (i < rows.length && rows[i].depth >= depth) {
        if (rows[i].depth === depth) {
          var node = { row: rows[i], kids: [] };
          var res = build(i + 1, depth + 1);
          node.kids = res.group; i = res.next;
          group.push(node);
        } else { i++; }
      }
      return { group: group, next: i };
    }
    function flatten(nodes) {
      nodes.slice().sort(function (a, b) { return cmp(a.row, b.row); }).forEach(function (n) {
        out.push(n.row); flatten(n.kids);
      });
    }
    if (rows.length) flatten(build(0, rows[0].depth).group);
    return out;
  }

  function sizeCell(r) {
    var td = el("td", { "class": "num" });
    if (r.size == null) { td.textContent = ""; return td; }
    td.title = r.size + " bytes";
    var pct = { plan: r.pct_plan, used: r.pct_used, parent: r.pct_parent }[S.mode];
    if (S.mode === "abs") td.textContent = humanSize(r.size);
    else if (pct == null) {
      td.textContent = "—";
      td.title = S.mode === "plan" ? "set du.plan_size" : "not known";
    } else {
      td.textContent = pct.toFixed(1) + "%";
      td.title = r.size + " bytes (" + humanSize(r.size) + ")";
    }
    return td;
  }
  function trashText(r) {
    if (r.in_trash == null) return r.kind === "file" ? "—" : "";
    if (r.in_trash_complete) return humanSize(r.in_trash);
    var head = "≥ " + humanSize(r.in_trash);
    if (r.unchecked_bytes == null || !r.size) return head + " (unchecked: unknown)";
    return head + " (" + humanSize(r.unchecked_bytes) + " / " +
      Math.round(r.unchecked_bytes / r.size * 100) + "% unchecked)";
  }
  function fade(r) {
    var th = S.settings ? parseDuration(S.settings.refresh) : null;
    if (r.age_seconds == null || !th) return 1;
    return Math.max(0.6, 1 - 0.4 * Math.min(1, r.age_seconds / th));
  }

  function renderTable() {
    var tbody = $("#rows"), pending = {};
    S.pending.forEach(function (p) { pending[p] = true; });
    var frag = document.createDocumentFragment();
    sortedRows().forEach(function (r) {
      var cls = [];
      if (r.kind === "gap") cls.push("gap", "gap-" + ({ in_trash: "trash", unattributed: "unattributed",
        unreconciled: "unreconciled" }[r.gap_kind] || "unattributed"));
      else cls.push(r.kind);
      if (r.state === "sized") cls.push("sized");
      if (r.error) cls.push("error");
      if (r.exclusion !== "none" || r.state === "excluded") cls.push("excluded");
      if (r.kind !== "gap" && pending[r.path]) cls.push("pending");
      if (r.path === S.selected && r.kind !== "gap") cls.push("selected");
      var tr = el("tr", { "class": cls.join(" "), "data-path": r.path, "data-kind": r.kind });
      if (r.kind !== "gap") tr.tabIndex = -1;
      tr.style.opacity = String(fade(r));
      var name = el("td", { "class": "name" });
      var ind = el("span", { "class": "indent" });
      ind.style.width = (r.depth * 14) + "px";
      name.appendChild(ind);
      name.appendChild(el("span", { "class": "nm" }, (r.kind === "folder" ? "📁 " : "") + r.name));
      if (r.exclusion === "freeze") name.appendChild(el("span", { "class": "badge-frozen", title: "frozen (-x): cached figures, no Drive calls" }, "❄"));
      if (r.exclusion === "hide") name.appendChild(el("span", { "class": "badge-frozen", title: "hidden (-X)" }, "⊘"));
      name.title = r.error ? r.name + ": " + r.error : r.name;
      tr.appendChild(name);
      tr.appendChild(sizeCell(r));
      tr.appendChild(el("td", { "class": "num" }, r.descendants == null ? "" : String(r.descendants)));
      var tcell = el("td", { "class": "num" }, trashText(r));
      tr.appendChild(tcell);
      var age = el("td", { "class": "num" }, r.age_seconds == null ? "" : formatAge(r.age_seconds));
      if (r.checked_at != null) age.title = new Date(r.checked_at * 1000).toLocaleString();
      tr.appendChild(age);
      var full = r.exclusion === "none" ? r.state : r.state + " (" + r.exclusion + ")";
      var st = el("td", { "class": "state" });
      st.appendChild(el("span", { "class": "st" }, r.exclusion === "freeze" ? "frozen"
        : r.exclusion === "hide" ? "hidden" : r.state));
      st.title = r.error ? full + ": " + r.error : full;
      tr.appendChild(st);
      frag.appendChild(tr);
    });
    tbody.replaceChildren(frag);
    $("#sizehead").textContent = { abs: "Size", plan: "% plan", used: "% used", parent: "% parent" }[S.mode];
    document.querySelectorAll("th[data-sort]").forEach(function (th) {
      th.setAttribute("aria-sort", th.dataset.sort === S.sort.key
        ? (S.sort.dir === "asc" ? "ascending" : "descending") : "none");
    });
  }

  function renderNav() {
    var bc = $("#breadcrumb");
    bc.replaceChildren();
    var target = S.settings ? S.settings.target : "/";
    var parts = target === "/" ? [] : target.split("/").filter(Boolean);
    var acc = "";
    var crumbs = [{ label: "Drive", path: "/" }];
    parts.forEach(function (p) { acc += "/" + p; crumbs.push({ label: p, path: acc }); });
    var rest = S.path && S.path !== target ? S.path.slice(target === "/" ? 0 : target.length).split("/").filter(Boolean) : [];
    var base = target === "/" ? "" : target;
    rest.forEach(function (p) { base += "/" + p; crumbs.push({ label: p, path: base }); });
    // paths above the target are not browsable: show them as plain text
    var targetIdx = parts.length;
    crumbs.forEach(function (c, i) {
      var li = el("li");
      if (i < targetIdx) li.appendChild(el("span", null, c.label));
      else {
        var b = el("button", { type: "button" }, c.label);
        if (c.path === S.path) b.setAttribute("aria-current", "location");
        b.addEventListener("click", function () { if (c.path !== S.path) PFS.navigate(c.path); });
        li.appendChild(b);
      }
      bc.appendChild(li);
    });
    $("#goup").disabled = atTarget();
  }

  function renderProgress() {
    var p = S.progress, box = $("#progress"), cancel = $("#walk-cancel");
    if (!p) { box.textContent = ""; cancel.hidden = true; $("#walk").hidden = false; return; }
    var running = p.state === "running";
    $("#walk").hidden = running;
    cancel.hidden = !running;
    box.textContent = running ? "Walking " + p.done + (p.total ? " / " + p.total : "") + " folders"
      : p.state === "done" ? "Walk finished" : p.state === "cancelled" ? "Walk cancelled"
      : "Walk failed" + (p.message ? ": " + p.message : "");
  }

  PFS.render = function () {
    renderNav();
    renderTable();
    renderProgress();
    document.querySelectorAll("#sizemode button").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.mode === S.mode));
    });
    if (PFS.charts && typeof PFS.charts.render === "function") PFS.charts.render();
  };

  // ------------------------------------------------------------------ table events
  var tbody = $("#rows");
  function rowOf(ev) { return ev.target.closest("tr[data-path]"); }
  tbody.addEventListener("click", function (ev) {
    var tr = rowOf(ev);
    if (tr && tr.dataset.kind !== "gap") PFS.select(tr.dataset.path);
  });
  tbody.addEventListener("dblclick", function (ev) {
    var tr = rowOf(ev);
    if (tr && tr.dataset.kind === "folder" && tr.dataset.path !== S.path) PFS.navigate(tr.dataset.path);
  });
  function selectedRow() {
    return S.rows.filter(function (r) { return r.path === S.selected && r.kind !== "gap"; })[0];
  }
  document.addEventListener("keydown", function (ev) {
    var t = ev.target;
    if (t.matches && t.matches("input, select, textarea, [role=separator]")) return;
    if (ev.key === "Backspace") { ev.preventDefault(); PFS.up(); return; }
    var inTable = t.closest && t.closest("#explorer");
    if (ev.key === "Enter" && (inTable || t === document.body)) {
      var r = selectedRow();
      if (r && r.kind === "folder" && r.path !== S.path && !(t.closest && t.closest("button"))) {
        ev.preventDefault(); PFS.navigate(r.path);
      }
      return;
    }
    if ((ev.key === "ArrowDown" || ev.key === "ArrowUp") && inTable) {
      var list = sortedRows().filter(function (r) { return r.kind !== "gap"; });
      var i = list.findIndex(function (r) { return r.path === S.selected; });
      i = Math.max(0, Math.min(list.length - 1, i + (ev.key === "ArrowDown" ? 1 : -1)));
      if (list[i]) { ev.preventDefault(); PFS.select(list[i].path); }
    }
  });
  // the table body is the keyboard focus target for Enter / arrows
  $("#tablewrap").tabIndex = 0;
  $("#tablewrap").setAttribute("aria-label", "Folder contents");

  document.querySelectorAll("th[data-sort]").forEach(function (th) {
    $("button", th).addEventListener("click", function () {
      var key = th.dataset.sort;
      S.sort = { key: key, dir: S.sort.key === key && S.sort.dir === "desc" ? "asc" :
        S.sort.key === key ? "desc" : (key === "name" ? "asc" : "desc") };
      PFS.render();
    });
  });
  $("#goup").addEventListener("click", function () { PFS.up(); });
  document.querySelectorAll("#sizemode button").forEach(function (b) {
    b.addEventListener("click", function () { S.mode = b.dataset.mode; PFS.render(); });
  });
  $("#walk").addEventListener("click", function () {
    api("POST", "/api/walk", { paths: [S.path] }).catch(function (e) { setStatus("Walk: " + e.message); });
  });
  $("#walk-cancel").addEventListener("click", function () { api("POST", "/api/walk/cancel", {}); });
  $("#refresh-here").addEventListener("click", function () {
    api("POST", "/api/refresh", { path: S.path }).then(function (d) {
      S.pending = d.pending || S.pending; PFS.render();
    }).catch(function (e) { setStatus("Refresh: " + e.message); });
  });

  // ------------------------------------------------------------------ tabs
  function showTab(name) {
    S.tab = name;
    document.querySelectorAll("#tabs [data-tab]").forEach(function (b) {
      b.setAttribute("aria-selected", String(b.dataset.tab === name));
    });
    document.querySelectorAll("#charts .pane").forEach(function (p) { p.hidden = p.dataset.pane !== name; });
    if (PFS.charts && typeof PFS.charts.render === "function") PFS.charts.render();
  }
  document.querySelectorAll("#tabs [data-tab]").forEach(function (b) {
    b.addEventListener("click", function () { showTab(b.dataset.tab); });
  });
  $("#icicle-toggle").addEventListener("change", function (ev) {
    S.icicle = ev.target.checked;
    if (PFS.charts && typeof PFS.charts.render === "function") PFS.charts.render();
  });

  // ------------------------------------------------------------------ splitter
  var main = $("#main"), explorer = $("#explorer"), splitter = $("#splitter");
  function clampSplit(v) { return Math.min(85, Math.max(15, v)); }
  function applySplit(pct) {
    pct = clampSplit(pct);
    explorer.style.flexBasis = pct + "%";
    splitter.setAttribute("aria-valuenow", String(Math.round(pct)));
    return pct;
  }
  var saved = parseFloat(localStorage.getItem(SPLIT_KEY));
  applySplit(isNaN(saved) ? 45 : saved);
  function ratioAt(clientX) {
    var box = main.getBoundingClientRect();
    return (clientX - box.left) / box.width * 100;
  }
  splitter.addEventListener("pointerdown", function (ev) {
    splitter.setPointerCapture(ev.pointerId);
    splitter.classList.add("dragging");
    document.body.classList.add("dragging");
  });
  splitter.addEventListener("pointermove", function (ev) {
    if (splitter.hasPointerCapture(ev.pointerId)) applySplit(ratioAt(ev.clientX));
  });
  function endDrag(ev) {
    if (!splitter.hasPointerCapture(ev.pointerId)) return;
    splitter.releasePointerCapture(ev.pointerId);
    splitter.classList.remove("dragging");
    document.body.classList.remove("dragging");
    localStorage.setItem(SPLIT_KEY, String(applySplit(ratioAt(ev.clientX))));
    if (PFS.charts && typeof PFS.charts.render === "function") PFS.charts.render();
  }
  splitter.addEventListener("pointerup", endDrag);
  splitter.addEventListener("pointercancel", endDrag);
  splitter.addEventListener("keydown", function (ev) {
    var cur = parseFloat(splitter.getAttribute("aria-valuenow")) || 45;
    var d = ev.key === "ArrowLeft" ? -2 : ev.key === "ArrowRight" ? 2 : 0;
    if (!d) return;
    ev.preventDefault();
    localStorage.setItem(SPLIT_KEY, String(applySplit(cur + d)));
    if (PFS.charts && typeof PFS.charts.render === "function") PFS.charts.render();
  });

  // ------------------------------------------------------------------ settings
  var excludeRows = $("#exclude-rows");
  function addExcludeRow(kind, pattern) {
    var row = el("div", { "class": "exclude-row" });
    var input = el("input", { type: "text", "aria-label": "Pattern", spellcheck: "false" });
    input.value = pattern || "";
    var sel = el("select", { "aria-label": "Kind" });
    sel.appendChild(el("option", { value: "freeze" }, "freeze"));
    sel.appendChild(el("option", { value: "hide" }, "hide"));
    sel.value = kind;
    var rm = el("button", { type: "button", "aria-label": "Remove pattern" }, "×");
    row.appendChild(input); row.appendChild(sel); row.appendChild(rm);
    excludeRows.appendChild(row);
    input.addEventListener("change", postExcludes);
    sel.addEventListener("change", postExcludes);
    rm.addEventListener("click", function () { row.remove(); postExcludes(); });
    return row;
  }
  function fillControls() {
    var s = S.settings;
    $("#refresh").value = s.refresh;
    $("#depth").value = s.maxdepth;
    $("#depth-out").textContent = s.maxdepth;
    $("#dirs-only").checked = !!s.dirs_only;
    excludeRows.replaceChildren();
    (s.freeze || []).forEach(function (p) { addExcludeRow("freeze", p); });
    (s.hide || []).forEach(function (p) { addExcludeRow("hide", p); });
  }
  function applySettings(patch, msgEl, revert) {
    msgEl.textContent = "";
    return api("POST", "/api/settings", patch).then(function (s) {
      S.settings = s;
      return load("stale");
    }).catch(function (e) {
      msgEl.textContent = e.message;
      revert();
    });
  }
  function postExcludes() {
    var freeze = [], hide = [];
    excludeRows.querySelectorAll(".exclude-row").forEach(function (row) {
      var text = $("input", row).value.trim();
      if (!text) return;
      ($("select", row).value === "hide" ? hide : freeze).push(text);
    });
    applySettings({ freeze: freeze, hide: hide }, $("#excludes-msg"), function () {
      var cur = S.settings;
      excludeRows.replaceChildren();
      (cur.freeze || []).forEach(function (p) { addExcludeRow("freeze", p); });
      (cur.hide || []).forEach(function (p) { addExcludeRow("hide", p); });
    });
  }
  $("#exclude-add").addEventListener("click", function () { $("input", addExcludeRow("freeze", "")).focus(); });
  $("#refresh").addEventListener("change", function (ev) {
    var v = ev.target.value.trim();
    applySettings({ refresh: v }, $("#refresh-msg"), function () { $("#refresh").value = S.settings.refresh; });
  });
  $("#depth").addEventListener("input", function (ev) { $("#depth-out").textContent = ev.target.value; });
  $("#depth").addEventListener("change", function (ev) {
    applySettings({ maxdepth: parseInt(ev.target.value, 10) }, $("#depth-msg"), function () {
      $("#depth").value = S.settings.maxdepth; $("#depth-out").textContent = S.settings.maxdepth;
    });
  });
  $("#dirs-only").addEventListener("change", function (ev) {
    applySettings({ dirs_only: ev.target.checked }, $("#dirs-msg"), function () {
      $("#dirs-only").checked = !!S.settings.dirs_only;
    });
  });
  $("#settings-toggle").addEventListener("click", function () {
    var panel = $("#settings"), open = panel.hidden;
    panel.hidden = !open;
    $("#settings-toggle").setAttribute("aria-expanded", String(open));
  });

  $("#notice-dismiss").addEventListener("click", function () { $("#notice").hidden = true; });

  // ------------------------------------------------------------------ live events
  function within(path, base) {
    return path === base || path.indexOf(base === "/" ? "/" : base + "/") === 0;
  }
  function connect() {
    var es = new EventSource("/api/events");
    es.addEventListener("breakdown", function (ev) {
      var d = parseEvent(ev);
      if (!d) return;
      if (S.path && within(d.path, S.path) && d.breakdown) S.breakdowns[d.path] = d.breakdown;
      scheduleRefetch();
    });
    es.addEventListener("progress", function (ev) {
      var d = parseEvent(ev);
      if (!d) return;
      S.progress = d;
      renderProgress();
      scheduleRefetch();
    });
    es.addEventListener("notice", function (ev) {
      var d = parseEvent(ev);
      if (!d) return;
      $("#notice-text").textContent = d.message || "";
      $("#notice").hidden = !d.message;
    });
    es.addEventListener("trash", function () { scheduleRefetch(); });
    es.addEventListener("error", function (ev) {
      if (ev && ev.data) {
        var d = parseEvent(ev);
        if (!d) return;
        setStatus((d.path ? d.path + ": " : "") + d.message);
        scheduleRefetch();
      }
    });
    PFS.events = es;
  }

  // ------------------------------------------------------------------ start
  api("GET", "/api/settings").then(function (s) {
    S.settings = s;
    fillControls();
    S.path = s.target;
    connect();
    return load("stale");
  }).catch(function (e) { setStatus("Could not start: " + e.message); });
})();
