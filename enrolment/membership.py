from django.db.models import Q

from enrolment.apps import EnrolmentConfig


def active_household_memberships():
    from individual.models import GroupIndividual

    return GroupIndividual.objects.filter(
        is_deleted=False,
        group__is_deleted=False,
        individual__is_deleted=False,
    )


def household_head_memberships():
    """Return active memberships whose role is household head."""
    from individual.models import GroupIndividual

    return active_household_memberships().filter(
        role=GroupIndividual.Role.HEAD
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


# Subject names shared by profile criteria, order_by items and quota
# conditions. household and self name the candidate record itself.
SELF_SUBJECT = "self"
HOUSEHOLD_SUBJECT = "household"
HEAD_SUBJECT = "head"
PRIMARY_WORKER_SUBJECT = "primary_worker"
ANY_MEMBER_SUBJECT = "any_member"
# The subject that names the candidate record, by Phase type.
CANDIDATE_SUBJECTS = {"GROUP": HOUSEHOLD_SUBJECT, "INDIVIDUAL": SELF_SUBJECT}
# Household members a GROUP subject reads, with the memberships that hold them.
MEMBER_SUBJECTS = {
    PRIMARY_WORKER_SUBJECT: authoritative_primary_worker_memberships,
    HEAD_SUBJECT: household_head_memberships,
    ANY_MEMBER_SUBJECT: active_household_memberships,
}


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
