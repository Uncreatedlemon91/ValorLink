/* Squad Moves picker: filtering and a confirmation that names the person.
 *
 * Everything here is an enhancement. The picker is a radio group and the
 * two buttons are ordinary submits, so with scripting off the page still
 * works -- you just scroll to find somebody and get the browser's own
 * "required" prompt instead of a tailored one. */
(function () {
  var form = document.getElementById('roster-form');
  if (!form) return;

  var search = document.getElementById('roster-search');
  var count = document.getElementById('roster-count');
  var noMatch = document.getElementById('roster-no-match');
  var chips = Array.prototype.slice.call(form.querySelectorAll('.roster-chip'));

  function selected() {
    return form.querySelector('input[name="discord_id"]:checked');
  }

  function selectedName() {
    var input = selected();
    if (!input) return null;
    var name = input.closest('.roster-chip').querySelector('.roster-chip-name');
    return name ? name.textContent.trim() : null;
  }

  if (search) {
    search.addEventListener('input', function () {
      var q = search.value.trim().toLowerCase();
      var shown = 0;
      chips.forEach(function (chip) {
        /* A chip holding the current selection stays visible even when it
         * doesn't match: hiding it would make the form look empty while
         * still submitting that person. */
        var hit = !q || chip.dataset.name.indexOf(q) !== -1
          || chip.querySelector('input').checked;
        chip.hidden = !hit;
        if (hit) shown++;
      });
      if (count) {
        count.textContent = q
          ? shown + ' of ' + chips.length + ' members'
          : chips.length + ' members';
      }
      if (noMatch) noMatch.hidden = shown > 0;
    });
  }

  /* Which button submitted. event.submitter covers current browsers;
   * Safari only got it in 15.4, so the last-pressed button is tracked as
   * a fallback -- without it that Safari would skip the confirm and
   * publish a departure on one click. */
  var lastPressed = null;
  form.addEventListener('click', function (event) {
    var button = event.target.closest('button[type="submit"], button:not([type])');
    if (button && form.contains(button)) lastPressed = button;
  });

  /* One listener on the form rather than one per button, so the confirm
   * fires for keyboard submits too -- and it runs on submit, after the
   * browser's own required-field check, so it can name who was picked. */
  form.addEventListener('submit', function (event) {
    var button = event.submitter || lastPressed;
    var message = button && button.dataset ? button.dataset.confirm : null;
    if (!message) return;
    var name = selectedName();
    if (name) message = message.replace('the selected member', name);
    if (!window.confirm(message)) event.preventDefault();
  });
})();
