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

def load_export(data, *, inspect_formulas=True):
    from .xlsx_safety import inspect_xlsx,validate_loaded_workbook,UnsafeWorkbookError
    try:
        extents=inspect_xlsx(data)
    except UnsafeWorkbookError as error:
        raise SheetFormatError(str(error)) from error
    book=load_workbook(BytesIO(data),read_only=True,data_only=True)
    try:
        validate_loaded_workbook(book,data,extents)
    except UnsafeWorkbookError as error:
        book.close()
        raise SheetFormatError(str(error)) from error
    if inspect_formulas:
        formulas=load_workbook(BytesIO(data),read_only=True,data_only=False)
        uncached={}
        try:
            for formula_sheet in formulas:
                cached_sheet=book[formula_sheet.title]
                budget=extents.get(getattr(formula_sheet,'_worksheet_path',None))
                empty_strings=budget.empty_formula_strings if budget else frozenset()
                missing=set()
                for formula_row,cached_row in zip(formula_sheet.iter_rows(),cached_sheet.iter_rows()):
                    for formula,cached in zip(formula_row,cached_row):
                        if formula.data_type=='f' and cached.value is None:
                            confirmed_empty=(cached.data_type=='str' and
                                             (formula.row,formula.column) in empty_strings)
                            if not confirmed_empty:missing.add(formula.coordinate)
                if missing:uncached[formula_sheet.title]=missing
        except Exception:
            book.close()
            raise
        finally:
            formulas.close()
        book._uncached_formula_cells=uncached
    return book

def fetch_workbook(sheet_id, *, inspect_formulas=True):
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,100}',sheet_id):
        raise ValueError('Identificador do Google Sheets inválido.')
    with urlopen(f'https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx',timeout=45) as response:
        data=response.read(20*1024*1024+1)
    if len(data)>20*1024*1024:
        raise ValueError('Planilha excede o limite de 20 MB.')
    return load_export(data,inspect_formulas=inspect_formulas)

def parse_shift_hours(day,value):
    match=HOURS.fullmatch(str(value or ''))
    if not match: return None
    h,m,eh,em=match.groups();h=int(h);m=int(m or 0);eh=int(eh);em=int(em or 0)
    if h>23 or m>59 or eh>24 or em>59 or (eh==24 and em!=0): return None
    start=timezone.make_aware(datetime.combine(day,time(h,m)))
    end=timezone.make_aware(datetime.combine(day+timedelta(days=1) if eh==24 else day,time(eh%24,em)))
    if end<=start: end+=timedelta(days=1)
    return start,end

def validate_weekly_input(book,rows,days):
    """Reject incomplete cached data before treating missing cells as removals."""
    columns={column for column,day in days}
    weekdays={'segunda','terca','quarta','quinta','sexta','sabado','domingo'}
    if len(rows)>1:
        columns.update(index for index,value in enumerate(rows[1])
                       if isinstance(value,str) and slugify(value).split('-')[0] in weekdays)
    # Weekly blocks have seven columns. Keep the existing dynamic date offsets,
    # and inspect missing blocks only when known days agree on that geometry.
    remainders={column%7 for column,day in days}
    if len(remainders)==1:
        width=max((len(row) for row in rows),default=0)
        columns.update(range(next(iter(remainders)),width,7))
    missing=getattr(book,'_uncached_formula_cells',{}).get('Espelho Semanal',set())
    for column in sorted(columns):
        def value(row,offset):return row[column+offset] if len(row)>column+offset else None
        names_present=any(isinstance(value(row,offset),str) and value(row,offset).strip()
                          for row in rows[2:] for offset in [4,5])
        day_value=rows[0][column] if len(rows[0])>column else None
        if names_present and not isinstance(day_value,datetime):
            cell=f'{get_column_letter(column+1)}1'
            raise SheetFormatError(f'Data ausente ou inválida em {cell} para um dia com residentes; dados anteriores preservados.')
        for row_number,row in enumerate(rows[2:],3):
            raw_hours=value(row,1)
            has_names=any(isinstance(value(row,offset),str) and value(row,offset).strip() for offset in [4,5])
            hour_cell=f'{get_column_letter(column+2)}{row_number}'
            if has_names and (hour_cell in missing or (isinstance(raw_hours,str) and raw_hours.startswith('='))):
                raise SheetFormatError(f'Fórmula sem horário confirmado em {hour_cell}; dados anteriores preservados.')
            for offset in [4,5]:
                name=value(row,offset)
                name_cell=f'{get_column_letter(column+offset+1)}{row_number}'
                if (HOURS.fullmatch(str(raw_hours or '')) or has_names) and (name_cell in missing or (isinstance(name,str) and name.startswith('='))):
                    raise SheetFormatError(f'Fórmula sem nome confirmado em {name_cell}; dados anteriores preservados.')

