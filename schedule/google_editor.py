"""Google editor connection. Credentials stay outside the image; refresh tokens encrypted."""
import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.crypto import constant_time_compare
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_POST, require_GET

from .identity import is_account_enabled
from .models import SheetsEditorConnection

SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/userinfo.email']
TOKEN_URI = 'https://oauth2.googleapis.com/token'


class EditorError(ValueError):
    """Safe explanation; never include provider responses or credential values."""


def cipher():
    path = os.environ.get('GOOGLE_SHEETS_TOKEN_KEY_FILE','/run/secrets/google_sheets_token_key')
    try:
        with open(path,'rb') as stream:
            return Fernet(stream.read().strip())
    except (OSError, ValueError) as error:
        raise EditorError('A chave de proteção da conexão Google não está configurada na instância.') from error


def client_config():
    path = os.environ.get('GOOGLE_SHEETS_OAUTH_CLIENT_FILE','/run/secrets/google_sheets_oauth_client')
    try:
        with open(path) as stream:
            config = json.load(stream)['web']
        if not config.get('client_id') or not config.get('client_secret'):
            raise ValueError
        return config
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise EditorError('Configure o arquivo OAuth do tipo Aplicativo da Web na instância.') from error


def callback_uri(request):
    uri = request.build_absolute_uri(reverse('google_editor_callback'))
    config = client_config()
    if uri not in config.get('redirect_uris',[]) or (not uri.startswith('https://') and not uri.startswith('http://127.0.0.1:')):
        raise EditorError('Cadastre no cliente OAuth o endereço HTTPS de retorno mostrado nesta página.')
    return uri


def ready(request):
    try:
        client_config();cipher();callback_uri(request)
        return ''
    except EditorError as error:
        return str(error)


def encrypt_token(token):
    return cipher().encrypt(token.encode()).decode()


@sensitive_variables()
def authorized_session(account):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession
    connection = SheetsEditorConnection.objects.select_related('connected_by').filter(key='pilot',connected_by=account).first()
    if not connection or not is_account_enabled(connection.connected_by) or not connection.connected_by.is_superuser:
        raise EditorError('Conecte uma conta Google com acesso de editor pela conta administrativa.')
    try:
        refresh = cipher().decrypt(connection.encrypted_refresh_token.encode()).decode()
    except Exception as error:
        raise EditorError('Reconecte o Google; a conexão armazenada não pôde ser aberta.') from error
    config = client_config()
    credentials = Credentials(None, refresh_token=refresh, token_uri=TOKEN_URI,
                              client_id=config['client_id'], client_secret=config['client_secret'])
    return AuthorizedSession(credentials)


@login_required
@require_POST
@sensitive_variables()
def connect(request):
    if not request.user.is_superuser:return HttpResponseForbidden()
    try:
        uri = callback_uri(request);cipher();config=client_config()
    except EditorError as error:
        messages.error(request,str(error));return redirect('sheets_write_pilot')
    verifier=secrets.token_urlsafe(64);state=secrets.token_urlsafe(32)
    request.session['sheets_oauth']={'state':state,'verifier':verifier,'account':request.user.pk,'time':time.time(),'redirect':uri}
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    params={'client_id':config['client_id'],'redirect_uri':uri,'response_type':'code','scope':' '.join(SCOPES),
            'state':state,'access_type':'offline','prompt':'consent select_account',
            'code_challenge':challenge,'code_challenge_method':'S256'}
    response=redirect('https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(params))
    response['Cache-Control']='private, no-store'
    return response


@login_required
@require_GET
@sensitive_variables()
def callback(request):
    if not request.user.is_superuser:return HttpResponseForbidden()
    saved=request.session.pop('sheets_oauth',None)
    valid=(saved and saved.get('account')==request.user.pk and time.time()-saved.get('time',0)<600
           and constant_time_compare(saved.get('state',''),request.GET.get('state','')))
    if not valid:
        messages.error(request,'A autorização expirou ou não corresponde a esta sessão. Conecte novamente.')
        return redirect('sheets_write_pilot')
    if request.GET.get('error') or not request.GET.get('code'):
        messages.error(request,'A conexão Google não foi autorizada.');return redirect('sheets_write_pilot')
    try:
        config=client_config()
        response=requests.post(TOKEN_URI,data={'client_id':config['client_id'],'client_secret':config['client_secret'],
            'code':request.GET['code'],'redirect_uri':saved['redirect'],'grant_type':'authorization_code',
            'code_verifier':saved['verifier']},timeout=30)
        response.raise_for_status();payload=response.json()
        if not set(SCOPES).issubset(set(payload.get('scope','').split())) or not payload.get('refresh_token'):
            raise EditorError('Autorize as permissões solicitadas e conecte novamente para permitir o teste.')
        info=requests.get('https://www.googleapis.com/oauth2/v2/userinfo',headers={'Authorization':'Bearer '+payload['access_token']},timeout=30)
        info.raise_for_status();identity=info.json()
        if not identity.get('verified_email') or not identity.get('email'):raise EditorError('A conta Google não retornou um e-mail verificado.')
        SheetsEditorConnection.objects.update_or_create(key='pilot',defaults={
            'email':identity['email'],'encrypted_refresh_token':encrypt_token(payload['refresh_token']), 'connected_by':request.user})
        messages.success(request,'Conta Google conectada. Confira o e-mail exibido antes de testar a escrita.')
    except EditorError as error:
        messages.error(request,str(error))
    except Exception:
        messages.error(request,'Não foi possível concluir a conexão Google. Confira o cliente OAuth e tente novamente.')
    response=redirect('sheets_write_pilot');response['Cache-Control']='private, no-store';response['Referrer-Policy']='no-referrer'
    return response


@login_required
@require_POST
def disconnect(request):
    if not request.user.is_superuser:return HttpResponseForbidden()
    SheetsEditorConnection.objects.filter(key='pilot').delete()
    from .models import SheetsWritePilot
    SheetsWritePilot.objects.filter(key='pilot').update(enabled=False)
    messages.success(request,'Conexão removida do aplicativo e teste desativado.')
    return redirect('sheets_write_pilot')
