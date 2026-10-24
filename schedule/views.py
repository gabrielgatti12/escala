from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import render,redirect,get_object_or_404
from django.views.decorators.http import require_POST
from django.utils import timezone
from .models import Shift,Exchange,RotationActivity,SyncState,SharedActivity,Allocation,IncompleteActivity
from .services import propose_exchange, respond_exchange
from .identity import resident_for_account
@login_required
def dashboard(request):
    from django.core.paginator import Paginator
    from django.contrib.auth import get_user_model
    from datetime import date
    shifts=Shift.objects.filter(active=True).select_related('resident')
    rotations=RotationActivity.objects.none()
    shared=SharedActivity.objects.prefetch_related('residents').all()
    filters={}
    for key,lookup in [('from','starts_at__date__gte'),('to','starts_at__date__lte')]:
        value=request.GET.get(key,'')
        if value:
            try:
                filters[lookup]=date.fromisoformat(value)
                rotations=rotations.filter(**{'date__gte' if key=='from' else 'date__lte':filters[lookup]})
            except ValueError:
                messages.error(request,'Informe datas válidas.')
    shifts=shifts.filter(**filters)
    shared=shared.filter(**filters)
    resident=request.GET.get('resident','')
    if resident.isdigit():
        shifts=shifts.filter(resident_id=int(resident))
        rotations=rotations.filter(resident_id=int(resident))
        shared=shared.filter(residents__pk=int(resident))
    if request.GET.get('interest')=='1':
        shifts=shifts.filter(wants_exchange=True)
        rotations=rotations.none()
        shared=shared.none()
    future=Shift.objects.filter(active=True,starts_at__gt=timezone.now()).select_related('resident')
    from django.db.models import Min,Max
    from datetime import timedelta
    person=resident_for_account(request.user)
    bounds=Shift.objects.filter(active=True).aggregate(first=Min('starts_at'),last=Max('ends_at'))
    today=timezone.localdate()
    try:
        selected=date.fromisoformat(request.GET.get('week',today.isoformat()))
    except ValueError:
        selected=today
    week_start=selected-timedelta(days=selected.weekday())
    week_end=week_start+timedelta(days=7)
    weekly=shifts.filter(starts_at__date__gte=week_start,starts_at__date__lt=week_end)
    if request.GET.get('scope','mine')!='all': weekly=weekly.filter(resident=person)
    weekly_shared=shared.filter(active=True,allocated_to__isnull=True,starts_at__date__gte=week_start,starts_at__date__lt=week_end)
    incomplete=IncompleteActivity.objects.filter(date__gte=week_start,date__lt=week_end).prefetch_related('residents')
    if request.GET.get('scope','mine')!='all':
        weekly_shared=weekly_shared.filter(residents=person)
        incomplete=incomplete.filter(residents=person)
    if resident.isdigit(): incomplete=incomplete.filter(residents__pk=int(resident))
    if request.GET.get('interest')=='1': incomplete=incomplete.none()
    weekly_days=[]
    grouped={}
    for shift in weekly:
        grouped.setdefault(timezone.localtime(shift.starts_at).date(),[]).append(shift)
    unassigned={}
    for activity in weekly_shared: unassigned.setdefault(timezone.localtime(activity.starts_at).date(),[]).append(activity)
    undated={}
    for activity in incomplete: undated.setdefault(activity.date,[]).append(activity)
    for offset in range(7):
        day=week_start+timedelta(days=offset)
        weekly_days.append((day,grouped.get(day,[]),unassigned.get(day,[]),undated.get(day,[])))
    resident_choices=get_user_model().objects.filter(Q(shift__active=True)|Q(sharedactivity__active=True)|Q(incompleteactivity__isnull=False)).distinct().order_by('first_name','username')
    return render(request,'dashboard.html',{
        'resident_user':person,'week_days':weekly_days,'week_start':week_start,'week_previous':week_start-timedelta(days=7),'week_next':week_end,'bounds':bounds,'future_count':future.count(),'has_offered':future.filter(resident=person).exists(),'has_requested':future.exclude(resident=person).exists(),'resident_choices':resident_choices,'linked':person.pk!=request.user.pk,'mine':shifts.filter(resident=person),'now':timezone.now(),
        'shared':shared.filter(active=True,allocated_to__isnull=True),'my_shared':SharedActivity.objects.filter(active=True,allocated_to__isnull=True,residents=resident_for_account(request.user)),'allocations':Allocation.objects.filter(Q(proposer=resident_for_account(request.user))|Q(recipient=resident_for_account(request.user))).select_related('mine','theirs','proposer','recipient')[:100],
        'offered':future.filter(resident=resident_for_account(request.user)),
        'requested':future.exclude(resident=resident_for_account(request.user)),
        'others':Paginator(shifts.exclude(resident=resident_for_account(request.user)),50).get_page(request.GET.get('page')),
        'rotations':Paginator(rotations,100).get_page(request.GET.get('rotation_page')),
        'residents':resident_choices.filter(is_active=True),
        'syncs':SyncState.objects.all(),
        'exchanges':Exchange.objects.filter(Q(proposer=resident_for_account(request.user))|Q(recipient=resident_for_account(request.user))).select_related('offered','requested','proposer','recipient')[:100]})
