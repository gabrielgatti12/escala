"""Read-only Google Sheets adapter: explicit hours only; rotations retain original slots."""
import hashlib
import re
from datetime import datetime, time, timedelta
from io import BytesIO
from urllib.request import urlopen
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify
from openpyxl import load_workbook
from .models import Shift, Exchange, RotationActivity, SyncState, SharedActivity, Allocation

HOURS = re.compile(r'^\s*(\d{1,2})(?::(\d{2}))?\s*[-–]\s*(\d{1,2})(?::(\d{2}))?\s*$')
GROUP = re.compile(r'^(R[123]|C[123][ab])\s*-\s*[A-Z]+$',re.I)

def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:32]

def resident_for(name):
    name=' '.join(str(name).split())
    # Full names are imported as identities; accounts have no usable password until provisioned.
    username=f'{slugify(name)[:100]}-{digest(name.casefold())[:8]}'
    user, created=get_user_model().objects.get_or_create(username=username,defaults={'first_name':name[:150]})
    if created:
        user.set_unusable_password()
        user.save(update_fields=['password'])
    return user

def fetch_workbook(sheet_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,100}',sheet_id):
        raise ValueError('Identificador do Google Sheets inválido.')
    with urlopen(f'https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx',timeout=45) as response:
        data=response.read(20*1024*1024+1)
    if len(data)>20*1024*1024:
        raise ValueError('Planilha excede o limite de 20 MB.')
    return load_workbook(BytesIO(data),read_only=True,data_only=True)

@transaction.atomic
def sync_workbook(book, source):
    state,_=SyncState.objects.get_or_create(source=source)
    state=SyncState.objects.select_for_update().get(pk=state.pk)
    if 'Espelho Semanal' not in book.sheetnames:
        raise ValueError('Aba Espelho Semanal ausente; nada foi alterado.')
    rows=list(book['Espelho Semanal'].values)
    if not rows:
        raise ValueError('Espelho Semanal vazio.')
    days=[(i,v.date()) for i,v in enumerate(rows[0]) if isinstance(v,datetime)]
    if not days:
        raise ValueError('Datas não reconhecidas; nada foi alterado.')
    seen=[]; shared_seen=[]; occurrences={}; preserved=0
    for col,day in days:
        sector=''; location=''
        for row in rows[2:]:
            def cell(offset): return row[col+offset] if len(row)>col+offset else None
            if row and row[0]: location=str(row[0]).strip()
            if cell(0) and not cell(1) and not GROUP.fullmatch(str(cell(0))):
                sector=str(cell(0)).strip()
            match=HOURS.fullmatch(str(cell(1) or ''))
            if not match: continue
            h,m,eh,em=match.groups()
            start=timezone.make_aware(datetime.combine(day,time(int(h),int(m or 0))))
            end=timezone.make_aware(datetime.combine(day,time(int(eh),int(em or 0))))
            if end<=start: end+=timedelta(days=1)
            names=[cell(offset).strip() for offset in [4,5] if isinstance(cell(offset),str) and cell(offset).strip()]
            if len(set(names))>1:
                users=[resident_for(name) for name in sorted(set(names))]
                label=f'{location} · {sector}'[:120]
                key=f'{day}|{start.isoformat()}|{end.isoformat()}|{label}|'+','.join(u.username for u in users)
                ext=f'{source[:40]}:{digest(key)}'
                shared_seen.append(ext)
                activity,_=SharedActivity.objects.update_or_create(external_id=ext,defaults={'source':source,'active':True,'starts_at':start,'ends_at':end,'sector':label})
                activity.residents.set(users)
                continue
            for offset in [4,5]:
                name=cell(offset)
                if not isinstance(name,str) or not name.strip(): continue
                user=resident_for(name)
                label=f'{location} · {sector}'[:120]
                key=f'{day}|{start.isoformat()}|{end.isoformat()}|{user.username}|{label}'
                occurrences[key]=occurrences.get(key,0)+1
                ext=f'{source[:40]}:{digest(key+str(occurrences[key]))}'
                seen.append(ext)
                shift=Shift.objects.select_for_update().filter(external_id=ext).first()
                if shift and Exchange.objects.filter(status='accepted').filter(Q(offered=shift)|Q(requested=shift)).exists():
                    preserved+=1
                    continue
                Shift.objects.update_or_create(external_id=ext,defaults={'source':source,'active':True,'resident':user,'starts_at':start,'ends_at':end,'sector':label})
    if not seen and not shared_seen:
        raise ValueError('Nenhum horário reconhecido; sincronização cancelada para preservar os dados.')
    removed_shared=list(SharedActivity.objects.filter(source=source,allocated_to__isnull=True).exclude(external_id__in=shared_seen).values_list('pk',flat=True))
    SharedActivity.objects.filter(pk__in=removed_shared).update(active=False)
    Allocation.objects.filter(status='pending').filter(Q(mine_id__in=removed_shared)|Q(theirs_id__in=removed_shared)).update(status='cancelled',resolved_at=timezone.now())
    assigned_obsolete=SharedActivity.objects.filter(source=source,allocated_to__isnull=False).exclude(external_id__in=shared_seen).count()
    obsolete=Shift.objects.filter(source=source).exclude(external_id__in=seen)
    protected=list(Exchange.objects.filter(status='accepted').values_list('offered_id','requested_id'))
    protected_ids={pk for pair in protected for pk in pair}
    # Accepted exchanges remain available for manual reconciliation when their source changes.
    retained=obsolete.filter(pk__in=protected_ids).count()
    removed=list(obsolete.exclude(pk__in=protected_ids).values_list('pk',flat=True))
    obsolete.exclude(pk__in=protected_ids).update(active=False)
    Exchange.objects.filter(status='pending').filter(Q(offered_id__in=removed)|Q(requested_id__in=removed)).update(status='cancelled',resolved_at=timezone.now())
    rotation_ids=[]
    for title in book.sheetnames:
        if not re.fullmatch(r'R[123]|C[123][ab]',title,re.I): continue
        data=list(book[title].values)
        if not data or len(data[0])<5: continue
        first,last=data[0][3:5]
        if not isinstance(first,datetime) or not isinstance(last,datetime):
            raise ValueError(f'Período de rotação inválido: {title}')
        for index,row in enumerate(data):
            if len(row)<2 or not GROUP.fullmatch(str(row[0] or '')) or not row[1]: continue
            user=resident_for(row[1]); day=first.date()
            while day<=last.date():
                if day.weekday()<5:
                    for slot in range(2):
                        activity_row=data[index+slot+1] if index+slot+1<len(data) else ()
                        activity=activity_row[day.weekday()] if len(activity_row)>day.weekday() else None
                        if not activity: continue
                        key=f'{source}:{title}:{row[0]}:{day}:{slot}'
                        ext=digest(key);rotation_ids.append(ext)
                        RotationActivity.objects.update_or_create(external_id=ext,defaults={'source':source,'resident':user,'date':day,'period':f'Linha {slot+1}','activity':str(activity)[:250],'group':title})
                day+=timedelta(days=1)
    RotationActivity.objects.filter(source=source).exclude(external_id__in=rotation_ids).delete()
    state.last_success=timezone.now()
    pending=SharedActivity.objects.filter(source=source,active=True,allocated_to__isnull=True).count()
    state.summary=f'{len(seen)} plantões definidos; {pending} alocações pendentes; {len(rotation_ids)} referências de rotação; {preserved+retained} trocas preservadas; {retained+assigned_obsolete} exigem conciliação.'
    state.save()
    return state.summary
