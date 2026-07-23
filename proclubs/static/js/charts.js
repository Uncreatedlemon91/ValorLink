// Small dependency-free SVG chart helpers. No external libraries -- this
// stays a fully local, offline-renderable page (only the API calls need
// network). Colors are read as CSS custom properties (--series-N,
// --status-*) so charts follow the page's palette in one place.

const Charts = (() => {
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function svgEl(tag, attrs = {}) {
    const e = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  }

  function tooltipEl() {
    let tip = document.getElementById('chart-tooltip');
    if (!tip) {
      tip = document.createElement('div');
      tip.id = 'chart-tooltip';
      tip.className = 'chart-tooltip hidden';
      document.body.appendChild(tip);
    }
    return tip;
  }

  // rows: [{label, value, color}] -- value is the strong/lead element,
  // label follows (see dataviz interaction spec: "values lead, labels follow").
  function showTooltip(clientX, clientY, rows) {
    const tip = tooltipEl();
    tip.innerHTML = '';
    rows.forEach(({ label, value, color }) => {
      const row = document.createElement('div');
      row.className = 'tt-row';
      const key = document.createElement('span');
      key.className = 'tt-key';
      key.style.background = color;
      const val = document.createElement('strong');
      val.textContent = value;
      const name = document.createElement('span');
      name.className = 'tt-label';
      name.textContent = label;
      row.append(key, val, name);
      tip.appendChild(row);
    });
    tip.classList.remove('hidden');
    const pad = 14;
    let x = clientX + pad;
    let y = clientY + pad;
    const rect = tip.getBoundingClientRect();
    if (x + rect.width > window.innerWidth) x = clientX - rect.width - pad;
    if (y + rect.height > window.innerHeight) y = clientY - rect.height - pad;
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  }

  function hideTooltip() {
    tooltipEl().classList.add('hidden');
  }

  function attachHover(mark, rowsFn) {
    mark.addEventListener('pointermove', (e) => showTooltip(e.clientX, e.clientY, rowsFn()));
    mark.addEventListener('pointerenter', () => mark.classList.add('chart-mark-hover'));
    mark.addEventListener('pointerleave', () => {
      mark.classList.remove('chart-mark-hover');
      hideTooltip();
    });
  }

  function emptyState(container, message = 'No data available.') {
    container.innerHTML = `<p class="chart-empty">${message}</p>`;
  }

  function legend(container, items) {
    const box = document.createElement('div');
    box.className = 'chart-legend';
    items.forEach(({ label, color }) => {
      const item = document.createElement('span');
      item.className = 'legend-item';
      const swatch = document.createElement('i');
      swatch.style.background = color;
      const text = document.createElement('span');
      text.textContent = label;
      item.append(swatch, text);
      box.appendChild(item);
    });
    container.appendChild(box);
  }

  // Horizontal bar chart -- for ranked lists with text labels (top scorers,
  // top assists). Single hue: "1-3 series, color alone is comfortable".
  function hBarChart(container, { data, color, unit = '' }) {
    container.innerHTML = '';
    if (!data.length) return emptyState(container);

    const max = Math.max(...data.map((d) => d.value), 1);
    const rowH = 22;
    const gap = 8;
    const labelW = 92;
    const chartW = 260;
    const valueW = 40;
    const width = labelW + chartW + valueW;
    const height = data.length * (rowH + gap);

    const svg = svgEl('svg', { viewBox: `0 0 ${width} ${height}`, width: '100%', height });

    data.forEach((d, i) => {
      const y = i * (rowH + gap);
      const barW = Math.max((d.value / max) * chartW, 3);

      const label = svgEl('text', {
        x: labelW - 8,
        y: y + rowH / 2 + 4,
        'text-anchor': 'end',
        class: 'chart-label',
      });
      label.textContent = d.label;

      const rect = svgEl('rect', {
        x: labelW,
        y,
        width: barW,
        height: rowH - 4,
        rx: 4,
        class: 'chart-bar',
      });
      rect.style.fill = color;

      const value = svgEl('text', {
        x: labelW + barW + 6,
        y: y + rowH / 2 + 4,
        class: 'chart-value',
      });
      value.textContent = `${d.value}${unit}`;

      attachHover(rect, () => [{ label: d.label, value: `${d.value}${unit}`, color }]);

      svg.append(label, rect, value);
    });

    container.appendChild(svg);
  }

  // Vertical column chart -- for a handful of directly comparable totals
  // (goals for/against, promotions/relegations).
  function vBarChart(container, { data, unit = '' }) {
    container.innerHTML = '';
    if (!data.length) return emptyState(container);

    const max = Math.max(...data.map((d) => d.value), 1);
    const barW = 56;
    const gap = 36;
    const chartH = 140;
    const width = data.length * (barW + gap) + gap;
    const height = chartH + 40;

    const svg = svgEl('svg', { viewBox: `0 0 ${width} ${height}`, width: '100%', height });
    svg.appendChild(
      svgEl('line', { x1: 0, y1: chartH, x2: width, y2: chartH, class: 'chart-baseline' })
    );

    data.forEach((d, i) => {
      const x = gap + i * (barW + gap);
      const h = Math.max((d.value / max) * (chartH - 20), 2);
      const y = chartH - h;

      const rect = svgEl('rect', { x, y, width: barW, height: h, rx: 4, class: 'chart-bar' });
      rect.style.fill = d.color;

      const label = svgEl('text', {
        x: x + barW / 2,
        y: chartH + 20,
        'text-anchor': 'middle',
        class: 'chart-label',
      });
      label.textContent = d.label;

      const value = svgEl('text', {
        x: x + barW / 2,
        y: y - 8,
        'text-anchor': 'middle',
        class: 'chart-value',
      });
      value.textContent = `${d.value}${unit}`;

      attachHover(rect, () => [{ label: d.label, value: `${d.value}${unit}`, color: d.color }]);

      svg.append(rect, label, value);
    });

    container.appendChild(svg);
  }

  // Diverging bar chart -- goal differential per match, centered on a zero
  // baseline. blue = positive, red = negative (the palette's diverging pair).
  function divergingBarChart(container, { data, unit = '', positiveColor, negativeColor }) {
    container.innerHTML = '';
    if (!data.length) return emptyState(container);

    const max = Math.max(...data.map((d) => Math.abs(d.value)), 1);
    const barW = 18;
    const gap = 10;
    const chartH = 130;
    const midY = chartH / 2;
    const width = data.length * (barW + gap) + gap;

    const svg = svgEl('svg', { viewBox: `0 0 ${width} ${chartH}`, width: '100%', height: chartH });
    svg.appendChild(svgEl('line', { x1: 0, y1: midY, x2: width, y2: midY, class: 'chart-baseline' }));

    data.forEach((d, i) => {
      const x = gap + i * (barW + gap);
      const positive = d.value >= 0;
      const h = Math.max((Math.abs(d.value) / max) * (midY - 10), 2);
      const y = positive ? midY - h : midY;
      const color = positive ? positiveColor : negativeColor;

      const rect = svgEl('rect', { x, y, width: barW, height: h, rx: 3, class: 'chart-bar' });
      rect.style.fill = color;

      const signed = d.value > 0 ? `+${d.value}` : `${d.value}`;
      attachHover(rect, () => [{ label: d.label, value: `${signed}${unit}`, color }]);

      svg.appendChild(rect);
    });

    container.appendChild(svg);
  }

  // Donut chart -- part-to-whole for a small number of categories (position
  // mix, win/draw/loss share). 2px surface gap between segments.
  function donutChart(container, { data, showLegend = true }) {
    container.innerHTML = '';
    const total = data.reduce((s, d) => s + d.value, 0);
    if (!total) return emptyState(container);

    const size = 150;
    const r = 56;
    const stroke = 22;
    const cx = size / 2;
    const cy = size / 2;
    const circumference = 2 * Math.PI * r;

    const svg = svgEl('svg', { viewBox: `0 0 ${size} ${size}`, width: size, height: size });
    const group = svgEl('g', { transform: `rotate(-90 ${cx} ${cy})` });

    let cumulative = 0;
    data.forEach((d) => {
      if (d.value <= 0) return;
      const frac = d.value / total;
      const dash = frac * circumference;
      const visibleDash = Math.max(dash - 2, 0); // 2px surface gap between segments

      const circle = svgEl('circle', {
        cx,
        cy,
        r,
        fill: 'none',
        'stroke-width': stroke,
        'stroke-dasharray': `${visibleDash} ${circumference - visibleDash}`,
        'stroke-dashoffset': -cumulative,
        class: 'chart-donut-seg',
      });
      circle.style.stroke = d.color;

      attachHover(circle, () => [
        { label: d.label, value: `${d.value} (${Math.round(frac * 100)}%)`, color: d.color },
      ]);

      group.appendChild(circle);
      cumulative += dash;
    });

    svg.appendChild(group);
    container.appendChild(svg);

    if (showLegend) {
      legend(
        container,
        data.map((d) => ({ label: `${d.label} (${d.value})`, color: d.color }))
      );
    }
  }

  // Sparkline -- a compact per-point trend (e.g. a player's goals across
  // their last N matches). One hue, small hit targets, no axis.
  function sparkline(container, { values, color, labels, formatValue }) {
    container.innerHTML = '';
    if (!values.length) return emptyState(container, 'No recent match data.');

    const fmt = formatValue || ((v) => `${v} goal${v === 1 ? '' : 's'}`);
    const max = Math.max(...values, 1);
    const barW = 12;
    const gap = 4;
    const height = 44;
    const width = values.length * (barW + gap);

    const svg = svgEl('svg', { viewBox: `0 0 ${width} ${height}`, width: '100%', height });

    values.forEach((v, i) => {
      const x = i * (barW + gap);
      const h = Math.max((v / max) * (height - 6), 2);
      const y = height - h;
      const rect = svgEl('rect', { x, y, width: barW, height: h, rx: 2, class: 'chart-bar' });
      rect.style.fill = color;
      const label = (labels && labels[i]) || `Match ${i + 1}`;
      attachHover(rect, () => [{ label, value: fmt(v), color }]);
      svg.appendChild(rect);
    });

    container.appendChild(svg);
  }

  return { hBarChart, vBarChart, divergingBarChart, donutChart, sparkline, legend, emptyState };
})();
