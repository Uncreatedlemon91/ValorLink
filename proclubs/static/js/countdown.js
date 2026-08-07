// Ticks the match-center countdown on the home page. Harmless no-op on any
// page without a [data-countdown] element.
document.querySelectorAll('[data-countdown]').forEach((el) => {
  const target = new Date(el.dataset.countdown).getTime();
  const dEl = el.querySelector('.cd-d');
  const hEl = el.querySelector('.cd-h');
  const mEl = el.querySelector('.cd-m');
  const sEl = el.querySelector('.cd-s');

  function pad(n) {
    return String(n).padStart(2, '0');
  }

  function tick() {
    const diff = Math.max(0, target - Date.now());
    dEl.textContent = pad(Math.floor(diff / 86400000));
    hEl.textContent = pad(Math.floor((diff % 86400000) / 3600000));
    mEl.textContent = pad(Math.floor((diff % 3600000) / 60000));
    sEl.textContent = pad(Math.floor((diff % 60000) / 1000));
  }

  tick();
  setInterval(tick, 1000);
});
