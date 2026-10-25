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
from openpyxl.utils import get_column_letter
from .models import Shift, Exchange, RotationActivity, SyncState, SharedActivity, Allocation, IncompleteActivity, is_rest_sector

HOURS = re.compile(r'^\s*(\d{1,2})(?::(\d{2}))?\s*[-–]\s*(\d{1,2})(?::(\d{2}))?\s*$')
GROUP = re.compile(r'^(R[123]|C[123][ab])\s*-\s*[A-Z]+$',re.I)

class SheetFormatError(ValueError):
    """A known workbook layout problem with a safe, user-facing explanation."""

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

def parse_shift_hours(day,value):
    match=HOURS.fullmatch(str(value or ''))
    if not match: return None
    h,m,eh,em=match.groups();h=int(h);m=int(m or 0);eh=int(eh);em=int(em or 0)
    if h>23 or m>59 or eh>24 or em>59 or (eh==24 and em!=0): return None
    start=timezone.make_aware(datetime.combine(day,time(h,m)))
    end=timezone.make_aware(datetime.combine(day+timedelta(days=1) if eh==24 else day,time(eh%24,em)))
    if end<=start: end+=timedelta(days=1)
    return start,end

@transaction.atomic
def sync_workbook(book, source):
    state,_=SyncState.objects.get_or_create(source=source)
    state=SyncState.objects.select_for_update().get(pk=state.pk)
    if 'Espelho Semanal' not in book.sheetnames:
        raise SheetFormatError('Aba Espelho Semanal ausente; nada foi alterado.')
    rows=list(book['Espelho Semanal'].values)
    if not rows:
        raise SheetFormatError('Espelho Semanal vazio.')
    days=[(i,v.date()) for i,v in enumerate(rows[0]) if isinstance(v,datetime)]
    if not days:
        raise SheetFormatError('Datas não reconhecidas; nada foi alterado.')
    seen=[]; shared_seen=[]; incomplete_seen=[]; rest_count=0; occurrences={}; preserved=0
    for col,day in days:
        sector=''; location=''
        for row_number,row in enumerate(rows[2:],3):
            def cell(offset): return row[col+offset] if len(row)>col+offset else None
            if row and row[0]:
                location=str(row[0]).strip()
            match=HOURS.fullmatch(str(cell(1) or ''))
            has_names=any(isinstance(cell(offset),str) and cell(offset).strip() for offset in [4,5])
            source_cell=f'{get_column_letter(col+2)}{row_number}:{get_column_letter(col+6)}{row_number}'
            parsed=parse_shift_hours(day,cell(1))
            if match and not has_names: continue
            if parsed is None and has_names:
                if not sector:
                    raise SheetFormatError(f'Linha {row_number}: setor não identificado.')
                users=[resident_for(cell(offset)) for offset in [4,5] if isinstance(cell(offset),str) and cell(offset).strip()]
                key=f'{source}|{day}|{source_cell}|{sector}|'+','.join(u.username for u in users)
                external_id=digest(key)
                incomplete_seen.append(external_id)
                if is_rest_sector(sector): rest_count+=1
                activity,_=IncompleteActivity.objects.update_or_create(external_id=external_id,defaults={'source':source,'source_cell':source_cell,'date':day,'sector':f'{location} · {sector}'[:120],'raw_time':str(cell(1) or '')[:100]})
                activity.residents.set(users)
                continue
            if not match:
                # Section headings can be merged B:G, or C:G under R1/R2/R3.
                if not has_names:
                    if isinstance(cell(1),str) and cell(1).strip():
                        sector=cell(1).strip()
                    elif isinstance(cell(0),str) and cell(0).strip() and not re.fullmatch(r'R[123]|C[123][ab]?',cell(0).strip(),re.I):
                        sector=cell(0).strip()
                continue
            if has_names and not sector:
                raise SheetFormatError(f'Linha {row_number}: setor não identificado acima dos nomes; importação cancelada.')
            source_cell=f'{get_column_letter(col+2)}{row_number}:{get_column_letter(col+6)}{row_number}'
            start,end=parsed
            names=[cell(offset).strip() for offset in [4,5] if isinstance(cell(offset),str) and cell(offset).strip()]
            # F/G may repeat the same person, including differences in spacing
            # or capitalization. Resolve identities before deciding it is a pair.
            users=list({user.pk:user for user in (resident_for(name) for name in sorted(set(names)))}.values())
            if len(users)>1:
                label=f'{location} · {sector}'[:120]
                key=f'{day}|{start.isoformat()}|{end.isoformat()}|{label}|'+','.join(u.username for u in users)
                ext=f'{source[:40]}:{digest(key)}'
                shared_seen.append(ext)
                activity,_=SharedActivity.objects.update_or_create(external_id=ext,defaults={'source':source,'active':True,'source_cell':source_cell,'starts_at':start,'ends_at':end,'sector':label})
                activity.residents.set(users)
                continue
            for user in users:
                label=f'{location} · {sector}'[:120]
                key=f'{day}|{start.isoformat()}|{end.isoformat()}|{user.username}|{label}'
                occurrences[key]=occurrences.get(key,0)+1
                ext=f'{source[:40]}:{digest(key+str(occurrences[key]))}'
                seen.append(ext)
                shift=Shift.objects.select_for_update().filter(external_id=ext).first()
                if shift and Exchange.objects.filter(status='accepted').filter(Q(offered=shift)|Q(requested=shift)).exists():
                    preserved+=1
                    continue
                Shift.objects.update_or_create(external_id=ext,defaults={'source':source,'active':True,'source_cell':source_cell,'resident':user,'starts_at':start,'ends_at':end,'sector':label})
    if not seen and not shared_seen and not incomplete_seen:
        raise SheetFormatError('Nenhum horário reconhecido; sincronização cancelada para preservar os dados.')
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
    IncompleteActivity.objects.filter(source=source).exclude(external_id__in=incomplete_seen).delete()
    # The weekly mirror is authoritative. Do not fabricate daily assignments
    # by expanding reference rotations over their entire date ranges.
    RotationActivity.objects.filter(source=source).delete()
    state.last_success=timezone.now()
    pending=SharedActivity.objects.filter(source=source,active=True,allocated_to__isnull=True).count()
    state.summary=f'{len(seen)} plantões definidos; {pending} alocações pendentes; {len(incomplete_seen)-rest_count} sem horário válido; {rest_count} registros de pós-plantão; {preserved+retained} trocas preservadas; {retained+assigned_obsolete} exigem conciliação.'
    state.save()
    return state.summary

def workbook_week_label(book):
    row=next(book['Espelho Semanal'].iter_rows(min_row=1,max_row=1,values_only=True),())
    dates=[value.date() for value in row if isinstance(value,datetime)]
    if not dates: return 'Planilha semanal'
    first,last=min(dates),max(dates)
    if (last-first).days<=6:
        first=first-timedelta(days=first.weekday())
        last=first+timedelta(days=6)
    return f'Semana de {first:%d/%m} a {last:%d/%m/%Y}'
