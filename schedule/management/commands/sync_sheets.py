from django.core.management.base import BaseCommand,CommandError
from schedule.sheets import fetch_workbook,sync_workbook,workbook_week_label,SheetFormatError
from schedule.models import SheetSource,DriveFolder
from schedule.services import expire_pending_requests
from schedule.reminders import refresh_request_notifications
from schedule.monitoring import begin_cycle,touch_cycle,set_cycle_sources,record_source_result,finish_cycle,wait_for_next_cycle

def refresh_local_requests():
    expire_pending_requests()
    refresh_request_notifications()

class Command(BaseCommand):
    help='Reúne planilhas semanais cadastradas e descobre subpastas do Drive quando configuradas.'
    def add_arguments(self,parser):
        parser.add_argument('sheet_ids',nargs='*')
        parser.add_argument('--interval',type=int,default=0)
        parser.add_argument('--registered',action='store_true')
        parser.add_argument('--only',action='store_true',help='Importa somente os IDs informados, sem descoberta de pastas.')
        parser.add_argument('--format',choices=['weekly','cinderelas'],help='Formato para cadastrar os IDs informados.')
    def handle(self,sheet_ids,interval,registered,only,**options):
        if only and not sheet_ids: raise CommandError('--only exige IDs de planilhas.')
        if interval and interval<30: raise CommandError('Intervalo mínimo: 30 segundos.')
        if not sheet_ids and not registered: raise CommandError('Informe IDs ou use --registered.')
        for sheet_id in sheet_ids:
            from schedule.drive import valid_id
            valid_id(sheet_id)
            source,_=SheetSource.objects.get_or_create(spreadsheet_id=sheet_id,defaults={'format':options.get('format') or 'weekly'})
            if options.get('format') and source.format!=options['format']:
                from schedule.models import SyncState
                if SyncState.objects.filter(source=sheet_id,last_success__isnull=False).exists():
                    raise CommandError('A fonte já foi importada com outro formato; revise o cadastro antes de alterar.')
                source.format=options['format'];source.save(update_fields=['format'])
        session=None
        try:
            while True:
                begin_cycle()
                refresh_local_requests()
                discovery_failed=False
                folders=DriveFolder.objects.none() if only else DriveFolder.objects.filter(enabled=True)
                if folders.exists():
                    try:
                        from schedule.drive import authenticated_session,discover_folder
                        if session is None: session=authenticated_session()
                        for folder in folders:
                            try:
                                count=discover_folder(session,folder)
                                self.stdout.write(f'{count} planilhas encontradas nas pastas mensais.')
                            except Exception as error:
                                discovery_failed=True
                                folder.error=f'Descoberta falhou ({type(error).__name__}). Verifique acesso de leitura à pasta.'
                                folder.save(update_fields=['error'])
                                self.stderr.write(folder.error)
                            finally:
                                touch_cycle()
                    except Exception as error:
                        discovery_failed=True
                        folders.update(error=f'Acesso Drive indisponível ({type(error).__name__}). Configure a chave de API para a pasta pública ou a credencial de leitura na instância.')
                        self.stderr.write('Pastas do Drive ainda não acessíveis; continuando com as fontes conhecidas.')
                failures=0
                sources=SheetSource.objects.filter(enabled=True)
                if only: sources=sources.filter(spreadsheet_id__in=sheet_ids)
                total_sources=sources.count()
                set_cycle_sources(total_sources)
                self.stdout.write(f'Iniciando importação de {total_sources} fontes ativas.')
                for source in sources:
                    book=None
                    source_failed=False
                    try:
                        if source.folder_id:
                            if session is None:
                                from schedule.drive import authenticated_session
                                session=authenticated_session()
                            if getattr(session,'public_drive',False):
                                book=fetch_workbook(source.spreadsheet_id,inspect_formulas=True) if source.format=='cinderelas' else fetch_workbook(source.spreadsheet_id)
                            else:
                                from schedule.drive import fetch_private_workbook
                                book=fetch_private_workbook(session,source.spreadsheet_id,inspect_formulas=True) if source.format=='cinderelas' else fetch_private_workbook(session,source.spreadsheet_id)
                        else:
                            book=fetch_workbook(source.spreadsheet_id,inspect_formulas=True) if source.format=='cinderelas' else fetch_workbook(source.spreadsheet_id)
                        if source.format=='cinderelas':
                            from schedule.cinderelas_import import sync_cinderelas
                            summary=sync_cinderelas(book,source.spreadsheet_id)
                        else:
                            summary=sync_workbook(book,source.spreadsheet_id)
                        if not source.name: source.name='Cinderelas 18–24' if source.format=='cinderelas' else workbook_week_label(book)
                        source.error='';source.save(update_fields=['name','error'])
                        self.stdout.write(summary)
                    except Exception as error:
                        source_failed=True
                        failures+=1
                        if isinstance(error,SheetFormatError):
                            source.error=f'Sincronização falhou; dados anteriores preservados. {error}'[:250]
                        else:
                            # Network/credential exceptions may contain URLs and API
                            # keys, so only known workbook errors expose details.
                            source.error=f'Sincronização falhou ({type(error).__name__}); dados anteriores preservados.'
                        source.save(update_fields=['error']);self.stderr.write(source.error)
                    finally:
                        if book: book.close()
                        record_source_result(failed=source_failed)
                refresh_request_notifications()
                finish_cycle(interval,discovery_failed=discovery_failed)
                self.stdout.write(f'Ciclo concluído: {total_sources} fontes; {failures} falhas; próxima tentativa em {interval} segundos.' if interval else f'Ciclo concluído: {total_sources} fontes; {failures} falhas.')
                if not interval:
                    if failures or folders.filter(error__gt='').exists(): raise CommandError('Há fontes com falha. Consulte as mensagens e o cadastro de fontes no administrador.')
                    return
                wait_for_next_cycle(interval,maintenance=refresh_local_requests)
        finally:
            if session: session.close()
