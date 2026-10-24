import csv
import time
from datetime import datetime
from pathlib import Path
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from openpyxl import load_workbook
from schedule.models import Shift, Exchange

class Command(BaseCommand):
    help = 'Importa CSV UTF-8 ou XLSX local. --interval SEGUNDOS repete a leitura.'
    def add_arguments(self, parser):
        parser.add_argument('path')
        parser.add_argument('--interval', type=int, default=0)
    def handle(self, path, interval, **options):
        if interval and interval < 30:
            raise CommandError('Intervalo mínimo: 30 segundos.')
        while True:
            try:
                count = self.import_file(Path(path))
                self.stdout.write(self.style.SUCCESS(f'{count} plantões importados.'))
            except (ValueError, OSError, KeyError) as error:
                raise CommandError(str(error)) from error
            if not interval:
                return
            time.sleep(interval)
    @transaction.atomic
    def import_file(self, path):
        if path.suffix.lower() == '.csv':
            with path.open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
        elif path.suffix.lower() == '.xlsx':
            book = load_workbook(path, read_only=True, data_only=True)
            try:
                values = iter(book.active.values)
                headers = next(values)
                rows = [dict(zip(headers, row)) for row in values if any(v is not None for v in row)]
            finally:
                book.close()
        else:
            raise ValueError('Use um arquivo .csv ou .xlsx.')
        seen = set()
        for number, row in enumerate(rows, 2):
            required = ['id','residente','inicio','fim','setor']
            if any(not row.get(key) for key in required):
                raise ValueError(f'Linha {number}: colunas obrigatórias: {", ".join(required)}.')
            external_id = str(row['id']).strip()
            if external_id in seen:
                raise ValueError(f'Linha {number}: id duplicado.')
            seen.add(external_id)
            try:
                resident = get_user_model().objects.get(username=str(row['residente']).strip(),is_active=True)
            except get_user_model().DoesNotExist:
                raise ValueError(f'Linha {number}: residente sem conta ativa.')
            dates = []
            for key in ['inicio','fim']:
                value = row[key]
                dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
                dates.append(timezone.make_aware(dt) if timezone.is_naive(dt) else dt)
            if dates[1] <= dates[0]:
                raise ValueError(f'Linha {number}: fim deve ser posterior ao início.')
            existing = Shift.objects.select_for_update().filter(external_id=external_id).first()
            if existing:
                exchanged = Exchange.objects.filter(status='accepted').filter(Q(offered=existing)|Q(requested=existing)).exists()
                if exchanged:
                    # Never silently undo an accepted exchange from stale spreadsheet data.
                    if (existing.starts_at,existing.ends_at,existing.sector) != (dates[0],dates[1],str(row['setor']).strip()):
                        raise ValueError(f'Linha {number}: plantão trocado exige conciliação manual antes de alterar horário/setor.')
                    continue
            Shift.objects.update_or_create(external_id=external_id,defaults={'resident':resident,'starts_at':dates[0],'ends_at':dates[1],'sector':str(row['setor']).strip()})
        return len(rows)
