/* Loom UI -- vanilla JS, no build step, no dependencies.
 *
 * All DOM insertion goes through textContent / createElement. Catalog text is
 * third-party data (synopses, titles) and must never be interpolated as HTML.
 */

(function () {
  "use strict";

  var PAGE_SIZE = 40;

  var els = {
    search: document.getElementById("search"),
    domain: document.getElementById("domain"),
    grid: document.getElementById("grid"),
    empty: document.getElementById("empty"),
    count: document.getElementById("result-count"),
    stats: document.getElementById("stats"),
    loading: document.getElementById("loading"),
    sentinel: document.getElementById("sentinel"),
    detail: document.getElementById("detail"),
    detailBody: document.getElementById("detail-body")
  };

  var state = { q: "", domain: "", offset: 0, total: 0, loading: false, done: false, seq: 0 };

  // ---- helpers ----------------------------------------------------------

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function debounce(fn, ms) {
    var t;
    return function () {
      var args = arguments;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(null, args); }, ms);
    };
  }

  function fmtNumber(n) {
    return typeof n === "number" ? n.toLocaleString() : "";
  }

  function api(path) {
    return fetch(path, { headers: { Accept: "application/json" } }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status + " for " + path);
      return r.json();
    });
  }

  // ---- rendering --------------------------------------------------------

  function makeCard(item) {
    var card = el("button", "card");
    card.type = "button";
    card.setAttribute("aria-label", item.title);

    var thumb = el("div", "thumb");
    if (item.image_url) {
      var img = el("img");
      img.loading = "lazy";
      img.alt = "";
      img.src = item.image_url;
      img.addEventListener("error", function () {
        thumb.replaceChildren(el("div", "placeholder", "no image"));
      });
      thumb.appendChild(img);
    } else {
      thumb.appendChild(el("div", "placeholder", "no image"));
    }
    if (typeof item.mean_rating === "number") {
      thumb.appendChild(el("span", "rating", item.mean_rating.toFixed(1)));
    }
    card.appendChild(thumb);

    var body = el("div", "card-body");
    body.appendChild(el("div", "card-title", item.title));

    var meta = el("div", "card-meta");
    var bits = [];
    if (item.year) bits.push(String(item.year));
    bits.push(item.domain);
    meta.appendChild(el("span", null, bits.join(" · ")));
    body.appendChild(meta);

    if (item.tags && item.tags.length) {
      var tags = el("div", "tags");
      item.tags.slice(0, 3).forEach(function (t) { tags.appendChild(el("span", "tag", t)); });
      body.appendChild(tags);
    }

    card.appendChild(body);
    card.addEventListener("click", function () { openDetail(item.item_uid); });
    return card;
  }

  function renderPage(items, append) {
    if (!append) els.grid.replaceChildren();
    var frag = document.createDocumentFragment();
    items.forEach(function (item) { frag.appendChild(makeCard(item)); });
    els.grid.appendChild(frag);
  }

  function renderCount() {
    if (state.total === 0) {
      els.count.textContent = "";
      return;
    }
    var shown = els.grid.childElementCount;
    els.count.textContent = fmtNumber(state.total) + " item" + (state.total === 1 ? "" : "s") +
      (shown < state.total ? " · showing " + fmtNumber(shown) : "");
  }

  // ---- data loading -----------------------------------------------------

  function buildUrl() {
    var p = new URLSearchParams();
    if (state.q) p.set("q", state.q);
    if (state.domain) p.set("domain", state.domain);
    p.set("limit", String(PAGE_SIZE));
    p.set("offset", String(state.offset));
    return "/api/items?" + p.toString();
  }

  function load(append) {
    if (state.loading || (append && state.done)) return;
    state.loading = true;
    els.loading.hidden = false;

    // Guards against an older in-flight request overwriting a newer query.
    var seq = ++state.seq;

    api(buildUrl())
      .then(function (page) {
        if (seq !== state.seq) return;
        state.total = page.total;
        state.offset += page.items.length;
        state.done = state.offset >= page.total || page.items.length === 0;

        renderPage(page.items, append);
        renderCount();
        els.empty.hidden = page.total !== 0;
      })
      .catch(function (err) {
        if (seq !== state.seq) return;
        els.grid.replaceChildren();
        els.count.textContent = "";
        els.empty.hidden = false;
        els.empty.querySelector(".empty-title").textContent = "Could not reach the API";
        els.empty.querySelector(".empty-body").textContent = String(err.message || err);
      })
      .finally(function () {
        if (seq === state.seq) {
          state.loading = false;
          els.loading.hidden = true;
        }
      });
  }

  function reload() {
    state.offset = 0;
    state.done = false;
    load(false);
  }

  function loadStats() {
    api("/api/stats").then(function (s) {
      var parts = s.by_domain.map(function (d) { return d.domain + " " + fmtNumber(d.n_items); });
      var text = parts.length ? parts.join("  │  ") : "catalog empty";
      if (s.last_ingest_at) {
        text += "  │  ingested " + new Date(s.last_ingest_at).toLocaleString();
      }
      els.stats.textContent = text;
    }).catch(function () { /* stats are decoration; failure is not fatal */ });
  }

  // ---- detail dialog ----------------------------------------------------

  function detailRow(dl, label, value) {
    if (value === null || value === undefined || value === "") return;
    dl.appendChild(el("dt", null, label));
    dl.appendChild(el("dd", null, value));
  }

  function openDetail(uid) {
    api("/api/items/" + encodeURIComponent(uid)).then(function (item) {
      var inner = el("div", "detail-inner");

      if (item.image_url) {
        var img = el("img", "detail-poster");
        img.src = item.image_url;
        img.alt = "";
        inner.appendChild(img);
      }

      var main = el("div", "detail-main");
      main.appendChild(el("h2", null, item.title));

      var sub = [];
      if (item.year) sub.push(item.year);
      sub.push(item.domain + " · " + item.source);
      if (typeof item.mean_rating === "number") {
        sub.push(item.mean_rating.toFixed(2) + "/10" +
          (item.n_ratings ? " (" + fmtNumber(item.n_ratings) + " ratings)" : ""));
      }
      main.appendChild(el("p", "detail-sub", sub.join("  ·  ")));

      if (item.description) main.appendChild(el("p", "detail-desc", item.description));

      var dl = el("dl", "kv");
      if (item.creators && item.creators.length) {
        detailRow(dl, "Creators", item.creators.map(function (c) {
          return c.role ? c.name + " (" + c.role + ")" : c.name;
        }).join(", "));
      }
      if (item.tags && item.tags.length) detailRow(dl, "Tags", item.tags.join(", "));
      if (item.alt_titles && item.alt_titles.length) {
        detailRow(dl, "Also known as", item.alt_titles.slice(0, 6).join(" · "));
      }
      detailRow(dl, "UID", item.item_uid);

      var ext = item.external_ids || {};
      Object.keys(ext).forEach(function (k) {
        var v = ext[k];
        if (/^https?:\/\//.test(v)) {
          dl.appendChild(el("dt", null, k));
          var dd = el("dd");
          var a = el("a", null, v);
          a.href = v;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          dd.appendChild(a);
          dl.appendChild(dd);
        } else {
          detailRow(dl, k, v);
        }
      });
      main.appendChild(dl);

      inner.appendChild(main);
      els.detailBody.replaceChildren(inner);
      els.detail.showModal();
    });
  }

  // ---- wiring -----------------------------------------------------------

  els.search.addEventListener("input", debounce(function (e) {
    state.q = e.target.value.trim();
    reload();
  }, 220));

  els.domain.addEventListener("change", function (e) {
    state.domain = e.target.value;
    reload();
  });

  els.detail.addEventListener("click", function (e) {
    // Click on the backdrop (outside the dialog box) closes it.
    if (e.target === els.detail || e.target.hasAttribute("data-close")) els.detail.close();
  });

  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (entries) {
      if (entries[0].isIntersecting) load(true);
    }, { rootMargin: "400px" }).observe(els.sentinel);
  }

  load(false);
  loadStats();
})();
