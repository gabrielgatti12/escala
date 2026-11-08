(() => {
  'use strict';
  const exchange = document.getElementById('exchange-dialog');
  const allocation = document.getElementById('allocation-dialog');
  const offered = document.getElementById('exchange-offered');
  const requested = document.getElementById('exchange-requested');
  const mine = document.getElementById('allocation-mine');
  const theirs = document.getElementById('allocation-theirs');
  const allocationDay = document.getElementById('allocation-day');
  const searches = [...document.querySelectorAll('[data-filter-select]')];
  const normalize = value => value.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase('pt-BR');
  const shiftCategory = option => option.dataset.category || 'regular';
  function shiftGroup(option) {
    const group = (option.dataset.group || '').trim().toUpperCase();
    return group === 'R1' ? 'R1' : ['R2', 'R3'].includes(group) ? 'R2R3' : '';
  }
  function validShift(option) {
    return shiftCategory(option) === 'regular' || (shiftCategory(option) === 'cinderela' && !!shiftGroup(option));
  }
  function compatibleShifts(option, other) {
    return validShift(option) && (!other || (shiftCategory(option) === shiftCategory(other) &&
      (shiftCategory(option) !== 'cinderela' || shiftGroup(option) === shiftGroup(other))));
  }
  function selectedShift(select) {
    const option = select?.selectedOptions[0];
    return option?.value ? option : null;
  }
  function matchesSearch(option, words) {
    const text = normalize(option.textContent);
    return words.every(word => text.includes(word));
  }
  function filterExchangeShifts(changed) {
    if (!offered || !requested) return;
    const words = new Map([offered, requested].map(select => {
      const input = searches.find(field => field.dataset.filterSelect === select.id);
      return [select, normalize(input?.value || '').trim().split(/\s+/).filter(Boolean)];
    }));
    for (const select of [offered, requested]) {
      const choice = selectedShift(select);
      if (choice && (!validShift(choice) || !matchesSearch(choice, words.get(select)))) select.value = '';
    }
    const offer = selectedShift(offered);
    const request = selectedShift(requested);
    if (offer && request && !compatibleShifts(offer, request)) {
      if (changed === requested.id) offered.value = '';
      else requested.value = '';
    }
    for (const [select, other] of [[offered, requested], [requested, offered]]) {
      const anchor = selectedShift(other);
      let count = 0;
      for (const option of select.options) {
        if (!option.value) continue;
        const matches = compatibleShifts(option, anchor) && matchesSearch(option, words.get(select));
        option.hidden = !matches;
        option.disabled = !matches;
        if (matches) count += 1;
      }
      const counter = document.getElementById(select.id + '-count');
      if (counter) counter.textContent = count ? (count === 1 ? '1 plantão encontrado' : `${count} plantões encontrados`) : 'Nenhum plantão compatível encontrado. Altere a busca ou a seleção.';
    }
    const submit = document.getElementById('exchange-submit');
    if (submit) submit.disabled = !offered.value || !requested.value;
  }
  searches.forEach(input => {
    input.addEventListener('input', () => filterExchangeShifts(input.dataset.filterSelect));
  });
  if (offered) offered.addEventListener('change', () => filterExchangeShifts(offered.id));
  if (requested) requested.addEventListener('change', () => filterExchangeShifts(requested.id));
  filterExchangeShifts();
  function updateAllocationSubmit() {
    if (mine && theirs) document.getElementById('allocation-submit').disabled = !mine.value || !theirs.value || theirs.disabled;
  }
  function filterActivities() {
    if (!mine || !theirs) return;
    const choice = mine.selectedOptions[0];
    const available = [];
    for (const option of theirs.options) {
      if (!option.value) continue;
      const matches = !!mine.value && option.value !== mine.value && option.dataset.pair === choice.dataset.pair && option.dataset.day === allocationDay.value && option.dataset.day === choice.dataset.day && Date.parse(option.dataset.start) < Date.parse(choice.dataset.end) && Date.parse(choice.dataset.start) < Date.parse(option.dataset.end);
      if (matches) available.push(option);
      option.hidden = !matches;
      option.disabled = !matches;
    }
    theirs.value = available.length === 1 ? available[0].value : '';
    theirs.disabled = available.length === 0;
    theirs.options[0].textContent = available.length ? 'Selecione a atividade do colega' : 'Sem outra atividade compatível neste dia';
    document.getElementById('allocation-help').hidden = !mine.value || available.length > 0;
    updateAllocationSubmit();
  }
  function filterAllocationDay() {
    for (const option of mine.options) {
      if (!option.value) continue;
      const matches = option.dataset.day === allocationDay.value && (!allocation.dataset.pair || option.dataset.pair === allocation.dataset.pair);
      option.hidden = !matches;
      option.disabled = !matches;
      if (!matches && option.selected) mine.value = '';
    }
    filterActivities();
  }
  document.querySelectorAll('[data-open-exchange]').forEach(button => {
    button.addEventListener('click', () => {
      if (!exchange) return;
      searches.forEach(input => { input.value = ''; });
      offered.value = button.dataset.offered || '';
      requested.value = button.dataset.requested || '';
      filterExchangeShifts(button.dataset.requested ? requested.id : offered.id);
      exchange.showModal();
    });
  });
  document.querySelectorAll('[data-open-allocation]').forEach(button => {
    button.addEventListener('click', () => {
      if (!allocation) return;
      const selected = [...mine.options].find(option => option.value === button.dataset.mine);
      allocation.dataset.pair = selected?.dataset.pair || '';
      allocationDay.value = selected?.dataset.day || [...mine.options].find(option => option.value)?.dataset.day || '';
      mine.value = selected?.value || '';
      filterAllocationDay();
      allocation.showModal();
    });
  });
  if (mine) mine.addEventListener('change', filterActivities);
  if (theirs) theirs.addEventListener('change', updateAllocationSubmit);
  if (allocationDay) allocationDay.addEventListener('change', () => { mine.value = ''; filterAllocationDay(); });
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
