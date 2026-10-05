// The page shell: the sidebar's collapse toggle (remembered per browser)
// and the UTC clock in the top bar -- every fixture time on the site is
// stored in UTC, so the clock is the reference people check against.
(function () {
  var KEY = "yfc-nav-collapsed";
  var root = document.documentElement;
  var toggle = document.querySelector("[data-nav-toggle]");

  function apply(collapsed) {
    root.classList.toggle("nav-collapsed", collapsed);
    if (toggle) {
      toggle.setAttribute("aria-pressed", collapsed ? "true" : "false");
      toggle.setAttribute("aria-label", collapsed ? "Expand the sidebar" : "Collapse the sidebar");
    }
  }

  var saved = false;
  try { saved = localStorage.getItem(KEY) === "1"; } catch (e) { /* storage blocked */ }
  apply(saved);

  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = !root.classList.contains("nav-collapsed");
      apply(next);
      try { localStorage.setItem(KEY, next ? "1" : "0"); } catch (e) { /* storage blocked */ }
    });
  }

  var clock = document.querySelector("[data-utc-clock] .clock-text");
  if (clock) {
    var tick = function () {
      var now = new Date();
      var hh = String(now.getUTCHours()).padStart(2, "0");
      var mm = String(now.getUTCMinutes()).padStart(2, "0");
      clock.textContent = hh + ":" + mm + " UTC";
    };
    tick();
    setInterval(tick, 15000);
  }
})();
