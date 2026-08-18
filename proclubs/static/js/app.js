const statusEl = document.getElementById('status');
const clubView = document.getElementById('club-view');
const clubHeader = document.getElementById('club-header');

let currentClubId = null;
let currentPlatform = null;
let latestMatches = []; // raw match list from the last /matches fetch, shared
                         // with the Players tab so a goalkeeper's detail
                         // panel can aggregate save stats without an extra
                         // API call.

// Filter/sort state for the Players tab's roster table -- kept at module
// scope so a re-render (position chip, search, sort change) doesn't need to
// refetch anything, just re-derive the table from the already-loaded roster.
let playerFilterState = { pos: 'ALL', sort: 'goals', q: '' };

function setStatus(msg, isError = false) {
  statusEl.textContent = msg || '';
  statusEl.style.color = isError ? '#e05a5a' : '';
}

async function api(path) {
  const res = await fetch(path);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.error || `request failed (${res.status})`);
  }
  return body;
}

// --------------------------------------------------------------------------
// Report tabs -- Overview / Players / Matches / Competition. Overview's
// "Explore" cards and cross-links (data-goto) jump straight into one of the
// other three via the same activateTab() the tab bar itself uses.
// --------------------------------------------------------------------------
function activateTab(name) {
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach((p) => p.classList.toggle('active', p.id === `tab-${name}`));
}
function goToTab(name) {
  activateTab(name);
  document.getElementById('tabs').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
document.querySelectorAll('#tabs button').forEach((btn) => {
  btn.addEventListener('click', () => activateTab(btn.dataset.tab));
});

async function loadClub(clubId, platform) {
  currentClubId = clubId;
  currentPlatform = platform;
  clubView.classList.remove('hidden');
  setStatus('Loading club...');

  const [overview, standings, members, matches, historyDivision, historyMatches, historyRivals] =
    await Promise.allSettled([
      api('/api/overview'),
      api('/api/standings'),
      api('/api/members'),
      api('/api/matches?matchType=leagueMatch&count=10'),
      api('/api/history/division'),
      api('/api/history/matches'),
      api('/api/history/rivals'),
    ]);

  latestMatches = matches.status === 'fulfilled' ? matches.value || [] : [];

  renderOverview(overview, standings, members, matches, historyDivision);
  renderPlayers(members);
  renderMatches(matches, 'leagueMatch', 10);
  renderCompetition(standings, historyDivision, historyMatches, historyRivals);
  setStatus('');
}

// Re-fetches just the Matches tab with new matchType/count controls, without
// reloading the other three tabs. Also refreshes latestMatches, so a
// goalkeeper's save breakdown on the Players tab reflects whatever sample is
// currently loaded here.
async function reloadMatches(matchType, count) {
  const panel = document.getElementById('tab-matches');
  panel.style.opacity = '0.5'; // hold the previous render while refetching
  try {
    const data = await api(`/api/matches?matchType=${matchType}&count=${count}`);
    latestMatches = data || [];
    renderMatches({ status: 'fulfilled', value: data }, matchType, count);
  } catch (err) {
    renderMatches({ status: 'rejected', reason: err }, matchType, count);
  }
}

function panelError(panel, result) {
  panel.innerHTML = `<p style="color:#e05a5a">${result.reason.message}</p>`;
}

// A stat tile is: label, value, and optionally a line of context under it.
// Tiles with nothing to show are marked so they recede instead of sitting at
// full strength shouting a dash -- EA simply doesn't report some of these
// until a club has played enough, and a wall of confident "-" reads as broken.
function statCard(label, value, hint) {
  const empty = value == null || value === '-' || value === '';
  return `<div class="stat-card${empty ? ' is-empty' : ''}">
    <div class="label">${label}</div>
    <div class="value">${empty ? '-' : value}</div>
    ${hint ? `<div class="stat-hint">${esc(hint)}</div>` : ''}
  </div>`;
}

// The one number the page leads with. Exactly one per view: if everything is
// emphasised, nothing is. Carries its own sparkline when we have history, so
// the headline figure shows its direction rather than just its level.
function heroStat(label, value, hint, delta) {
  const dir = delta == null || delta === 0 ? '' : (delta > 0 ? ' up' : ' down');
  return `<div class="stat-card stat-hero">
    <div class="label">${label}</div>
    <div class="value">${value ?? '-'}</div>
    <div class="stat-meta">
      ${hint ? `<span class="stat-hint">${esc(hint)}</span>` : ''}
      ${delta != null && delta !== 0
        ? `<span class="stat-delta${dir}">${delta > 0 ? '+' : ''}${delta} since tracking began</span>`
        : ''}
    </div>
  </div>`;
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

// Club/player names come from EA's data (other players' chosen names), not
// from us -- escape before dropping them into a template-string innerHTML.
function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// --- Grouping by day -------------------------------------------------------
// EA gives us readings at whatever cadence we happened to poll or play at,
// so raw per-reading charts are noisy and unevenly spaced. Every trend chart
// here collapses to one point per local calendar day first; they differ only
// in how a day's readings get combined.
function _dayKey(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

// rows -> [{ label, value, count }], one per day with at least one usable
// reading, oldest first. A reading with no usable numeric value is skipped
// rather than counted as 0 -- a missing rating is not a rating of zero, and
// averaging one in would drag the day down.
function groupByDay(rows, { time, value, combine }) {
  const byDay = new Map();
  (rows || []).forEach((row) => {
    const seconds = Number(time(row));
    const raw = value(row);
    if (!Number.isFinite(seconds) || !seconds || raw == null || raw === '') return;
    const n = Number(raw);
    if (!Number.isFinite(n)) return;
    const date = new Date(seconds * 1000);
    const key = _dayKey(date);
    const bucket = byDay.get(key);
    if (bucket) bucket.values.push(n);
    else byDay.set(key, { date, values: [n] });
  });
  return [...byDay.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([, { date, values }]) => ({
      label: date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
      value: combine(values),
      count: values.length,
    }));
}

// The highest reading that day. Club snapshots come from hourly polls (see
// poll.py), so a day holds several readings of the same drifting number and
// the peak is the meaningful daily figure.
function dailyPeak(snapshots, field) {
  return groupByDay(snapshots, {
    time: (s) => s.captured_at,
    value: (s) => s[field],
    combine: (values) => Math.max(...values),
  });
}

// The mean of that day's readings. For per-match player stats the mean is
// the honest daily figure -- a session of four matches is one day's
// performance, and taking the best of them would flatter it.
function dailyAverage(rows, { time, value, decimals = 2 }) {
  return groupByDay(rows, {
    time, value,
    combine: (values) => Number((values.reduce((a, b) => a + b, 0) / values.length).toFixed(decimals)),
  });
}

// --------------------------------------------------------------------------
// Overview -- an executive summary: identity, headline KPIs, the skill-
// rating trend (real tracked history, not a sample), a season result mix,
// a squad spotlight, and cards into the three detail reports.
// --------------------------------------------------------------------------

function matchOutcomeFor(rawMatch, clubId) {
  const us = (rawMatch.clubs || {})[clubId];
  if (!us) return 'D';
  return us.wins === '1' ? 'W' : us.losses === '1' ? 'L' : 'D';
}

function renderOverview(overviewResult, standingsResult, membersResult, matchesResult, historyResult) {
  const panel = document.getElementById('tab-overview');
  if (overviewResult.status !== 'fulfilled') return panelError(panel, overviewResult);
  const { info, stats } = overviewResult.value;
  const name = info?.name || `Club ${currentClubId}`;
  const standings = standingsResult.status === 'fulfilled' ? standingsResult.value : {};

  clubHeader.innerHTML = '';
  const identity = document.createElement('div');
  identity.className = 'club-hero-identity';
  const h2 = document.createElement('h2');
  h2.textContent = name;
  const idSpan = document.createElement('span');
  idSpan.className = 'club-hero-id';
  idSpan.textContent = `ID ${currentClubId}`;
  identity.append(h2, idSpan);
  clubHeader.append(identity);

  if (!stats) {
    panel.innerHTML = '<p>No overall stats available for this club yet.</p>';
    return;
  }

  const badges = document.createElement('div');
  badges.className = 'club-hero-badges';
  const recordBadge = document.createElement('span');
  recordBadge.className = 'hero-badge';
  recordBadge.textContent = `${stats.wins ?? 0}W ${stats.losses ?? 0}L ${stats.ties ?? 0}D`;
  const ratingBadge = document.createElement('span');
  ratingBadge.className = 'hero-badge accent';
  ratingBadge.textContent = `${stats.skillRating ?? '-'} SR`;
  badges.append(recordBadge, ratingBadge);
  if (standings.currentDivision != null) {
    const divBadge = document.createElement('span');
    divBadge.className = 'hero-badge';
    divBadge.textContent = `Division ${standings.currentDivision}`;
    badges.append(divBadge);
  }
  clubHeader.append(badges);

  const recentMatches = matchesResult.status === 'fulfilled' ? (matchesResult.value || []) : [];
  if (recentMatches.length) {
    const strip = document.createElement('div');
    strip.className = 'form-strip';
    strip.setAttribute('aria-label', 'Recent results, oldest to newest');
    [...recentMatches].reverse().forEach((m) => {
      const oc = matchOutcomeFor(m, currentClubId);
      const oppId = Object.keys(m.clubs || {}).find((id) => id !== currentClubId);
      const oppName = m.clubs?.[oppId]?.details?.name ?? 'opponent';
      const chip = document.createElement('span');
      chip.className = `form-chip ${oc}`;
      chip.title = `${oc} vs ${oppName}`;
      chip.textContent = oc;
      strip.appendChild(chip);
    });
    clubHeader.append(strip);
  }

  // Skill-rating movement across everything we've tracked. Deliberately not
  // shown when there's only one snapshot: a delta needs two readings, and
  // "+0" would read as "flat" rather than "we don't know yet".
  const ratingSnapshots = historyResult.status === 'fulfilled' ? (historyResult.value.snapshots || []) : [];
  const ratingSeries = dailyPeak(ratingSnapshots, 'skill_rating');
  const ratingDelta = ratingSeries.length > 1
    ? ratingSeries[ratingSeries.length - 1].value - ratingSeries[0].value
    : null;

  const played = num(stats.wins) + num(stats.losses) + num(stats.ties);
  const winRate = played > 0 ? Math.round((num(stats.wins) / played) * 100) : null;
  const goalDiff = stats.goals != null && stats.goalsAgainst != null ? num(stats.goals) - num(stats.goalsAgainst) : null;

  panel.innerHTML = `
    ${heroStat('Skill Rating', stats.skillRating,
               standings.currentDivision != null ? `Division ${standings.currentDivision}` : null,
               ratingDelta)}
    <div class="stat-grid">
      ${statCard('Points', standings.points, played ? `${played} played` : null)}
      ${statCard('Win Rate', winRate != null ? `${winRate}%` : '-',
                 played ? `${num(stats.wins)}W ${num(stats.ties)}D ${num(stats.losses)}L` : null)}
      ${statCard('Goal Difference', goalDiff != null ? (goalDiff > 0 ? `+${goalDiff}` : goalDiff) : '-',
                 stats.goals != null ? `${num(stats.goals)} for, ${num(stats.goalsAgainst)} against` : null)}
      ${statCard('Win Streak', stats.wstreak)}
      ${statCard('Unbeaten Streak', standings.unbeatenstreak)}
    </div>
    <div class="chart-row">
      <div class="chart-card">
        <h3>Skill Rating Over Time</h3>
        <div id="chart-overview-rating"></div>
        <p class="chart-caption" id="chart-overview-rating-caption"></p>
      </div>
      <div class="chart-card">
        <h3>Result Mix (season)</h3>
        <div id="chart-record"></div>
      </div>
    </div>
    <div class="chart-row">
      <div class="chart-card">
        <h3>Top Scorers</h3>
        <div id="chart-spotlight-scorers"></div>
      </div>
      <div class="chart-card">
        <h3>Top Assists</h3>
        <div id="chart-spotlight-assists"></div>
      </div>
      <div class="chart-card">
        <h3>Man of the Match</h3>
        <div id="chart-spotlight-motm"></div>
      </div>
    </div>
    <p class="page-lede" style="margin-top:2rem; font-weight:600">Explore</p>
    <div class="explore-grid">
      <button class="explore-card" data-goto="players" type="button">
        <h4>Players</h4>
        <p>Every rostered player, filterable by position and sortable by any stat -- click through for a full per-player breakdown.</p>
        <span class="explore-cta">Open report &rarr;</span>
      </button>
      <button class="explore-card" data-goto="matches" type="button">
        <h4>Matches</h4>
        <p>Every tracked result with shots, pass accuracy, and tackle trends -- expand any match for a full team-vs-team comparison.</p>
        <span class="explore-cta">Open report &rarr;</span>
      </button>
      <button class="explore-card" data-goto="competition" type="button">
        <h4>Competition</h4>
        <p>Division standing and promotion history, plus a full head-to-head record against every club we've faced.</p>
        <span class="explore-cta">Open report &rarr;</span>
      </button>
    </div>
  `;

  panel.querySelectorAll('[data-goto]').forEach((btn) => btn.addEventListener('click', () => goToTab(btn.dataset.goto)));

  const ratingEl = document.getElementById('chart-overview-rating');
  const ratingCaptionEl = document.getElementById('chart-overview-rating-caption');
  const snapshots = historyResult.status === 'fulfilled' ? (historyResult.value.snapshots || []) : [];
  if (snapshots.length) {
    Charts.areaChart(ratingEl, {
      data: dailyPeak(snapshots, 'skill_rating'),
      color: 'var(--series-1)',
    });
    ratingCaptionEl.textContent = 'One point per day (highest reading that day), oldest → most recent.';
  } else {
    Charts.emptyState(ratingEl, 'Not enough tracked snapshots yet -- check back after the next poll.');
  }

  Charts.donutChart(document.getElementById('chart-record'), {
    data: [
      { label: 'Wins', value: num(stats.wins), color: 'var(--status-good)' },
      { label: 'Ties', value: num(stats.ties), color: 'var(--status-neutral)' },
      { label: 'Losses', value: num(stats.losses), color: 'var(--status-critical)' },
    ],
  });

  const members = membersResult.status === 'fulfilled' ? (membersResult.value.members || []) : [];
  const nameOf = (m) => m.proName || m.name || 'Unknown';
  // One hue across all three: each is a single series, and the titles say
  // what's plotted. Three different hues would imply the color meant
  // something it doesn't.
  Charts.hBarChart(document.getElementById('chart-spotlight-scorers'), {
    data: [...members].sort((a, b) => num(b.goals) - num(a.goals)).slice(0, 5).map((m) => ({ label: nameOf(m), value: num(m.goals) })),
    color: 'var(--series-1)',
  });
  Charts.hBarChart(document.getElementById('chart-spotlight-assists'), {
    data: [...members].sort((a, b) => num(b.assists) - num(a.assists)).slice(0, 5).map((m) => ({ label: nameOf(m), value: num(m.assists) })),
    color: 'var(--series-1)',
  });
  Charts.hBarChart(document.getElementById('chart-spotlight-motm'), {
    data: [...members].sort((a, b) => num(b.manOfTheMatch) - num(a.manOfTheMatch)).slice(0, 5).map((m) => ({ label: nameOf(m), value: num(m.manOfTheMatch) })),
    color: 'var(--series-1)',
  });
}

// --------------------------------------------------------------------------
// Players (formerly "Members") -- the full roster: filter by position,
// search by name, sort by any column, click a row for the full breakdown.
// --------------------------------------------------------------------------

// One entry per sortable column. `key` pulls the comparable value; `text`
// marks the columns that sort alphabetically (and so default to ascending --
// A-Z is the useful direction for a name, while 20 goals is the useful
// direction for goals).
const PLAYER_COLUMNS = [
  { id: 'name', label: 'Name', text: true, key: (m) => (m.proName || m.name || '') },
  { id: 'position', label: 'Position', text: true, key: (m) => (m.favoritePosition || m.proPos || '') },
  { id: 'gp', label: 'GP', key: (m) => num(m.gamesPlayed) },
  { id: 'goals', label: 'Goals', key: (m) => num(m.goals) },
  { id: 'assists', label: 'Assists', key: (m) => num(m.assists) },
  { id: 'rating', label: 'Avg Rating', key: (m) => num(m.ratingAve) },
  { id: 'motm', label: 'MOTM', key: (m) => num(m.manOfTheMatch) },
  { id: 'careerGoals', label: 'Career Goals', key: (m) => num(m.careerGoals) },
];

function playerSorter(sortId, dir) {
  const col = PLAYER_COLUMNS.find((c) => c.id === sortId) || PLAYER_COLUMNS[3];
  const sign = dir === 'asc' ? 1 : -1;
  return (a, b) => {
    const av = col.key(a);
    const bv = col.key(b);
    if (col.text) return sign * String(av).localeCompare(String(bv));
    return sign * (av - bv);
  };
}

function renderPlayers(result) {
  const panel = document.getElementById('tab-players');
  if (result.status !== 'fulfilled') return panelError(panel, result);
  const members = result.value.members || [];
  const positionCount = result.value.positionCount || {};
  if (!members.length) {
    panel.innerHTML = '<p>No member stats available.</p>';
    return;
  }

  playerFilterState = { pos: 'ALL', sort: 'goals', dir: 'desc', q: '' };

  panel.innerHTML = `
    <div class="chart-row">
      <div class="chart-card">
        <h3>Position Mix</h3>
        <div id="chart-positions" class="chart-compact"></div>
      </div>
    </div>
    <div class="filter-row">
      <div class="chip-group" id="pos-filter">
        <button class="chip active" data-pos="ALL" type="button">All</button>
        <button class="chip" data-pos="goalkeeper" type="button">GK</button>
        <button class="chip" data-pos="defender" type="button">DEF</button>
        <button class="chip" data-pos="midfielder" type="button">MID</button>
        <button class="chip" data-pos="forward" type="button">FWD</button>
      </div>
      <input id="member-filter" type="text" placeholder="Filter roster by name..." style="min-width:200px" />
    </div>
    <p class="chart-caption">
      The name filter searches this club's roster only -- EA's API has no way to look up a player
      across clubs, only within a club you already have loaded. Click a player for their full stat breakdown.
    </p>
    <table>
      <thead>
        <tr id="players-head">
          ${PLAYER_COLUMNS.map((c) => `
            <th class="sortable" data-sort="${c.id}" tabindex="0" role="columnheader"
                aria-sort="none" title="Sort by ${c.label}">
              <span>${c.label}</span><span class="sort-caret" aria-hidden="true"></span>
            </th>`).join('')}
        </tr>
      </thead>
      <tbody id="players-body"></tbody>
    </table>
  `;

  document.getElementById('pos-filter').addEventListener('click', (e) => {
    const btn = e.target.closest('.chip');
    if (!btn) return;
    document.querySelectorAll('#pos-filter .chip').forEach((c) => c.classList.remove('active'));
    btn.classList.add('active');
    playerFilterState.pos = btn.dataset.pos;
    renderPlayersTable(members);
  });
  document.getElementById('member-filter').addEventListener('input', (e) => {
    playerFilterState.q = e.target.value.trim().toLowerCase();
    renderPlayersTable(members);
  });
  const sortBy = (id) => {
    if (playerFilterState.sort === id) {
      playerFilterState.dir = playerFilterState.dir === 'asc' ? 'desc' : 'asc';
    } else {
      const col = PLAYER_COLUMNS.find((c) => c.id === id);
      playerFilterState.sort = id;
      // Start each column in its useful direction rather than always
      // descending: names read A-Z, counts read biggest-first.
      playerFilterState.dir = col && col.text ? 'asc' : 'desc';
    }
    renderPlayersTable(members);
  };
  document.getElementById('players-head').addEventListener('click', (e) => {
    const th = e.target.closest('th.sortable');
    if (th) sortBy(th.dataset.sort);
  });
  document.getElementById('players-head').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const th = e.target.closest('th.sortable');
    if (th) { e.preventDefault(); sortBy(th.dataset.sort); }
  });

  renderPlayersTable(members);

  // Bars, not a donut: these values sit close together (four defenders and
  // four midfielders are indistinguishable as two arcs) and a donut can only
  // show part-to-whole at a glance. One hue -- the labels carry identity, so
  // a colour per position would encode nothing.
  Charts.hBarChart(document.getElementById('chart-positions'), {
    data: [
      { label: 'Goalkeeper', value: num(positionCount.goalkeeper) },
      { label: 'Defender', value: num(positionCount.defender) },
      { label: 'Midfielder', value: num(positionCount.midfielder) },
      { label: 'Forward', value: num(positionCount.forward) },
    ],
    color: 'var(--series-1)',
  });
}

function renderPlayersTable(members) {
  const body = document.getElementById('players-body');
  const { pos, q, sort, dir } = playerFilterState;

  // Reflect the sort in the header: the caret shows direction, aria-sort
  // says the same thing to a screen reader.
  document.querySelectorAll('#players-head th.sortable').forEach((th) => {
    const active = th.dataset.sort === sort;
    th.classList.toggle('is-sorted', active);
    th.classList.toggle('asc', active && dir === 'asc');
    th.setAttribute('aria-sort', active ? (dir === 'asc' ? 'ascending' : 'descending') : 'none');
  });

  const filtered = members
    .filter((m) => {
      const mp = (m.favoritePosition || m.proPos || '').toLowerCase();
      if (pos !== 'ALL' && mp !== pos) return false;
      if (q) {
        const hay = `${m.proName ?? ''} ${m.name ?? ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    })
    .sort(playerSorter(sort, dir));

  if (!filtered.length) {
    body.innerHTML = `<tr><td colspan="8" class="chart-empty" style="padding:1rem 0.75rem">No players match this filter.</td></tr>`;
    return;
  }

  const maxGoals = Math.max(...filtered.map((m) => num(m.goals)), 1);
  const maxAssists = Math.max(...filtered.map((m) => num(m.assists)), 1);
  const barCell = (value, max) => {
    if (value == null) return '<td>-</td>';
    const pct = Math.round((num(value) / max) * 100);
    return `<td class="bar-cell">
      <span class="bar-fill" style="width:${pct}%"></span>
      <span class="bar-value">${num(value)}</span>
    </td>`;
  };

  body.innerHTML = filtered
    .map((m) => {
      const idx = members.indexOf(m);
      return `
      <tr class="member-row" data-idx="${idx}" tabindex="0">
        <td>${esc(m.proName ?? m.name ?? '-')}</td>
        <td>${esc(m.favoritePosition ?? m.proPos ?? '-')}</td>
        <td>${m.gamesPlayed ?? '-'}</td>
        ${barCell(m.goals, maxGoals)}
        ${barCell(m.assists, maxAssists)}
        <td>${m.ratingAve ?? '-'}</td>
        <td>${m.manOfTheMatch ?? '-'}</td>
        <td>${m.careerGoals ?? '-'}</td>
      </tr>`;
    })
    .join('');

  body.querySelectorAll('.member-row').forEach((row) => {
    row.addEventListener('click', () => togglePlayerDetail(row, members[Number(row.dataset.idx)]));
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        togglePlayerDetail(row, members[Number(row.dataset.idx)]);
      }
    });
  });
}

// prevGoals/prevGoals1..prevGoals10 is the player's goals in each of their
// last ~11 matches, most recent first (same convention as overallStats'
// lastMatch0..9). Reversed here so the sparkline reads oldest -> newest,
// left to right, consistent with the other trend chart on this page.
function recentGoals(m) {
  const keys = ['prevGoals', ...Array.from({ length: 10 }, (_, i) => `prevGoals${i + 1}`)];
  return keys
    .map((k) => m[k])
    .filter((v) => v !== undefined && v !== null && v !== '-1')
    .map(num)
    .reverse();
}

// The season-aggregate members/stats endpoint is a final tally -- no
// per-match detail, no keeper save-types. Per-match data (clubs/matches ->
// players) has a much richer per-appearance breakdown for EVERY position,
// but only for the matches we've fetched -- so everything here is "last N
// fetched matches", not full-season, and the UI says so.
function playerMatchAggregate(playerName) {
  const totals = {
    matches: 0,
    goals: 0,
    assists: 0,
    shots: 0,
    passesMade: 0,
    passAttempts: 0,
    tacklesMade: 0,
    tackleAttempts: 0,
    redCards: 0,
    mom: 0,
    secondsPlayed: 0,
    wins: 0,
    losses: 0,
    ties: 0,
    ratings: [],
    gkMatches: 0,
    saves: 0,
    ballDiveSaves: 0,
    crossSaves: 0,
    goodDirectionSaves: 0,
    parrySaves: 0,
    punchSaves: 0,
    reflexSaves: 0,
    goalsConceded: 0,
  };
  latestMatches.forEach((m) => {
    const roster = m.players?.[currentClubId];
    if (!roster) return;
    const rec = Object.values(roster).find((p) => p.playername === playerName);
    if (!rec) return;

    totals.matches += 1;
    totals.goals += num(rec.goals);
    totals.assists += num(rec.assists);
    totals.shots += num(rec.shots);
    totals.passesMade += num(rec.passesmade);
    totals.passAttempts += num(rec.passattempts);
    totals.tacklesMade += num(rec.tacklesmade);
    totals.tackleAttempts += num(rec.tackleattempts);
    totals.redCards += num(rec.redcards);
    totals.mom += num(rec.mom);
    totals.secondsPlayed += num(rec.secondsPlayed ?? rec.gameTime);
    totals.ratings.push(num(rec.rating));
    if (num(rec.wins)) totals.wins += 1;
    else if (num(rec.losses)) totals.losses += 1;
    else totals.ties += 1;

    if (rec.pos === 'goalkeeper') {
      totals.gkMatches += 1;
      totals.saves += num(rec.saves);
      totals.ballDiveSaves += num(rec.ballDiveSaves);
      totals.crossSaves += num(rec.crossSaves);
      totals.goodDirectionSaves += num(rec.goodDirectionSaves);
      totals.parrySaves += num(rec.parrySaves);
      totals.punchSaves += num(rec.punchSaves);
      totals.reflexSaves += num(rec.reflexSaves);
      totals.goalsConceded += num(rec.goalsconceded);
    }
  });
  return totals;
}

function togglePlayerDetail(row, member) {
  const idx = row.dataset.idx;
  const existing = row.nextElementSibling;
  const isOpenForThisRow = existing?.classList.contains('member-detail-row') && existing.dataset.forIdx === idx;

  document.querySelectorAll('.member-detail-row').forEach((r) => r.remove());
  document.querySelectorAll('.member-row.expanded').forEach((r) => r.classList.remove('expanded'));

  if (isOpenForThisRow) return; // click on an already-open row just closes it

  row.classList.add('expanded');

  const detailRow = document.createElement('tr');
  detailRow.className = 'member-detail-row';
  detailRow.dataset.forIdx = idx;
  const td = document.createElement('td');
  td.colSpan = 8;

  const proName = esc(member.proName ?? member.name ?? 'Unknown');
  const gamertag = esc(member.name ?? '');
  const position = esc(member.favoritePosition ?? member.proPos ?? '-');

  td.innerHTML = `
    <div class="player-detail">
      <div class="player-detail-head">
        <strong>${proName}</strong>
        ${gamertag && gamertag !== proName ? `<span class="muted">(${gamertag})</span>` : ''}
        <span class="muted">${position}</span>
        ${member.proOverall ? `<span class="muted">OVR ${esc(member.proOverall)}</span>` : ''}
      </div>
      <div class="stat-grid compact">
        ${statCard('Win Rate', member.winRate != null ? `${member.winRate}%` : '-')}
        ${statCard('Shot Success', member.shotSuccessRate != null ? `${member.shotSuccessRate}%` : '-')}
        ${statCard('Passes Made', member.passesMade)}
        ${statCard('Pass Success', member.passSuccessRate != null ? `${member.passSuccessRate}%` : '-')}
        ${statCard('Tackles Made', member.tacklesMade)}
        ${statCard('Tackle Success', member.tackleSuccessRate != null ? `${member.tackleSuccessRate}%` : '-')}
        ${statCard('Clean Sheets (Def)', member.cleanSheetsDef)}
        ${statCard('Clean Sheets (GK)', member.cleanSheetsGK)}
        ${statCard('Red Cards', member.redCards)}
        ${statCard('Height (cm)', member.proHeight)}
      </div>
      <div class="chart-row">
        <div class="chart-card">
          <h3>Season vs Career</h3>
          <div class="stat-grid compact">
            ${statCard('Season Goals', member.goals)}
            ${statCard('Career Goals', member.careerGoals)}
            ${statCard('Season Assists', member.assists)}
            ${statCard('Career Assists', member.careerAssists)}
            ${statCard('Season GP', member.gamesPlayed)}
            ${statCard('Career GP', member.careerGamesPlayed)}
            ${statCard('Season Avg Rating', member.ratingAve)}
            ${statCard('Career Avg Rating', member.careerRatingAve)}
          </div>
        </div>
        <div class="chart-card">
          <h3>Recent Form (Goals)</h3>
          <div id="spark-${idx}"></div>
          <p class="chart-caption">Oldest &rarr; most recent match.</p>
        </div>
      </div>
      <div class="chart-row">
        <div class="chart-card">
          <h3>Recent Match Performance</h3>
          <p class="chart-caption">
            From the matches currently loaded on the Matches tab, not
            full-season -- this per-appearance detail (shots, pass/tackle
            attempts, minutes, personal W/L) isn't in the season-aggregate
            endpoint at all.
          </p>
          <div id="perf-summary-${idx}"></div>
        </div>
        <div class="chart-card">
          <h3>Rating Trend</h3>
          <div id="rating-spark-${idx}"></div>
          <p class="chart-caption">Oldest &rarr; most recent match.</p>
        </div>
      </div>
      <div class="chart-row">
        <div class="chart-card">
          <h3>Rating &mdash; Full Tracked History</h3>
          <div id="history-rating-${idx}"></div>
          <p class="chart-caption" id="history-rating-caption-${idx}">
            Everything we've captured for this player since tracking began (see Competition),
            not just the recent sample above.
          </p>
        </div>
      </div>
      ${member.favoritePosition === 'goalkeeper' ? goalkeeperSectionHtml(idx) : ''}
    </div>
  `;

  detailRow.appendChild(td);
  row.after(detailRow);

  Charts.sparkline(document.getElementById(`spark-${idx}`), {
    values: recentGoals(member),
    color: 'var(--series-1)',
  });

  const agg = playerMatchAggregate(member.name);
  const perfSummary = document.getElementById(`perf-summary-${idx}`);
  const ratingSpark = document.getElementById(`rating-spark-${idx}`);

  if (agg.matches === 0) {
    perfSummary.innerHTML = '<p class="chart-empty">Didn\'t appear in the matches currently loaded.</p>';
    Charts.emptyState(ratingSpark, 'No recent match data.');
  } else {
    const passPct = agg.passAttempts ? Math.round((agg.passesMade / agg.passAttempts) * 100) : null;
    const tacklePct = agg.tackleAttempts ? Math.round((agg.tacklesMade / agg.tackleAttempts) * 100) : null;
    const convPct = agg.shots ? Math.round((agg.goals / agg.shots) * 100) : null;
    const minutes = Math.round(agg.secondsPlayed / 60);
    perfSummary.innerHTML = `
      <div class="stat-grid compact">
        ${statCard('Matches', agg.matches)}
        ${statCard('Goals', agg.goals)}
        ${statCard('Assists', agg.assists)}
        ${statCard('Shots', agg.shots)}
        ${statCard('Shot Conversion', convPct != null ? `${convPct}%` : '-')}
        ${statCard('Pass Accuracy', passPct != null ? `${passPct}%` : '-')}
        ${statCard('Tackle Accuracy', tacklePct != null ? `${tacklePct}%` : '-')}
        ${statCard('Minutes Played', minutes)}
        ${statCard('MOTM', agg.mom)}
        ${statCard('Red Cards', agg.redCards)}
        ${statCard('Personal Record', `${agg.wins}-${agg.losses}-${agg.ties}`)}
      </div>
    `;
    Charts.sparkline(ratingSpark, {
      values: [...agg.ratings].reverse(),
      color: 'var(--series-3)',
      formatValue: (v) => v.toFixed(1),
    });
  }

  loadPlayerHistoryTrend(member.name, idx);

  if (member.favoritePosition === 'goalkeeper') {
    const summary = document.getElementById(`gk-summary-${idx}`);
    if (agg.gkMatches === 0) {
      summary.innerHTML = '<p class="chart-empty">This keeper didn\'t appear in goal in the matches currently loaded.</p>';
    } else {
      summary.innerHTML = `
        <div class="stat-grid compact">
          ${statCard('Matches in Goal', agg.gkMatches)}
          ${statCard('Total Saves', agg.saves)}
          ${statCard('Goals Conceded', agg.goalsConceded)}
          ${statCard('Saves / Match', (agg.saves / agg.gkMatches).toFixed(1))}
        </div>
        <div id="gk-donut-${idx}"></div>
      `;
      Charts.donutChart(document.getElementById(`gk-donut-${idx}`), {
        data: [
          { label: 'Diving', value: agg.ballDiveSaves, color: 'var(--series-1)' },
          { label: 'Reflex', value: agg.reflexSaves, color: 'var(--series-2)' },
          { label: 'Crosses', value: agg.crossSaves, color: 'var(--series-3)' },
          { label: 'Good Direction', value: agg.goodDirectionSaves, color: 'var(--series-4)' },
          { label: 'Parries', value: agg.parrySaves, color: 'var(--series-5)' },
          { label: 'Punches', value: agg.punchSaves, color: 'var(--series-6)' },
        ],
      });
    }
  }
}

function goalkeeperSectionHtml(idx) {
  return `
    <div class="chart-card">
      <h3>Goalkeeping -- save breakdown</h3>
      <p class="chart-caption">
        From the matches currently loaded on the Matches tab (not full-season --
        EA's aggregate stats endpoint doesn't include save-type detail).
      </p>
      <div id="gk-summary-${idx}"></div>
    </div>
  `;
}

// A player's rating across every match we've ever tracked for this club
// (see db.py's match_players / player_trend) -- deeper than the "matches
// currently loaded" sample above, but only goes back to whenever this
// server started tracking the club. Loaded on demand, not on every roster
// render, since most players won't have this drawer opened.
async function loadPlayerHistoryTrend(playerName, idx) {
  const container = document.getElementById(`history-rating-${idx}`);
  const captionEl = document.getElementById(`history-rating-caption-${idx}`);
  if (!container) return;
  try {
    const data = await api(`/api/history/players?name=${encodeURIComponent(playerName)}`);
    const rows = data.matches || [];
    // One point per day rather than per match: people play in sessions, so
    // per-match points bunch up on match nights and leave gaps elsewhere,
    // which reads as a trend that isn't there. The mean is the day's
    // performance -- the peak would flatter a bad night with one good game.
    const daily = dailyAverage(rows, {
      time: (r) => r.played_at,
      value: (r) => r.rating,
      decimals: 2,
    });
    if (!daily.length) {
      // Distinguish "nothing captured" from "captured, but nothing we can
      // place on a day" -- grouping by date drops any reading with no usable
      // timestamp, and silently showing the empty-history message for those
      // would misreport why the chart is blank.
      Charts.emptyState(container, rows.length
        ? 'Tracked matches for this player have no usable dates, so they cannot be plotted by day.'
        : 'No tracked history for this player yet.');
      if (captionEl) captionEl.textContent = '';
      return;
    }
    Charts.areaChart(container, {
      data: daily.map((d) => ({
        ...d,
        // Say what each point is made of -- a day averaging four matches and
        // a day with one shouldn't look equally solid without saying so.
        hint: `${d.count} ${d.count === 1 ? 'match' : 'matches'}`,
      })),
      color: 'var(--series-1)',
    });
    if (captionEl) {
      const matches = daily.reduce((sum, d) => sum + d.count, 0);
      captionEl.textContent =
        `Average match rating per day, oldest → most recent — `
        + `${matches} match${matches === 1 ? '' : 'es'} across ${daily.length} day${daily.length === 1 ? '' : 's'}.`;
    }
  } catch (err) {
    Charts.emptyState(container, err.message);
  }
}

// --------------------------------------------------------------------------
// Matches
// --------------------------------------------------------------------------

const MATCH_TYPES = [
  { value: 'leagueMatch', label: 'League' },
  { value: 'playoffMatch', label: 'Playoff' },
  { value: 'friendlyMatch', label: 'Friendly' },
];
const MATCH_COUNTS = [10, 20, 30];

function matchControlsHtml(matchType, count) {
  const typeOptions = MATCH_TYPES.map(
    (t) => `<option value="${t.value}" ${t.value === matchType ? 'selected' : ''}>${t.label}</option>`
  ).join('');
  const countOptions = MATCH_COUNTS.map(
    (c) => `<option value="${c}" ${c === count ? 'selected' : ''}>${c} matches</option>`
  ).join('');
  return `
    <div class="search-row match-controls">
      <select id="match-type-select">${typeOptions}</select>
      <select id="match-count-select">${countOptions}</select>
      <p class="chart-caption">EA's API may cap how many matches it returns regardless of this selector.</p>
    </div>
  `;
}

function wireMatchControls(matchType, count) {
  document.getElementById('match-type-select').addEventListener('change', (e) => {
    reloadMatches(e.target.value, count);
  });
  document.getElementById('match-count-select').addEventListener('change', (e) => {
    reloadMatches(matchType, Number(e.target.value));
  });
}

// Team-level totals from one match's per-player breakdown, for a given
// club -- used for the shots/pass-accuracy trend charts, the MOTM
// leaderboard, and the team-vs-team comparison strip in the match detail
// (which needs this for BOTH sides, not just our own club).
function teamMatchAggregate(rawMatch, clubId) {
  const roster = Object.values(rawMatch.players?.[clubId] || {});
  return {
    shots: roster.reduce((s, p) => s + num(p.shots), 0),
    passesMade: roster.reduce((s, p) => s + num(p.passesmade), 0),
    passAttempts: roster.reduce((s, p) => s + num(p.passattempts), 0),
    tacklesMade: roster.reduce((s, p) => s + num(p.tacklesmade), 0),
    tackleAttempts: roster.reduce((s, p) => s + num(p.tackleattempts), 0),
    redCards: roster.reduce((s, p) => s + num(p.redcards), 0),
    motm: roster.find((p) => num(p.mom) === 1)?.playername ?? null,
  };
}

function renderMatches(result, matchType = 'leagueMatch', count = 10) {
  const panel = document.getElementById('tab-matches');
  if (result.status !== 'fulfilled') {
    panel.style.opacity = '1';
    panel.innerHTML = `${matchControlsHtml(matchType, count)}<p style="color:#e05a5a">${esc(result.reason.message)}</p>`;
    wireMatchControls(matchType, count);
    return;
  }
  const matches = result.value || [];
  panel.style.opacity = '1';

  if (!matches.length) {
    panel.innerHTML = `${matchControlsHtml(matchType, count)}<p>No matches found for this filter.</p>`;
    wireMatchControls(matchType, count);
    return;
  }

  try {
    const parsed = matches.map((m) => {
      const clubs = m.clubs || {};
      const clubIds = Object.keys(clubs);
      const usClub = clubs[currentClubId] || clubs[clubIds[0]];
      const oppId = clubIds.find((id) => id !== currentClubId) || clubIds[1];
      const oppClub = clubs[oppId];
      const oppName = oppClub?.details?.name ?? 'Opponent';
      const usScore = num(usClub?.goals);
      const oppScore = num(oppClub?.goals);
      const outcome = usClub?.wins === '1' ? 'W' : usClub?.losses === '1' ? 'L' : 'D';
      const when = m.timeAgo ? `${m.timeAgo.number} ${m.timeAgo.unit} ago` : '-';
      const forfeit = usClub?.winnerByDnf === '1' || oppClub?.winnerByDnf === '1';
      const cleanSheet = !forfeit && oppScore === 0;
      const team = teamMatchAggregate(m, currentClubId);
      return { when, outcome, usScore, oppScore, oppName, forfeit, cleanSheet, team };
    });

    const rows = parsed
      .map(
        (p, i) => `
      <tr class="match-row" data-idx="${i}" tabindex="0">
        <td>${p.when}</td>
        <td>${p.outcome}${p.forfeit ? ' <span class="badge badge-warn">FF</span>' : ''}</td>
        <td>${p.usScore} - ${p.oppScore}</td>
        <td>${esc(p.oppName)}</td>
      </tr>`
      )
      .join('');

    panel.innerHTML = `
      ${matchControlsHtml(matchType, count)}
      <div class="chart-row">
        <div class="chart-card">
          <h3>Goal Differential</h3>
          <div id="chart-goaldiff"></div>
          <p class="chart-caption">Oldest &rarr; most recent. Hover a bar for the score.</p>
        </div>
        <div class="chart-card">
          <h3>Result Mix</h3>
          <div id="chart-results"></div>
        </div>
      </div>
      <div class="chart-row">
        <div class="chart-card">
          <h3>Shots per Match</h3>
          <div id="chart-shots"></div>
          <p class="chart-caption">Oldest &rarr; most recent. Team total, forfeits excluded.</p>
        </div>
        <div class="chart-card">
          <h3>Pass Accuracy per Match</h3>
          <div id="chart-passacc"></div>
        </div>
        <div class="chart-card">
          <h3>Shot Conversion per Match</h3>
          <div id="chart-convrate"></div>
        </div>
      </div>
      <div class="chart-row">
        <div class="chart-card">
          <h3>Man of the Match (this sample)</h3>
          <div id="chart-motm"></div>
        </div>
        <div class="chart-card">
          <h3>This stretch</h3>
          <div class="stat-grid compact">
            ${statCard('Clean Sheets', parsed.filter((p) => p.cleanSheet).length)}
            ${statCard('Forfeits', parsed.filter((p) => p.forfeit).length)}
            ${statCard('Red Cards (team)', parsed.reduce((s, p) => s + p.team.redCards, 0))}
          </div>
        </div>
      </div>
      <p class="chart-caption">Click a match for a team-vs-team comparison and each player's individual stats.</p>
      <table>
        <thead><tr><th>When</th><th>Result</th><th>Score</th><th>Opponent</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;

    wireMatchControls(matchType, count);

    panel.querySelectorAll('.match-row').forEach((row) => {
      const idx = Number(row.dataset.idx);
      row.addEventListener('click', () => toggleMatchDetail(row, matches[idx]));
      row.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          toggleMatchDetail(row, matches[idx]);
        }
      });
    });

    const oldestFirst = [...parsed].reverse();
    const nonForfeit = oldestFirst.filter((p) => !p.forfeit);

    Charts.divergingBarChart(document.getElementById('chart-goaldiff'), {
      data: oldestFirst.map((p) => ({ label: p.oppName, value: p.usScore - p.oppScore })),
      positiveColor: 'var(--series-1)',
      negativeColor: 'var(--series-8)',
    });

    Charts.trendBarChart(document.getElementById('chart-shots'), {
      data: nonForfeit.map((p) => ({ label: `vs ${p.oppName}`, value: p.team.shots })),
      color: 'var(--series-1)',
    });

    Charts.trendBarChart(document.getElementById('chart-passacc'), {
      data: nonForfeit.map((p) => ({
        label: `vs ${p.oppName}`,
        value: p.team.passAttempts ? Math.round((p.team.passesMade / p.team.passAttempts) * 100) : 0,
      })),
      color: 'var(--series-2)',
      unit: '%',
    });

    Charts.trendBarChart(document.getElementById('chart-convrate'), {
      data: nonForfeit.map((p) => ({
        label: `vs ${p.oppName}`,
        value: p.team.shots ? Math.round((p.usScore / p.team.shots) * 100) : 0,
      })),
      color: 'var(--series-3)',
      unit: '%',
    });

    const wins = parsed.filter((p) => p.outcome === 'W').length;
    const losses = parsed.filter((p) => p.outcome === 'L').length;
    const ties = parsed.filter((p) => p.outcome === 'D').length;
    Charts.donutChart(document.getElementById('chart-results'), {
      data: [
        { label: 'Wins', value: wins, color: 'var(--status-good)' },
        { label: 'Losses', value: losses, color: 'var(--status-critical)' },
        { label: 'Ties', value: ties, color: 'var(--status-neutral)' },
      ],
    });

    const motmCounts = {};
    parsed.forEach((p) => {
      if (p.team.motm) motmCounts[p.team.motm] = (motmCounts[p.team.motm] || 0) + 1;
    });
    const motmData = Object.entries(motmCounts)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 8)
      .map(([label, value]) => ({ label, value }));
    Charts.hBarChart(document.getElementById('chart-motm'), {
      data: motmData,
      color: 'var(--series-5)',
    });
  } catch (err) {
    // The matches schema is undocumented and unofficial -- fall back to raw
    // JSON if the shape doesn't match what we expect.
    panel.innerHTML = `${matchControlsHtml(matchType, count)}<p style="color:var(--muted)">Couldn't parse match data into a table, showing raw response:</p><pre>${JSON.stringify(matches, null, 2)}</pre>`;
    wireMatchControls(matchType, count);
  }
}

