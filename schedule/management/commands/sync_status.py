import json
import os

from django.core.management.base import BaseCommand
from django.utils import timezone

from schedule.monitoring import sync_health


class Command(BaseCommand):
    help = 'Mostra o estado do sincronizador e das fontes sem acessar o Google.'

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true')

    def handle(self, **options):
        try:
            interval = int(os.environ.get('SYNC_INTERVAL', '86400'))
        except ValueError:
            interval = 86400
        status = sync_health(interval=interval)
        if options['json']:
            self.stdout.write(json.dumps(status, default=str, ensure_ascii=False))
            return
        self.stdout.write(status['headline'])
        self.stdout.write(f'Pausa entre leituras: {status["interval_label"]}.')
        for label, key in [('Último contato', 'last_contact'), ('Último ciclo concluído', 'finished_at'), ('Próxima leitura prevista', 'next_attempt')]:
            value = status[key]
            self.stdout.write(f'{label}: {timezone.localtime(value):%d/%m/%Y %H:%M:%S}' if value else f'{label}: não registrado')
        self.stdout.write(f'Fontes ativas: {status["total_sources"]}; processadas no ciclo: {status["processed_sources"]}/{status["cycle_total_sources"]}.')
        self.stdout.write(f'Falhas de fontes: {status["source_error_count"]}; de pastas: {status["folder_error_count"]}; nunca importadas: {status["never_imported_count"]}; sem leitura recente: {status["stale_source_count"]}.')