@transaction.atomic
def sync_workbook(book, source):
    state,_=SyncState.objects.get_or_create(source=source)
    state=SyncState.objects.select_for_update().get(pk=state.pk)
    from .changes import snapshot_source,notify_schedule_changes
    previous_success=state.last_success
    snapshot_time=timezone.now()
    before=snapshot_source(source,now=snapshot_time) if previous_success else {}
    if 'Espelho Semanal' not in book.sheetnames:
        raise SheetFormatError('Aba Espelho Semanal ausente; nada foi alterado.')
    from .source_metadata import theme_palette,heading_colors,right_note,update_metadata
    cell_rows=list(book['Espelho Semanal'].iter_rows())
    rows=[[cell.value for cell in row] for row in cell_rows]
    palette=theme_palette(book)
    if not rows:
        raise SheetFormatError('Espelho Semanal vazio.')
    days=[(i,v.date()) for i,v in enumerate(rows[0]) if isinstance(v,datetime)]
    if not days:
        raise SheetFormatError('Datas não reconhecidas; nada foi alterado.')
    validate_weekly_input(book,rows,days)
    from collections import defaultdict
    from .bundle_integrations import accepted_bundle_shift_ids,bundle_former_owners,cancel_pending_bundles
    bundle_ids=accepted_bundle_shift_ids(source)
    accepted_slots=defaultdict(list)
    accepted_source_ids=set()
    for decision in Shift.objects.filter(source=source).filter(Q(offers__status='accepted')|Q(requests__status='accepted')|Q(pk__in=bundle_ids)).distinct():
        accepted_source_ids.add(decision.pk)
        if decision.active:
            accepted_slots[(decision.source_cell,decision.starts_at,decision.ends_at,decision.sector)].append(decision)
    former_owners=bundle_former_owners(source)
    for offered_id,requested_id,proposer_id,recipient_id in Exchange.objects.filter(status='accepted').filter(
        Q(offered_id__in=accepted_source_ids)|Q(requested_id__in=accepted_source_ids)
    ).values_list('offered_id','requested_id','proposer_id','recipient_id'):
        former_owners[offered_id].add(proposer_id)
        former_owners[requested_id].add(recipient_id)
    from .sheets_reconciliation import reverted_identity_slots,source_conflict_shift_ids
    source_conflicts=source_conflict_shift_ids(source)
    reverted_slots=defaultdict(list)
    pending_reverted_cells=defaultdict(list)
    for restored in reverted_identity_slots(source):
        reverted_slots[(restored.source_cell,restored.starts_at,restored.ends_at,restored.sector)].append(restored)
        if (restored.source_snapshot or {}).get('google_revert_export_pending'):
            pending_reverted_cells[restored.source_cell].append(restored)
    allocated_metadata=defaultdict(list)
    for allocation_id,mine_id,theirs_id in Allocation.objects.filter(status='accepted').filter(
        Q(mine__source=source)|Q(theirs__source=source)
    ).values_list('pk','mine_id','theirs_id'):
        for activity_id in [mine_id,theirs_id]:
            allocated_metadata[activity_id].append(f'allocation:{allocation_id}:{activity_id}')
    seen=[]; shared_seen=[]; incomplete_seen=[]; rest_count=0; occurrences={}; preserved=0; conflict_ids=set()
    for col,day in days:
        sector=''; location='';sector_colors={'source_background':'','source_foreground':''}
        for row_number,row in enumerate(rows[2:],3):
            def cell(offset): return row[col+offset] if len(row)>col+offset else None
            if row and row[0]:
                location=str(row[0]).strip()
            match=HOURS.fullmatch(str(cell(1) or ''))
            has_names=any(isinstance(cell(offset),str) and cell(offset).strip() for offset in [4,5])
            source_cell=f'{get_column_letter(col+2)}{row_number}:{get_column_letter(col+6)}{row_number}'
            parsed=parse_shift_hours(day,cell(1))
            metadata={**sector_colors,'notes':right_note(row,col)}
            pending_restored=pending_reverted_cells.get(source_cell,[])
            if pending_restored:
                parsed_people={resident_for(cell(offset)).pk for offset in [4,5]
                               if isinstance(cell(offset),str) and cell(offset).strip()}
                if (any((item.source_snapshot or {}).get('google_revert_check_conflict') for item in pending_restored)
                    or len(parsed_people)!=1 or parsed is None or any(
                    (item.starts_at,item.ends_at,item.sector)!=(parsed[0],parsed[1],f'{location} · {sector}'[:120])
                    for item in pending_restored
                )):
                    # A changed/partial export is not evidence to discard the
                    # identity of an undo already confirmed by the Google API.
                    for restored in pending_restored:
                        update_metadata(restored,metadata)
                        restored.requires_reconciliation=True;restored.wants_exchange=False
                        restored.save(update_fields=['requires_reconciliation','wants_exchange'])
                        seen.append(restored.external_id);conflict_ids.add(restored.pk)
                        preserved+=1
                    continue
            if match and not has_names: continue
            if parsed is None and has_names:
                if not sector:
                    raise SheetFormatError(f'Linha {row_number}: setor não identificado.')
                users=[resident_for(cell(offset)) for offset in [4,5] if isinstance(cell(offset),str) and cell(offset).strip()]
                key=f'{source}|{day}|{source_cell}|{sector}|'+','.join(u.username for u in users)
                external_id=digest(key)
                incomplete_seen.append(external_id)
                if is_rest_sector(sector): rest_count+=1
                activity,_=IncompleteActivity.objects.update_or_create(external_id=external_id,defaults={'source':source,'source_cell':source_cell,'date':day,'sector':f'{location} · {sector}'[:120],'raw_time':str(cell(1) or '')[:100],**metadata})
                activity.residents.set(users)
                continue
            if not match:
                # Section headings can be merged B:G, or C:G under R1/R2/R3.
                if not has_names:
                    if isinstance(cell(1),str) and cell(1).strip():
                        sector=cell(1).strip()
                        sector_colors=heading_colors(cell_rows[row_number-1][col+1],palette)
                    elif isinstance(cell(0),str) and cell(0).strip() and not re.fullmatch(r'R[123]|C[123][ab]?',cell(0).strip(),re.I):
                        sector=cell(0).strip()
                        sector_colors=heading_colors(cell_rows[row_number-1][col],palette)
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
                activity,_=SharedActivity.objects.update_or_create(external_id=ext,defaults={'source':source,'active':True,'source_cell':source_cell,'starts_at':start,'ends_at':end,'sector':label,**metadata})
                activity.residents.set(users)
                if activity.pk in allocated_metadata:
                    Shift.objects.filter(external_id__in=allocated_metadata[activity.pk]).update(**metadata)
                continue
            for user in users:
                label=f'{location} · {sector}'[:120]
                key=f'{day}|{start.isoformat()}|{end.isoformat()}|{user.username}|{label}'
                occurrences[key]=occurrences.get(key,0)+1
                ext=f'{source[:40]}:{digest(key+str(occurrences[key]))}'
                acknowledged=accepted_slots.get((source_cell,start,end,label),[])
                if len(acknowledged)>1:
                    raise SheetFormatError('Há decisões duplicadas para a mesma atividade; confira a conciliação.')
                decision=acknowledged[0] if acknowledged else None
                if decision:update_metadata(decision,metadata)
                if decision and user.pk!=decision.resident_id and user.pk in former_owners[decision.pk]:
                    # A cached/old source may still contain a former owner,
                    # including after a previous read acknowledged the swap.
                    seen.append(decision.external_id)
                    if decision.requires_reconciliation and decision.pk not in source_conflicts:
                        decision.requires_reconciliation=False;decision.save(update_fields=['requires_reconciliation'])
                    preserved+=1
                    continue
                if decision and user.pk!=decision.resident_id:
                    # A third owner at the same reviewed slot is a conflict,
                    # not permission to add a duplicate assignment beside it.
                    decision.requires_reconciliation=True;decision.wants_exchange=False
                    decision.save(update_fields=['requires_reconciliation','wants_exchange'])
                    conflict_ids.add(decision.pk)
                    seen.append(decision.external_id)
                    preserved+=1
                    continue
                restored=None
                if not decision:
                    reverted=reverted_slots.get((source_cell,start,end,label),[])
                    if len(reverted)>1:
                        raise SheetFormatError('Há identidades duplicadas para uma atividade desfeita; confira a conciliação.')
                    restored=reverted[0] if reverted else None
                    if restored and user.pk!=restored.resident_id:
                        snapshot=restored.source_snapshot or {}
                        if snapshot.get('google_revert_export_pending'):
                            # OAuth confirmed the undo, but Google's XLSX export
                            # can still contain the swapped name. Wait for the
                            # export to acknowledge the restored owner.
                            update_metadata(restored,metadata)
                            seen.append(restored.external_id)
                            if user.pk!=snapshot.get('google_revert_previous_owner'):
                                restored.requires_reconciliation=True
                                restored.wants_exchange=False
                                restored.save(update_fields=['requires_reconciliation','wants_exchange'])
                                conflict_ids.add(restored.pk)
                            preserved+=1
                            continue
                        restored=None  # Subsequent source edits remain authoritative.
                seen.append(ext)
                shift=Shift.objects.select_for_update().filter(external_id=ext).first()
                if decision and user.pk==decision.resident_id:
                    if shift and shift.pk!=decision.pk:
                        raise SheetFormatError('Há identidades duplicadas para a atividade trocada; confira a conciliação.')
                    shift=decision
                    if shift.external_id!=ext:
                        shift.external_id=ext
                        shift.save(update_fields=['external_id'])
                if restored:
                    if shift and shift.pk!=restored.pk:
                        raise SheetFormatError('Há identidades duplicadas para a atividade desfeita; confira a conciliação.')
                    shift=restored
                    shift.external_id=ext
                    snapshot=dict(shift.source_snapshot or {})
                    snapshot.pop('google_revert_export_pending',None)
                    snapshot.pop('google_revert_previous_owner',None)
                    shift.source_snapshot=snapshot
                    shift.requires_reconciliation=shift.pk in source_conflicts
                    shift.save(update_fields=['external_id','source_snapshot','requires_reconciliation'])
                if shift and shift.pk in accepted_source_ids:
                    update_metadata(shift,metadata)
                    shift.source_cell=source_cell
                    shift.requires_reconciliation=shift.pk in source_conflicts
                    shift.save(update_fields=['source_cell','requires_reconciliation'])
                    preserved+=1
                    continue
                Shift.objects.update_or_create(external_id=ext,defaults={'source':source,'active':True,'source_cell':source_cell,'resident':user,'starts_at':start,'ends_at':end,'sector':label,**metadata})
    if not seen and not shared_seen and not incomplete_seen:
        raise SheetFormatError('Nenhum horário reconhecido; sincronização cancelada para preservar os dados.')
    removed_shared=list(SharedActivity.objects.filter(source=source,allocated_to__isnull=True).exclude(external_id__in=shared_seen).values_list('pk',flat=True))
    SharedActivity.objects.filter(pk__in=removed_shared).update(active=False)
    Allocation.objects.filter(status='pending').filter(Q(mine_id__in=removed_shared)|Q(theirs_id__in=removed_shared)).update(status='cancelled',resolved_at=timezone.now())
    assigned_obsolete=SharedActivity.objects.filter(source=source,allocated_to__isnull=False).exclude(external_id__in=shared_seen).count()
    obsolete=Shift.objects.filter(source=source).exclude(external_id__in=seen)
    protected=list(Exchange.objects.filter(status='accepted').values_list('offered_id','requested_id'))
    awaiting_export={item.pk for items in pending_reverted_cells.values() for item in items
                     if (item.source_snapshot or {}).get('google_revert_export_pending')}
    protected_ids={pk for pair in protected for pk in pair}|bundle_ids|awaiting_export
    # Accepted exchanges remain available for manual reconciliation when their source changes.
    held=obsolete.filter(pk__in=protected_ids)
    retained=held.count()
    conflict_ids.update(held.values_list('pk',flat=True))
    held.update(requires_reconciliation=True,wants_exchange=False)
    removed=list(obsolete.exclude(pk__in=protected_ids).values_list('pk',flat=True))
    obsolete.exclude(pk__in=protected_ids).update(active=False)
    Exchange.objects.filter(status='pending').filter(Q(offered_id__in=removed)|Q(requested_id__in=removed)).update(status='cancelled',resolved_at=timezone.now())
    cancel_pending_bundles(set(removed)|conflict_ids,now=timezone.now())
    if conflict_ids:
        from .models import ExchangeTopic
        ExchangeTopic.objects.filter(shift_id__in=conflict_ids,closed_at__isnull=True).update(closed_at=timezone.now())
    IncompleteActivity.objects.filter(source=source).exclude(external_id__in=incomplete_seen).delete()
    # The weekly mirror is authoritative. Do not fabricate daily assignments
    # by expanding reference rotations over their entire date ranges.
    RotationActivity.objects.filter(source=source).delete()
    state.last_success=timezone.now()
    pending=SharedActivity.objects.filter(source=source,active=True,allocated_to__isnull=True).count()
    reconciliation=Shift.objects.filter(source=source,active=True,requires_reconciliation=True).count()+assigned_obsolete
    state.summary=f'{len(seen)} plantões definidos; {pending} alocações pendentes; {len(incomplete_seen)-rest_count} sem horário válido; {rest_count} registros de pós-plantão; {preserved+retained} trocas preservadas; {reconciliation} exigem conciliação.'
    state.save()
    if previous_success:
        notify_schedule_changes(source,before,snapshot_source(source,now=snapshot_time),previous_success=previous_success)
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
