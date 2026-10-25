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
    from datetime import date,timedelta
    from django.contrib.auth import get_user_model
    from django.db.models import Count,Min,Max
    from django.http import HttpResponseForbidden
    from .models import SheetSource,DriveFolder
    person=resident_for_account(request.user)
    now=timezone.now()
    today=timezone.localdate()
    tab=request.GET.get('tab','week')
    if tab not in ['week','exchanges','sources']: tab='week'
    if tab=='sources' and not request.user.is_superuser:
        return HttpResponseForbidden('Somente o administrador pode gerenciar fontes.')
    scope='all' if request.GET.get('scope')=='all' else 'mine'
    active=Shift.objects.filter(active=True).select_related('resident')
    future=active.filter(starts_at__gt=now)
    upcoming=future.filter(resident=person).first() or future.first()
    default_day=timezone.localtime(upcoming.starts_at).date() if upcoming else today
    try:
        selected=date.fromisoformat(request.GET.get('week',default_day.isoformat()))
    except ValueError:
        selected=default_day
    week_start=selected-timedelta(days=selected.weekday())
    week_next=week_start+timedelta(days=7)
    window={'starts_at__date__gte':week_start,'starts_at__date__lt':week_next}
    weekly=active.filter(**window)
    shared=SharedActivity.objects.filter(active=True,allocated_to__isnull=True).prefetch_related('residents')
    weekly_shared=shared.filter(**window)
    incomplete=IncompleteActivity.objects.filter(date__gte=week_start,date__lt=week_next).prefetch_related('residents')
    own_week_count=weekly.filter(resident=person).count()
    own_shared_count=weekly_shared.filter(residents=person).count()
    if scope=='mine':
        weekly=weekly.filter(resident=person)
        weekly_shared=weekly_shared.filter(residents=person)
        incomplete=incomplete.filter(residents=person)
    resident_id=request.GET.get('resident','')
    if resident_id.isdigit():
        weekly=weekly.filter(resident_id=int(resident_id))
        weekly_shared=weekly_shared.filter(residents__pk=int(resident_id))
        incomplete=incomplete.filter(residents__pk=int(resident_id))
    if request.GET.get('interest')=='1':
        weekly=weekly.filter(wants_exchange=True)
        weekly_shared=weekly_shared.none()
        incomplete=incomplete.none()
    grouped={};pending={};missing={}
    for item in weekly: grouped.setdefault(timezone.localtime(item.starts_at).date(),[]).append(item)
    for item in weekly_shared: pending.setdefault(timezone.localtime(item.starts_at).date(),[]).append(item)
    for item in incomplete: missing.setdefault(item.date,[]).append(item)
    days=[]
    for offset in range(7):
        day=week_start+timedelta(days=offset)
        shifts=grouped.get(day,[]);unassigned=pending.get(day,[]);no_time=missing.get(day,[])
        days.append({'date':day,'shifts':shifts,'shared':unassigned,'incomplete':no_time,'today':day==today,'weekend':offset>4,'count':len(shifts)+len(unassigned)+len(no_time)})
    residents=get_user_model().objects.filter(is_active=True).filter(Q(shift__active=True)|Q(sharedactivity__active=True)|Q(incompleteactivity__isnull=False)).distinct().order_by('first_name','username')
    exchanges=Exchange.objects.filter(Q(proposer=person)|Q(recipient=person)).select_related('offered','requested','proposer','recipient')
    allocations=Allocation.objects.filter(Q(proposer=person)|Q(recipient=person)).select_related('mine','theirs','proposer','recipient').order_by('-created_at')
    pending_count=exchanges.filter(status='pending').count()+allocations.filter(status='pending').count()
    received_count=exchanges.filter(status='pending',recipient=person).count()+allocations.filter(status='pending',recipient=person).count()
    my_shared=list(shared.filter(residents=person))
    candidates=[{'activity':item,'pair':','.join(str(pk) for pk in sorted(u.pk for u in item.residents.all())),'day':timezone.localtime(item.starts_at).date().isoformat(),'start':item.starts_at.isoformat(),'end':item.ends_at.isoformat()} for item in my_shared if len(item.residents.all())==2]
    allocation_options=[option for option in candidates if any(other['activity'].pk!=option['activity'].pk and other['pair']==option['pair'] and other['day']==option['day'] and option['activity'].starts_at<other['activity'].ends_at and other['activity'].starts_at<option['activity'].ends_at for other in candidates)]
    allocation_eligible_ids={option['activity'].pk for option in allocation_options}
    my_shared=[option['activity'] for option in allocation_options]
    states={state.source:state for state in SyncState.objects.all()}
    source_cards=[]
    if request.user.is_superuser:
        totals={item['source']:item['total'] for item in active.values('source').annotate(total=Count('pk'))}
        for source in SheetSource.objects.select_related('folder').all().order_by('name','pk'):
            source_cards.append({'source':source,'state':states.get(source.spreadsheet_id),'count':totals.get(source.spreadsheet_id,0)})
    bounds=active.aggregate(first=Min('starts_at'),last=Max('ends_at'))
    return render(request,'dashboard.html',{
        'tab':tab,'scope':scope,'resident_user':person,'linked':person.pk!=request.user.pk,'resident_choices':residents,'residents':residents,
        'days':days,'week_start':week_start,'week_last':week_next-timedelta(days=1),'week_previous':week_start-timedelta(days=7),'week_next':week_next,'today':today,'now':now,
        'own_week_count':own_week_count,'own_shared_count':own_shared_count,'pending_count':pending_count,'received_count':received_count,
        'future_count':future.count(),'has_offered':future.filter(resident=person).exists(),'has_requested':future.exclude(resident=person).exists(),
        'offered':future.filter(resident=person),'requested':future.exclude(resident=person),'bounds':bounds,
        'exchanges':exchanges[:100],'recent_exchanges':exchanges[:4],'allocations':allocations[:100],'my_shared':my_shared,'allocation_options':allocation_options,'allocation_eligible_ids':allocation_eligible_ids,
        'latest_sync':SyncState.objects.order_by('-last_success').first(),'source_cards':source_cards,'drive_folders':DriveFolder.objects.all() if request.user.is_superuser else [],
    })
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
    person=resident_for_account(request.user)
    get_object_or_404(Shift,pk=pk,resident=person,active=True,starts_at__gt=timezone.now())
    from .services import set_exchange_interest
    try:
        topic=set_exchange_interest(person,pk)
        if topic:
            messages.success(request,'Publicado no mural! Todos os residentes podem ver e propor uma troca.')
            return redirect('forum_topic',pk=topic.pk)
        messages.success(request,'Interesse retirado do mural.')
    except ValidationError as error:
        messages.error(request,'; '.join(error.messages))
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

