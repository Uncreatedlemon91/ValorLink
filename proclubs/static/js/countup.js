// Two small scroll-triggered effects, both harmless no-ops on a page with
// neither: [data-countup] numbers animate from 0 to their target value, and
// [data-reveal] elements fade/slide into place, the first time each scrolls
// into view. No dependencies, matches charts.js/countdown.js.
(function () {
  if (!('IntersectionObserver' in window)) {
    document.querySelectorAll('[data-countup]').forEach((el) => {
      el.textContent = el.dataset.countup;
    });
    document.querySelectorAll('[data-reveal]').forEach((el) => {
      el.classList.add('is-visible');
    });
    return;
  }

  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function countUp(el) {
    const target = Number(el.dataset.countup);
    if (!Number.isFinite(target)) {
      el.textContent = el.dataset.countup;
      return;
    }
    if (reduceMotion) {
      el.textContent = target;
      return;
    }
    const duration = 900;
    const start = performance.now();
    function tick(now) {
      const progress = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
      el.textContent = Math.round(target * eased);
      if (progress < 1) requestAnimationFrame(tick);
      else el.textContent = target;
    }
    requestAnimationFrame(tick);
  }

  const countObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      countObserver.unobserve(entry.target);
      countUp(entry.target);
    });
  }, { threshold: 0.4 });
  document.querySelectorAll('[data-countup]').forEach((el) => countObserver.observe(el));

  const revealObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      revealObserver.unobserve(entry.target);
      entry.target.classList.add('is-visible');
    });
  }, { threshold: 0.2 });
  document.querySelectorAll('[data-reveal]').forEach((el) => revealObserver.observe(el));
})();
