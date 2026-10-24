(() => {
  'use strict';
  const exchange = document.getElementById('exchange-dialog');
  const allocation = document.getElementById('allocation-dialog');
  const offered = document.getElementById('exchange-offered');
  const requested = document.getElementById('exchange-requested');
  const mine = document.getElementById('allocation-mine');
  const theirs = document.getElementById('allocation-theirs');
  function filterActivities() {
    if (!mine || !theirs) return;
    const choice = mine.selectedOptions[0];
    let available = 0;
    for (const option of theirs.options) {
      if (!option.value) continue;
      const matches = !!mine.value && option.value !== mine.value && option.dataset.pair === choice.dataset.pair && option.dataset.day === choice.dataset.day;
      if (matches) available += 1;
      option.hidden = !matches;
      option.disabled = !matches;
    }
    theirs.value = '';
    theirs.disabled = available === 0;
    theirs.options[0].textContent = available ? 'Selecione a atividade do colega' : 'Sem outra atividade compatível';
    document.getElementById('allocation-submit').disabled = available === 0;
    document.getElementById('allocation-help').hidden = !mine.value || available > 0;
  }
  document.querySelectorAll('[data-open-exchange]').forEach(button => {
    button.addEventListener('click', () => {
      if (!exchange) return;
      offered.value = button.dataset.offered || '';
      requested.value = button.dataset.requested || '';
      exchange.showModal();
    });
  });
  document.querySelectorAll('[data-open-allocation]').forEach(button => {
    button.addEventListener('click', () => {
      if (!allocation) return;
      mine.value = button.dataset.mine || '';
      filterActivities();
      allocation.showModal();
    });
  });
  if (mine) mine.addEventListener('change', filterActivities);
  document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
  document.querySelectorAll('dialog').forEach(dialog => {
    dialog.addEventListener('click', event => {
      if (event.target !== dialog) return;
      const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
    });
  });
})();

// Check for new data without replacing an unsent comment or an open exchange.
(() => {
  'use strict';
  const url = document.body.dataset.liveStatus;
  if (!url) return;
  let revision = document.body.dataset.liveRevision || null;
  let pending = false;
  let checking = false;
  let lastInteraction = 0;
  const dirty = new Set();
  const banner = document.getElementById('live-update');
  const button = document.getElementById('live-refresh');
  document.addEventListener('input', event => {
    if (event.target.form) dirty.add(event.target.form);
  });
  document.addEventListener('change', event => {
    if (event.target.form) dirty.add(event.target.form);
  });
  document.addEventListener('submit', event => dirty.delete(event.target));
  document.addEventListener('reset', event => dirty.delete(event.target));
  document.addEventListener('pointerdown', () => { lastInteraction = Date.now(); }, {passive: true});
  document.addEventListener('keydown', () => { lastInteraction = Date.now(); });
  document.addEventListener('wheel', () => { lastInteraction = Date.now(); }, {passive: true});
  document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('close', () => {
    dialog.querySelectorAll('form').forEach(form => dirty.delete(form));
  }));
  function applyPending() {
    if (!pending || document.hidden) return;
    const busy = dirty.size > 0 || document.querySelector('dialog[open]') || document.activeElement.matches('input,textarea,select') || Date.now() - lastInteraction < 20000;
    if (busy) { banner.hidden = false; return; }
    window.location.reload();
  }
  button.addEventListener('click', () => window.location.reload());
  async function check() {
    if (document.hidden || checking) return;
    checking = true;
    try {
      const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', headers: {'Accept': 'application/json'}});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) return;
      const payload = await response.json();
      if (typeof payload.revision !== 'string') return;
      if (revision !== null && revision !== payload.revision) pending = true;
      revision = payload.revision;
      applyPending();
    } catch (_) {
      // A temporary network failure leaves the current screen and drafts intact.
    } finally { checking = false; }
  }
  check();
  setInterval(check, 45000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
})();
