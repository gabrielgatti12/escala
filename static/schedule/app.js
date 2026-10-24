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
