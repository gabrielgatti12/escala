"""Discover weekly Google Sheets recursively through the read-only Drive API."""
import os
import re
from io import BytesIO
from django.db import transaction
from django.utils import timezone
from openpyxl import load_workbook
from .models import SheetSource

FOLDER_MIME='application/vnd.google-apps.folder'
SHEET_MIME='application/vnd.google-apps.spreadsheet'
API='https://www.googleapis.com/drive/v3'

def authenticated_session():
    key_path=os.environ.get('GOOGLE_DRIVE_API_KEY_FILE','')
    if key_path:
        with open(key_path) as stream: key=stream.read().strip()
    else:
        key=os.environ.get('GOOGLE_DRIVE_API_KEY','').strip()
    if key_path and not key:
        raise ValueError('O arquivo da chave de API está vazio.')
    if key:
        from requests import Session
        session=Session()
        session.params['key']=key
        session.public_drive=True
        return session
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession
    path=os.environ.get('GOOGLE_APPLICATION_CREDENTIALS','')
    if not path:
        raise ValueError('Configure uma chave de API para a pasta pública ou uma credencial de leitura do Google Drive na instância.')
    credentials=Credentials.from_service_account_file(path,scopes=['https://www.googleapis.com/auth/drive.readonly'])
    return AuthorizedSession(credentials)

def valid_id(value):
    if not re.fullmatch('[A-Za-z0-9_-]{10,100}',value):
        raise ValueError('Identificador de pasta ou planilha inválido.')
    return value

@transaction.atomic
def discover_folder(session,folder):
    queue=[valid_id(folder.folder_id)];seen=set();sheets={}
    while queue:
        folder_id=queue.pop()
        if folder_id in seen: continue
        seen.add(folder_id)
        if len(seen)>1000: raise ValueError('Limite de 1000 subpastas excedido; descoberta não foi salva.')
        token=None;tokens=set()
        while True:
            params={'q':f"'{folder_id}' in parents and trashed = false",'fields':'nextPageToken,files(id,name,mimeType)','pageSize':1000,'supportsAllDrives':'true','includeItemsFromAllDrives':'true'}
            if token: params['pageToken']=token
            response=session.get(API+'/files',params=params,timeout=45)
            response.raise_for_status()
            payload=response.json()
            if not isinstance(payload.get('files'),list): raise ValueError('Listagem de pasta inválida.')
            for item in payload['files']:
                item_id=valid_id(item['id'])
                if item['mimeType']==FOLDER_MIME: queue.append(item_id)
                elif item['mimeType']==SHEET_MIME: sheets[item_id]=item.get('name','')[:200]
                if len(sheets)>10000: raise ValueError('Limite de 10000 planilhas excedido; descoberta não foi salva.')
            token=payload.get('nextPageToken')
            if not token: break
            if token in tokens: raise ValueError('Paginação da pasta repetida; descoberta cancelada.')
            tokens.add(token)
    for spreadsheet_id,name in sheets.items():
        source,created=SheetSource.objects.get_or_create(spreadsheet_id=spreadsheet_id,defaults={'name':name,'folder':folder})
        if not created:
            source.name=name;source.folder=folder;source.save(update_fields=['name','folder'])
    # Absence never deletes history or decisions; sources can be disabled by an administrator.
    folder.last_success=timezone.now();folder.error='';folder.save(update_fields=['last_success','error'])
    return len(sheets)

def fetch_private_workbook(session,spreadsheet_id):
    spreadsheet_id=valid_id(spreadsheet_id)
    with session.get(f'{API}/files/{spreadsheet_id}/export',params={'mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'},timeout=60,stream=True) as response:
        response.raise_for_status()
        data=BytesIO()
        for chunk in response.iter_content(65536):
            data.write(chunk)
            if data.tell()>20*1024*1024: raise ValueError('Planilha excede o limite de 20 MB.')
        data.seek(0)
        return load_workbook(data,read_only=True,data_only=True)
