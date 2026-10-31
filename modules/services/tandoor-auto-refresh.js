/* Homelab patch: auto-refresh an open recipe when it changes on the server.
 *
 * Appended verbatim to the compiled Vue entry chunk. Deliberately plain ES5
 * with no imports and no reliance on Tandoor internals, so a Tandoor upgrade
 * cannot break it by renaming anything.
 *
 * Only the recipe VIEW route (/recipe/<id>) is watched. /edit/Recipe/<id> is a
 * different route, so an in-progress edit is never reloaded out from under you.
 */
(function () {
  var POLL_MS = 30000;
  var SCROLL_KEY = "hl-recipe-scroll";

  function viewedRecipeId() {
    var m = /^\/recipe\/(\d+)\/?$/.exec(window.location.pathname);
    return m ? m[1] : null;
  }

  /* --- restore the scroll position after one of our own reloads ----------
   * The page is an SPA: at load time the document is still short, so a
   * single scrollTo would clamp to 0. Retry until it sticks, give up after
   * a few seconds, and bail out the moment the user scrolls themselves. */
  try {
    var saved = JSON.parse(window.sessionStorage.getItem(SCROLL_KEY) || "null");
    window.sessionStorage.removeItem(SCROLL_KEY);
    if (saved && saved.path === window.location.pathname && saved.y > 0) {
      var deadline = Date.now() + 5000;
      var timer = null;
      var giveUp = ["wheel", "touchstart", "keydown"];
      var stop = function () {
        window.clearInterval(timer);
        giveUp.forEach(function (e) { window.removeEventListener(e, stop); });
      };
      timer = window.setInterval(function () {
        window.scrollTo(0, saved.y);
        if (Math.abs(window.scrollY - saved.y) < 2 || Date.now() > deadline) stop();
      }, 100);
      giveUp.forEach(function (e) {
        window.addEventListener(e, stop, { once: true, passive: true });
      });
    }
  } catch (e) {
    /* private mode, or no storage quota: scroll restore is a nicety */
  }

  /* --- watch updated_at and reload on a change -------------------------- */
  var baseline = {};   /* recipe id -> the updated_at we are showing */

  function check() {
    var id = viewedRecipeId();
    if (!id || document.hidden) return;
    window
      .fetch("/api/recipe/" + id + "/", {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (d) {
        if (!d || !d.updated_at) return;
        if (!(id in baseline)) {
          baseline[id] = d.updated_at;      /* first sight of this recipe */
          return;
        }
        if (baseline[id] === d.updated_at) return;
        try {
          window.sessionStorage.setItem(
            SCROLL_KEY,
            JSON.stringify({ path: window.location.pathname, y: window.scrollY })
          );
        } catch (e) {}
        window.location.reload();
      })
      .catch(function () {
        /* offline or a transient 5xx: just try again next tick */
      });
  }

  window.setInterval(check, POLL_MS);
  /* Coming back to the tab should not cost a full poll interval. */
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) check();
  });
})();
