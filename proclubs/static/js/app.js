const statusEl = document.getElementById('status');
const searchForm = document.getElementById('search-form');
const resultsSection = document.getElementById('search-results');
const resultsList = document.getElementById('results-list');
const clubView = document.getElementById('club-view');
const clubHeader = document.getElementById('club-header');

let currentClubId = null;
let currentPlatform = null;
let latestMatches = []; // raw match list from the last /matches fetch, shared
                         // with the Members tab so a goalkeeper's detail
                         // panel can aggregate save stats without an extra
                         // API call.

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

searchForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = document.getElementById('club-name').value.trim();
  const platform = document.getElementById('platform').value;
  if (!name) return;

  clubView.classList.add('hidden');
  resultsSection.classList.add('hidden');
  setStatus('Searching...');

  try {
    const results = await api(`/api/clubs/search?name=${encodeURIComponent(name)}&platform=${platform}`);
    if (!results.length) {
      setStatus(`No clubs found matching "${name}" on this platform.`, true);
      return;
    }
    setStatus('');
    renderResults(results, platform);
  } catch (err) {
    setStatus(err.message, true);
  }
});

function renderResults(results, platform) {
  resultsList.innerHTML = '';
  results.forEach((club) => {
    const li = document.createElement('li');
    li.textContent = `${club.name} (club ID ${club.clubId})`;
    li.addEventListener('click', () => loadClub(club.clubId, platform));
    resultsList.appendChild(li);
  });
  resultsSection.classList.remove('hidden');
}

document.querySelectorAll('#tabs button').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#tabs button').forEach((b) => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach((p) => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
  });
});

async function loadClub(clubId, platform) {
  currentClubId = clubId;
  currentPlatform = platform;
  resultsSection.classList.add('hidden');
  clubView.classList.remove('hidden');
  setStatus('Loading club...');

  const [overview, standings, members, matches] = await Promise.allSettled([
    api(`/api/clubs/${clubId}/overview?platform=${platform}`),
    api(`/api/clubs/${clubId}/standings?platform=${platform}`),
    api(`/api/clubs/${clubId}/members?platform=${platform}`),
    api(`/api/clubs/${clubId}/matches?platform=${platform}&matchType=leagueMatch`),
  ]);

  latestMatches = matches.status === 'fulfilled' ? matches.value || [] : [];

  renderOverview(overview);
  renderStandings(standings);
  renderMembers(members);
  renderMatches(matches);
  setStatus('');
}

function panelError(panel, result) {
  panel.innerHTML = `<p style="color:#e05a5a">${result.reason.message}</p>`;
}