function rosterTableHtml(rawMatch, clubId, isOwnClub) {
  const clubMeta = rawMatch.clubs?.[clubId];
  const clubName = esc(clubMeta?.details?.name ?? (isOwnClub ? 'Your club' : 'Opponent'));
  const roster = Object.values(rawMatch.players?.[clubId] || {}).sort((a, b) => num(b.rating) - num(a.rating));

  if (!roster.length) {
    return `<div class="chart-card"><h3>${clubName}</h3><p class="chart-empty">No player data for this match.</p></div>`;
  }

  const rowsHtml = roster
    .map((p) => {
      const passPct = num(p.passattempts) ? `${Math.round((num(p.passesmade) / num(p.passattempts)) * 100)}%` : '-';
      const tacklePct = num(p.tackleattempts)
        ? `${Math.round((num(p.tacklesmade) / num(p.tackleattempts)) * 100)}%`
        : '-';
      const convPct = num(p.shots) ? `${Math.round((num(p.goals) / num(p.shots)) * 100)}%` : '-';
      const minutes = Math.round(num(p.secondsPlayed ?? p.gameTime) / 60);
      const saves = p.pos === 'goalkeeper' ? p.saves ?? '0' : '-';
      const badges = [
        num(p.mom) === 1 ? '<span class="badge badge-motm">MOTM</span>' : '',
        num(p.redcards) > 0 ? '<span class="badge badge-warn">RC</span>' : '',
        num(p.cleansheetsgk) === 1 || num(p.cleansheetsdef) === 1
          ? '<span class="badge badge-good">CS</span>'
          : '',
      ]
        .filter(Boolean)
        .join(' ');
      return `<tr>
        <td>${esc(p.playername ?? '-')}</td>
        <td>${esc(p.pos ?? '-')}</td>
        <td>${minutes || '-'}</td>
        <td>${p.rating ?? '-'}</td>
        <td>${p.goals ?? '-'}</td>
        <td>${p.assists ?? '-'}</td>
        <td>${p.shots ?? '-'}</td>
        <td>${convPct}</td>
        <td>${passPct}</td>
        <td>${tacklePct}</td>
        <td>${saves}</td>
        <td>${badges || '-'}</td>
      </tr>`;
    })
    .join('');

  return `
    <div class="chart-card match-roster">
      <h3>${clubName}${isOwnClub ? ' (You)' : ''}</h3>
      <table class="mini-table">
        <thead>
          <tr>
            <th>Name</th><th>Pos</th><th>Min</th><th>Rtg</th><th>G</th><th>A</th>
            <th>Shots</th><th>Conv%</th><th>Pass%</th><th>Tkl%</th><th>Saves</th><th></th>
          </tr>
        </thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>`;
}

