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
  const exchangeError = document.getElementById('exchange-error');
  const exchangeRetry = document.getElementById('exchange-retry');
  const submitExchange = document.getElementById('exchange-submit');
  const choiceState = new Map([offered, requested].filter(Boolean).map(select => [select, {version: 0, controller: null, timer: null, pending: false, error: ''}]));
  let dialogVersion = 0;
  const selectedShift = select => select?.selectedOptions[0]?.value ? select.selectedOptions[0] : null;
  const shiftGroup = option => option.dataset.group === 'R1' ? 'R1' : ['R2', 'R3'].includes(option.dataset.group) ? 'R2R3' : '';
  function compatibleShifts(first, second) {
    return first && second && first.dataset.category === second.dataset.category &&
      (first.dataset.category === 'regular' || (shiftGroup(first) && shiftGroup(first) === shiftGroup(second)));
  }
  function updateExchangeState() {
    if (!submitExchange) return;
    const states = [...choiceState.values()];
    const errors = states.map(state => state.error).filter(Boolean);
    submitExchange.disabled = states.some(state => state.pending || state.error) || !compatibleShifts(selectedShift(offered), selectedShift(requested));
    exchangeError.hidden = !errors.length;
    exchangeError.textContent = errors.length ? 'Não foi possível carregar os plantões. Confira sua conexão e tente novamente.' : '';
    exchangeRetry.hidden = !errors.length;
  }
  function cancelChoices(select) {
    const state = choiceState.get(select);
    state.version += 1;
    state.controller?.abort();
    clearTimeout(state.timer);
    state.pending = false;
  }
  async function loadChoices(select, selected = select.value, anchorOverride = null) {
    if (!exchange?.open) return false;
    const state = choiceState.get(select);
    cancelChoices(select);
    const version = state.version;
    const session = dialogVersion;
    const controller = new AbortController();
    state.controller = controller;
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 15000);
    state.pending = true;
    state.error = '';
    select.disabled = true;
    const counter = document.getElementById(select.id + '-count');
    counter.textContent = 'Carregando plantões…';
    updateExchangeState();
    const other = select === offered ? requested : offered;
    const input = searches.find(field => field.dataset.filterSelect === select.id);
    const params = new URLSearchParams({side: select === offered ? 'offered' : 'requested', q: input?.value || '', selected: selected || '', anchor: anchorOverride === null ? other.value : anchorOverride});
    try {
      const response = await fetch(`${exchange.dataset.choicesUrl}?${params}`, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal, headers: {Accept: 'application/json'}});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('choices');
      const payload = await response.json();
      if (!Array.isArray(payload.options)) throw new Error('choices');
      if (version !== state.version || session !== dialogVersion || !exchange.open) return false;
      select.replaceChildren(new Option(select === offered ? 'Escolha o plantão que você oferece' : 'Escolha um plantão do colega', ''));
      for (const item of payload.options) {
        const option = new Option(item.label, item.id);
        option.dataset.category = item.category;
        option.dataset.group = item.group;
        select.add(option);
      }
      select.value = payload.selected || '';
      select.disabled = !payload.options.length;
      counter.textContent = payload.has_more ? `Exibindo ${payload.options.length} plantões. Busque por nome, data ou setor para encontrar outros.` : payload.options.length ? `${payload.options.length} plantão(ões) encontrado(s).` : 'Nenhum plantão compatível encontrado. Altere a busca ou a seleção.';
      if (selected && !payload.selected) counter.textContent = 'O plantão selecionado não está mais disponível. Escolha outro.';
      return true;
    } catch (error) {
      if ((error.name === 'AbortError' && !timedOut) || version !== state.version || session !== dialogVersion || !exchange.open) return false;
      state.error = 'load';
      select.replaceChildren(new Option('Tente carregar os plantões novamente', ''));
      select.disabled = true;
      counter.textContent = 'Falha ao carregar.';
      return false;
    } finally {
      clearTimeout(timeout);
      if (version === state.version && session === dialogVersion) {
        state.pending = false;
        updateExchangeState();
      }
    }
  }
  searches.forEach(input => input.addEventListener('input', () => {
    const select = input.dataset.filterSelect === offered?.id ? offered : requested;
    if (!select || !exchange.open) return;
    cancelChoices(select);
    const state = choiceState.get(select);
    state.pending = true;
    select.disabled = true;
    updateExchangeState();
    state.timer = setTimeout(() => loadChoices(select), 250);
  }));
  for (const select of [offered, requested].filter(Boolean)) {
    select.addEventListener('change', () => {
      const other = select === offered ? requested : offered;
      if (selectedShift(select) && selectedShift(other) && !compatibleShifts(selectedShift(select), selectedShift(other))) other.value = '';
      updateExchangeState();
      loadChoices(other);
    });
  }
  exchange?.querySelector('form')?.addEventListener('submit', event => {
    updateExchangeState();
    if (submitExchange.disabled) event.preventDefault();
  });
  exchangeRetry?.addEventListener('click', () => {
    // Rebuild both sides so a stale or removed anchor cannot trap the dialog.
    offered.value = requested.value = '';
    Promise.all([loadChoices(offered, '', ''), loadChoices(requested, '', '')]);
  });
  exchange?.addEventListener('close', () => {
    dialogVersion += 1;
    for (const select of choiceState.keys()) cancelChoices(select);
    updateExchangeState();
  });
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
    button.addEventListener('click', async () => {
      if (!exchange) return;
      dialogVersion += 1;
      const session = dialogVersion;
      for (const select of choiceState.keys()) {
        cancelChoices(select);
        select.replaceChildren(new Option('Escolha um plantão', ''));
        choiceState.get(select).error = '';
      }
      searches.forEach(input => { input.value = ''; });
      exchange.showModal();
      updateExchangeState();
      if (button.dataset.offered || button.dataset.requested) {
        const first = button.dataset.offered ? offered : requested;
        const id = button.dataset.offered || button.dataset.requested;
        const loaded = await loadChoices(first, id, '');
        if (session === dialogVersion && exchange.open && loaded) await loadChoices(first === offered ? requested : offered);
      } else {
        await Promise.all([loadChoices(offered, '', ''), loadChoices(requested, '', '')]);
      }
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
