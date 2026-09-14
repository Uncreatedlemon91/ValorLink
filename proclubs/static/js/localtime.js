// Times in the viewer's own timezone, and event times entered in the
// author's.
//
// Every time on this site is stored and rendered server-side as UTC, which
// is correct but means everyone outside UTC converts in their head. The
// server emits <time datetime="...Z" data-localtime="FORMAT">UTC text</time>
// (see app._localtime); this rewrites the text into the reader's zone. If
// this file never runs, the UTC text stands -- correct, and labelled UTC.
//
// No dependencies, matches charts.js/countdown.js/countup.js.
(function () {
  // Locale-aware on purpose: hour cycle, month order and separators all
  // follow the reader's own conventions rather than the site's. The zone
  // abbreviation rides along on anything with a clock in it, so a time is
  // never ambiguous about which zone it has been converted into.
  var FORMATS = {
    full: { weekday: 'long', month: 'short', day: 'numeric', year: 'numeric',
            hour: 'numeric', minute: '2-digit', timeZoneName: 'short' },
    short: { weekday: 'long', month: 'short', day: 'numeric',
             hour: 'numeric', minute: '2-digit', timeZoneName: 'short' },
    date: { weekday: 'long', month: 'short', day: 'numeric', year: 'numeric' },
    daymonth: { weekday: 'short', month: 'short', day: 'numeric' },
    time: { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' },
    month: { month: 'short' },
    day: { day: 'numeric' },
  };

  function render(el) {
    var iso = el.getAttribute('datetime');
    if (!iso) return;
    var when = new Date(iso);
    if (isNaN(when.getTime())) return;   // leave the server's text alone
    var options = FORMATS[el.dataset.localtime] || FORMATS.full;
    try {
      el.textContent = new Intl.DateTimeFormat(undefined, options).format(when);
    } catch (e) {
      return;  // ancient browser: the UTC fallback is still correct
    }
    // The full moment on hover, whichever short format is displayed.
    if (!el.title) {
      try {
        el.title = new Intl.DateTimeFormat(undefined, FORMATS.full).format(when);
      } catch (e) { /* no title is fine */ }
    }
  }

  document.querySelectorAll('time[data-localtime]').forEach(render);

  // --- The event form -----------------------------------------------------
  // <input type="datetime-local"> has no timezone: it hands the server the
  // wall-clock digits and nothing else. So the author types their own local
  // time and a hidden field carries the offset for THAT instant -- not for
  // today, which would be an hour out for a fixture booked across a
  // daylight-saving boundary.
  var input = document.getElementById('scheduled_at');
  var offset = document.getElementById('tz_offset');
  if (!input || !offset) return;

  // Editing an existing event: the server rendered the stored UTC into the
  // input, so shift it into the author's zone before they see it.
  var storedUtc = input.dataset.utc;
  if (storedUtc) {
    var stored = new Date(storedUtc);
    if (!isNaN(stored.getTime())) {
      var local = new Date(stored.getTime() - stored.getTimezoneOffset() * 60000);
      input.value = local.toISOString().slice(0, 16);
    }
  }

  var zoneLabel = document.getElementById('tz-label');
  function syncOffset() {
    if (!input.value) return;
    var picked = new Date(input.value);
    if (isNaN(picked.getTime())) return;
    offset.value = String(picked.getTimezoneOffset());
    if (zoneLabel) {
      try {
        zoneLabel.textContent = 'Saving as ' + new Intl.DateTimeFormat(undefined, {
          timeZoneName: 'short', hour: 'numeric', minute: '2-digit',
          weekday: 'short', month: 'short', day: 'numeric',
        }).format(picked) + '.';
      } catch (e) { /* the static hint still explains the field */ }
    }
  }

  syncOffset();
  input.addEventListener('change', syncOffset);
  input.addEventListener('input', syncOffset);
  // Belt and braces: if the field was never touched, the submit handler
  // still stamps an offset, so a save can't silently fall back to UTC.
  if (input.form) input.form.addEventListener('submit', syncOffset);
})();