@login_required
@require_POST
def propose(request):
    try:
        propose_exchange(resident_for_account(request.user),int(request.POST.get('offered','')),int(request.POST.get('requested','')))
        messages.success(request,'Solicitação enviada. Falta a confirmação do outro residente.')
    except (ValidationError,ValueError) as error:
        messages.error(request,'; '.join(error.messages) if isinstance(error,ValidationError) else 'Escolha dois plantões válidos.')
    return redirect('dashboard')
@login_required
@require_POST
def respond(request,pk,action):
    get_object_or_404(Exchange.objects.filter(Q(proposer=resident_for_account(request.user))|Q(recipient=resident_for_account(request.user))),pk=pk)
    try:
        respond_exchange(resident_for_account(request.user),pk,action)
        messages.success(request,'Solicitação atualizada.')
    except ValidationError as error:
        messages.error(request,'; '.join(error.messages))
    return redirect('dashboard')

@login_required
@require_POST
def toggle_interest(request, pk):
    shift = get_object_or_404(Shift, pk=pk,resident=resident_for_account(request.user),active=True,starts_at__gt=timezone.now())
    shift.wants_exchange = not shift.wants_exchange
    shift.save(update_fields=['wants_exchange'])
    return redirect('dashboard')

@login_required
@require_POST
def allocation_propose(request):
    from .services import propose_allocation
    try:
        propose_allocation(resident_for_account(request.user),int(request.POST.get('mine','')),int(request.POST.get('theirs','')))
        messages.success(request,'Divisão proposta. Aguarde o aceite do colega para atualizar o calendário.')
    except (ValidationError,ValueError) as error:
        messages.error(request,'; '.join(error.messages) if isinstance(error,ValidationError) else 'Escolha duas atividades válidas.')
    return redirect('dashboard')

@login_required
@require_POST
def allocation_respond(request,pk,action):
    from .models import Allocation
    from .services import respond_allocation
    get_object_or_404(Allocation.objects.filter(Q(proposer=resident_for_account(request.user))|Q(recipient=resident_for_account(request.user))),pk=pk)
    try:
        respond_allocation(resident_for_account(request.user),pk,action)
        messages.success(request,'Divisão atualizada. As definições aceitas já aparecem no calendário final.')
    except ValidationError as error: messages.error(request,'; '.join(error.messages))
    return redirect('dashboard')

@login_required
def calendar_view(request):
    import calendar
    from datetime import date
    today=timezone.localdate()
    try:
        month=date.fromisoformat(request.GET.get('month',today.strftime('%Y-%m'))+'-01')
    except ValueError: month=today.replace(day=1)
    shifts=Shift.objects.filter(active=True,starts_at__year=month.year,starts_at__month=month.month).select_related('resident')
    if request.GET.get('all')!='1': shifts=shifts.filter(resident=resident_for_account(request.user))
    days={}
    for shift in shifts:
        day=timezone.localtime(shift.starts_at).day
        days.setdefault(day,[]).append(shift)
    weeks=[[(day,days.get(day,[])) for day in week] for week in calendar.monthcalendar(month.year,month.month)]
    return render(request,'calendar.html',{'weeks':weeks,'month':month})

@login_required
def calendar_ics(request):
    from django.http import HttpResponse
    from datetime import timezone as utc
    def escape(text):
        return text.replace('\\','\\\\').replace('\n','\\n').replace(';','\\;').replace(',','\\,').replace('\r','')
    lines=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//Escala//Residencia//PT','CALSCALE:GREGORIAN']
    for shift in Shift.objects.filter(active=True,resident=resident_for_account(request.user)):
        lines += ['BEGIN:VEVENT',f'UID:shift-{shift.pk}@escala',f'DTSTAMP:{timezone.now().astimezone(utc.utc):%Y%m%dT%H%M%SZ}',f'DTSTART:{shift.starts_at.astimezone(utc.utc):%Y%m%dT%H%M%SZ}',f'DTEND:{shift.ends_at.astimezone(utc.utc):%Y%m%dT%H%M%SZ}',f'SUMMARY:{escape(shift.sector)}','END:VEVENT']
    lines+=['END:VCALENDAR']
    response=HttpResponse('\r\n'.join(lines)+'\r\n',content_type='text/calendar; charset=utf-8')
    response['Content-Disposition']='attachment; filename="minha-escala.ics"'
    return response

@login_required
@require_POST
def link_resident(request):
    from django.http import HttpResponseForbidden
    from django.contrib.auth import get_user_model
    from django.db import IntegrityError, transaction
    from .models import ResidentAccess
    if not request.user.is_superuser:
        return HttpResponseForbidden('Somente o administrador pode vincular uma conta.')
    try:
        pk=int(request.POST.get('resident',''))
    except ValueError:
        messages.error(request,'Selecione seu nome de residente.')
        return redirect('dashboard')
    target=get_object_or_404(get_user_model().objects.filter(Q(shift__active=True)|Q(sharedactivity__active=True)|Q(incompleteactivity__isnull=False)).distinct(),pk=pk)
    try:
        with transaction.atomic():
            if ResidentAccess.objects.filter(resident=target).exclude(account=request.user).exists():
                raise ValidationError('Este residente já está vinculado a outra conta.')
            if target.pk==request.user.pk:
                ResidentAccess.objects.filter(account=request.user).delete()
            else:
                ResidentAccess.objects.update_or_create(account=request.user,defaults={'resident':target})
        messages.success(request,'Sua conta foi vinculada ao residente selecionado.')
    except (ValidationError,IntegrityError):
        messages.error(request,'Este residente já está vinculado a outra conta.')
    return redirect('dashboard')
