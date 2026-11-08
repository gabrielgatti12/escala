"""Resolve names against existing schedule identities without creating users."""

from collections import defaultdict
import unicodedata

from .identity import operational_residents


def _exact_name(value):
    return ' '.join(value.split()).casefold()


def _normalized_name(value):
    decomposed = unicodedata.normalize('NFKD', value).casefold()
    return ''.join(char for char in decomposed if char.isalnum())


def resolve_roster_names(names):
    """Return original-name → active source User and safe issues for unresolved names.

    Complete names take priority over their accent/punctuation variants. Both
    matching methods require a single existing identity; partial or similar
    names are never guessed. Login aliases and usernames are not roster names.
    """
    exact = defaultdict(list)
    normalized = defaultdict(list)
    residents = operational_residents().filter(is_active=True, resident_access__isnull=True)
    for resident in residents:
        full_name = resident.get_full_name()
        if not full_name.strip():
            continue
        exact[_exact_name(full_name)].append(resident)
        key = _normalized_name(full_name)
        if key:
            normalized[key].append(resident)

    resolved = {}
    issues = []
    seen = set()
    for original in names:
        if not isinstance(original, str):
            issues.append('Nome de residente inválido na origem; informe o nome completo.')
            continue
        if original in seen:
            continue
        seen.add(original)
        display_name = ' '.join(original.split())
        if not display_name or '@' in display_name or not _normalized_name(display_name):
            issues.append('Nome de residente inválido na origem; informe o nome completo, sem e-mail.')
            continue
        matches = exact.get(_exact_name(original)) or normalized.get(_normalized_name(original), [])
        if len(matches) == 1:
            resolved[original] = matches[0]
        elif matches:
            issues.append(f'Nome ambíguo entre os residentes ativos: {display_name}. Revisão necessária.')
        else:
            issues.append(f'Nome não encontrado entre os residentes ativos: {display_name}.')
    return resolved, issues