// A single "us vs them" stat row for the match-detail comparison strip --
// a thin two-color bar split proportionally, values leading (dataviz spec).
function compareStatHtml(label, usVal, oppVal, unit = '') {
  const total = usVal + oppVal || 1;
  return `
    <div class="compare-stat">
      <div class="compare-label">${esc(label)}</div>
      <div class="compare-row">
        <span class="compare-value">${usVal}${unit}</span>
        <div class="compare-bar">
          <span style="width:${((usVal / total) * 100).toFixed(0)}%"></span>
          <span style="width:${((oppVal / total) * 100).toFixed(0)}%"></span>
        </div>
        <span class="compare-value">${oppVal}${unit}</span>
      </div>
    </div>`;
}

function toggleMatchDetail(row, rawMatch) {
  const idx = row.dataset.idx;
  const existing = row.nextElementSibling;
  const isOpenForThisRow = existing?.classList.contains('match-detail-row') && existing.dataset.forIdx === idx;

  document.querySelectorAll('.match-detail-row').forEach((r) => r.remove());
  document.querySelectorAll('.match-row.expanded').forEach((r) => r.classList.remove('expanded'));

  if (isOpenForThisRow) return;

  row.classList.add('expanded');

  const detailRow = document.createElement('tr');
  detailRow.className = 'match-detail-row';
  detailRow.dataset.forIdx = idx;
  const td = document.createElement('td');
  td.colSpan = 4;

  const clubIds = Object.keys(rawMatch.clubs || {});
  const orderedIds = [currentClubId, ...clubIds.filter((id) => id !== currentClubId)].filter((id) =>
    clubIds.includes(id)
  );
  const oppId = orderedIds.find((id) => id !== currentClubId);

  let compareHtml = '';
  if (oppId) {
    const us = teamMatchAggregate(rawMatch, currentClubId);
    const opp = teamMatchAggregate(rawMatch, oppId);
    const oppName = esc(rawMatch.clubs?.[oppId]?.details?.name ?? 'Opponent');
    const usPassAcc = us.passAttempts ? Math.round((us.passesMade / us.passAttempts) * 100) : 0;
    const oppPassAcc = opp.passAttempts ? Math.round((opp.passesMade / opp.passAttempts) * 100) : 0;
    const usTackleAcc = us.tackleAttempts ? Math.round((us.tacklesMade / us.tackleAttempts) * 100) : 0;
    const oppTackleAcc = opp.tackleAttempts ? Math.round((opp.tacklesMade / opp.tackleAttempts) * 100) : 0;
    compareHtml = `
      <div class="compare-head">
        <span class="compare-team">Your club</span>
        <span class="compare-vs">Team totals</span>
        <span class="compare-team">${oppName}</span>
      </div>
      <div class="team-compare">
        ${compareStatHtml('Shots', us.shots, opp.shots)}
        ${compareStatHtml('Pass accuracy', usPassAcc, oppPassAcc, '%')}
        ${compareStatHtml('Tackle accuracy', usTackleAcc, oppTackleAcc, '%')}
      </div>`;
  }

  td.innerHTML = `
    ${compareHtml}
    <div class="chart-row match-detail">
      ${orderedIds.map((id) => rosterTableHtml(rawMatch, id, id === currentClubId)).join('')}
    </div>
  `;

  detailRow.appendChild(td);
  row.after(detailRow);
}

