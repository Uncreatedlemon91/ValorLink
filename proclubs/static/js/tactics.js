// Tactics board: a formation pitch staff can drag roster names onto. No
// external drag-and-drop library -- plain HTML5 drag events, same
// dependency-free approach as charts.js. Read-only for everyone else:
// the server only sends bench/save markup to staff at all (see
// tactics.html), and this file checks TACTICS_IS_STAFF before wiring any
// interactivity, but the real enforcement is server-side (POST /api/tactics
// requires auth.require_staff) -- this is convenience, not the boundary.
(function () {
  const FORMATIONS = window.TACTICS_FORMATIONS || {};
  const ALL_SLOTS = window.TACTICS_ALL_SLOTS || {};
  const IS_STAFF = !!window.TACTICS_IS_STAFF;
  const csrfInput = document.getElementById('tactics-csrf-token');
  const CSRF_TOKEN = csrfInput ? csrfInput.value : '';

  let activeFormation = window.TACTICS_ACTIVE_FORMATION;
  if (!FORMATIONS[activeFormation]) {
    activeFormation = Object.keys(FORMATIONS)[0];
  }
  // In-memory working copy -- staff can drag several names around before
  // hitting Save, which sends this whole object in one request rather
  // than one call per drag.
  let currentSlots = { ...(ALL_SLOTS[activeFormation] || {}) };

  const pitch = document.getElementById('tactics-pitch');
  const select = document.getElementById('tactics-formation-select');
  const saveBtn = document.getElementById('tactics-save-btn');
  const saveStatus = document.getElementById('tactics-save-status');
  const benchList = document.getElementById('tactics-bench-list');

  function esc(v) {
    return String(v ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function renderPitch() {
    if (!pitch) return;
    pitch.querySelectorAll('.pitch-slot').forEach((el) => el.remove());
    const layout = FORMATIONS[activeFormation] || {};

    Object.entries(layout).forEach(([slotKey, def]) => {
      const slot = document.createElement('div');
      slot.className = 'pitch-slot';
      slot.style.top = `${def.top}%`;
      slot.style.left = `${def.left}%`;
      slot.dataset.slotKey = slotKey;

      const name = currentSlots[slotKey];
      slot.classList.toggle('filled', !!name);
      slot.innerHTML = name
        ? `<span class="pitch-slot-name">${esc(name)}</span><span class="pitch-slot-pos">${esc(def.label)}</span>`
        : `<span class="pitch-slot-pos">${esc(def.label)}</span>`;

      if (IS_STAFF) {
        slot.addEventListener('dragover', (e) => {
          e.preventDefault();
          slot.classList.add('drag-over');
        });
        slot.addEventListener('dragleave', () => slot.classList.remove('drag-over'));
        slot.addEventListener('drop', (e) => {
          e.preventDefault();
          slot.classList.remove('drag-over');
          const droppedName = e.dataTransfer.getData('text/plain');
          if (!droppedName) return;
          currentSlots[slotKey] = droppedName;
          renderPitch();
        });
        // Click a filled slot to clear it -- the touch/no-drag-support fallback.
        slot.addEventListener('click', () => {
          if (!currentSlots[slotKey]) return;
          delete currentSlots[slotKey];
          renderPitch();
        });
      }

      pitch.appendChild(slot);
    });
  }

  function renderBench(names) {
    if (!benchList) return;
    if (!names.length) {
      benchList.innerHTML = '<p class="chart-empty">No roster data available right now.</p>';
      return;
    }
    benchList.innerHTML = '';
    names.forEach((name) => {
      const chip = document.createElement('div');
      chip.className = 'tactics-chip';
      chip.textContent = name;
      chip.draggable = true;
      chip.addEventListener('dragstart', (e) => {
        e.dataTransfer.setData('text/plain', name);
        e.dataTransfer.effectAllowed = 'copy';
      });
      benchList.appendChild(chip);
    });
  }

  function loadBench() {
    if (!IS_STAFF || !benchList) return;
    fetch('/api/members')
      .then((r) => {
        if (!r.ok) throw new Error('failed to load roster');
        return r.json();
      })
      .then((data) => {
        const names = (data.members || [])
          .map((m) => m.proName || m.name)
          .filter(Boolean)
          .sort((a, b) => a.localeCompare(b));
        renderBench(names);
      })
      .catch(() => {
        benchList.innerHTML = '<p class="chart-empty">Couldn’t load the roster right now.</p>';
      });
  }

  if (select) {
    select.addEventListener('change', () => {
      activeFormation = select.value;
      currentSlots = { ...(ALL_SLOTS[activeFormation] || {}) };
      renderPitch();
    });
  }

  if (saveBtn) {
    saveBtn.addEventListener('click', () => {
      saveBtn.disabled = true;
      saveStatus.textContent = 'Saving…';
      const body = new URLSearchParams();
      body.set('formation', activeFormation);
      body.set('slots_json', JSON.stringify(currentSlots));
      body.set('csrf_token', CSRF_TOKEN);

      fetch('/api/tactics', { method: 'POST', body })
        .then(async (r) => {
          const data = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(data.error || 'Save failed.');
          ALL_SLOTS[activeFormation] = { ...currentSlots };
          saveStatus.textContent = 'Saved.';
          setTimeout(() => { saveStatus.textContent = ''; }, 3000);
        })
        .catch((err) => {
          saveStatus.textContent = err.message || 'Save failed.';
        })
        .finally(() => {
          saveBtn.disabled = false;
        });
    });
  }

  renderPitch();
  loadBench();
})();
