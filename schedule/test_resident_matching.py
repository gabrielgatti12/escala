from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .models import IncompleteActivity, ResidentAccess, SharedActivity, Shift
from .resident_matching import resolve_roster_names


class ResidentMatchingTests(TestCase):
    def source(self, username, name, active=True, has_shift=True):
        user = get_user_model().objects.create_user(username, first_name=name, is_active=active)
        if has_shift:
            start = timezone.now() + timedelta(days=1)
            Shift.objects.create(
                external_id=f'matching:{username}', resident=user, starts_at=start,
                ends_at=start + timedelta(hours=4), sector='Setor de teste',
            )
        return user

    def test_full_names_ignore_case_and_spaces_and_preserve_original_keys(self):
        resident = self.source('matching-source-a', 'Pessoa Aurora', has_shift=False)
        resident.first_name = 'Pessoa'
        resident.last_name = 'Aurora'
        resident.save(update_fields=['first_name', 'last_name'])
        incomplete = IncompleteActivity.objects.create(
            external_id='matching-incomplete', source='test', source_cell='F8',
            date=timezone.localdate(), sector='Setor de teste', raw_time='',
        )
        incomplete.residents.add(resident)
        original = '  PESSOA   Aurora  '

        mapping, issues = resolve_roster_names([original])

        self.assertEqual(mapping, {original: resident})
        self.assertEqual(issues, [])

    def test_unique_accent_and_punctuation_variants_resolve_to_same_source(self):
        resident = self.source('matching-source-b', "Pessoa Ágata D'Água")

        mapping, issues = resolve_roster_names(['Pessoa Agata D Agua', 'Pessoa Ágata D’Água'])

        self.assertEqual(set(mapping.values()), {resident})
        self.assertEqual(len(mapping), 2)
        self.assertEqual(issues, [])

    def test_exact_match_has_priority_over_a_normalization_collision(self):
        accented = self.source('matching-source-accented', 'Pessoa Ágata')
        plain = self.source('matching-source-plain', 'Pessoa Agata')

        mapping, issues = resolve_roster_names(['pessoa ágata', 'Pessoa Agata', 'Pessoa-Agata'])

        self.assertEqual(mapping, {'pessoa ágata': accented, 'Pessoa Agata': plain})
        self.assertEqual(len(issues), 1)
        self.assertIn('ambíguo', issues[0])

    def test_duplicate_exact_full_names_are_ambiguous(self):
        self.source('matching-duplicate-a', 'Pessoa Aurora')
        self.source('matching-duplicate-b', 'Pessoa Aurora')

        mapping, issues = resolve_roster_names(['Pessoa Aurora'])

        self.assertEqual(mapping, {})
        self.assertEqual(len(issues), 1)
        self.assertIn('ambíguo', issues[0])

    def test_unknown_and_partial_names_do_not_create_or_approximate_users(self):
        self.source('matching-known', 'Pessoa Aurora')
        before = get_user_model().objects.count()

        mapping, issues = resolve_roster_names(['Pessoa', 'Pessoa Auror', 'Pessoa Brisa'])

        self.assertEqual(mapping, {})
        self.assertEqual(len(issues), 3)
        self.assertTrue(all('não encontrado' in issue for issue in issues))
        self.assertEqual(get_user_model().objects.count(), before)

    def test_inactive_and_non_operational_users_are_not_candidates(self):
        self.source('matching-inactive', 'Pessoa Inativa', active=False)
        self.source('matching-no-roster', 'Pessoa Sem Escala', has_shift=False)
        inactive_shift = self.source('matching-inactive-shift', 'Pessoa Sem Atividade')
        Shift.objects.filter(resident=inactive_shift).update(active=False)

        mapping, issues = resolve_roster_names(['Pessoa Inativa', 'Pessoa Sem Escala', 'Pessoa Sem Atividade'])

        self.assertEqual(mapping, {})
        self.assertEqual(len(issues), 3)

    def test_mapped_email_login_never_replaces_the_source_identity(self):
        resident = self.source('matching-profile', 'Pessoa Aurora')
        alias = get_user_model().objects.create_user(
            'matching-alias@example.test', first_name='Pessoa Aurora', password='Test-password-932!',
        )
        ResidentAccess.objects.create(account=alias, resident=resident)
        # Even if an old record accidentally points to the alias, do not use it.
        Shift.objects.create(
            external_id='matching-alias-shift', resident=alias,
            starts_at=timezone.now(), ends_at=timezone.now() + timedelta(hours=1), sector='Setor de teste',
        )

        mapping, issues = resolve_roster_names(['Pessoa Aurora'])

        self.assertEqual(mapping, {'Pessoa Aurora': resident})
        self.assertEqual(issues, [])
        self.assertNotEqual(mapping['Pessoa Aurora'], alias)

    def test_active_shared_activity_is_sufficient_for_an_existing_resident(self):
        resident = self.source('matching-shared', 'Pessoa Compartilhada', has_shift=False)
        activity = SharedActivity.objects.create(
            external_id='matching-shared-activity', source='test', sector='Setor de teste',
            starts_at=timezone.now(), ends_at=timezone.now() + timedelta(hours=1),
        )
        activity.residents.add(resident)

        mapping, issues = resolve_roster_names(['Pessoa Compartilhada'])

        self.assertEqual(mapping, {'Pessoa Compartilhada': resident})
        self.assertEqual(issues, [])

    def test_usernames_and_blank_names_do_not_match_or_expose_email_addresses(self):
        self.source('matching-technical-username', '', has_shift=True)
        self.source('matching-person@example.test', 'Pessoa Aurora')

        mapping, issues = resolve_roster_names([
            'matching-technical-username', 'matching-person@example.test', ' ', '---',
        ])

        self.assertEqual(mapping, {})
        self.assertEqual(len(issues), 4)
        self.assertTrue(all('@' not in issue for issue in issues))

    def test_repeated_original_name_is_processed_once(self):
        resident = self.source('matching-repeated', 'Pessoa Aurora')

        mapping, issues = resolve_roster_names(['Pessoa Aurora', 'Pessoa Aurora', 'Pessoa Ausente', 'Pessoa Ausente'])

        self.assertEqual(mapping, {'Pessoa Aurora': resident})
        self.assertEqual(len(issues), 1)