// --------------------------------------------------------------------------
// Competition -- our own divisional progress (EA has no full league table
// to show), plus a head-to-head record against every club we've actually
// played, built from tracked match history (see db.py's rival_records).
// --------------------------------------------------------------------------

// EA Sports FC Pro Clubs has 10 divisions as of this writing -- undocumented
// by EA (see ea_client.py), so this is a reasonable default rather than a
// hard fact; the ladder extends past it automatically if a club's current
// or best division ever reports higher, rather than silently truncating.
const DIVISION_COUNT_DEFAULT = 10;

function renderDivisionLadder(container, current, best) {
  const cur = num(current);
  const bestNum = num(best);
  const top = Math.max(DIVISION_COUNT_DEFAULT, cur, bestNum, 1);
  const rows = [];
  for (let d = 1; d <= top; d++) {
    const isCurrent = cur > 0 && d === cur;
    const isBest = bestNum > 0 && d === bestNum;
    rows.push(`
      <div class="rung ${isCurrent ? 'current' : ''} ${isBest ? 'best' : ''}">
        <span class="rn">D${d}</span>
        <div class="bar"><span style="width:${isCurrent ? 100 : isBest ? 45 : 8}%"></span></div>
        <span class="rung-note">${isCurrent ? 'Current' : isBest ? 'Best finish' : ''}</span>
      </div>`);
  }
  container.innerHTML = `<div class="ladder">${rows.reverse().join('')}</div>`;
}

