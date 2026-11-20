"""Administrator-only pilot console; no remote read or write on GET."""
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .google_editor import EditorError, ready
from .models import BundleExchange, Exchange, SheetsEditorConnection, SheetsWritePilot, SheetsWritePreview
from .sheets_write import WEEK, LAST, pilot_sources, preview_exchange, preview_bundle_exchange, apply_preview


@login_required
def pilot(request):
    if not request.user.is_superuser:return HttpResponseForbidden()
    start=timezone.make_aware(datetime(2026,11,23));end=start+timedelta(days=7)
    selected=SheetsWritePilot.objects.select_related('source').filter(key='pilot').first()
    exchanges=Exchange.objects.none()
    bundles=BundleExchange.objects.none()
    if selected:
        exchanges=Exchange.objects.filter(status='accepted',offered__source=selected.source.spreadsheet_id,
            requested__source=selected.source.spreadsheet_id,offered__starts_at__gte=start,
            offered__starts_at__lt=end,requested__starts_at__gte=start,requested__starts_at__lt=end).select_related('offered','requested','proposer','recipient').order_by('-pk')[:50]
        bundles=BundleExchange.objects.filter(status='accepted',items__shift__source=selected.source.spreadsheet_id,
            items__shift__starts_at__gte=start,items__shift__starts_at__lt=end).select_related('proposer','recipient').prefetch_related('items__shift').distinct().order_by('-pk')[:50]
    previews=SheetsWritePreview.objects.filter(account=request.user).select_related('exchange','bundle','source').order_by('-pk')[:30]
    response=render(request,'sheets_write_pilot.html',{'tab':'sources','pilot':selected,
        'sources':pilot_sources(),'exchanges':exchanges,'bundles':bundles,'previews':previews,
        'connection':SheetsEditorConnection.objects.filter(key='pilot').first(),
        'setup_error':ready(request),'callback_uri':request.build_absolute_uri(reverse('google_editor_callback')),
        'week':WEEK,'last':LAST})
    response['Cache-Control']='private, no-store'
    return response


@login_required
@require_POST
def configure(request):
    if not request.user.is_superuser:return HttpResponseForbidden()
    if request.POST.get('action')=='disable':
        SheetsWritePilot.objects.filter(key='pilot').update(enabled=False)
        messages.success(request,'Teste de escrita desativado.')
    else:
        source_id=request.POST.get('source','')
        if not source_id.isdigit() or len(source_id)>18:
            messages.error(request,'Selecione uma fonte importada para a semana do teste.')
            return redirect('sheets_write_pilot')
        source=get_object_or_404(pilot_sources(),pk=source_id)
        SheetsWritePilot.objects.update_or_create(key='pilot',defaults={'source':source,'enabled':True,'configured_by':request.user})
        messages.success(request,'Teste limitado à fonte selecionada, na semana de 23–29/11/2026. A escrita exige conferência de cada troca.')
    return redirect('sheets_write_pilot')


@login_required
@require_POST
def preview(request,pk):
    if not request.user.is_superuser:return HttpResponseForbidden()
    get_object_or_404(Exchange,pk=pk)
    try:
        result=preview_exchange(request.user,pk)
    except EditorError as error:
        messages.error(request,str(error));return redirect('sheets_write_pilot')
    return redirect('sheets_write_review',pk=result.pk)


@login_required
@require_POST
def preview_bundle(request,pk):
    if not request.user.is_superuser:return HttpResponseForbidden()
    get_object_or_404(BundleExchange,pk=pk)
    try:
        result=preview_bundle_exchange(request.user,pk)
    except EditorError as error:
        messages.error(request,str(error));return redirect('sheets_write_pilot')
    return redirect('sheets_write_review',pk=result.pk)


@login_required
def review(request,pk):
    if not request.user.is_superuser:return HttpResponseForbidden()
    result=get_object_or_404(SheetsWritePreview.objects.select_related('exchange','bundle','source'),pk=pk,account=request.user)
    return render(request,'sheets_write_review.html',{'tab':'sources','preview':result})


@login_required
@require_POST
def apply(request,pk):
    if not request.user.is_superuser:return HttpResponseForbidden()
    get_object_or_404(SheetsWritePreview,pk=pk,account=request.user)
    if request.POST.get('confirm')!='yes':
        messages.error(request,'Confirme a conferência de todos os nomes e o uso de valores fixos nas células indicadas.')
        return redirect('sheets_write_review',pk=pk)
    try:
        result=apply_preview(request.user,pk)
        if result.state=='done':messages.success(request,'Gravação conferida no Google Sheets.')
        else:messages.error(request,result.message)
    except EditorError as error:messages.error(request,str(error))
    return redirect('sheets_write_review',pk=pk)
