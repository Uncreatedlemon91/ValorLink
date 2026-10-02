// The team-sheet editor: shows beside each player where they're picked,
// and flags anybody picked twice before the form is submitted (the server
// refuses it either way; this just says so sooner).
(function () {
  var form = document.querySelector("[data-teamsheet]");
  if (!form) return;
  var selects = Array.prototype.slice.call(form.querySelectorAll("select[data-slot]"));

  function refresh() {
    var picked = {};
    selects.forEach(function (s) {
      if (!s.value) return;
      var label = s.getAttribute("aria-label");
      (picked[s.value] = picked[s.value] || []).push(label);
    });
    form.querySelectorAll("[data-picked-for]").forEach(function (el) {
      var where = picked[el.getAttribute("data-picked-for")];
      el.textContent = where ? where.join(" + ") : "";
      el.classList.toggle("is-twice", !!(where && where.length > 1));
    });
    selects.forEach(function (s) {
      var twice = s.value && picked[s.value].length > 1;
      s.classList.toggle("is-twice", !!twice);
      s.closest("label").classList.toggle("is-filled", !!s.value);
    });
  }

  selects.forEach(function (s) { s.addEventListener("change", refresh); });
  refresh();
})();
