"""Calendar choices shared by the weekly view and its navigation."""
from collections import defaultdict
from django.utils import timezone
from .models import Shift, SharedActivity, IncompleteActivity


def next_activity_day(resident, now):
    today = timezone.localdate(now)
    dates = []
    for model, owner in [(Shift, 'resident'), (SharedActivity, 'residents')]:
        query = model.objects.filter(active=True, ends_at__gt=now, **{owner: resident})
        if model is SharedActivity:
            query = query.filter(allocated_to__isnull=True)
        item = query.order_by('starts_at').first()
        if item:
            dates.append(timezone.localdate(item.starts_at))
    missing = IncompleteActivity.objects.filter(residents=resident, date__gte=today).order_by('date').first()
    if missing:
        dates.append(missing.date)
    return min(dates) if dates else None


def allocation_choices(activities):
    groups = defaultdict(list)
    for activity in activities:
        people = list(activity.residents.all())
        if len(people) != 2:
            continue
        pair = ','.join(str(pk) for pk in sorted(person.pk for person in people))
        day = timezone.localdate(activity.starts_at).isoformat()
        groups[pair, day].append({
            'activity': activity, 'pair': pair, 'day': day,
            'start': activity.starts_at.isoformat(), 'end': activity.ends_at.isoformat(),
        })
    choices = []
    for group in groups.values():
        for option in group:
            activity = option['activity']
            if any(other['activity'].pk != activity.pk and
                   activity.starts_at < other['activity'].ends_at and
                   other['activity'].starts_at < activity.ends_at for other in group):
                choices.append(option)
    return sorted(choices, key=lambda option: (option['activity'].starts_at, option['activity'].pk))