function renderCompetition(standingsResult, historyDivisionResult, historyMatchesResult, rivalsResult) {
  const panel = document.getElementById('tab-competition');
  if (standingsResult.status !== 'fulfilled') return panelError(panel, standingsResult);
  const s = standingsResult.value;

  panel.innerHTML = `
    <p style="color:var(--muted)">
      EA's Pro Clubs API does not expose a full league table -- only your club's own divisional
      progress, and, below, your own head-to-head record against clubs you've actually played.
    </p>
    <div class="chart-card">
      <h3>Division Ladder</h3>
      <div id="chart-ladder"></div>
    </div>
    <div class="stat-grid">
      ${statCard('Current Division', s.currentDivision)}
      ${statCard('Points', s.points)}
      ${statCard('Best Division', s.bestDivision)}
      ${statCard('Best Finish', s.bestFinishGroup)}
      ${statCard('Promotions', s.promotions)}
      ${statCard('Relegations', s.relegations)}
      ${statCard('League Appearances', s.leagueAppearances)}
      ${statCard('Unbeaten Streak', s.unbeatenstreak)}
    </div>
    <div class="chart-row" id="competition-history-row"></div>
    <div class="chart-row">
      <div class="chart-card" style="flex-basis:100%">
        <h3>Head-to-Head Record</h3>
        <p class="chart-caption">
          Every club we've faced since tracking began -- built from our own tracked match history,
          not EA's API (which has no cross-club lookup at all).
        </p>
        <div id="rivals-wrap"></div>
      </div>
    </div>
  `;

  renderDivisionLadder(document.getElementById('chart-ladder'), s.currentDivision, s.bestDivision);


  const historyRow = document.getElementById('competition-history-row');
  const trackedSince = historyDivisionResult.status === 'fulfilled' ? historyDivisionResult.value.trackedSince : null;
  if (!trackedSince) {
    historyRow.innerHTML = `
      <div class="chart-card" style="flex-basis:100%">
        <p>This club isn't being tracked yet, so there's no season-long history to show here.</p>
        <p class="chart-caption">
          History only accumulates going forward from when a club is added to this server's
          tracked-clubs list -- it can't be backfilled from before that, since EA doesn't expose
          match data that old.
        </p>
      </div>`;
  } else {
    const matches = historyMatchesResult.status === 'fulfilled' ? (historyMatchesResult.value.matches || []) : [];
    const since = new Date(trackedSince * 1000).toLocaleDateString();
    historyRow.innerHTML = `
      <div class="chart-card" style="flex-basis:100%">
        <h3>Cumulative Win Rate</h3>
        <p class="chart-caption">Tracking since ${since} &middot; ${matches.length} match${matches.length === 1 ? '' : 'es'} captured so far.</p>
        <div id="chart-hist-record"></div>
      </div>`;
    const recordEl = document.getElementById('chart-hist-record');
    if (matches.length) {
      let wins = 0;
      const cumData = matches.map((m, i) => {
        if (m.outcome === 'W') wins += 1;
        return { label: `vs ${m.opp_name}`, value: Math.round((wins / (i + 1)) * 100) };
      });
      Charts.trendBarChart(recordEl, { data: cumData, color: 'var(--status-good)', unit: '%' });
    } else {
      Charts.emptyState(recordEl, 'No matches captured yet -- check back after the next poll.');
    }
  }

  renderRivalsTable(rivalsResult);
}

