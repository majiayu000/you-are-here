
(function () {
  var raw = document.getElementById("yah");
  var DATA = {};
  try { DATA = JSON.parse(raw ? raw.textContent : "{}"); } catch (e) { DATA = {}; }
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var frames = DATA.frames || [];

  function motion() { return reduce ? "auto" : "smooth"; }

  var here = document.getElementById("here");
  if (here && here.parentElement && here.parentElement.scrollWidth > here.parentElement.clientWidth) {
    var list = here.parentElement, lr = list.getBoundingClientRect(), hr = here.getBoundingClientRect();
    var target = hr.left - lr.left + list.scrollLeft - (list.clientWidth - hr.width) / 2;
    list.scrollLeft = Math.max(0, Math.min(target, list.scrollWidth - list.clientWidth));
  }

  var pin = document.getElementById("pin");
  if (here && pin && "IntersectionObserver" in window) {
    new IntersectionObserver(function (entries) {
      pin.hidden = entries[0].isIntersecting;
    }, { threshold: 0.35 }).observe(here);
  }

  function setHist(el, status) {
    if (!status) return;
    el.setAttribute("data-hist", status);
  }
  function applyFrame(frame) {
    if (!frame) return;
    var replay = document.getElementById("replay-context");
    var historical = document.body.getAttribute("data-scrub") === "1";
    if (replay) replay.hidden = !historical;
    var context = document.getElementById("replay-text");
    if (context && historical) {
      context.textContent = "";
      function row(label, value) {
        if (!value) return;
        var p = document.createElement("p");
        p.textContent = (label ? label + ": " : "") + value;
        context.appendChild(p);
      }
      var labels = DATA.labels || {}, step = frame.next_step || {};
      row("", frame.headline);
      row(labels.next_step, step.title);
      row("", step.owner ? labels["owner_" + step.owner] : "");
      row("", step.why);
      row(labels.how, step.action);
      row(labels.unblocks, step.unblocks);
      var nodes = (frame.chain || {}).nodes || [];
      var bn = nodes.filter(function (n) { return n.bottleneck; });
      row(labels.bottleneck, bn.length ? bn.map(function (n) { return n.label; }).join(" / ") : "—");
      var decisions = frame.decisions || [];
      if (!decisions.length) row(labels.decisions, "—");
      decisions.forEach(function (q) {
        row(labels.decisions, q.question + " · " + labels.default + ": " + q.default);
      });
    }
    var cards = document.querySelectorAll(".part[data-link]");
    for (var i = 0; i < cards.length; i++) {
      var p = (frame.parts || {})[cards[i].getAttribute("data-link")];
      setHist(cards[i], p ? p.status : cards[i].getAttribute("data-now"));
    }
    var ms = document.querySelectorAll(".ms[data-name]");
    for (var j = 0; j < ms.length; j++) {
      var name = ms[j].getAttribute("data-name");
      var found = null;
      var list = frame.milestones || [];
      for (var k = 0; k < list.length; k++) if (list[k].name === name) found = list[k];
      var st = found ? found.status : ms[j].getAttribute("data-now");
      ms[j].classList.remove("ms-done", "ms-current", "ms-future", "done", "current", "future", "reached");
      if (st) ms[j].classList.add("ms-" + st, st);
      var marker = ms[j].querySelector(".here");
      if (marker) marker.textContent = DATA.labels.you_are_here;
    }
    var last = -1, active = -1;
    for (var index = 0; index < ms.length; index++) {
      if (ms[index].classList.contains("done")) last = index;
      if (ms[index].classList.contains("current")) active = index;
    }
    if (active < 0 && last >= 0) {
      ms[last].classList.add("reached");
      ms[last].querySelector(".here").textContent = DATA.labels.reached;
    }
    var rail = document.querySelector(".rail");
    if (rail) rail.style.setProperty("--completed", Math.max(0, active < 0 ? last : active) / Math.max(1, ms.length - 1) * 100 + "%");
    var cap = document.getElementById("scrubcap");
    if (cap) cap.textContent = frame.label || "";
    var changes = document.querySelector(".changes-panel ul");
    if (changes && frame.changes && historical) {
      if (!changes.getAttribute("data-saved")) changes.setAttribute("data-saved", changes.innerHTML);
      var html = "";
      for (var c = 0; c < frame.changes.length; c++) {
        html += '<li><i></i>' + frame.changes[c].replace(/[&<>]/g, function (ch) {
          return ch === "&" ? "&amp;" : ch === "<" ? "&lt;" : "&gt;";
        }) + "</li>";
      }
      changes.innerHTML = html;
    }
  }
  function restoreChanges() {
    var changes = document.querySelector(".changes-panel ul");
    if (changes && changes.getAttribute("data-saved")) {
      changes.innerHTML = changes.getAttribute("data-saved");
      changes.removeAttribute("data-saved");
    }
  }
  var scrub = document.getElementById("scrub");
  if (scrub && frames.length >= 2) {
    scrub.addEventListener("input", function () {
      document.body.classList.remove("replaying");
      if (Number(scrub.value) < frames.length - 1) document.body.setAttribute("data-scrub", "1");
      else { document.body.removeAttribute("data-scrub"); restoreChanges(); }
      applyFrame(frames[Number(scrub.value)]);
    });
  }
  // Open on the current snapshot. History is only selected by the reader.

  function clearFocus() {
    document.body.removeAttribute("data-focus");
    var on = document.querySelectorAll(".linkable.on");
    for (var i = 0; i < on.length; i++) on[i].classList.remove("on");
  }
  function focusLink(id, origin) {
    if (!id) return;
    if (document.body.getAttribute("data-focus") === id) { clearFocus(); return; }
    document.body.setAttribute("data-focus", id);
    var all = document.querySelectorAll(".linkable");
    var card = null, node = null;
    for (var i = 0; i < all.length; i++) {
      var hit = all[i].getAttribute("data-link") === id;
      all[i].classList.toggle("on", hit);
      if (hit && all[i].classList.contains("part")) card = all[i];
      if (hit && all[i].classList.contains("chain-node")) node = all[i];
    }
    var dest = origin && origin.classList.contains("part") ? node : card;
    if (card) {
    }
    if (dest && dest.scrollIntoView) dest.scrollIntoView({ block: "nearest", behavior: motion() });
    if (origin && origin.classList.contains("is-bn") && card) {
      var ev = card.querySelector(".ev");
      if (ev) openPeek(ev);
    }
  }

  var peek = document.getElementById("peek");
  function openPeek(btn) {
    if (!peek) return;
    var kind = btn.getAttribute("data-kind");
    var ref = btn.getAttribute("data-ref");
    var db = DATA.peek || {};
    var text = ref;
    var href = "";
    if (kind === "issue") {
      text = (db.issues && db.issues[ref]) || (db.prs && db.prs[ref]) || ("#" + ref);
      if (db.issues && db.issues[ref]) text = "#" + ref + " " + db.issues[ref];
      else if (db.prs && db.prs[ref]) text = "#" + ref + " " + db.prs[ref];
      if (DATA.web) href = DATA.web + "/issues/" + ref;
    } else if (kind === "commit") {
      text = (db.commits && db.commits[ref]) ? (ref + " " + db.commits[ref]) : ref;
      if (DATA.web) href = DATA.web + "/commit/" + ref;
    } else if (kind === "link") {
      text = ref;
      if (/^https?:\/\//.test(ref)) href = ref;
    } else if (kind === "run") {
      text = "CI run " + ref;
      if (DATA.web) href = DATA.web + "/actions/runs/" + ref;
    } else {
      text = ref;
      var files = db.files || {};
      if (files[ref]) text = ref + " — " + files[ref];
      else {
        for (var path in files) {
          if (files.hasOwnProperty(path) && (path.indexOf(ref) === 0 || ref.indexOf(path) === 0)) {
            text = ref + " — " + files[path];
            break;
          }
        }
      }
    }
    if (kind === "file" && ref.charAt(0) === "/") {
      var local = new URL("file:///");
      local.pathname = ref;
      href = local.href;
    }
    document.getElementById("peek-text").textContent = text;
    var link = document.getElementById("peek-link");
    if (href) { link.hidden = false; link.href = href; link.textContent = kind === "file" ? "打开本地证据" : ((DATA.labels && DATA.labels.open) || "Open"); }
    else link.hidden = true;
    peek.hidden = false;
    var box = btn.getBoundingClientRect();
    peek.style.top = Math.max(8, Math.min(box.bottom + 6, window.innerHeight - 110)) + "px";
    peek.style.left = Math.max(8, Math.min(box.left, window.innerWidth - 290)) + "px";
  }

  var partDialog = document.getElementById("part-dialog");
  function openPart(card) {
    var templates = document.querySelectorAll("template[data-detail]");
    for (var i = 0; i < templates.length; i++) {
      if (templates[i].getAttribute("data-detail") !== card.getAttribute("data-link")) continue;
      document.getElementById("part-detail-title").textContent = DATA.labels.part_detail;
      var detail = document.getElementById("part-detail");
      detail.textContent = "";
      detail.appendChild(templates[i].content.cloneNode(true));
      if (peek) { peek.hidden = true; partDialog.appendChild(peek); }
      partDialog.showModal();
      return;
    }
  }
  function openStage(stage) {
    var item = (DATA.milestones || [])[Number(stage.getAttribute("data-stage"))];
    if (!item) return;
    document.getElementById("part-detail-title").textContent = DATA.labels.milestone;
    var detail = document.getElementById("part-detail");
    detail.textContent = "";
    var title = document.createElement("h3"); title.textContent = item.name; detail.appendChild(title);
    var note = document.createElement("p"); note.textContent = item.note || ""; detail.appendChild(note);
    if (item.date) { var date = document.createElement("p"); date.textContent = item.date; detail.appendChild(date); }
    partDialog.showModal();
  }
  document.getElementById("close-part").addEventListener("click", function () { partDialog.close(); });
  partDialog.addEventListener("click", function (ev) {
    if (ev.target !== partDialog) return;
    var box = partDialog.getBoundingClientRect();
    if (ev.clientX < box.left || ev.clientX > box.right || ev.clientY < box.top || ev.clientY > box.bottom) partDialog.close();
  });
  partDialog.addEventListener("close", function () {
    if (peek) { peek.hidden = true; document.body.appendChild(peek); }
  });

  function copyText(text, li) {
    function ok() { if (li) li.classList.add("copied-on"); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(ok, function () { fallback(); });
    } else fallback();
    function fallback() {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); ok(); } catch (e) {}
      ta.parentNode.removeChild(ta);
    }
  }

  function tick() {
    var nodes = document.querySelectorAll("time[data-deadline]");
    var now = Date.now();
    for (var i = 0; i < nodes.length; i++) {
      var t = Date.parse(nodes[i].getAttribute("data-deadline"));
      var count = nodes[i].parentNode.querySelector(".count");
      var li = nodes[i].closest("li");
      if (!t || !count) continue;
      var delta = t - now;
      if (delta <= 0) {
        count.textContent = (DATA.labels && DATA.labels.expired) || "";
        if (li) li.classList.add("expired");
      } else {
        var mins = Math.round(delta / 60000);
        var h = Math.floor(mins / 60);
        var m = mins % 60;
        count.textContent = (h ? h + "h " : "") + m + "m ";
      }
    }
  }
  tick();
  setInterval(tick, 30000);

  document.body.addEventListener("click", function (ev) {
    var stage = ev.target.closest ? ev.target.closest("[data-stage]") : null;
    if (stage) { openStage(stage); return; }
    var arrow = ev.target.closest ? ev.target.closest(".arrow") : null;
    if (arrow) {
      ev.stopPropagation();
      var open = document.querySelectorAll(".arrow.open");
      for (var i = 0; i < open.length; i++) if (open[i] !== arrow) open[i].classList.remove("open");
      arrow.classList.toggle("open");
      return;
    }
    var evidence = ev.target.closest ? ev.target.closest(".ev") : null;
    if (evidence) { ev.stopPropagation(); openPeek(evidence); return; }
    var filter = ev.target.closest ? ev.target.closest("button[data-filter]") : null;
    if (filter) {
      var name = filter.getAttribute("data-filter");
      if (document.body.getAttribute("data-filter") === name) name = "all";
      var buttons = document.querySelectorAll("button[data-filter]");
      for (var f = 0; f < buttons.length; f++) buttons[f].setAttribute("aria-pressed", buttons[f] === filter && name !== "all" ? "true" : "false");
      if (name === "all") document.body.removeAttribute("data-filter");
      else document.body.setAttribute("data-filter", name);
      return;
    }
    var accept = ev.target.closest ? ev.target.closest(".accept") : null;
    if (accept) {
      ev.stopPropagation();
      copyText(accept.getAttribute("data-copy") || "", accept.closest("li"));
      return;
    }
    var over = ev.target.closest ? ev.target.closest(".override") : null;
    if (over && over.tagName === "BUTTON") {
      ev.stopPropagation();
      var form = over.parentNode.parentNode.querySelector(".override-form");
      if (form) form.hidden = !form.hidden;
      return;
    }
    var alt = ev.target.closest ? ev.target.closest(".altpath") : null;
    if (alt) {
      ev.stopPropagation();
      var id = alt.getAttribute("data-fork");
      if (document.body.getAttribute("data-fork") === id) {
        document.body.removeAttribute("data-fork");
        alt.textContent = alt.getAttribute("data-show");
      } else {
        document.body.setAttribute("data-fork", id);
        alt.textContent = alt.getAttribute("data-hide");
      }
      return;
    }
    if (peek && !peek.hidden && !(ev.target.closest && ev.target.closest("#peek"))) peek.hidden = true;
    var link = ev.target.closest ? ev.target.closest("[data-link]") : null;
    if (link && link.classList.contains("part")) openPart(link);
    else if (link) focusLink(link.getAttribute("data-link"), link);
  });

  document.body.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form.classList || !form.classList.contains("override-form")) return;
    ev.preventDefault();
    var input = form.querySelector("input");
    var text = input ? input.value.replace(/^\s+|\s+$/g, "") : "";
    if (!text) return;
    var li = form.closest("li");
    var q = li ? (li.querySelector(".q").textContent || "") : "";
    var id = li ? li.getAttribute("data-decision") : "";
    var tpl = DATA.answer_tpl || "Question: {Q}\nAnswer: {A}";
    copyText(tpl.split("{Q}").join(q).split("{A}").join(text.replace(/"/g, "'")).split("{ID}").join(id), li);
  });

  document.body.addEventListener("keydown", function (ev) {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    var stage = ev.target.closest ? ev.target.closest("[data-stage]") : null;
    if (stage) { ev.preventDefault(); openStage(stage); return; }
    var link = ev.target.closest ? ev.target.closest("[data-link]") : null;
    if (!link || ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA" || ev.target.tagName === "BUTTON") return;
    ev.preventDefault();
    if (link.classList.contains("part")) openPart(link);
    else focusLink(link.getAttribute("data-link"), link);
  });
})();
