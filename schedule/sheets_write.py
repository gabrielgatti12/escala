"""Manual, previewed pilot: accepted regular exchanges in 23–29 November 2026 only."""
import hashlib
import json
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


def pilot_sources():
    from .models import Shift
    start=timezone.make_aware(datetime(2026,11,23))
    end=start+timedelta(days=7)
    ids=Shift.objects.filter(active=True,category='regular',starts_at__gte=start,starts_at__lt=end).exclude(source='').values_list('source',flat=True)
    return SheetSource.objects.filter(enabled=True,format='weekly',spreadsheet_id__in=ids).order_by('name')


def _eligible(exchange,account):
    pilot=SheetsWritePilot.objects.select_related('source').filter(key='pilot',enabled=True).first()
    if not pilot or not pilot.source.enabled or pilot.source.format!='weekly':
        raise EditorError('Selecione a fonte semanal e habilite o teste de 23–29/11/2026.')
    if exchange.status!='accepted':raise EditorError('A troca precisa ter sido aceita pelos dois residentes.')
    offered,requested=exchange.offered,exchange.requested
    if (exchange.proposer.get_full_name() or exchange.proposer.username).casefold()==(exchange.recipient.get_full_name() or exchange.recipient.username).casefold():
        raise EditorError('Os nomes dos residentes são ambíguos para este teste.')
    for shift in [offered,requested]:
        if (not shift.active or shift.requires_reconciliation or shift.category!='regular' or
                shift.source!=pilot.source.spreadsheet_id or shift.source_tab!=TAB or
                not WEEK<=timezone.localdate(shift.starts_at)<=LAST or shift.starts_at<=timezone.now()):
            raise EditorError('Este teste aceita apenas atividades futuras da mesma fonte semanal de 23–29/11/2026, sem conciliação.')
    if offered.pk==requested.pk or offered.resident_id!=exchange.recipient_id or requested.resident_id!=exchange.proposer_id:
        raise EditorError('A titularidade da troca mudou; confira a escala antes de gravar.')
    if Exchange.objects.filter(status='accepted').exclude(pk=exchange.pk).filter(Q(offered_id__in=[offered.pk,requested.pk])|Q(requested_id__in=[offered.pk,requested.pk])).exists():
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
    fields='sheets(properties(sheetId,title,gridProperties),merges,data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue,formattedValue))))'
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


def _value(cells,row,col):
    value=cells.get((row,col),{}).get('effectiveValue',{})
    if 'errorValue' in value:raise EditorError('Há um erro de fórmula nas células da troca. Confira a planilha.')
    return value.get('stringValue',value.get('numberValue',value.get('boolValue')))


def _matches(name,person):
    name=' '.join(str(name).split())
    key=f'{slugify(name)[:100]}-{digest(name.casefold())[:8]}'
    return person.username==key or name.casefold()==' '.join((person.get_full_name() or person.username).split()).casefold()


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
                         'name':name,'address':get_column_letter(column)+str(row)})
    if not occupied:raise EditorError('A origem não contém o residente desta atividade.')
    old=all(_matches(cell['name'],old_owner) for cell in occupied)
    final=all(_matches(cell['name'],new_owner) for cell in occupied)
    if not old and not final:raise EditorError('O nome na origem diverge dos dois residentes da troca. Nenhuma célula foi gravada.')
    return {'cells':occupied,'state':'old' if old else 'final','sector':shift.display_sector,
            'date':day.isoformat(),'name':occupied[0]['name'],'new_label':new_owner.get_full_name() or new_owner.username}


def _app_state(exchange):
    return [{'id':shift.pk,'owner':shift.resident_id,'source':shift.source,'tab':shift.source_tab,
             'cell':shift.source_cell,'start':shift.starts_at.isoformat(),'end':shift.ends_at.isoformat(),
             'sector':shift.sector} for shift in [exchange.offered,exchange.requested]]


