/* protonfs du: treemap, sunburst and icicle charts. Reads only PFS.state; navigation and
   selection go through PFS.navigate / PFS.up / PFS.select. Drive names are untrusted: they
   reach the DOM only through D3 .text() / .attr() and textContent, never as markup. */
(function () {
  "use strict";
  var PFS = window.PFS;
  if (!PFS) return;
  var S = PFS.state;
  var d3 = window.d3;
  var HEADER = 15;

  // ------------------------------------------------------------------ formatting
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
  function pct(v) { return v == null ? "n/a" : v.toFixed(1) + "%"; }
  function gapClass(kind) {
    return "gap-" + ({ in_trash: "trash", unattributed: "unattributed",
      unreconciled: "unreconciled" }[kind] || "unattributed");
  }

  // ------------------------------------------------------------------ data shaping
  // Build a plain tree from the flat, pre-ordered state.rows (depth-first, children after
  // their parent), down to `depth` levels below the first row.
  function hierarchy(state, depth) {
    var rows = state.rows || [];
    if (!rows.length) return null;
    var base = rows[0].depth, stack = [], root = null, negative = 0;
    rows.forEach(function (r) {
      var rel = r.depth - base;
      if (rel > depth || r.exclusion === "hide") return;
      // a negative figure (an unreconciled remainder) has no area: it stays in the table
      if (r.size != null && r.size < 0 && rel > 0) { negative += 1; return; }
      var node = {
        path: r.path, name: r.name, kind: r.kind, gapKind: r.kind === "gap" ? r.gap_kind : null,
        state: r.state, exclusion: r.exclusion, row: r, children: null, value: 0,
        sized: false, placeholder: false, unsized: 0
      };
      while (stack.length > rel) stack.pop();
      if (rel === 0) { root = node; stack = [node]; return; }
      var parent = stack[rel - 1];
      if (!parent) return;
      if (r.size == null && !(r.exclusion === "freeze" && r.kind === "folder")) {
        parent.unsized += 1; // not drawable: no figure to size it by
        return;
      }
      (parent.children = parent.children || []).push(node);
      stack[rel] = node;
      stack.length = rel + 1;
    });
    if (!root) return null;
    root.negative = negative;
    var dirsOnly = !!(state.settings && state.settings.dirs_only);
    (function finish(n) {
      if (n.children) {
        n.children.forEach(finish);
        n.value = n.children.reduce(function (a, c) { return a + c.value; }, 0);
        // the drawn children sum below the folder's own size (files left out by Folders
        // only, items without a size): draw the shortfall so areas stay proportional
        var size = n.row.size;
        if (size != null && size > n.value && n.kind === "folder") {
          var short = size - n.value;
          n.children.push({
            path: n.path + "/\u0000filler", name: dirsOnly ? "(files, hidden by Folders only)"
              : "(not drawn)", kind: "gap", gapKind: "filler", state: "gap", exclusion: "none",
            children: null, value: short, sized: false, placeholder: false, unsized: 0,
            row: { path: n.path + "/\u0000filler", name: dirsOnly ?
              "(files, hidden by Folders only)" : "(not drawn)", kind: "gap", size: short,
              state: "gap", exclusion: "none", gap_kind: "filler", error: null,
              age_seconds: null, descendants: null, pct_plan: null, pct_used: null,
              pct_parent: null, in_trash: null, in_trash_complete: null }
          });
          n.value = size;
        }
      } else {
        n.value = n.row.size == null ? 0 : n.row.size;
        n.placeholder = n.row.size == null;
        // hatched: not walked, or listed but none of its children has a size to draw
        n.sized = n.kind === "folder" && n.state !== "error" && !n.placeholder &&
          (n.state !== "listed" || n.unsized > 0);
      }
    })(root);
    return root;
  }

  // ------------------------------------------------------------------ shared drawing bits
  var tip = null;
  function tooltip() {
    if (tip) return tip;
    tip = document.createElement("div");
    tip.id = "chart-tooltip";
    tip.setAttribute("role", "tooltip");
    tip.hidden = true;
    document.body.appendChild(tip);
    return tip;
  }
  function line(box, text, cls) {
    var d = document.createElement("div");
    if (cls) d.className = cls;
    d.textContent = text;
    box.appendChild(d);
  }
  // the row as last fetched: a redraw is skipped when only ages changed (see render), so
  // the row bound to a drawn node can be older than S.rows
  function currentRow(r) {
    var rows = S.rows || [];
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].path === r.path && rows[i].kind === r.kind) return rows[i];
    }
    return r;
  }
  function showTip(ev, n) {
    var r = currentRow(n.row), box = tooltip();
    box.replaceChildren();
    line(box, r.name, "tt-name");
    line(box, r.size == null ? "size unknown" : humanSize(r.size) + " (" + r.size + " bytes)");
    line(box, "of plan " + pct(r.pct_plan) + ", of used " + pct(r.pct_used) +
      ", of parent " + pct(r.pct_parent));
    if (r.descendants != null) line(box, r.descendants + " items");
    if (r.in_trash != null) {
      line(box, "in trash " + (r.in_trash_complete ? "" : "at least ") + humanSize(r.in_trash) +
        (r.trash_unresolved > 0 ? " (" + r.trash_unresolved + " trashed folder(s) unmeasured)" : ""));
    } else if (r.kind === "folder") line(box, "in trash unknown");
    line(box, r.age_seconds == null ? "age unknown" : formatAge(r.age_seconds) + " ago");
    line(box, "state " + (r.exclusion === "none" ? r.state : r.state + " (" + r.exclusion + ")") +
      (n.sized ? (r.state === "listed" ? ", children without a size" : ", not walked") : ""));
    if (n.unsized) line(box, n.unsized + " child item(s) without a size are not drawn");
    if (r.error) line(box, r.error, "tt-error");
    box.hidden = false;
    moveTip(ev);
  }
  function moveTip(ev) {
    var box = tooltip(), w = box.offsetWidth, h = box.offsetHeight;
    var x = ev.clientX + 14, y = ev.clientY + 14;
    if (x + w > window.innerWidth - 4) x = Math.max(4, ev.clientX - w - 14);
    if (y + h > window.innerHeight - 4) y = Math.max(4, ev.clientY - h - 14);
    box.style.left = x + "px";
    box.style.top = y + "px";
  }
  function hideTip() { if (tip) tip.hidden = true; }

  function nodeClasses(n, extra) {
    var c = ["node", n.kind === "gap" ? gapClass(n.gapKind) : n.kind];
    if (n.sized) c.push("sized");
    if (n.row.error) c.push("error");
    if (n.exclusion !== "none" || n.state === "excluded") c.push("excluded");
    if (n.path === S.selected && n.kind !== "gap") c.push("selected");
    if (extra) c.push(extra);
    return c.join(" ");
  }
  function bind(sel) {
    sel.on("mouseenter", function (ev, d) {
        showTip(ev, d.data);
      })
      .on("mousemove", moveTip)
      .on("mouseleave", hideTip)
      .on("click", function (ev, d) {
        var n = d.data;
        // the second click of a double-click: the first already acted, and going up
        // has already moved S.path, so acting again would undo or repeat it
        if (n.kind === "gap" || ev.detail > 1) return;
        if (d.depth === 0) { PFS.up(); return; }
        PFS.select(n.path);
      })
      .on("dblclick", function (ev, d) {
        var n = d.data;
        if (n.kind === "folder" && d.depth > 0 && n.path !== S.path) PFS.navigate(n.path);
      });
  }
  function valueLabel(r) {
    if (r.size == null) return "";
    if (S.mode === "abs") return humanSize(r.size);
    var p = { plan: r.pct_plan, used: r.pct_used, parent: r.pct_parent }[S.mode];
    return p == null ? humanSize(r.size) : p.toFixed(1) + "%";
  }
  function fit(text, px, perChar) {
    var max = Math.floor(px / perChar);
    if (max < 2) return "";
    return text.length <= max ? text : text.slice(0, Math.max(1, max - 1)) + "…";
  }
  // One shared, always-rendered defs element: a paint server inside a display:none subtree
  // (the hidden pane) does not resolve, so per-chart patterns would not hatch the other tab.
  function sharedDefs() {
    if (document.getElementById("pfs-defs")) return;
    var svg = d3.select(document.body).append("svg").attr("id", "pfs-defs")
      .attr("width", 0).attr("height", 0).attr("aria-hidden", "true");
    var d = svg.append("defs");
    var p = d.append("pattern").attr("id", "pfs-hatch").attr("width", 6).attr("height", 6)
      .attr("patternUnits", "userSpaceOnUse").attr("patternTransform", "rotate(45)");
    p.append("line").attr("x1", 0).attr("y1", 0).attr("x2", 0).attr("y2", 6)
      .attr("class", "hatch-line");
  }
  var palette = null;
  function colourOf(n, tops) {
    if (!palette) palette = d3.scaleOrdinal(d3.schemeTableau10);
    var top = n;
    while (top.depth > 1) top = top.parent;
    var base = d3.color(palette(top.data.name));
    if (!tops) return base.formatHex();
    return (n.children ? base.brighter(0.4 * (n.depth - 1)).copy({ opacity: 0.55 }) : base)
      .toString();
  }
  function newSvg(box, w, h, label) {
    var svg = d3.select(box).append("svg").attr("width", w).attr("height", h)
      .attr("viewBox", "0 0 " + w + " " + h).attr("role", "img").attr("aria-label", label);
    return svg;
  }
  function emptyNote(box, text) {
    var p = document.createElement("p");
    p.className = "chart-empty";
    p.textContent = text;
    box.appendChild(p);
  }
  function build(root) {
    return d3.hierarchy(root, function (d) { return d.children; })
      .sum(function (d) { return d.children ? 0 : d.value; });
  }

  // ------------------------------------------------------------------ treemap
  function treemap(box, root, mode) {
    box.replaceChildren();
    var w = box.clientWidth, h = box.clientHeight;
    if (!root || w < 20 || h < 20) return;
    if (!root.value || !root.children) {
      emptyNote(box, root.children || !root.value
        ? "Nothing with a known size here to draw."
        : root.name + " has no listing yet (" + valueLabel(root.row) + "); walk it to see inside.");
      return;
    }
    var top = build(root);
    d3.treemap().tile(d3.treemapSquarify).size([w, h]).paddingInner(1).paddingOuter(2)
      .paddingTop(function (d) { return d.children && d.depth > 0 ? HEADER : 2; })
      .round(true)(top);
    var svg = newSvg(box, w, h, "Treemap of " + root.path);
    var nodes = top.descendants().filter(function (d) {
      return d.depth > 0 && (d.x1 - d.x0) >= 1 && (d.y1 - d.y0) >= 1;
    });
    var g = svg.selectAll("g.node").data(nodes)
      .join("g")
      .attr("class", function (d) { return nodeClasses(d.data, d.children ? "branch" : "leaf"); })
      .attr("data-path", function (d) { return d.data.path; })
      .attr("transform", function (d) { return "translate(" + d.x0 + "," + d.y0 + ")"; });
    g.append("rect").attr("class", "fill")
      .attr("width", function (d) { return d.x1 - d.x0; })
      .attr("height", function (d) { return d.y1 - d.y0; })
      .attr("fill", function (d) { return colourOf(d, true); });
    g.filter(function (d) { return d.data.sized || d.data.kind === "gap" &&
      d.data.gapKind !== "in_trash"; })
      .append("rect").attr("class", "hatch")
      .attr("width", function (d) { return d.x1 - d.x0; })
      .attr("height", function (d) { return d.y1 - d.y0; });
    g.append("text").attr("class", "label").attr("x", 3).attr("y", 11)
      .text(function (d) {
        var pw = d.x1 - d.x0 - 6;
        if (d.y1 - d.y0 < 13) return "";
        var txt = d.data.name + (d.children ? "" : " " + valueLabel(d.data.row));
        return fit(txt.trim(), pw, 6.4);
      });
    bind(g);
    chartNotes(box, root);
  }
  function chartNotes(box, root) {
    var texts = [];
    if (root.unsized) texts.push(root.unsized + " item(s) without a size are not drawn.");
    if (root.negative) {
      texts.push(root.negative + " remainder(s) below zero (children measured larger than " +
        "their folder) are not drawn; see the table.");
    }
    texts.forEach(function (t) {
      var note = document.createElement("p");
      note.className = "chart-note";
      note.textContent = t;
      box.appendChild(note);
    });
  }

  // ------------------------------------------------------------------ sunburst / icicle
  function sunburst(box, root, mode, icicle) {
    box.replaceChildren();
    var w = box.clientWidth, h = box.clientHeight;
    if (!root || w < 20 || h < 20) return;
    if (!root.value && !hasPlaceholder(root)) {
      emptyNote(box, "Nothing with a known size here to draw.");
      return;
    }
    var top = build(root);
    var levels = top.height + 1;
    var svg, nodes, g;
    if (icicle) {
      d3.partition().size([w, h]).padding(1)(top);
      svg = newSvg(box, w, h, "Icicle chart of " + root.path);
      nodes = top.descendants();
      g = svg.selectAll("g.node").data(nodes).join("g")
        .attr("class", function (d) { return nodeClasses(d.data, d.depth ? "" : "root"); })
        .attr("data-path", function (d) { return d.data.path; });
      var rowH = h / levels;
      g.append("rect").attr("class", "cell fill")
        .attr("x", function (d) { return d.x0; })
        .attr("y", function (d) { return d.depth * rowH; })
        .attr("width", function (d) { return Math.max(d.data.placeholder ? 3 : 0, d.x1 - d.x0); })
        .attr("height", rowH - 1)
        .attr("fill", function (d) { return d.depth ? colourOf(d, false) : "#8884"; });
      g.filter(function (d) { return d.data.sized || d.data.kind === "gap" &&
        d.data.gapKind !== "in_trash"; })
        .append("rect").attr("class", "hatch")
        .attr("x", function (d) { return d.x0; })
        .attr("y", function (d) { return d.depth * rowH; })
        .attr("width", function (d) { return Math.max(0, d.x1 - d.x0); })
        .attr("height", rowH - 1);
      g.append("text").attr("class", "label")
        .attr("x", function (d) { return d.x0 + 3; })
        .attr("y", function (d) { return d.depth * rowH + Math.min(12, rowH - 3); })
        .text(function (d) { return fit(d.data.name, d.x1 - d.x0 - 6, 6.4); });
    } else {
      var R = Math.min(w, h) / 2 - 4;
      d3.partition().size([2 * Math.PI, R])(top);
      var ring = R / levels;
      svg = newSvg(box, w, h, "Sunburst chart of " + root.path);
      var c = svg.append("g").attr("transform", "translate(" + w / 2 + "," + h / 2 + ")");
      var arc = d3.arc()
        .startAngle(function (d) { return d.x0; })
        .endAngle(function (d) { return d.data.placeholder ? d.x0 + 0.012 : d.x1; })
        .padAngle(0.004)
        .innerRadius(function (d) { return d.depth * ring; })
        .outerRadius(function (d) { return (d.depth + 1) * ring - 1; });
      nodes = top.descendants().filter(function (d) { return d.depth > 0; });
      g = c.selectAll("g.node").data(nodes).join("g")
        .attr("class", function (d) {
          return nodeClasses(d.data, d.data.placeholder ? "placeholder" : "");
        })
        .attr("data-path", function (d) { return d.data.path; });
      g.append("path").attr("class", "arc fill").attr("d", arc)
        .attr("fill", function (d) { return colourOf(d, false); });
      g.filter(function (d) { return d.data.sized || d.data.kind === "gap" &&
        d.data.gapKind !== "in_trash"; })
        .append("path").attr("class", "hatch").attr("d", arc);
      // a zero-width placeholder shares its start angle with the next sibling: keep it on top
      g.filter(function (d) { return d.data.placeholder; }).raise();
      var hub = c.append("g").attr("class", "node hub root")
        .attr("data-path", root.path);
      hub.append("circle").attr("r", ring - 1).attr("class", "fill");
      hub.append("text").attr("class", "label").attr("text-anchor", "middle").attr("dy", "-0.2em")
        .text(fit(root.name, ring * 1.8, 6.4));
      hub.append("text").attr("class", "label sub").attr("text-anchor", "middle").attr("dy", "1.1em")
        .text(valueLabel(root.row));
      hub.on("mouseenter", function (ev) { showTip(ev, root); }).on("mousemove", moveTip)
        .on("mouseleave", hideTip)
        .on("click", function (ev) { if (ev.detail <= 1) PFS.up(); });
    }
    bind(g);
    chartNotes(box, root);
  }
  function hasPlaceholder(n) {
    return n.placeholder || (n.children || []).some(hasPlaceholder);
  }

  // ------------------------------------------------------------------ actions bar
  var bar = null;
  function token() {
    var m = document.querySelector('meta[name="pfs-token"]');
    return m ? m.getAttribute("content") : "";
  }
  function say(text) {
    var m = document.getElementById("chart-msg");
    if (m) m.textContent = text || "";
  }
  // POST an action; any failure (HTTP error, network) is shown in the bar, never thrown.
  function post(url, body, label) {
    say("");
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "X-Protonfs-Token": token(), "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) say(label + " failed: " + (data.error || "HTTP " + r.status));
        else if (data && data.queued === false) say(label + " did not start: the server is shutting down");
        return r.ok ? data : null;
      });
    }).catch(function (e) { say(label + " failed: " + e.message); return null; });
  }
  function btn(id, text, title) {
    var b = document.createElement("button");
    b.type = "button"; b.id = id; b.textContent = text; b.title = title;
    return b;
  }
  function selectedFolder() {
    var r = (S.rows || []).filter(function (x) {
      return x.path === S.selected && x.kind === "folder";
    })[0];
    return r ? r.path : null;
  }
  function ensureBar() {
    if (bar) return bar;
    sharedDefs();
    bar = document.createElement("div");
    bar.id = "chart-actions";
    var refresh = btn("chart-refresh", "Refresh now", "Re-measure the current folder now");
    var walk = btn("chart-walk", "Walk selected", "Measure everything under the selected folder");
    var wb = document.createElement("div");
    wb.id = "walkbar"; wb.setAttribute("role", "status"); wb.hidden = true;
    var txt = document.createElement("span"); txt.id = "walkbar-text";
    var prog = document.createElement("progress"); prog.id = "walkbar-meter"; prog.max = 1;
    var cancel = btn("chart-walk-cancel", "Cancel", "Stop the walk before its next folder");
    wb.appendChild(txt); wb.appendChild(prog); wb.appendChild(cancel);
    var msg = document.createElement("span");
    msg.id = "chart-msg"; msg.setAttribute("role", "alert");
    bar.appendChild(refresh); bar.appendChild(walk); bar.appendChild(wb); bar.appendChild(msg);
    document.getElementById("charts").appendChild(bar);
    refresh.addEventListener("click", function () {
      if (S.path != null) post("/api/refresh", { path: S.path }, "Refresh");
    });
    walk.addEventListener("click", function () {
      var p = selectedFolder();
      if (p) post("/api/walk", { paths: [p] }, "Walk").then(function (d) {
        if (d && d.started === false) say("A walk is already running.");
      });
    });
    cancel.addEventListener("click", function () { post("/api/walk/cancel", {}, "Cancel"); });
    var live = document.getElementById("progress");
    if (live) new MutationObserver(renderBar).observe(live, { childList: true, characterData: true,
      subtree: true });
    return bar;
  }
  var endKey = "", endTimer = null, dismissed = false, END_LINGER_MS = 5000;
  function renderBar() {
    if (!bar) return;
    var p = S.progress, wb = document.getElementById("walkbar");
    document.getElementById("chart-walk").disabled = !selectedFolder();
    var running = p && p.state === "running";
    // a finished/cancelled/failed walk's line clears itself after a few seconds
    var key = p ? p.state + "|" + (p.message || "") : "";
    if (p && !running && key !== endKey) {
      endKey = key; dismissed = false;
      clearTimeout(endTimer);
      endTimer = setTimeout(function () { dismissed = true; renderBar(); },
        PFS.charts.endLingerMs || END_LINGER_MS);
    }
    if (running) { endKey = ""; dismissed = false; clearTimeout(endTimer); }
    wb.hidden = !p || dismissed;
    if (!p) return;
    document.getElementById("walkbar-text").textContent = running
      ? "Walking " + p.done + (p.total ? " / " + p.total : "") + " folders"
      : p.state === "done" ? "Walk finished" : p.state === "cancelled" ? "Walk cancelled"
      : "Walk failed" + (p.message ? ": " + p.message : "");
    var meter = document.getElementById("walkbar-meter");
    meter.hidden = !running;
    if (running && p.total) { meter.max = p.total; meter.value = p.done; }
    else meter.removeAttribute("value");
    document.getElementById("chart-walk-cancel").hidden = !running;
  }

  // ------------------------------------------------------------------ render
  var lastSig = null;
  function sizeKey() {
    var box = document.getElementById(S.tab === "sunburst" ? "chart-sunburst" : "chart-treemap");
    return box ? box.clientWidth + "x" + box.clientHeight : "";
  }
  function restyleSelection() {
    d3.selectAll("#charts g.node").classed("selected", function () {
      return this.getAttribute("data-path") === S.selected && !this.classList.contains("gap");
    });
  }
  function render() {
    ensureBar();
    renderBar();
    var depth = (S.settings && S.settings.maxdepth) || 3;
    // compare content, not identity: every refetch assigns a fresh S.rows array
    // ages change on every refetch but only the tooltip shows them (read fresh there)
    var rowsSig = JSON.stringify(S.rows, function (k, v) {
      return k === "age_seconds" ? undefined : v;
    });
    var sig = [S.tab, S.icicle, S.mode, S.path, depth, sizeKey(), rowsSig].join("|");
    var same = sig === lastSig;
    lastSig = sig;
    if (same) { restyleSelection(); return; }
    hideTip();
    var root = hierarchy(S, depth);
    if (S.tab === "sunburst") {
      PFS.charts.sunburst(document.getElementById("chart-sunburst"), root, S.mode, !!S.icicle);
    } else {
      PFS.charts.treemap(document.getElementById("chart-treemap"), root, S.mode);
    }
  }
  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(render, 120);
  });

  PFS.charts = { render: render, treemap: treemap, sunburst: sunburst, hierarchy: hierarchy };
  if (S.rows && S.rows.length) render();
})();
