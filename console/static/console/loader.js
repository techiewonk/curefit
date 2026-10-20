/* The loader: a bar at the top as soon as something loads, a "Loading…" overlay when it takes more than
   half a second, and the pressed button marked busy (no double clicks). Shown for page changes, form posts and
   HTMX requests a person triggered; the automatic refreshes (job panel, live monitor) stay quiet. */
(function () {
  var OVERLAY_AFTER = 500;   // ms: quick pages never flash the overlay
  var GIVE_UP_AFTER = 20000; // ms: a download or a cancelled navigation must not leave the loader on
  var bar, overlay, overlayTimer, giveUpTimer, busyButton, active = 0;

  function build() {
    if (bar) return;
    bar = document.createElement("div");
    bar.className = "cf-loader-bar";
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-label", "Loading");
    overlay = document.createElement("div");
    overlay.className = "cf-loader-overlay";
    overlay.setAttribute("role", "status");
    overlay.setAttribute("aria-live", "polite");
    overlay.innerHTML = '<div class="cf-loader-box"><span class="cf-loader-spinner"></span><span>Loading…</span></div>';
    document.body.appendChild(bar);
    document.body.appendChild(overlay);
  }

  function show(button, giveUpAfter) {
    build();
    active += 1;
    bar.classList.add("cf-on");
    document.documentElement.classList.add("cf-loading");
    clearTimeout(overlayTimer);
    overlayTimer = setTimeout(function () { overlay.classList.add("cf-on"); }, OVERLAY_AFTER);
    clearTimeout(giveUpTimer);
    giveUpTimer = setTimeout(hideAll, giveUpAfter || GIVE_UP_AFTER);
    if (button && !button.classList.contains("cf-busy")) {
      busyButton = button;
      button.classList.add("cf-busy");
      button.setAttribute("aria-busy", "true");
    }
  }

  function hide() {
    active = Math.max(0, active - 1);
    if (!active) hideAll();
  }

  function hideAll() {
    active = 0;
    clearTimeout(overlayTimer);
    clearTimeout(giveUpTimer);
    if (bar) bar.classList.remove("cf-on");
    if (overlay) overlay.classList.remove("cf-on");
    document.documentElement.classList.remove("cf-loading");
    if (busyButton) {
      busyButton.classList.remove("cf-busy");
      busyButton.removeAttribute("aria-busy");
      busyButton = null;
    }
  }

  function isDownload(url, el) {
    return (el && el.hasAttribute("download")) || /\/csv\/|[?&]format=xlsx|\/export\/|\.csv$|\.xlsx$/i.test(url);
  }

  // A link to another page of the app (not a new tab, not a #section, not a download).
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a || a.target === "_blank" || a.hasAttribute("hx-get") || a.hasAttribute("hx-post")) return;
    var href = a.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#" || /^(javascript|mailto|tel):/i.test(href)) return;
    var url = new URL(a.href, location.href);
    if (url.origin !== location.origin) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
    show(null, isDownload(url.href, a) ? 3000 : GIVE_UP_AFTER);
  }, true);

  // A form that is sent the normal way (HTMX forms are handled below).
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || form.target === "_blank" || form.hasAttribute("hx-post") || form.hasAttribute("hx-get")) return;
    var submitter = e.submitter || null;
    var action = (submitter && submitter.getAttribute("formaction")) || form.action || location.href;
    show(submitter, isDownload(action, form) || /export/i.test(action) ? 4000 : GIVE_UP_AFTER);
  });

  // HTMX: only requests a person caused (a click, a change, a submit); the "every Ns" refreshes have no event.
  document.addEventListener("htmx:beforeRequest", function (e) {
    var cause = e.detail && e.detail.requestConfig && e.detail.requestConfig.triggeringEvent;
    if (!cause || cause.type === "load") return;
    e.detail.elt.__cfLoader = true;
    var el = e.detail.elt;
    show(el.tagName === "BUTTON" ? el : (cause.submitter || null));
  });
  ["htmx:afterRequest", "htmx:sendError", "htmx:timeout", "htmx:responseError"].forEach(function (name) {
    document.addEventListener(name, function (e) {
      if (e.detail && e.detail.elt && e.detail.elt.__cfLoader) {
        e.detail.elt.__cfLoader = false;
        hide();
      }
    });
  });

  // Coming back with the browser's Back button shows the page as it was: no loader left over.
  window.addEventListener("pageshow", hideAll);
  window.cfLoader = { show: show, hide: hide, hideAll: hideAll };
})();