def make_plan(exchange,session,account):
    source,connection=_eligible(exchange,account)
    sheet,cells=_read_book(session,source)
    first=_slot(sheet,cells,exchange.offered,exchange.proposer,exchange.recipient)
    second=_slot(sheet,cells,exchange.requested,exchange.recipient,exchange.proposer)
    if first['state']!=second['state']:raise EditorError('Só uma atividade já reflete a troca. Confira a divergência antes de continuar.')
    result={'spreadsheet_id':source.spreadsheet_id,'sheet_id':sheet['properties']['sheetId'],
            'exchange':exchange.pk,'connection':connection.connected_at.isoformat(),'email':connection.email,
            'already_applied':first['state']=='final','cells':[], 'summary':[],'app_state':_app_state(exchange)}
    for item,opposite in [(first,second),(second,first)]:
        desired=opposite['name'] if first['state']=='old' else item['name']
        result['summary'].append({'date':item['date'],'sector':item['sector'],'before':item['name'],'after':desired})
        for cell in item['cells']:
            result['cells'].append({**cell,'new_name':desired})
    coordinates=[(cell['row'],cell['column']) for cell in result['cells']]
    if len(coordinates)!=len(set(coordinates)):raise EditorError('As duas atividades apontam para a mesma célula; confira a origem.')
    return result


def _exchange(pk):
    return Exchange.objects.select_related('offered__resident','requested__resident','proposer','recipient').get(pk=pk)


def preview_exchange(account,pk):
    if not is_account_enabled(account) or not account.is_superuser:raise EditorError('Somente o administrador pode preparar este teste.')
    exchange=_exchange(pk);source,_=_eligible(exchange,account)
    try:
        with authorized_session(account) as session:plan=make_plan(exchange,session,account)
    except EditorError:raise
    except Exception as error:raise EditorError('Não foi possível ler a fonte com essa conta Google. Confira a conexão e o acesso de editor.') from error
    return SheetsWritePreview.objects.create(exchange=exchange,account=account,source=source,plan=plan)


def _fingerprint(plan):
    return hashlib.sha256(json.dumps(plan,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def apply_preview(account,pk):
    if not is_account_enabled(account) or not account.is_superuser:raise EditorError('Somente o administrador pode gravar este teste.')
    with transaction.atomic():
        preview=SheetsWritePreview.objects.select_for_update().get(pk=pk,account=account)
        if preview.state!='preview' or timezone.now()-preview.created_at>timedelta(minutes=10):raise EditorError('Esta prévia já foi utilizada ou expirou. Prepare outra conferência.')
        busy=SheetsWritePreview.objects.filter(exchange=preview.exchange,state='writing')
        cutoff=timezone.now()-timedelta(minutes=10)
        busy.filter(Q(started_at__lt=cutoff)|Q(started_at__isnull=True,created_at__lt=cutoff)).update(
            state='unknown',message='Envio interrompido; o resultado precisa ser conferido por uma nova prévia.')
        if busy.exists():raise EditorError('Já há um envio dessa troca em andamento. Aguarde antes de conferir novamente.')
        preview.state='writing';preview.started_at=timezone.now();preview.save(update_fields=['state','started_at'])
    sending=False
    try:
        exchange=_exchange(preview.exchange_id)
        with authorized_session(account) as session:
            current=make_plan(exchange,session,account)
            if _fingerprint(current)!=_fingerprint(preview.plan):raise EditorError('A planilha, a conta Google ou a troca mudou desde a prévia. Confira novamente; nada foi gravado.')
            latest=_exchange(preview.exchange_id)
            source,connection=_eligible(latest,account)
            if (_app_state(latest)!=current['app_state'] or source.spreadsheet_id!=current['spreadsheet_id']
                    or connection.connected_at.isoformat()!=current['connection']):
                raise EditorError('A configuração ou a escala mudou durante a conferência. Prepare outra prévia.')
            if not current['already_applied']:
                requests=[]
                for cell in current['cells']:
                    requests.append({'updateCells':{'range':{'sheetId':current['sheet_id'],'startRowIndex':cell['row'],
                        'endRowIndex':cell['row']+1,'startColumnIndex':cell['column'],'endColumnIndex':cell['column']+1},
                        'rows':[{'values':[{'userEnteredValue':{'stringValue':cell['new_name']}}]}], 'fields':'userEnteredValue'}})
                sending=True
                _request_json(session,'POST',API+current['spreadsheet_id']+':batchUpdate',json={'requests':requests})
                confirmed=make_plan(_exchange(preview.exchange_id),session,account)
                if not confirmed['already_applied']:raise EditorError('O retorno Google não confirmou todos os nomes. Confira o resultado antes de reenviar.')
        preview.state='done';preview.message='Troca conferida na planilha.'
    except Exception as error:
        preview.state='unknown' if sending else 'conflict'
        preview.message=(str(error) if isinstance(error,EditorError) else 'Não foi possível confirmar o resultado. Confira a fonte e a conexão Google antes de preparar outra prévia.')[:250]
    preview.completed_at=timezone.now();preview.save(update_fields=['state','message','completed_at'])
    return preview
