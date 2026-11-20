from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from .monitoring import request_refresh
from .sheets_reconciliation import reconcile_confirmed_writes


@login_required
@require_POST
def request_source_refresh(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden('Somente o administrador pode solicitar uma nova leitura das fontes.')
    checked = reconcile_confirmed_writes(request.user)
    if checked.reverted:
        messages.success(request, f'{checked.reverted} troca(s) desfeita(s) na planilha e restaurada(s) no calendário. Os residentes foram avisados.')
    if checked.conflicts:
        messages.warning(request, f'{checked.conflicts} troca(s) com divergência parcial ou atividades alteradas. Os nomes locais foram preservados para conciliação.')
    if checked.unavailable:
        messages.warning(request, f'Não foi possível conferir {checked.unavailable} troca(s) gravada(s). Confira o motivo abaixo e a conexão Google desta conta administrativa. Os nomes locais foram preservados.')
    if checked.checked and not checked.reverted and not checked.conflicts:
        messages.info(request, f'{checked.checked} troca(s) conferida(s). Os nomes atuais da planilha foram confirmados; consulte o resultado abaixo.')
    if not any([checked.checked, checked.reverted, checked.conflicts, checked.unavailable]):
        messages.info(request, 'Nenhuma gravação confirmada no Google foi encontrada para conferir. Uma troca aceita somente no aplicativo permanece local até a gravação confirmada.')
    if request.POST.get('check_only') == 'yes':
        return redirect('/?tab=sources')
    queued = request_refresh()
    messages.success(request, 'Atualização solicitada. O sincronizador verificará o pedido em até 30 segundos; uma leitura em andamento será concluída primeiro.' if queued else 'Já há uma atualização solicitada. Aguarde o sincronizador concluir a leitura.')
    return redirect('/?tab=sources')
