"""Separate an operator's administrative login from their imported identity."""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q

from schedule.contacts import name_key
from schedule.identity import operational_residents
from schedule.models import CalendarSubscription, ResidentAccess


class Command(BaseCommand):
    help = 'Separa o login administrativo e o acesso pessoal de um residente, preservando a escala e as senhas existentes.'

    def add_arguments(self, parser):
        parser.add_argument('--resident-name', required=True)
        parser.add_argument('--admin-email', required=True)
        parser.add_argument('--resident-email', required=True)
        parser.add_argument('--admin-username', help='Usuário administrativo atual, necessário se houver mais de um candidato.')

    @transaction.atomic
    def handle(self, **options):
        users = get_user_model()
        admin_email = options['admin_email'].strip().casefold()
        personal_email = options['resident_email'].strip().casefold()
        try:
            for email in [admin_email, personal_email]:
                validate_email(email)
                if len(email) > 150:
                    raise ValidationError('E-mail longo demais para o login.')
        except ValidationError as error:
            raise CommandError('; '.join(error.messages))
        if admin_email == personal_email:
            raise CommandError('Informe e-mails diferentes para os dois acessos.')
        matches = [person for person in operational_residents().filter(is_active=True)
                   if name_key(person.get_full_name() or person.username) == name_key(options['resident_name'])]
        if len(matches) != 1:
            raise CommandError('O nome deve identificar exatamente um residente ativo na escala.')
        resident = users.objects.select_for_update().get(pk=matches[0].pk)
        mapping = ResidentAccess.objects.select_for_update().select_related('account').filter(resident=resident).first()

        def email_account(email):
            found = list(users.objects.select_for_update().filter(Q(username__iexact=email) | Q(email__iexact=email)))
            if len(found) > 1:
                raise CommandError('E-mail associado a mais de uma conta; revise os acessos antes de separar.')
            return found[0] if found else None

        target_admin = email_account(admin_email)
        if options.get('admin_username'):
            admin = users.objects.select_for_update().filter(username=options['admin_username'], is_active=True, is_superuser=True).first()
        elif target_admin:
            admin = target_admin
        elif mapping and mapping.account.is_superuser:
            admin = mapping.account
        else:
            admins = list(users.objects.select_for_update().filter(is_active=True, is_superuser=True))
            admin = admins[0] if len(admins) == 1 else None
        if not admin or not admin.is_active or not admin.is_superuser or not admin.has_usable_password():
            raise CommandError('Identifique um administrador ativo com senha usando --admin-username.')
        if target_admin and target_admin.pk != admin.pk:
            raise CommandError('O e-mail administrativo já pertence a outra conta.')
        if admin.pk == resident.pk or resident.is_staff or resident.is_superuser:
            raise CommandError('A identidade da planilha possui privilégios administrativos; revise esse cadastro primeiro.')
        if ResidentAccess.objects.filter(account=admin).exclude(resident=resident).exists():
            raise CommandError('O administrador está vinculado a outro residente; revise o vínculo primeiro.')

        personal = email_account(personal_email)
        if personal and personal.pk == admin.pk:
            personal = None  # The administrator is about to release this email.
        if personal is None and mapping and mapping.account_id != admin.pk:
            personal = mapping.account
        if personal and (personal.pk == resident.pk or personal.is_staff or personal.is_superuser or not personal.is_active):
            raise CommandError('O acesso pessoal precisa ser uma conta comum ativa, separada da identidade importada.')
        if personal and ResidentAccess.objects.filter(account=personal).exclude(resident=resident).exists():
            raise CommandError('O e-mail pessoal já está vinculado a outro residente.')
        if mapping and mapping.account_id not in [admin.pk, personal.pk if personal else None]:
            raise CommandError('O residente já tem outro acesso; revise o vínculo para evitar logins duplicados.')

        admin.username = admin_email
        admin.email = admin_email
        admin.save(update_fields=['username', 'email'])
        if personal is None:
            personal = users(username=personal_email, email=personal_email, first_name=resident.first_name, last_name=resident.last_name)
            personal.set_unusable_password()
            personal.save()
        else:
            personal.username = personal_email
            personal.email = personal_email
            personal.save(update_fields=['username', 'email'])
        if mapping:
            mapping.account = personal
            mapping.save(update_fields=['account'])
        else:
            ResidentAccess.objects.create(account=personal, resident=resident)
        if resident.has_usable_password():
            resident.set_unusable_password()
            resident.save(update_fields=['password'])
        CalendarSubscription.objects.filter(resident=resident).exclude(account=personal).update(enabled=False)
        self.stdout.write(self.style.SUCCESS('Acessos separados. Escala, solicitações e senha administrativa preservadas.'))
        self.stdout.write('Administrador: use o e-mail administrativo e a senha atual.')
        self.stdout.write('Residente: use a senha pessoal existente.' if personal.has_usable_password() else 'Residente: gere o link individual em Contas e acessos para escolher a senha. Não há senha padrão.')
