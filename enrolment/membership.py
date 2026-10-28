from django.db.models import Q

from enrolment.apps import EnrolmentConfig


def active_household_memberships():
    from individual.models import GroupIndividual

    return GroupIndividual.objects.filter(
        is_deleted=False,
        group__is_deleted=False,
        individual__is_deleted=False,
    )


def authoritative_primary_worker_memberships():
    """Return active primary workers belonging to verified households."""
    primary_worker_lookup = (
        f"json_ext__{EnrolmentConfig.registry_primary_worker_field}"
    )
    verification_lookup = (
        f"group__json_ext__{EnrolmentConfig.registry_household_verification_field}"
    )
    return active_household_memberships().filter(
        **{
            primary_worker_lookup: True,
            verification_lookup: EnrolmentConfig.registry_verified_household_value,
        }
    )


def primary_worker_membership_filter(prefix="groupindividuals"):
    """Build the equivalent relation filter for Group queryset annotations."""
    return Q(
        **{
            f"{prefix}__is_deleted": False,
            f"{prefix}__individual__is_deleted": False,
            f"{prefix}__json_ext__{EnrolmentConfig.registry_primary_worker_field}": True,
            f"json_ext__{EnrolmentConfig.registry_household_verification_field}": (
                EnrolmentConfig.registry_verified_household_value
            ),
        }
    )