function statCard(label, value) {
  return `<div class="stat-card"><div class="label">${label}</div><div class="value">${value ?? '-'}</div></div>`;
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

function renderOverview(result) {
  const panel = document.getElementById('tab-overview');
  if (result.status !== 'fulfilled') return panelError(panel, result);
  const { info, stats } = result.value;
  const name = info?.name || `Club ${currentClubId}`;
  clubHeader.innerHTML = '';
  const h2 = document.createElement('h2');
  h2.textContent = name;
  const idSpan = document.createElement('span');
  idSpan.style.color = 'var(--muted)';
  idSpan.textContent = `ID ${currentClubId}`;
  clubHeader.append(h2, idSpan);

  if (!stats) {
    panel.innerHTML = '<p>No overall stats available for this club yet.</p>';
    return;
  }

  panel.innerHTML = `
    <div class="chart-row">
      <div class="chart-card">
        <h3>Record</h3>
        <div id="chart-record"></div>
      </div>
      <div class="chart-card">
        <h3>Goals For / Against</h3>
        <div id="chart-goals"></div>
      </div>
    </div>
    <div class="stat-grid">
      ${statCard('Wins', stats.wins)}
      ${statCard('Losses', stats.losses)}
      ${statCard('Ties', stats.ties)}
      ${statCard('Games Played', stats.gamesPlayed)}
      ${statCard('Goals', stats.goals)}
      ${statCard('Goals Against', stats.goalsAgainst)}
      ${statCard('Skill Rating', stats.skillRating)}
      ${statCard('Win Streak', stats.wstreak)}
    </div>
  `;

  Charts.donutChart(document.getElementById('chart-record'), {
    data: [
      { label: 'Wins', value: num(stats.wins), color: 'var(--status-good)' },
      { label: 'Losses', value: num(stats.losses), color: 'var(--status-critical)' },
      { label: 'Ties', value: num(stats.ties), color: 'var(--status-neutral)' },
    ],
  });

  Charts.vBarChart(document.getElementById('chart-goals'), {
    data: [
      { label: 'For', value: num(stats.goals), color: 'var(--series-1)' },
      { label: 'Against', value: num(stats.goalsAgainst), color: 'var(--series-2)' },
    ],
  });
}

function renderStandings(result) {
  const panel = document.getElementById('tab-standings');
  if (result.status !== 'fulfilled') return panelError(panel, result);
  const s = result.value;
  panel.innerHTML = `
    <p style="color:var(--muted)">EA's Pro Clubs API does not expose a full league table -- only your club's own divisional progress.</p>
    <div class="chart-row">
      <div class="chart-card">
        <h3>Promotions vs Relegations</h3>
        <div id="chart-promo"></div>
      </div>
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
  `;

  Charts.vBarChart(document.getElementById('chart-promo'), {
    data: [
      { label: 'Promotions', value: num(s.promotions), color: 'var(--status-good)' },
      { label: 'Relegations', value: num(s.relegations), color: 'var(--status-critical)' },
    ],
  });
}

function renderMembers(result) {
  const panel = document.getElementById('tab-members');
  if (result.status !== 'fulfilled') return panelError(panel, result);
  const members = result.value.members || [];
  const positionCount = result.value.positionCount || {};
  if (!members.length) {
    panel.innerHTML = '<p>No member stats available.</p>';
    return;
  }

  const nameOf = (m) => m.proName || m.name || 'Unknown';
  const topScorers = [...members]
    .sort((a, b) => num(b.goals) - num(a.goals))
    .slice(0, 8)
    .map((m) => ({ label: nameOf(m), value: num(m.goals) }));
  const topAssists = [...members]
    .sort((a, b) => num(b.assists) - num(a.assists))
    .slice(0, 8)
    .map((m) => ({ label: nameOf(m), value: num(m.assists) }));

  const rows = members
    .map(
      (m, i) => `
      <tr class="member-row" data-idx="${i}" tabindex="0">
        <td>${esc(m.proName ?? m.name ?? '-')}</td>
        <td>${esc(m.favoritePosition ?? m.proPos ?? '-')}</td>
        <td>${m.gamesPlayed ?? '-'}</td>
        <td>${m.goals ?? '-'}</td>
        <td>${m.assists ?? '-'}</td>
        <td>${m.ratingAve ?? '-'}</td>
        <td>${m.manOfTheMatch ?? '-'}</td>
        <td>${m.careerGoals ?? '-'}</td>
      </tr>`
    )
    .join('');

  panel.innerHTML = `
    <div class="chart-row">
      <div class="chart-card">
        <h3>Top Scorers</h3>
        <div id="chart-scorers"></div>
      </div>
      <div class="chart-card">
        <h3>Top Assists</h3>
        <div id="chart-assists"></div>
      </div>
      <div class="chart-card">
        <h3>Position Mix</h3>
        <div id="chart-positions"></div>
      </div>
    </div>
    <div class="search-row">
      <input id="member-filter" type="text" placeholder="Filter this club's roster by name..." />
      <p class="chart-caption">
        Searches this club's roster only -- EA's API has no way to look up a
        player across clubs, only within a club you already have loaded.
      </p>
    </div>
    <p class="chart-caption">Click a player for their full stat breakdown.</p>
    <table>
      <thead>
        <tr>
          <th>Name</th><th>Position</th><th>GP</th><th>Goals</th>
          <th>Assists</th><th>Avg Rating</th><th>MOTM</th><th>Career Goals</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;

  panel.querySelectorAll('.member-row').forEach((row) => {
    row.addEventListener('click', () => togglePlayerDetail(row, members[Number(row.dataset.idx)]));
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        togglePlayerDetail(row, members[Number(row.dataset.idx)]);
      }
    });
  });

  document.getElementById('member-filter').addEventListener('input', (e) => {
    const q = e.target.value.trim().toLowerCase();
    panel.querySelectorAll('.member-row').forEach((row) => {
      const m = members[Number(row.dataset.idx)];
      const hay = `${m.proName ?? ''} ${m.name ?? ''}`.toLowerCase();
      const isMatch = !q || hay.includes(q);
      row.style.display = isMatch ? '' : 'none';
      if (!isMatch) {
        const next = row.nextElementSibling;
        if (next?.classList.contains('member-detail-row')) next.remove();
        row.classList.remove('expanded');
      }
    });
  });

  Charts.hBarChart(document.getElementById('chart-scorers'), {
    data: topScorers,
    color: 'var(--series-1)',
  });
  Charts.hBarChart(document.getElementById('chart-assists'), {
    data: topAssists,
    color: 'var(--series-2)',
  });
  Charts.donutChart(document.getElementById('chart-positions'), {
    data: [
      { label: 'Goalkeeper', value: num(positionCount.goalkeeper), color: 'var(--series-1)' },
      { label: 'Defender', value: num(positionCount.defender), color: 'var(--series-2)' },
      { label: 'Midfielder', value: num(positionCount.midfielder), color: 'var(--series-3)' },
      { label: 'Forward', value: num(positionCount.forward), color: 'var(--series-4)' },
    ],
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
    const minutes = Math.round(agg.secondsPlayed / 60);
    perfSummary.innerHTML = `
      <div class="stat-grid compact">
        ${statCard('Matches', agg.matches)}
        ${statCard('Goals', agg.goals)}
        ${statCard('Assists', agg.assists)}
        ${statCard('Shots', agg.shots)}
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

function renderMatches(result) {
  const panel = document.getElementById('tab-matches');
  if (result.status !== 'fulfilled') return panelError(panel, result);
  const matches = result.value || [];
  if (!matches.length) {
    panel.innerHTML = '<p>No recent matches found.</p>';
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
      return { when, outcome, usScore, oppScore, oppName };
    });

    const rows = parsed
      .map(
        (p, i) =>
          `<tr class="match-row" data-idx="${i}" tabindex="0"><td>${p.when}</td><td>${p.outcome}</td><td>${p.usScore} - ${p.oppScore}</td><td>${esc(p.oppName)}</td></tr>`
      )
      .join('');

    panel.innerHTML = `
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
      <p class="chart-caption">Click a match for each player's individual stats from that game.</p>
      <table>
        <thead><tr><th>When</th><th>Result</th><th>Score</th><th>Opponent</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;

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
    Charts.divergingBarChart(document.getElementById('chart-goaldiff'), {
      data: oldestFirst.map((p) => ({ label: p.oppName, value: p.usScore - p.oppScore })),
      positiveColor: 'var(--series-1)',
      negativeColor: 'var(--series-8)',
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
  } catch (err) {
    // The matches schema is undocumented and unofficial -- fall back to raw
    // JSON if the shape doesn't match what we expect.
    panel.innerHTML = `<p style="color:var(--muted)">Couldn't parse match data into a table, showing raw response:</p><pre>${JSON.stringify(matches, null, 2)}</pre>`;
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
      const saves = p.pos === 'goalkeeper' ? p.saves ?? '0' : '-';
      return `<tr>
        <td>${esc(p.playername ?? '-')}</td>
        <td>${esc(p.pos ?? '-')}</td>
        <td>${p.rating ?? '-'}</td>
        <td>${p.goals ?? '-'}</td>
        <td>${p.assists ?? '-'}</td>
        <td>${p.shots ?? '-'}</td>
        <td>${passPct}</td>
        <td>${saves}</td>
      </tr>`;
    })
    .join('');

  return `
    <div class="chart-card match-roster">
      <h3>${clubName}${isOwnClub ? ' (You)' : ''}</h3>
      <table class="mini-table">
        <thead><tr><th>Name</th><th>Pos</th><th>Rtg</th><th>G</th><th>A</th><th>Shots</th><th>Pass%</th><th>Saves</th></tr></thead>
        <tbody>${rowsHtml}</tbody>
      </table>
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

  td.innerHTML = `
    <div class="chart-row match-detail">
      ${orderedIds.map((id) => rosterTableHtml(rawMatch, id, id === currentClubId)).join('')}
    </div>
  `;

  detailRow.appendChild(td);
  row.after(detailRow);
}
