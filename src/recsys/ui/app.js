/* Loom UI -- vanilla JS, no build step, no dependencies.
 *
 * All DOM insertion goes through textContent / createElement. Catalog text is
 * third-party data (synopses, titles) and must never be interpolated as HTML.
 */

(function () {
  "use strict";

  var PAGE_SIZES = [20, 40, 80, 120, 200];
  var PREFS_KEY = "loom.prefs";

  // Sort orders per view. The empty value is "whatever the API defaults to",
  // which for Browse depends on whether you are searching -- so its label does
  // too, and is filled in by syncSort().
  var SORTS = {
    browse: [
      ["", ""],
      ["rating", "Highest rated"],
      ["newest", "Newest first"],
      ["oldest", "Oldest first"],
      ["title", "Title A–Z"]
    ],
    library: [
      ["", "Recently rated"],
      ["rating", "My rating"],
      ["title", "Title A–Z"]
    ]
  };

  var els = {
    search: document.getElementById("search"),
    domains: Array.prototype.slice.call(document.querySelectorAll("#domains .chip")),
    toolbar: document.getElementById("toolbar"),
    sort: document.getElementById("sort"),
    tagChips: document.getElementById("tag-chips"),
    tagInput: document.getElementById("tag-input"),
    tagOptions: document.getElementById("tag-options"),
    tagSuggest: document.getElementById("tag-suggest"),
    yearMin: document.getElementById("year-min"),
    yearMax: document.getElementById("year-max"),
    minRating: document.getElementById("min-rating"),
    sameFranchise: document.getElementById("same-franchise"),
    pageSize: document.getElementById("page-size"),
    clearFilters: document.getElementById("clear-filters"),
    emptyClear: document.getElementById("empty-clear"),
    grid: document.getElementById("grid"),
    empty: document.getElementById("empty"),
    count: document.getElementById("result-count"),
    stats: document.getElementById("stats"),
    loading: document.getElementById("loading"),
    sentinel: document.getElementById("sentinel"),
    toTop: document.getElementById("to-top"),
    detail: document.getElementById("detail"),
    detailBody: document.getElementById("detail-body"),
    notice: document.getElementById("notice"),
    tabs: Array.prototype.slice.call(document.querySelectorAll(".view-tab"))
  };

  var prefs = loadPrefs();

  var state = {
    view: "browse", q: "", domains: [],
    sort: { browse: "", library: "" },
    tags: [], yearMin: null, yearMax: null, minRating: "",
    pageSize: prefs.pageSize, sameFranchise: prefs.sameFranchise,
    offset: 0, total: 0, loading: false, done: false, seq: 0
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

  /* Only what is a matter of taste rather than of the question being asked:
   * how many to show, and whether you want sequels back. Filters are not
   * remembered -- reopening the app onto last week's "romance, 1990-1995" and
   * wondering where the catalog went is worse than choosing them again. */
  function loadPrefs() {
    var out = { pageSize: 40, sameFranchise: false };
    try {
      var saved = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
      if (PAGE_SIZES.indexOf(saved.pageSize) !== -1) out.pageSize = saved.pageSize;
      out.sameFranchise = saved.sameFranchise === true;
    } catch (e) { /* storage unavailable or unreadable: defaults are fine */ }
    return out;
  }

  function savePrefs() {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({
        pageSize: state.pageSize, sameFranchise: state.sameFranchise
      }));
    } catch (e) { /* a preference that does not stick is not an error */ }
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
        var detail = typeof body.detail === "string" ? body.detail : "";
        var err = new Error(detail || ("HTTP " + r.status + " for " + path));
        err.status = r.status;
        throw err;
      });
    });
  }

  // ---- rendering --------------------------------------------------------

  /* Stands in for a cover the catalog has none of. Coloured from the title so
   * a shelf of them is told apart at a glance, and the same work is the same
   * colour every time -- a grid of identical grey tiles reads as broken. */
  function makePlaceholder(item) {
    var hash = 0;
    for (var i = 0; i < item.title.length; i++) {
      hash = (hash * 31 + item.title.charCodeAt(i)) % 360;
    }
    var words = item.title.split(/\s+/).filter(Boolean);
    var initials = words.slice(0, 2).map(function (w) {
      return Array.from(w)[0] || "";
    }).join("").toUpperCase();

    var box = el("div", "placeholder");
    box.style.setProperty("--hue", String(hash));
    box.appendChild(el("span", "placeholder-initials", initials));
    box.appendChild(el("span", "placeholder-kind", item.domain));
    return box;
  }

  function makeCover(item, className) {
    if (!item.image_url) return makePlaceholder(item);
    var img = el("img", className);
    img.loading = "lazy";
    img.decoding = "async";
    img.alt = "";
    img.src = item.image_url;
    img.addEventListener("error", function () {
      img.replaceWith(makePlaceholder(item));
    });
    return img;
  }

  function makeCard(item, nSeries, onOpen) {
    var card = el("button", "card");
    card.type = "button";
    card.dataset.uid = item.item_uid;
    card.title = item.title;
    card.setAttribute("aria-label", item.title);

    var thumb = el("div", "thumb");
    thumb.appendChild(makeCover(item));
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
      item.tags.slice(0, 3).forEach(function (t) {
        var chip = el("span", "tag", t);
        chip.dataset.tag = t;
        chip.title = "Browse everything tagged " + t;
        tags.appendChild(chip);
      });
      body.appendChild(tags);
    }

    card.appendChild(body);
    card.addEventListener("click", function (e) {
      // A card is one button, so its tags cannot be buttons of their own; the
      // click is sorted out here instead.
      var chip = e.target.closest(".tag");
      if (chip) browseTag(chip.dataset.tag);
      else (onOpen || openDetail)(item.item_uid);
    });
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

  // ---- filters ----------------------------------------------------------

  function hasFilters() {
    return Boolean(state.q || state.tags.length || state.yearMin !== null ||
      state.yearMax !== null || state.minRating || state.sort.browse);
  }

  function renderTagChips() {
    els.tagChips.replaceChildren();
    state.tags.forEach(function (tag) {
      var chip = el("button", "chip is-on", tag + " ×");
      chip.type = "button";
      chip.setAttribute("aria-label", "Remove tag " + tag);
      chip.addEventListener("click", function () { toggleTag(tag); });
      els.tagChips.appendChild(chip);
    });
    Array.prototype.forEach.call(els.tagSuggest.querySelectorAll(".chip"), function (chip) {
      chip.classList.toggle("is-on", state.tags.indexOf(chip.dataset.tag) !== -1);
    });
  }

  function toggleTag(tag) {
    tag = String(tag || "").trim().toLowerCase();
    if (!tag) return;
    var at = state.tags.indexOf(tag);
    if (at === -1) state.tags.push(tag);
    else state.tags.splice(at, 1);
    renderTagChips();
    reload();
  }

  // From a card or the detail view: a tag is a question about the catalog, so
  // it is always answered in Browse, wherever it was clicked.
  function browseTag(tag) {
    tag = String(tag || "").trim().toLowerCase();
    if (!tag) return;
    if (els.detail.open) els.detail.close();
    if (state.tags.indexOf(tag) === -1) state.tags.push(tag);
    renderTagChips();
    if (state.view === "browse") reload();
    else setView("browse");
    window.scrollTo(0, 0);
  }

  /* The most-carried tags of the current domain, as one-click filters. The
   * vocabulary is tens of thousands of tags wide and nobody can guess it, so
   * the common ones are shown and the rest are a few letters away in the
   * picker. */
  function loadTagSuggestions() {
    var p = new URLSearchParams({ limit: "18" });
    addDomains(p);
    var asked = state.domains.join();
    api("/api/tags?" + p.toString()).then(function (rows) {
      if (asked !== state.domains.join()) return;
      els.tagSuggest.replaceChildren();
      rows.forEach(function (row) {
        var chip = el("button", "chip", row.tag);
        chip.type = "button";
        chip.dataset.tag = row.tag;
        chip.title = fmtNumber(row.n) + " works";
        chip.addEventListener("click", function () { toggleTag(row.tag); });
        els.tagSuggest.appendChild(chip);
      });
      renderTagChips();
    }).catch(function () { /* suggestions are a convenience; typing still works */ });
  }

  var suggestTags = debounce(function (text) {
    var p = new URLSearchParams({ limit: "25", q: text });
    addDomains(p);
    api("/api/tags?" + p.toString()).then(function (rows) {
      els.tagOptions.replaceChildren();
      rows.forEach(function (row) {
        var opt = el("option");
        opt.value = row.tag;
        opt.label = fmtNumber(row.n) + " works";
        els.tagOptions.appendChild(opt);
      });
    }).catch(function () { /* as above */ });
  }, 150);

  function commitTagInput() {
    var value = els.tagInput.value;
    els.tagInput.value = "";
    els.tagOptions.replaceChildren();
    if (value.trim() && state.tags.indexOf(value.trim().toLowerCase()) === -1) toggleTag(value);
  }

  function readYear(input) {
    var n = parseInt(input.value, 10);
    return n >= 1000 && n <= 2100 ? n : null;
  }

  function clearFilters() {
    state.q = "";
    state.tags = [];
    state.yearMin = state.yearMax = null;
    state.minRating = "";
    state.sort.browse = "";
    els.search.value = "";
    els.yearMin.value = els.yearMax.value = "";
    els.minRating.value = "";
    renderTagChips();
    syncSort();
    reload();
  }

  function syncSort() {
    var options = SORTS[state.view];
    if (!options) return;
    els.sort.replaceChildren();
    options.forEach(function (pair) {
      var opt = el("option", null, pair[1] ||
        (state.q ? "Best match" : "Most popular"));
      opt.value = pair[0];
      els.sort.appendChild(opt);
    });
    els.sort.value = state.sort[state.view];
  }

  function syncToolbar() {
    var nodes = document.querySelectorAll("[data-views]");
    Array.prototype.forEach.call(nodes, function (node) {
      node.hidden = node.dataset.views.split(" ").indexOf(state.view) === -1;
    });
    var filtered = state.view === "browse" && hasFilters();
    els.clearFilters.hidden = !filtered;
    els.emptyClear.hidden = !filtered;
  }

  // ---- data loading -----------------------------------------------------

  // None chosen is all of them, so nothing is sent and nothing is filtered.
  function addDomains(params) {
    state.domains.forEach(function (d) { params.append("domain", d); });
  }

  function buildUrl() {
    var p = new URLSearchParams();
    addDomains(p);

    if (state.view === "recommend") {
      // One shot, not a page: the profile ranks the whole catalog at once and
      // the tail of a taste ranking is not worth scrolling into.
      p.set("k", String(state.pageSize));
      if (state.sameFranchise) p.set("same_franchise", "true");
      return "/api/recommend?" + p.toString();
    }
    p.set("limit", String(state.pageSize));
    p.set("offset", String(state.offset));
    if (state.q) p.set("q", state.q);
    if (state.sort[state.view]) p.set("sort", state.sort[state.view]);
    if (state.view === "library") return "/api/library?" + p.toString();

    state.tags.forEach(function (tag) { p.append("tag", tag); });
    if (state.yearMin !== null) p.set("year_min", String(state.yearMin));
    if (state.yearMax !== null) p.set("year_max", String(state.yearMax));
    if (state.minRating) p.set("min_rating", state.minRating);
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
    // Only a *next page* waits its turn. A new question -- another view, a
    // changed filter -- always goes out, and `seq` discards whatever answer
    // to the old one is still on its way.
    if (append && (state.loading || state.done)) return;
    state.loading = true;
    fillViewport = false;
    els.loading.hidden = false;

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
        fillViewport = !state.done;
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
        if (seq !== state.seq) return;
        state.loading = false;
        els.loading.hidden = true;
        // The observer below only speaks when the sentinel *crosses* into
        // view. On a tall window a page of twenty never pushes it out, so it
        // never crosses back and the second page was never asked for. If the
        // page just drawn has not filled the screen, ask now.
        if (fillViewport && sentinelInReach()) load(true);
      });
  }

  var fillViewport = false;

  function sentinelInReach() {
    return state.view !== "recommend" &&
      els.sentinel.getBoundingClientRect().top < window.innerHeight + 400;
  }

  function emptyForView() {
    if (state.view === "library") {
      if (state.q || state.domains.length) {
        showEmpty("Nothing in your library matches", "Try another search or domain.");
      } else {
        showEmpty("Nothing rated yet",
          "Open anything in Browse and give it a score. Your ratings are what " +
          "For you is built from.");
      }
    } else if (state.view === "browse") {
      if (hasFilters() || state.domains.length) {
        showEmpty("Nothing matches these filters",
          "Every tag has to be carried and every bound met. Loosen one, or start over.");
      } else {
        showEmpty("Nothing here yet",
          "Run uv run python scripts/bootstrap.py to populate the catalog.");
      }
    } else {
      showEmpty("Nothing to recommend", "Nothing in this domain is near what you have rated.");
    }
  }

  function reload() {
    state.offset = 0;
    state.done = false;
    syncToolbar();
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

  // Opening something from "More like this" keeps where you came from, so the
  // dialog can go back instead of dropping you out to the grid.
  var detailTrail = [];

  function detailRow(dl, label, value) {
    if (value === null || value === undefined || value === "") return;
    dl.appendChild(el("dt", null, label));
    var dd = el("dd");
    if (value instanceof Node) dd.appendChild(value);
    else dd.textContent = String(value);
    dl.appendChild(dd);
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

  /* A long-running show credits eighty people. The first few are the ones a
   * reader is looking for; the rest are one click away rather than a wall
   * between the synopsis and everything under it. */
  function makeCreators(creators) {
    var LIMIT = 8;
    var names = creators.map(function (c) {
      return c.role ? c.name + " (" + c.role + ")" : c.name;
    });
    var box = el("span");
    var text = el("span", null, names.slice(0, LIMIT).join(", "));
    box.appendChild(text);
    if (names.length > LIMIT) {
      var more = el("button", "link-btn", "+ " + (names.length - LIMIT) + " more");
      more.type = "button";
      more.addEventListener("click", function () {
        text.textContent = names.join(", ");
        more.remove();
      });
      box.appendChild(document.createTextNode(" "));
      box.appendChild(more);
    }
    return box;
  }

  function makeTagList(tags) {
    var box = el("span", "chips");
    tags.forEach(function (t) {
      var chip = el("button", "chip", t);
      chip.type = "button";
      chip.title = "Browse everything tagged " + t;
      chip.addEventListener("click", function () { browseTag(t); });
      box.appendChild(chip);
    });
    return box;
  }

  /* Whatever a source filed under external_ids, shown without knowing which
   * source it was: a value that is a URL becomes a link named by its host,
   * anything else is listed as an id. Adding a source adds rows here and
   * changes nothing in this file. */
  function addExternal(dl, ext) {
    var links = el("span", "ext-links");
    var ids = [];
    Object.keys(ext).forEach(function (k) {
      var v = ext[k];
      if (/^https?:\/\//.test(v)) {
        var a = el("a");
        try { a.textContent = new URL(v).hostname.replace(/^www\./, ""); }
        catch (e) { a.textContent = v; }
        a.href = v;
        a.title = k.replace(/_/g, " ");
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        links.appendChild(a);
      } else {
        ids.push(k.replace(/_id$/, "").replace(/_/g, " ") + " " + v);
      }
    });
    if (links.childElementCount) detailRow(dl, "Links", links);
    if (ids.length) detailRow(dl, "IDs", ids.join("  ·  "));
  }

  function makeSimilar(uid) {
    var wrap = el("section", "similar");
    var head = el("div", "similar-head");
    head.appendChild(el("h3", null, "More like this"));

    var tools = el("div", "similar-tools");

    /* Off, this is "what else might I like". On, it is "what else is there of
     * this": the sequel, the film, the manga it came from -- which are the
     * nearest neighbours of any work and so arrive first. One switch, shared
     * with For you, because it is one preference. */
    var same = el("label", "field-check");
    same.title = "Sequels, prequels, spin-offs and source material of this";
    var sameBox = el("input", "switch");
    sameBox.type = "checkbox";
    sameBox.checked = state.sameFranchise;
    same.appendChild(sameBox);
    same.appendChild(el("span", null, "Same franchise"));
    tools.appendChild(same);

    // The same domains the top bar offers, so a new one appears here too.
    var pick = el("select");
    pick.setAttribute("aria-label", "Medium to look in");
    var any = el("option", null, "Any medium");
    any.value = "";
    pick.appendChild(any);
    els.domains.forEach(function (chip) {
      var copy = el("option", null, chip.textContent);
      copy.value = chip.dataset.domain;
      pick.appendChild(copy);
    });
    tools.appendChild(pick);
    head.appendChild(tools);
    wrap.appendChild(head);

    var note = el("p", "similar-note", "Loading…");
    var grid = el("div", "similar-grid");
    wrap.appendChild(note);
    wrap.appendChild(grid);

    function run() {
      var p = new URLSearchParams({ k: "12" });
      if (pick.value) p.set("domain", pick.value);
      if (sameBox.checked) p.set("same_franchise", "true");
      note.textContent = "Loading…";
      api("/api/similar/" + encodeURIComponent(uid) + "?" + p.toString())
        .then(function (body) {
          grid.replaceChildren();
          body.neighbours.forEach(function (n) {
            grid.appendChild(makeCard(n.item, 0, function (next) {
              detailTrail.push(uid);
              openDetail(next);
            }));
          });
          note.textContent = body.neighbours.length
            ? (body.n_suppressed
                ? fmtNumber(body.n_suppressed) + " from the same franchise hidden" : "")
            : "Nothing close enough to suggest.";
        })
        .catch(function (err) {
          // "Run scripts/embed.py" is the API's own wording and the useful one.
          grid.replaceChildren();
          note.textContent = String(err.message || err);
        });
    }
    pick.addEventListener("change", run);
    sameBox.addEventListener("change", function () {
      state.sameFranchise = els.sameFranchise.checked = sameBox.checked;
      savePrefs();
      run();
      if (state.view === "recommend") reload();
    });
    run();
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

      var poster = el("div", "detail-poster");
      poster.appendChild(makeCover(item));
      inner.appendChild(poster);

      var main = el("div", "detail-main");
      if (detailTrail.length) {
        var back = el("button", "link-btn detail-back", "← Back");
        back.type = "button";
        back.addEventListener("click", function () { openDetail(detailTrail.pop()); });
        main.appendChild(back);
      }
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
        detailRow(dl, "Creators", makeCreators(item.creators));
      }
      if (item.tags && item.tags.length) detailRow(dl, "Tags", makeTagList(item.tags));
      if (item.alt_titles && item.alt_titles.length) {
        detailRow(dl, "Also known as", item.alt_titles.slice(0, 6).join(" · "));
      }
      addExternal(dl, item.external_ids || {});
      detailRow(dl, "UID", item.item_uid);
      main.appendChild(dl);

      var series = el("section", "series");
      main.appendChild(series);
      loadSeries(series, item.item_uid);

      inner.appendChild(main);
      inner.appendChild(makeSimilar(item.item_uid));
      // Who-rated-what, beside the content model above: it fails
      // differently, and only answers where its corpus has ratings.
      var cf = el("section", "more");
      addNeighbourRow(cf, "People who liked this also liked",
        "/api/similar/" + encodeURIComponent(item.item_uid) + "?k=12&method=cf");
      inner.appendChild(cf);
      els.detailBody.replaceChildren(inner);
      if (!els.detail.open) els.detail.showModal();
      els.detail.scrollTop = 0;
    }).catch(function (err) {
      // Never leave a click with no response at all.
      els.detailBody.replaceChildren(
        el("p", "detail-error", "Could not open this: " + String(err.message || err)));
      if (!els.detail.open) els.detail.showModal();
    });
  }

  /* A compact card for a row of neighbours. Opening one replaces the dialog's
   * content, so a reader can walk from work to work. */
  function makeMini(item) {
    var b = el("button", "mini");
    b.type = "button";
    b.setAttribute("aria-label", item.title);
    var thumb = el("div", "mini-thumb");
    thumb.appendChild(makeCover(item));
    b.appendChild(thumb);
    b.appendChild(el("div", "mini-title", item.title));
    b.appendChild(el("div", "mini-meta", item.year ? item.domain + " · " + item.year : item.domain));
    b.addEventListener("click", function () { openDetail(item.item_uid); });
    return b;
  }

  function addNeighbourRow(section, title, path) {
    var wrap = el("div", "neighbours");
    section.appendChild(wrap);
    api(path).then(function (body) {
      if (!body.neighbours.length) {
        return;
      }
      wrap.appendChild(el("h3", "row-title", title));
      var row = el("div", "mini-row");
      body.neighbours.forEach(function (n) {
        row.appendChild(makeMini(n.item));
      });
      wrap.appendChild(row);
    }).catch(function (err) {
      // A missing CF model (503) is the normal state of a catalog
      // without a ratings corpus; the content row above already
      // explains any failure of its own. Nothing to say here.
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
    // For you is ranked against a profile, not matched against words, and
    // pretending otherwise would silently ignore what was typed.
    els.search.disabled = view === "recommend";
    els.search.placeholder = view === "browse" ? "Search titles and descriptions"
      : view === "library" ? "Search what you have rated"
      : "Search is for Browse and Library";
    syncSort();
    reload();
  }

  els.tabs.forEach(function (tab) {
    tab.addEventListener("click", function () { setView(tab.dataset.view); });
  });

  els.search.addEventListener("input", debounce(function (e) {
    state.q = e.target.value.trim();
    syncSort();  // the default order's name depends on whether this is a search
    reload();
  }, 220));

  els.domains.forEach(function (chip) {
    chip.addEventListener("click", function () {
      var on = chip.getAttribute("aria-pressed") !== "true";
      chip.setAttribute("aria-pressed", String(on));
      chip.classList.toggle("is-on", on);
      state.domains = els.domains
        .filter(function (c) { return c.getAttribute("aria-pressed") === "true"; })
        .map(function (c) { return c.dataset.domain; });
      loadTagSuggestions();
      reload();
    });
  });

  els.sort.addEventListener("change", function () {
    state.sort[state.view] = els.sort.value;
    reload();
  });

  els.tagInput.addEventListener("input", function (e) {
    // Picking a suggestion arrives as a replacement rather than as typing;
    // that is a choice made, so it is taken without waiting for Enter.
    if (e.inputType === "insertReplacementText" || e.inputType === undefined) {
      commitTagInput();
    } else {
      suggestTags(els.tagInput.value.trim());
    }
  });
  els.tagInput.addEventListener("focus", function () {
    if (!els.tagOptions.childElementCount) suggestTags(els.tagInput.value.trim());
  });
  els.tagInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      commitTagInput();
    } else if (e.key === "Backspace" && !els.tagInput.value && state.tags.length) {
      toggleTag(state.tags[state.tags.length - 1]);
    }
  });

  var yearChanged = debounce(function () {
    state.yearMin = readYear(els.yearMin);
    state.yearMax = readYear(els.yearMax);
    reload();
  }, 350);
  els.yearMin.addEventListener("input", yearChanged);
  els.yearMax.addEventListener("input", yearChanged);

  els.minRating.addEventListener("change", function () {
    state.minRating = els.minRating.value;
    reload();
  });

  els.sameFranchise.checked = state.sameFranchise;
  els.sameFranchise.addEventListener("change", function () {
    state.sameFranchise = els.sameFranchise.checked;
    savePrefs();
    reload();
  });

  PAGE_SIZES.forEach(function (n) {
    var opt = el("option", null, n + " per page");
    opt.value = String(n);
    els.pageSize.appendChild(opt);
  });
  els.pageSize.value = String(state.pageSize);
  els.pageSize.addEventListener("change", function () {
    state.pageSize = parseInt(els.pageSize.value, 10);
    savePrefs();
    reload();
  });

  els.clearFilters.addEventListener("click", clearFilters);
  els.emptyClear.addEventListener("click", clearFilters);

  els.detail.addEventListener("click", function (e) {
    // Click on the backdrop (outside the dialog box) closes it.
    if (e.target === els.detail || e.target.hasAttribute("data-close")) els.detail.close();
  });
  els.detail.addEventListener("close", function () { detailTrail = []; });

  document.addEventListener("keydown", function (e) {
    // "/" jumps to search, as it does nearly everywhere -- unless you are
    // already typing somewhere, where it is just a slash.
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (e.key === "/" && !typing && !els.detail.open && !els.search.disabled) {
      e.preventDefault();
      els.search.focus();
      els.search.select();
    }
  });

  window.addEventListener("scroll", function () {
    els.toTop.hidden = window.scrollY < 900;
  }, { passive: true });
  els.toTop.addEventListener("click", function () {
    window.scrollTo({ top: 0, behavior: "smooth" });
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
      Object.keys(myRatings).forEach(function (uid) { repaintCard(uid, myRatings[uid]); });
    }).catch(function () { /* badges are decoration; failure is not fatal */ });
  }

  syncSort();
  syncToolbar();
  load(false);
  loadStats();
  loadTagSuggestions();
  primeRatings(0);
})();
