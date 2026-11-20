"""Manual, previewed pilot: accepted regular exchanges in 23–29 November 2026 only."""
import hashlib
import json
from copy import deepcopy
from datetime import date, datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify
from openpyxl.utils.cell import range_boundaries, get_column_letter
from openpyxl.utils.datetime import from_excel

from .google_editor import EditorError, authorized_session
from .identity import is_account_enabled
from .models import Exchange, SheetSource, SheetsEditorConnection, SheetsWritePilot, SheetsWritePreview
from .sheets import HOURS, digest, parse_shift_hours

WEEK = date(2026,11,23)
LAST = date(2026,11,29)
TAB = 'Espelho Semanal'
API = 'https://sheets.googleapis.com/v4/spreadsheets/'
BLUE_COLOR = {'red':0,'green':0,'blue':1}
FONT_COLOR_FIELD = 'userEnteredFormat.textFormat.foregroundColorStyle'


def pilot_sources():
    from .models import Shift
    start=timezone.make_aware(datetime(2026,11,23))
    end=start+timedelta(days=7)
    ids=Shift.objects.filter(active=True,category='regular',starts_at__gte=start,starts_at__lt=end).exclude(source='').values_list('source',flat=True)
    return SheetSource.objects.filter(enabled=True,format='weekly',spreadsheet_id__in=ids).order_by('name')


def _is_bundle(exchange):
    from .models import BundleExchange
    return isinstance(exchange,BundleExchange)


def _entries(exchange):
    if not _is_bundle(exchange):
        return [(exchange.offered,exchange.proposer,exchange.recipient,'offered'),
                (exchange.requested,exchange.recipient,exchange.proposer,'requested')]
    items=list(exchange.items.select_related('shift__resident','original_owner').order_by('side','shift__starts_at','pk'))
    if not items or len(items)>40 or not any(item.side=='offered' for item in items) or not any(item.side=='requested' for item in items):
        raise EditorError('O conjunto precisa ter atividades válidas nos dois lados.')
    result=[]
    for item in items:
        if item.side not in ['offered','requested']:raise EditorError('Um lado do conjunto não é válido.')
        old_owner,new_owner=(exchange.proposer,exchange.recipient) if item.side=='offered' else (exchange.recipient,exchange.proposer)
        if item.original_owner_id!=old_owner.pk:raise EditorError('A titularidade original do conjunto mudou.')
        result.append((item.shift,old_owner,new_owner,item.side))
    if len({entry[0].pk for entry in result})!=len(result):raise EditorError('Há atividades duplicadas no conjunto.')
    return result


