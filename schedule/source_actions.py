from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from .monitoring import request_refresh


@login_required
@require_POST
def request_source_refresh(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden('Somente o administrador pode solicitar uma nova leitura das fontes.')
    queued = request_refresh()
    messages.success(request, 'Atualização solicitada. O sincronizador verificará o pedido em até 30 segundos; uma leitura em andamento será concluída primeiro.' if queued else 'Já há uma atualização solicitada. Aguarde o sincronizador concluir a leitura.')
    return redirect('/?tab=sources')
