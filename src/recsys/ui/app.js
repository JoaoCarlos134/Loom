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
    detailBody: document.getElementById("detail-body"),
    notice: document.getElementById("notice"),
    tabs: Array.prototype.slice.call(document.querySelectorAll(".view-tab"))
  };

  var state = {
    view: "browse", q: "", domain: "", offset: 0, total: 0,
    loading: false, done: false, seq: 0
  };

  // Your ratings, by uid, so a card can show its score without a request per
  // card. Kept in step by every write below rather than refetched.
  var myRatings = Object.create(null);

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

  function fmtScore(n) {
    // Whole scores are what the buttons produce; keep them whole.
    return n % 1 === 0 ? String(n) : n.toFixed(1);
  }

  function api(path, options) {
    var opts = options || {};
    var init = { method: opts.method || "GET", headers: { Accept: "application/json" } };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (r) {
      if (r.ok) return r.status === 204 ? null : r.json();
      // The API explains itself in `detail` -- "run scripts/embed.py", "rate
      // something first" -- and those are the messages worth showing.
      return r.json().catch(function () { return {}; }).then(function (body) {
        var err = new Error(body.detail || ("HTTP " + r.status + " for " + path));
        err.status = r.status;
        throw err;
      });
    });
  }

  // ---- rendering --------------------------------------------------------

  function makeCard(item, nSeries) {
    var card = el("button", "card");
    card.type = "button";
    card.dataset.uid = item.item_uid;
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
    paintMine(card, myRatings[item.item_uid]);

    var body = el("div", "card-body");
    body.appendChild(el("div", "card-title", item.title));

    var meta = el("div", "card-meta");
    var bits = [];
    if (item.year) bits.push(String(item.year));
    bits.push(item.domain);
    // A card standing for a whole book series says so; its volumes are one
    // click away in the detail view rather than eight cards on the shelf.
    if (nSeries > 1) bits.push(nSeries + " volumes");
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

  /* Deliberately a second badge rather than replacing the catalog's score:
   * what everyone thinks and what you think are different facts. */
  function paintMine(card, score) {
    var thumb = card.querySelector(".thumb");
    var badge = thumb.querySelector(".rating-mine");
    if (typeof score !== "number") {
      if (badge) badge.remove();
      return;
    }
    if (badge) badge.textContent = "you " + fmtScore(score);
    else thumb.appendChild(el("span", "rating rating-mine", "you " + fmtScore(score)));
  }

  // A write happens in the dialog, over a grid that is still on screen behind
  // it. Repainting the one card beats reloading the page under the user.
  function repaintCard(uid, score) {
    var card = els.grid.querySelector('[data-uid="' + CSS.escape(uid) + '"]');
    if (card) paintMine(card, score);
  }

  function renderPage(rows, append) {
    if (!append) els.grid.replaceChildren();
    var frag = document.createDocumentFragment();
    rows.forEach(function (row) { frag.appendChild(makeCard(row.item, row.nSeries)); });
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
    if (state.domain) p.set("domain", state.domain);

    if (state.view === "recommend") {
      // One shot, not a page: the profile ranks the whole catalog at once and
      // the tail of a taste ranking is not worth scrolling into.
      p.set("k", "40");
      return "/api/recommend?" + p.toString();
    }
    p.set("limit", String(PAGE_SIZE));
    p.set("offset", String(state.offset));
    if (state.view === "library") return "/api/library?" + p.toString();
    if (state.q) p.set("q", state.q);
    return "/api/items?" + p.toString();
  }

  /* The three views answer with three shapes. Normalising here keeps one
   * rendering path: rows of { item, rating? }, a total, and whether more of
   * them exist. */
  function normalise(body) {
    if (state.view === "recommend") {
      return {
        rows: body.neighbours.map(function (n) { return { item: n.item }; }),
        total: body.neighbours.length,
        more: false,
        notice: recommendNotice(body)
      };
    }
    if (state.view === "library") {
      body.items.forEach(function (r) { myRatings[r.item.item_uid] = r.rating; });
      return {
        rows: body.items.map(function (r) { return { item: r.item, rating: r.rating }; }),
        total: body.total,
        more: true,
        notice: ""
      };
    }
    var sizes = body.series_sizes || {};
    return {
      rows: body.items.map(function (i) { return { item: i, nSeries: sizes[i.item_uid] }; }),
      total: body.total,
      more: true,
      notice: ""
    };
  }

  function recommendNotice(body) {
    var text = "From " + fmtNumber(body.n_rated) + " rating" +
      (body.n_rated === 1 ? "" : "s");
    // The gap is the caveat on the whole page: a profile built from three of
    // your ninety ratings is not the profile ninety suggests. Worded without
    // naming a cause, because n_used folds two together -- ratings the matrix
    // could not place, and ratings below your own average.
    if (body.n_used !== body.n_rated) {
      text += "  ·  " + fmtNumber(body.n_used) + " shaped this";
    }
    if (body.n_suppressed) {
      // Adaptations, sequels and series volumes of what you rated: the same
      // stories, which are the nearest neighbours and the least useful "next".
      text += "  ·  " + fmtNumber(body.n_suppressed) + " of the same stories hidden";
    }
    return text;
  }

  function showEmpty(title, body) {
    els.grid.replaceChildren();
    els.count.textContent = "";
    els.notice.hidden = true;
    els.empty.hidden = false;
    els.empty.querySelector(".empty-title").textContent = title;
    els.empty.querySelector(".empty-body").textContent = body;
  }

  function load(append) {
    if (state.loading || (append && state.done)) return;
    state.loading = true;
    els.loading.hidden = false;

    // Guards against an older in-flight request overwriting a newer query.
    var seq = ++state.seq;

    api(buildUrl())
      .then(function (body) {
        if (seq !== state.seq) return;
        var page = normalise(body);
        state.total = page.total;
        state.offset += page.rows.length;
        state.done = !page.more || state.offset >= page.total || page.rows.length === 0;

        renderPage(page.rows, append);
        renderCount();
        els.notice.textContent = page.notice;
        els.notice.hidden = !page.notice;
        els.empty.hidden = page.total !== 0;
        if (page.total === 0) emptyForView();
      })
      .catch(function (err) {
        if (seq !== state.seq) return;
        // 409 and 503 on /api/recommend are not failures, they are the app
        // telling you what is missing -- rate something, or build the matrix.
        if (err.status === 409 || err.status === 503) {
          showEmpty("Nothing to recommend yet", String(err.message));
        } else {
          showEmpty("Could not reach the API", String(err.message || err));
        }
      })
      .finally(function () {
        if (seq === state.seq) {
          state.loading = false;
          els.loading.hidden = true;
        }
      });
  }

  function emptyForView() {
    if (state.view === "library") {
      showEmpty("Nothing rated yet",
        "Open anything in Browse and give it a score. Your ratings are what " +
        "For you is built from.");
    } else if (state.view === "browse") {
      showEmpty("Nothing here yet",
        "Run uv run python scripts/bootstrap.py to populate the catalog, or " +
        "adjust your search.");
    }
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

  /* The write path, and the only control in the app that changes anything.
   * Ten buttons rather than a slider or stars: the scale is 1-10 because that
   * is what every ingester rescales `mean_rating` onto and what MAL's ratings
   * use, and a control that can only produce whole numbers on that scale
   * cannot quietly invent a 7.3. */
  function makeRatingControl(uid, current) {
    var wrap = el("div", "rate");
    wrap.appendChild(el("span", "rate-label", "Your rating"));

    var row = el("div", "rate-buttons");
    var buttons = [];

    function paint(score) {
      buttons.forEach(function (b, i) {
        b.classList.toggle("is-set", typeof score === "number" && i + 1 <= score);
      });
      clear.hidden = typeof score !== "number";
    }

    function send(score) {
      var request = score === null
        ? api("/api/ratings/" + encodeURIComponent(uid), { method: "DELETE" })
        : api("/api/ratings/" + encodeURIComponent(uid), {
            method: "PUT", body: { rating: score }
          });
      row.classList.add("is-busy");
      return request.then(function (rated) {
        // The API resolves a rating onto the row representing the work, which
        // may not be the uid opened -- so paint what came back, not what was
        // sent, and key the cache by the same uid the listing uses.
        if (rated) {
          myRatings[rated.item.item_uid] = rated.rating;
          myRatings[uid] = rated.rating;
          paint(rated.rating);
          repaintCard(uid, rated.rating);
          repaintCard(rated.item.item_uid, rated.rating);
        } else {
          delete myRatings[uid];
          paint(null);
          repaintCard(uid, null);
        }
        // Browse is a catalog listing and a rating does not change it, so only
        // its badge is repainted. The other two views *are* lists of what you
        // rated, so their membership just changed.
        if (state.view === "library" || state.view === "recommend") reload();
      }).catch(function (err) {
        wrap.appendChild(el("p", "rate-error", String(err.message || err)));
      }).finally(function () {
        row.classList.remove("is-busy");
      });
    }

    for (var i = 1; i <= 10; i++) {
      (function (score) {
        var b = el("button", "rate-btn", String(score));
        b.type = "button";
        b.setAttribute("aria-label", "Rate " + score + " out of 10");
        b.addEventListener("click", function () { send(score); });
        buttons.push(b);
        row.appendChild(b);
      })(i);
    }
    wrap.appendChild(row);

    var clear = el("button", "rate-clear", "Clear");
    clear.type = "button";
    clear.hidden = true;
    clear.addEventListener("click", function () { send(null); });
    wrap.appendChild(clear);

    paint(current);
    return wrap;
  }

  function openDetail(uid) {
    // 404 from /api/ratings means "not rated", which is an answer rather than
    // an error -- so it resolves to null instead of rejecting the dialog.
    var rating = api("/api/ratings/" + encodeURIComponent(uid))
      .then(function (r) { return r.rating; })
      .catch(function () { return null; });

    Promise.all([api("/api/items/" + encodeURIComponent(uid)), rating])
      .then(function (both) {
      var item = both[0];
      var mine = both[1];
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

      main.appendChild(makeRatingControl(item.item_uid, mine));

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

      var series = el("section", "series");
      main.appendChild(series);
      loadSeries(series, item.item_uid);

      inner.appendChild(main);
      els.detailBody.replaceChildren(inner);
      els.detail.showModal();
    });
  }

  /* The other volumes of a book series, which listings fold behind one card.
   * 404 means "in no series" -- the common case -- and leaves nothing behind. */
  function loadSeries(section, uid) {
    api("/api/series/" + encodeURIComponent(uid)).then(function (body) {
      if (!body.items || body.items.length < 2) return;
      section.appendChild(el("h3", "row-title",
        "In this series" + (body.series ? " · " + body.series : "") +
        " (" + body.items.length + ")"));
      var list = el("ol", "series-list");
      body.items.forEach(function (vol) {
        var li = el("li");
        var b = el("button", "linkish", vol.title);
        b.type = "button";
        if (vol.item_uid === uid) b.classList.add("is-current");
        b.addEventListener("click", function () { openDetail(vol.item_uid); });
        li.appendChild(b);
        list.appendChild(li);
      });
      section.appendChild(list);
    }).catch(function (err) {
      // 404 is the usual answer -- most works are in no series -- and shows
      // nothing. Anything else is worth a line rather than silence.
      if (err.status !== 404) {
        section.appendChild(el("p", "rate-error", "Series unavailable: " + err.message));
      }
    });
  }

  // ---- wiring -----------------------------------------------------------

  function setView(view) {
    if (state.view === view) return;
    state.view = view;
    els.tabs.forEach(function (t) {
      t.classList.toggle("is-active", t.dataset.view === view);
    });
    // Search is a catalog query; the library and the profile are not searchable
    // and pretending otherwise would silently ignore what was typed.
    els.search.disabled = view !== "browse";
    els.search.placeholder = view === "browse"
      ? "Search titles and descriptions"
      : "Search is for Browse";
    reload();
  }

  els.tabs.forEach(function (tab) {
    tab.addEventListener("click", function () { setView(tab.dataset.view); });
  });

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
      if (entries[0].isIntersecting && state.view !== "recommend") load(true);
    }, { rootMargin: "400px" }).observe(els.sentinel);
  }

  /* Your scores, so a Browse card can say "you 8" without a request per card.
   * Every page of the library, not the first: the badge is most useful on
   * things you rated long ago and have half-forgotten, which are exactly the
   * ones a single first page would miss. All local, and it does not block the
   * first render -- a badge that appears a moment late is better than a grid
   * that waits for one. */
  function primeRatings(offset) {
    return api("/api/library?limit=200&offset=" + (offset || 0)).then(function (page) {
      page.items.forEach(function (r) { myRatings[r.item.item_uid] = r.rating; });
      var seen = (offset || 0) + page.items.length;
      if (page.items.length && seen < page.total) return primeRatings(seen);
      // Cards already on screen were built before the scores arrived.
      if (state.view === "browse" && seen) reload();
    }).catch(function () { /* badges are decoration; failure is not fatal */ });
  }

  load(false);
  loadStats();
  primeRatings(0);
})();
