"""Reserved household enrollment filters over the actual location relationship."""
import re

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError

from core.models import InteractiveUser
from location.models import Location, LocationManager, extend_allowed_locations


DISTRICT_FIELD = "district_id"


def district_from_condition(condition):
    """Return a validated district, or None for an unrelated custom filter."""
    if not isinstance(condition, str):
        raise ValidationError("Malformed enrollment operator filter.")
    expression, separator, value = condition.partition("=")
    if expression.split("__", 1)[0] != DISTRICT_FIELD:
        return None
    if (
        not separator
        or expression != "district_id__exact__integer"
        or not re.fullmatch(r"[1-9][0-9]*", value)
        or len(value) > 10
        or int(value) > 2147483647
    ):
        raise ValidationError("District filter requires Equals and a positive integer location ID.")
    from enrolment.apps import EnrolmentConfig
    if EnrolmentConfig.enable_household_district_filter is not True:
        raise ValidationError("Household district filtering is disabled.")
    district = Location.objects.filter(
        id=int(value), type="R", *Location.filter_validity()
    ).first()
    if district is None:
        raise ValidationError("Select an active District (R).")
    return district


def scoped_groups(query, user):
    """Use the household search access policy, including its missing-location rule."""
    if user is None or user.is_anonymous:
        raise PermissionDenied("An authenticated user is required for household enrollment.")
    return query.model.get_queryset(query, user)


def district_location_ids(district):
    # Strict expansion goes down only: ancestors must not bring in sibling districts.
    return Location.objects.filter(
        id__in=extend_allowed_locations([district.id], strict=True),
        *Location.filter_validity(),
    ).values_list("id", flat=True)


def check_district_access(district, user):
    if user is None or user.is_anonymous:
        raise PermissionDenied("An authenticated user is required for household enrollment.")
    if not settings.ROW_SECURITY or user.is_imis_admin:
        return
    interactive_user = getattr(user, "_u", user)
    # Match Group.get_queryset's existing technical-user policy.
    if not isinstance(interactive_user, InteractiveUser) or interactive_user.is_superuser:
        return
    allowed = extend_allowed_locations(
        LocationManager().get_allowed_ids(interactive_user), strict=True
    )
    if not district_location_ids(district).filter(id__in=allowed).exists():
        raise PermissionDenied("The selected district is outside your permitted locations.")


def apply_district_filters(query, filters, user, *, district_scopes=None):
    """Consume reserved relationship filters before the registry JSON wizard."""
    remaining = []
    for condition in filters:
        district = district_from_condition(condition)
        if district is None:
            remaining.append(condition)
        else:
            check_district_access(district, user)
            location_ids = district_location_ids(district)
            if district_scopes is not None:
                district_scopes[condition] = location_ids
            query = query.filter(location_id__in=location_ids)
    return query, remaining
