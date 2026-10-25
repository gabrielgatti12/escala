"""Preview and apply a private contact table; never guess a person's identity."""
import csv
import io
import re
import unicodedata
from collections import Counter,defaultdict
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from .identity import operational_residents
from .models import ResidentAccess

def name_key(name):
    name=unicodedata.normalize('NFKD',name).encode('ascii','ignore').decode().casefold()
    return ''.join(ch for ch in name if ch.isalnum())

def read_contacts(text):
    if len(text)>250000:raise ValidationError('A tabela excede o limite de 250 mil caracteres.')
    rows=[]
    for line in text.splitlines():
        line=line.strip()
        if not line:continue
        if '|' in line:cells=[c.strip().strip('*').strip() for c in line.strip('|').split('|')]
        elif '\t' in line:cells=[c.strip() for c in line.split('\t')]
        else:cells=next(csv.reader([line],delimiter=';' if ';' in line else ','))
        for index in range(1,min(len(cells),6),2):
            name=cells[index-1].strip();email=cells[index].strip()
            if not name and not email:continue
            if not name.strip(': -') and not email.strip(': -'):continue
            if name.casefold() in ['nome','residente'] and email.casefold() in ['email','e-mail']:continue
            rows.append({'name':name,'email':email.casefold()})
    if not rows:raise ValidationError('Nenhum par nome/e-mail encontrado. Cole a tabela ou um CSV com nome e e-mail.')
    if len(rows)>500:raise ValidationError('Importe até 500 contatos por vez.')
    return rows

def contact_preview(text):
    rows=read_contacts(text);counts=Counter(row['email'] for row in rows)
    exact=defaultdict(list);normalized=defaultdict(list)
    for resident in operational_residents():
        full=resident.get_full_name() or resident.username
        exact[' '.join(full.split()).casefold()].append(resident)
        normalized[name_key(full)].append(resident)
    for index,row in enumerate(rows):
        row.update(index=index,ready=False)
        try:
            validate_email(row['email'])
            if len(row['email'])>150:raise ValidationError('E-mail longo demais para o login.')
        except ValidationError:
            row['status']='E-mail inválido: confira o endereço original.';continue
        if not row['name'] or counts[row['email']]!=1:
            row['status']='Nome ausente ou e-mail repetido na tabela.';continue
        matches=exact.get(' '.join(row['name'].split()).casefold()) or normalized.get(name_key(row['name']),[])
        if len(matches)!=1:
            row['status']='Nome não encontrado na escala.' if not matches else 'Mais de um residente corresponde ao nome; revisão necessária.'
            continue
        resident=matches[0]
        row['resident']=resident
        if not resident.is_active:
            row['status']='Residente desativado; revisão administrativa necessária.';continue
        mapping=ResidentAccess.objects.select_related('account').filter(resident=resident).first()
        account=mapping.account if mapping else (resident if resident.username.casefold()==row['email'] else None)
        if account and not account.is_active:
            row['status']='Conta de acesso desativada; revisão administrativa necessária.';continue
        if resident.is_staff or resident.is_superuser:
            row['status']='Conta administrativa direta: revise o vínculo antes de importar.';continue
        conflicts=get_user_model().objects.filter(username__iexact=row['email'])|get_user_model().objects.filter(email__iexact=row['email'])
        allowed=[resident.pk]+([account.pk] if account else [])
        if conflicts.exclude(pk__in=allowed).exists():
            row['status']='E-mail já utilizado por outra conta.';continue
        row.update(ready=True,account=account,status='Atualizar login, preservando a senha.' if account or resident.has_usable_password() else 'Criar acesso por e-mail; residente define a própria senha.')
    # Different spellings in one table must not assign two emails to one resident.
    residents=Counter(row['resident'].pk for row in rows if row.get('ready'))
    for row in rows:
        if row.get('ready') and residents[row['resident'].pk]>1:
            row.update(ready=False,status='Residente repetido na tabela com mais de um e-mail.')
    return rows

@transaction.atomic
def apply_contacts(text,selected):
    rows=contact_preview(text);chosen=[row for row in rows if row['index'] in selected]
    if not chosen:raise ValidationError('Selecione pelo menos um contato válido.')
    if any(not row['ready'] for row in chosen):raise ValidationError('Uma associação mudou ou precisa de revisão. Confira a prévia novamente.')
    users=get_user_model();results=[]
    for row in chosen:
        resident=users.objects.select_for_update().get(pk=row['resident'].pk)
        mapping=ResidentAccess.objects.select_for_update().select_related('account').filter(resident=resident).first()
        account=mapping.account if mapping else (resident if resident.username.casefold()==row['email'] else None)
        if account is None:
            account=users(username=row['email'],email=row['email'],first_name=resident.first_name,last_name=resident.last_name,is_active=resident.is_active)
            if resident.has_usable_password():account.password=resident.password
            else:account.set_unusable_password()
            account.save()
            ResidentAccess.objects.create(account=account,resident=resident)
        else:
            account.username=row['email'];account.email=row['email']
            account.save(update_fields=['username','email'])
        # The imported identity remains the owner of schedule records, but its
        # former credentials must stop working after access moves to an alias.
        if account.pk!=resident.pk and resident.has_usable_password():
            resident.set_unusable_password()
            resident.save(update_fields=['password'])
        results.append(account)
    return results