@login_required
@require_POST
def register_source(request):
    from django.http import HttpResponseForbidden
    from urllib.parse import urlparse
    import re
    from .models import SheetSource
    if not request.user.is_superuser:
        return HttpResponseForbidden('Somente o administrador pode adicionar planilhas.')
    value=request.POST.get('url','').strip()
    parsed=urlparse(value)
    if parsed.scheme:
        match=re.fullmatch(r'/spreadsheets/d/([A-Za-z0-9_-]{20,100})(?:/.*)?',parsed.path)
        sheet_id=match.group(1) if parsed.scheme=='https' and parsed.hostname=='docs.google.com' and match else ''
    else: sheet_id=value if re.fullmatch(r'[A-Za-z0-9_-]{20,100}',value) else ''
    if not sheet_id:
        messages.error(request,'Cole um link válido de uma planilha Google Sheets.')
    else:
        source,created=SheetSource.objects.get_or_create(spreadsheet_id=sheet_id,defaults={'name':request.POST.get('name','').strip()[:200]})
        if not source.enabled:
            source.enabled=True;source.save(update_fields=['enabled'])
        messages.success(request,'Planilha cadastrada. A sincronização acontece no próximo ciclo, em até 5 minutos.')
    return redirect('/?tab=sources')


def forum_exchange_context(person):
    future=Shift.objects.filter(active=True,starts_at__gt=timezone.now()).select_related('resident')
    return {'resident_user':person,'tab':'forum','offered':future.filter(resident=person),'requested':future.exclude(resident=person),'future_count':future.count(),'has_offered':future.filter(resident=person).exists(),'has_requested':future.exclude(resident=person).exists()}

@login_required
def forum(request):
    from django.core.paginator import Paginator
    from django.db.models import F,Count
    from .models import ExchangeTopic
    person=resident_for_account(request.user)
    open_topics=ExchangeTopic.objects.filter(closed_at__isnull=True,shift__active=True,shift__wants_exchange=True,shift__starts_at__gt=timezone.now(),poster_id=F('shift__resident_id'))
    mine=request.GET.get('mine')=='1'
    topics=ExchangeTopic.objects.filter(poster=person) if mine else open_topics
    sector=request.GET.get('sector','')
    if sector: topics=topics.filter(shift__sector=sector)
    shift_id=request.GET.get('shift','')
    if shift_id.isdigit(): topics=topics.filter(shift_id=int(shift_id))
    context=forum_exchange_context(person)
    context.update({'topics':Paginator(topics.select_related('poster','shift__resident').annotate(comment_count=Count('comments')).order_by('-published_at','-pk'),12).get_page(request.GET.get('page')),'mine':mine,'open_count':open_topics.count(),'own_count':open_topics.filter(poster=person).count(),'sectors':open_topics.order_by('shift__sector').values_list('shift__sector',flat=True).distinct()})
    return render(request,'forum.html',context)

@login_required
def forum_topic(request,pk):
    from django.core.paginator import Paginator
    from .models import ExchangeTopic
    topic=get_object_or_404(ExchangeTopic.objects.select_related('poster','shift__resident'),pk=pk)
    person=resident_for_account(request.user)
    context=forum_exchange_context(person)
    context.update({'topic':topic,'topic_open':topic.is_open,'comments':Paginator(topic.comments.select_related('author'),30).get_page(request.GET.get('page'))})
    return render(request,'forum_topic.html',context)

@login_required
@require_POST
def forum_comment(request,pk):
    from .models import ExchangeTopic
    from .services import add_forum_comment
    get_object_or_404(ExchangeTopic,pk=pk)
    try:
        comment=add_forum_comment(resident_for_account(request.user),pk,request.POST.get('text',''))
        messages.success(request,'Comentário publicado.')
        from django.urls import reverse
        page=(comment.topic.comments.count()+29)//30
        return redirect(reverse('forum_topic',args=[pk])+f'?page={page}#comment-{comment.pk}')
    except ValidationError as error:
        messages.error(request,'; '.join(error.messages))
    return redirect('forum_topic',pk=pk)

@login_required
@require_POST
def forum_withdraw(request,pk):
    from .models import ExchangeTopic
    from .services import withdraw_forum_topic
    person=resident_for_account(request.user)
    get_object_or_404(ExchangeTopic,pk=pk,poster=person)
    try:
        withdraw_forum_topic(person,pk)
        messages.success(request,'Publicação encerrada. O histórico da conversa foi mantido.')
    except ValidationError as error:
        messages.error(request,'; '.join(error.messages))
    return redirect('forum_topic',pk=pk)

@login_required
def live_status(request):
    from django.http import JsonResponse
    from .live import data_revision
    response=JsonResponse({'revision':data_revision()})
    response['Cache-Control']='private, no-store'
    return response
