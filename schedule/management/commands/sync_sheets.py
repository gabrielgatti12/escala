import time
from django.core.management.base import BaseCommand,CommandError
from schedule.sheets import fetch_workbook,sync_workbook
class Command(BaseCommand):
    help='Sincroniza Google Sheets acessíveis por link sem alterar a planilha original.'
    def add_arguments(self,parser):
        parser.add_argument('sheet_ids',nargs='+')
        parser.add_argument('--interval',type=int,default=0)
    def handle(self,sheet_ids,interval,**options):
        if interval and interval<30: raise CommandError('Intervalo mínimo: 30 segundos.')
        while True:
            for sheet_id in sheet_ids:
                book=None
                try:
                    book=fetch_workbook(sheet_id)
                    self.stdout.write(sync_workbook(book,sheet_id))
                except Exception as error:
                    if not interval: raise CommandError(str(error)) from error
                    self.stderr.write(f'Sincronização falhou ({type(error).__name__}); dados anteriores preservados.')
                finally:
                    if book: book.close()
            if not interval: return
            time.sleep(interval)