function renderRivalsTable(rivalsResult) {
  const wrap = document.getElementById('rivals-wrap');
  if (rivalsResult.status !== 'fulfilled') {
    wrap.innerHTML = `<p style="color:#e05a5a">${esc(rivalsResult.reason.message)}</p>`;
    return;
  }
  const rivals = rivalsResult.value.rivals || [];
  if (!rivals.length) {
    wrap.innerHTML = '<p class="chart-empty">No tracked matches yet -- check back after the next poll.</p>';
    return;
  }

  const outcomeColor = (o) => (o === 'W' ? 'var(--status-good)' : o === 'L' ? 'var(--status-critical)' : 'var(--status-neutral)');

  wrap.innerHTML = `
    <table>
      <thead>
        <tr><th>Club</th><th>Played</th><th>Record</th><th>GF</th><th>GA</th><th>Diff</th><th>Last Result</th></tr>
      </thead>
      <tbody>
        ${rivals
          .map((r) => {
            const diff = r.gf - r.ga;
            return `
          <tr>
            <td>${esc(r.name)}</td>
            <td>${r.played}</td>
            <td>
              <div class="rival-bar" title="${r.wins}W ${r.draws}D ${r.losses}L">
                <span style="width:${(r.wins / r.played) * 100}%"></span>
                <span style="width:${(r.draws / r.played) * 100}%"></span>
                <span style="width:${(r.losses / r.played) * 100}%"></span>
              </div>
            </td>
            <td>${r.gf}</td>
            <td>${r.ga}</td>
            <td>${diff > 0 ? `+${diff}` : diff}</td>
            <td><span style="color:${outcomeColor(r.last_outcome)}; font-weight:700">${esc(r.last_outcome ?? '-')}</span></td>
          </tr>`;
          })
          .join('')}
      </tbody>
    </table>`;
}

// This site tracks exactly one club, configured server-side (CLUB_ID /
// CLUB_PLATFORM) -- there's no search box, so this is the only club view.
loadClub(window.CLUB_ID, window.CLUB_PLATFORM);