def _bundle_snapshot(exchange,entries):
    from django.utils.dateparse import parse_datetime
    if not exchange.proposer_acknowledged_at or not exchange.recipient_acknowledged_at or not exchange.resolved_at:
        raise EditorError('O conjunto precisa registrar o aceite explícito dos dois residentes.')
    snapshot=exchange.snapshot
    if not isinstance(snapshot,dict) or not isinstance(snapshot.get('terms'),dict):
        raise EditorError('O conjunto não tem uma conferência válida dos termos.')
    if (snapshot['terms'].get('proposer_id')!=exchange.proposer_id or snapshot['terms'].get('recipient_id')!=exchange.recipient_id or snapshot['terms'].get('mode')!=exchange.mode):
        raise EditorError('Os participantes ou o modo do conjunto mudaram desde o aceite.')
    signature=hashlib.sha256(json.dumps(snapshot['terms'],sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    if signature!=exchange.fingerprint or snapshot.get('fingerprint')!=exchange.fingerprint:
        raise EditorError('A conferência do conjunto foi alterada; revise os termos antes de gravar.')
    records=[]
    for side in ['offered','requested']:
        for record in snapshot['terms'].get(side,[]):
            if not isinstance(record,dict):raise EditorError('O conjunto contém termos inválidos.')
            records.append((side,record))
    if len(records)!=len(entries):raise EditorError('As atividades do conjunto mudaram desde o aceite.')
    by_id={(side,record.get('id')):record for side,record in records}
    if len(by_id)!=len(records):raise EditorError('Há atividades duplicadas na conferência do conjunto.')
    for shift,old_owner,new_owner,side in entries:
        record=by_id.get((side,shift.pk))
        if not record or record.get('resident_id')!=old_owner.pk:
            raise EditorError('As atividades do conjunto mudaram desde o aceite.')
        for field in ['sector','source','source_tab','source_cell','category']:
            if record.get(field)!=getattr(shift,field):raise EditorError('A atividade foi alterada depois do aceite do conjunto.')
        try:
            same=(parse_datetime(record['starts_at'])==shift.starts_at and parse_datetime(record['ends_at'])==shift.ends_at)
        except (KeyError,TypeError,ValueError):same=False
        if not same:raise EditorError('O horário foi alterado depois do aceite do conjunto.')


def _eligible(exchange,account):
    from .models import BundleExchangeItem
    pilot=SheetsWritePilot.objects.select_related('source').filter(key='pilot',enabled=True).first()
    if not pilot or not pilot.source.enabled or pilot.source.format!='weekly':
        raise EditorError('Selecione a fonte semanal e habilite o teste de 23–29/11/2026.')
    if exchange.status!='accepted':raise EditorError('A troca precisa ter sido aceita pelos dois residentes.')
    entries=_entries(exchange)
    if (exchange.proposer.get_full_name() or exchange.proposer.username).casefold()==(exchange.recipient.get_full_name() or exchange.recipient.username).casefold():
        raise EditorError('Os nomes dos residentes são ambíguos para este teste.')
    for shift,old_owner,new_owner,side in entries:
        if (not shift.active or shift.requires_reconciliation or shift.category!='regular' or
                shift.source!=pilot.source.spreadsheet_id or shift.source_tab!=TAB or
                not WEEK<=timezone.localdate(shift.starts_at)<=LAST or shift.starts_at<=timezone.now()):
            raise EditorError('Este teste aceita apenas atividades futuras da mesma fonte semanal de 23–29/11/2026, sem conciliação.')
        if shift.resident_id!=new_owner.pk:raise EditorError('A titularidade da troca mudou; confira a escala antes de gravar.')
    ids=[entry[0].pk for entry in entries]
    bundles=BundleExchangeItem.objects.filter(exchange__status='accepted',shift_id__in=ids)
    singles=Exchange.objects.filter(status='accepted').filter(Q(offered_id__in=ids)|Q(requested_id__in=ids))
    if _is_bundle(exchange):
        _bundle_snapshot(exchange,entries)
        bundles=bundles.exclude(exchange=exchange)
    else:
        singles=singles.exclude(pk=exchange.pk)
    if bundles.exists() or singles.exists():
        raise EditorError('Atividades com mais de uma troca aceita precisam de conferência; não entram neste teste inicial.')
    if not exchange.proposer.is_active or not exchange.recipient.is_active:
        raise EditorError('Um dos residentes está desativado.')
    connection=SheetsEditorConnection.objects.select_related('connected_by').filter(key='pilot',connected_by=account).first()
    if not connection or not is_account_enabled(connection.connected_by) or not connection.connected_by.is_superuser:
        raise EditorError('Conecte uma conta Google com acesso de editor.')
    return pilot.source,connection


def _request_json(session,method,url,**kwargs):
    response=session.request(method,url,timeout=30,stream=True,**kwargs)
    try:
        response.raise_for_status()
        data=bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data)>20*1024*1024:raise EditorError('A resposta Google excedeu o limite do teste.')
        return json.loads(data)
    finally:
        response.close()


def _read_book(session,source):
    fields='sheets(properties(sheetId,title,gridProperties),merges,data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue))))'
    payload=_request_json(session,'GET',API+source.spreadsheet_id,params={'ranges':"'Espelho Semanal'",'includeGridData':'true','fields':fields})
    sheets=[s for s in payload.get('sheets',[]) if s.get('properties',{}).get('title')==TAB]
    if len(sheets)!=1:raise EditorError('A aba Espelho Semanal não foi encontrada na fonte.')
    sheet=sheets[0];grid=sheet['properties'].get('gridProperties',{})
    if grid.get('rowCount',0)*grid.get('columnCount',0)>2_000_000:raise EditorError('A aba excede o limite deste teste.')
    cells={}
    for block in sheet.get('data',[]):
        for row_index,row in enumerate(block.get('rowData',[]),block.get('startRow',0)+1):
            for column,value in enumerate(row.get('values',[]),block.get('startColumn',0)+1):
                cells[row_index,column]=value
                if len(cells)>2_000_000:raise EditorError('A aba excede o limite deste teste.')
    return sheet,cells


def _read_name_formats(session,source,sheet,cells,slots):
    # Formatting on a whole weekly mirror can exceed 20 MB even when its values
    # are small. Read fonts only for the reviewed, occupied name cells.
    targets={(cell['row']+1,cell['column']+1) for slot,_,_,_ in slots for cell in slot['cells']}
    ranges=[f"'{TAB}'!{get_column_letter(column)}{row}" for row,column in sorted(targets)]
    fields='sheets(properties(sheetId,title),data(startRow,startColumn,rowData(values(userEnteredFormat(textFormat),effectiveFormat(textFormat),textFormatRuns))))'
    payload=_request_json(session,'GET',API+source.spreadsheet_id,params={'ranges':ranges,'includeGridData':'true','fields':fields})
    matches=[item for item in payload.get('sheets',[]) if item.get('properties',{}).get('sheetId')==sheet['properties']['sheetId'] and item.get('properties',{}).get('title')==TAB]
    if len(matches)!=1:raise EditorError('A aba mudou durante a leitura das fontes. Prepare outra conferência.')
    for block in matches[0].get('data',[]):
        for row,row_data in enumerate(block.get('rowData',[]),block.get('startRow',0)+1):
            for column,value in enumerate(row_data.get('values',[]),block.get('startColumn',0)+1):
                if (row,column) in targets:
                    target=cells[row,column]
                    for field in ['userEnteredFormat','effectiveFormat','textFormatRuns']:
                        if field in value:target[field]=value[field]


def _value(cells,row,col):
    value=cells.get((row,col),{}).get('effectiveValue',{})
    if 'errorValue' in value:raise EditorError('Há um erro de fórmula nas células da troca. Confira a planilha.')
    return value.get('stringValue',value.get('numberValue',value.get('boolValue')))


def _matches(name,person):
    name=' '.join(str(name).split())
    key=f'{slugify(name)[:100]}-{digest(name.casefold())[:8]}'
    return person.username==key or name.casefold()==' '.join((person.get_full_name() or person.username).split()).casefold()


def _blue_text_format(text_format):
    style=text_format.get('foregroundColorStyle')
    if style is not None:
        color=style.get('rgbColor')
    else:
        color=text_format.get('foregroundColor')
    if not isinstance(color,dict):return False
    return all(abs(color.get(channel,0)-value)<1e-6 for channel,value in BLUE_COLOR.items()) and abs(color.get('alpha',1)-1)<1e-6


def _blue_font(cell):
    entered=cell.get('userEnteredFormat',{}).get('textFormat',{})
    effective=cell.get('effectiveFormat',{}).get('textFormat',{})
    # Effective colors also account for a sheet's theme. Fall back to the entered
    # format when Google omits effective formatting from an otherwise valid cell.
    base=effective if any(key in effective for key in ['foregroundColorStyle','foregroundColor']) else entered
    if not _blue_text_format(base):return False
    for run in cell.get('textFormatRuns',[]):
        text_format=run.get('format',{})
        if any(key in text_format for key in ['foregroundColorStyle','foregroundColor']) and not _blue_text_format(text_format):return False
    return True


def _color_update(cell,write_name):
    value={'userEnteredFormat':{'textFormat':{'foregroundColorStyle':{'rgbColor':BLUE_COLOR.copy()}}}}
    fields=[FONT_COLOR_FIELD]
    if write_name:
        # Sheets clears existing rich-text runs when replacing the cell value.
        value['userEnteredValue']={'stringValue':cell['new_name']}
        fields.insert(0,'userEnteredValue')
    else:
        runs=deepcopy(cell.get('text_format_runs',[]))
        changed=False
        for run in runs:
            text_format=run.get('format',{})
            if any(key in text_format for key in ['foregroundColorStyle','foregroundColor']) and not _blue_text_format(text_format):
                text_format.pop('foregroundColor',None)
                text_format['foregroundColorStyle']={'rgbColor':BLUE_COLOR.copy()}
                changed=True
        if changed:
            # A runs array is replaced as a whole by the API. Keep every run and
            # its other formatting, changing only explicit foreground colors.
            value['textFormatRuns']=runs
            fields.append('textFormatRuns')
    return value,','.join(fields)


def _slot(sheet,cells,shift,old_owner,new_owner):
    try:
        col,row,end_col,end_row=range_boundaries(shift.source_cell)
        if row!=end_row or col+4!=end_col or col<2 or row<3:raise ValueError
    except (ValueError,TypeError):raise EditorError('A referência de origem desta atividade não é válida para escrita.')
    raw_date=_value(cells,1,col-1)
    try:
        day=from_excel(raw_date).date() if isinstance(raw_date,(int,float)) else datetime.strptime(raw_date,'%d/%m/%Y').date()
    except (ValueError,TypeError,AttributeError,OverflowError):raise EditorError('Não foi possível confirmar a data na planilha.')
    hours=_value(cells,row,col)
    try:interval=parse_shift_hours(day,hours)
    except (ValueError,TypeError):interval=None
    if not interval or interval!=(shift.starts_at,shift.ends_at):raise EditorError('Data ou horário divergente. Atualize e confira a fonte antes de gravar.')
    location='';sector=''
    for index in range(3,row+1):
        marker=_value(cells,index,1)
        if marker:location=str(marker).strip()
        names=[_value(cells,index,col+offset) for offset in [3,4]]
        has_names=any(isinstance(name,str) and name.strip() for name in names)
        value=_value(cells,index,col)
        if not HOURS.fullmatch(str(value or '')) and not has_names:
            if isinstance(value,str) and value.strip():sector=value.strip()
            else:
                label=_value(cells,index,col-1)
                import re
                if isinstance(label,str) and label.strip() and not re.fullmatch(r'R[123]|C[123][ab]?',label.strip(),re.I):sector=label.strip()
    if f'{location} · {sector}'[:120]!=shift.sector:raise EditorError('O setor da atividade mudou na origem. Atualize e confira a fonte.')
    occupied=[]
    for column in [col+3,col+4]:
        name=_value(cells,row,column)
        if name is None or (isinstance(name,str) and not name.strip()):continue
        if not isinstance(name,str):raise EditorError('O nome na planilha não é um texto válido.')
        for merge in sheet.get('merges',[]):
            if merge.get('startRowIndex',0)<=row-1<merge.get('endRowIndex',0) and merge.get('startColumnIndex',0)<=column-1<merge.get('endColumnIndex',0):
                if (merge.get('startRowIndex')!=row-1 or merge.get('endRowIndex')!=row or merge.get('startColumnIndex',0)<col+2 or merge.get('endColumnIndex',0)>col+4):
                    raise EditorError('A mesclagem dos nomes excede a atividade. Este caso precisa de conferência.')
        cell=cells.get((row,column),{})
        occupied.append({'row':row-1,'column':column-1,'old':cell.get('userEnteredValue',{}),
                         'name':name,'address':get_column_letter(column)+str(row),
                         'font_format':cell.get('userEnteredFormat',{}).get('textFormat',{}),
                         'effective_font_format':cell.get('effectiveFormat',{}).get('textFormat',{}),
                         'text_format_runs':cell.get('textFormatRuns',[]),'font_is_blue':_blue_font(cell)})
    if not occupied:raise EditorError('A origem não contém o residente desta atividade.')
    old=all(_matches(cell['name'],old_owner) for cell in occupied)
    final=all(_matches(cell['name'],new_owner) for cell in occupied)
    if not old and not final:raise EditorError('O nome na origem diverge dos dois residentes da troca. Nenhuma célula foi gravada.')
    return {'cells':occupied,'state':'old' if old else 'final','sector':shift.display_sector,
            'date':day.isoformat(),'name':occupied[0]['name'],'new_label':new_owner.get_full_name() or new_owner.username}


def _app_state(exchange):
    rows=[{'id':shift.pk,'owner':shift.resident_id,'original_owner':old_owner.pk,'side':side,
           'source':shift.source,'tab':shift.source_tab,'cell':shift.source_cell,
           'start':shift.starts_at.isoformat(),'end':shift.ends_at.isoformat(),'sector':shift.sector}
          for shift,old_owner,new_owner,side in _entries(exchange)]
    return rows


def make_plan(exchange,session,account):
    source,connection=_eligible(exchange,account)
    sheet,cells=_read_book(session,source)
    entries=_entries(exchange)
    slots=[(_slot(sheet,cells,shift,old_owner,new_owner),old_owner,new_owner,side)
           for shift,old_owner,new_owner,side in entries]
    _read_name_formats(session,source,sheet,cells,slots)
    slots=[(_slot(sheet,cells,shift,old_owner,new_owner),old_owner,new_owner,side)
           for shift,old_owner,new_owner,side in entries]
    states={item['state'] for item,_,_,_ in slots}
    if len(states)!=1:raise EditorError('Só uma atividade ou parte do conjunto já reflete a troca. Confira a divergência antes de continuar.')
    already=states=={'final'}
    canonical={}
    if not already:
        for item,old_owner,_,_ in slots:
            canonical.setdefault(old_owner.pk,item['name'])
    result={'spreadsheet_id':source.spreadsheet_id,'sheet_id':sheet['properties']['sheetId'],
            'exchange':exchange.pk,'decision_type':'bundle' if _is_bundle(exchange) else 'single',
            'connection':connection.connected_at.isoformat(),'email':connection.email,
            'already_applied':already,'cells':[], 'summary':[],'app_state':_app_state(exchange)}
    if _is_bundle(exchange):result['bundle_fingerprint']=exchange.fingerprint
    for item,old_owner,new_owner,side in slots:
        desired=item['name'] if already else canonical[new_owner.pk]
        result['summary'].append({'date':item['date'],'sector':item['sector'],'before':item['name'],'after':desired,'side':side})
        for cell in item['cells']:
            result['cells'].append({**cell,'new_name':desired})
    coordinates=[(cell['row'],cell['column']) for cell in result['cells']]
    if len(coordinates)!=len(set(coordinates)):raise EditorError('As atividades apontam para a mesma célula; confira a origem.')
    result['needs_color']=any(not cell['font_is_blue'] for cell in result['cells'])
    return result


def _exchange(pk):
    return Exchange.objects.select_related('offered__resident','requested__resident','proposer','recipient').get(pk=pk)


def _bundle(pk):
    from .models import BundleExchange
    return BundleExchange.objects.select_related('proposer','recipient').prefetch_related('items__shift__resident','items__original_owner').get(pk=pk)


def _preview(account,decision):
    if not is_account_enabled(account) or not account.is_superuser:raise EditorError('Somente o administrador pode preparar este teste.')
    source,_=_eligible(decision,account)
    try:
        with authorized_session(account) as session:plan=make_plan(decision,session,account)
    except EditorError:raise
    except Exception as error:raise EditorError('Não foi possível ler a fonte com essa conta Google. Confira a conexão e o acesso de editor.') from error
    target={'bundle':decision} if _is_bundle(decision) else {'exchange':decision}
    return SheetsWritePreview.objects.create(**target,account=account,source=source,plan=plan)


def preview_exchange(account,pk):
    return _preview(account,_exchange(pk))


def preview_bundle_exchange(account,pk):
    return _preview(account,_bundle(pk))


def _decision(preview):
    return _bundle(preview.bundle_id) if preview.bundle_id else _exchange(preview.exchange_id)


def _fingerprint(plan):
    return hashlib.sha256(json.dumps(plan,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def apply_preview(account,pk):
    if not is_account_enabled(account) or not account.is_superuser:raise EditorError('Somente o administrador pode gravar este teste.')
    with transaction.atomic():
        preview=SheetsWritePreview.objects.select_for_update().get(pk=pk,account=account)
        if preview.state!='preview' or timezone.now()-preview.created_at>timedelta(minutes=10):raise EditorError('Esta prévia já foi utilizada ou expirou. Prepare outra conferência.')
        target={'bundle_id':preview.bundle_id} if preview.bundle_id else {'exchange_id':preview.exchange_id}
        busy=SheetsWritePreview.objects.filter(**target,state='writing')
        cutoff=timezone.now()-timedelta(minutes=10)
        busy.filter(Q(started_at__lt=cutoff)|Q(started_at__isnull=True,created_at__lt=cutoff)).update(
            state='unknown',message='Envio interrompido; o resultado precisa ser conferido por uma nova prévia.')
        if busy.exists():raise EditorError('Já há um envio dessa troca em andamento. Aguarde antes de conferir novamente.')
        preview.state='writing';preview.started_at=timezone.now();preview.save(update_fields=['state','started_at'])
    sending=False
    try:
        exchange=_decision(preview)
        with authorized_session(account) as session:
            current=make_plan(exchange,session,account)
            if _fingerprint(current)!=_fingerprint(preview.plan):raise EditorError('A planilha, a conta Google ou a troca mudou desde a prévia. Confira novamente; nada foi gravado.')
            latest=_decision(preview)
            source,connection=_eligible(latest,account)
            if (_app_state(latest)!=current['app_state'] or source.spreadsheet_id!=current['spreadsheet_id']
                    or connection.connected_at.isoformat()!=current['connection']):
                raise EditorError('A configuração ou a escala mudou durante a conferência. Prepare outra prévia.')
            requests=[]
            for cell in current['cells']:
                if not current['already_applied'] or not cell['font_is_blue']:
                    value,fields=_color_update(cell,write_name=not current['already_applied'])
                    requests.append({'updateCells':{'range':{'sheetId':current['sheet_id'],'startRowIndex':cell['row'],
                        'endRowIndex':cell['row']+1,'startColumnIndex':cell['column'],'endColumnIndex':cell['column']+1},
                        'rows':[{'values':[value]}], 'fields':fields}})
            if requests:
                sending=True
                _request_json(session,'POST',API+current['spreadsheet_id']+':batchUpdate',json={'requests':requests})
                confirmed=make_plan(_decision(preview),session,account)
                if not confirmed['already_applied']:raise EditorError('O retorno Google não confirmou todos os nomes. Confira o resultado antes de reenviar.')
                if confirmed['needs_color']:raise EditorError('O retorno Google não confirmou a fonte azul #0000ff. Confira a formatação antes de preparar outra prévia.')
        preview.state='done';preview.message='Troca conferida na planilha.'
    except Exception as error:
        preview.state='unknown' if sending else 'conflict'
        preview.message=(str(error) if isinstance(error,EditorError) else 'Não foi possível confirmar o resultado. Confira a fonte e a conexão Google antes de preparar outra prévia.')[:250]
    preview.completed_at=timezone.now();preview.save(update_fields=['state','message','completed_at'])
    return preview
