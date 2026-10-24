import time
from django.core.management.base import BaseCommand,CommandError
from schedule.sheets import fetch_workbook,sync_workbook
from schedule.models import SheetSource,DriveFolder

class Command(BaseCommand):
    help='Reúne planilhas semanais cadastradas e descobre subpastas do Drive quando configuradas.'
    def add_arguments(self,parser):
        parser.add_argument('sheet_ids',nargs='*')
        parser.add_argument('--interval',type=int,default=0)
        parser.add_argument('--registered',action='store_true')
    def handle(self,sheet_ids,interval,registered,**options):
        if interval and interval<30: raise CommandError('Intervalo mínimo: 30 segundos.')
        if not sheet_ids and not registered: raise CommandError('Informe IDs ou use --registered.')
        for sheet_id in sheet_ids:
            from schedule.drive import valid_id
            valid_id(sheet_id)
            SheetSource.objects.get_or_create(spreadsheet_id=sheet_id)
        session=None
        try:
            while True:
                folders=DriveFolder.objects.filter(enabled=True)
                if folders.exists():
                    try:
                        from schedule.drive import authenticated_session,discover_folder
                        if session is None: session=authenticated_session()
                        for folder in folders:
                            try:
                                count=discover_folder(session,folder)
                                self.stdout.write(f'{count} planilhas encontradas nas pastas mensais.')
                            except Exception as error:
                                folder.error=f'Descoberta falhou ({type(error).__name__}). Verifique acesso de leitura à pasta.'
                                folder.save(update_fields=['error'])
                                self.stderr.write(folder.error)
                    except Exception as error:
                        folders.update(error=f'Credencial Drive indisponível ({type(error).__name__}). Configure o arquivo de credencial de leitura na instância.')
                        self.stderr.write('Pastas do Drive ainda não acessíveis; continuando com as fontes conhecidas.')
                failures=0
                for source in SheetSource.objects.filter(enabled=True):
                    book=None
                    try:
                        if source.folder_id:
                            if session is None:
                                from schedule.drive import authenticated_session
                                session=authenticated_session()
                            from schedule.drive import fetch_private_workbook
                            book=fetch_private_workbook(session,source.spreadsheet_id)
                        else:
                            book=fetch_workbook(source.spreadsheet_id)
                        summary=sync_workbook(book,source.spreadsheet_id)
                        source.error='';source.save(update_fields=['error'])
                        self.stdout.write(summary)
                    except Exception as error:
                        failures+=1
                        source.error=f'Sincronização falhou ({type(error).__name__}); dados anteriores preservados.'
                        source.save(update_fields=['error']);self.stderr.write(source.error)
                    finally:
                        if book: book.close()
                if not interval:
                    if failures or folders.filter(error__gt='').exists(): raise CommandError('Há fontes com falha. Consulte as mensagens e o cadastro de fontes no administrador.')
                    return
                time.sleep(interval)
        finally:
            if session: session.close()
