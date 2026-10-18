from django.db.models import Exists, OuterRef

from core.signals import register_service_signal
from individual.models import Group, Individual
from social_protection.models import Beneficiary, GroupBeneficiary

from enrolment.candidate_sources import GROUP_SOURCE, INDIVIDUAL_SOURCE
from enrolment.criteria import load_enrolment_phase
from enrolment.ranking import rank_and_cap_queryset


def build_individual_enrolment_queryset(custom_filters, benefit_plan_id, status):
    return INDIVIDUAL_SOURCE.eligible_queryset(custom_filters, benefit_plan_id, status)


def build_group_enrolment_queryset(custom_filters, benefit_plan_id, status):
    return GROUP_SOURCE.eligible_queryset(custom_filters, benefit_plan_id, status)


def _build_selection(
    *,
    source,
    assignment_model,
    assignment_field,
    custom_filters,
    benefit_plan_id,
    status,
    user=None,
    materialize_selected_ids=True,
):
    benefit_plan = load_enrolment_phase(
        benefit_plan_id,
        status,
        source.phase_type,
    )
    eligible = source.eligible_queryset(custom_filters, benefit_plan_id, status)
    active_assignment = assignment_model.objects.filter(
        **{
            f"{assignment_field}_id": OuterRef("pk"),
            "benefit_plan_id": benefit_plan_id,
            "status": status,
            "is_deleted": False,
        }
    )
    eligible = eligible.annotate(_assigned_to_selected_plan=Exists(active_assignment))
    assigned = eligible.filter(_assigned_to_selected_plan=True)
    unassigned = eligible.filter(_assigned_to_selected_plan=False)
    current_count = assignment_model.objects.filter(
        benefit_plan_id=benefit_plan_id,
        status=status,
        is_deleted=False,
    ).count()
    selected, metadata = rank_and_cap_queryset(
        unassigned,
        benefit_plan,
        status,
        current_count,
        materialize_selected_ids=materialize_selected_ids,
    )
    return benefit_plan, eligible, assigned, selected, metadata


def build_individual_enrolment_selection(
    custom_filters,
    benefit_plan_id,
    status,
    user=None,
    materialize_selected_ids=True,
):
    _, eligible, assigned, selected, metadata = _build_selection(
        source=INDIVIDUAL_SOURCE,
        assignment_model=Beneficiary,
        assignment_field="individual",
        custom_filters=custom_filters,
        benefit_plan_id=benefit_plan_id,
        status=status,
        user=user,
        materialize_selected_ids=materialize_selected_ids,
    )
    return {
        "individuals_assigned_to_selected_programme": assigned,
        "individuals_not_assigned_to_selected_programme": selected,
        "individual_query_with_filters": eligible,
        "benefit_plan_id": benefit_plan_id,
        "status": status,
        "user": user,
        **metadata,
    }


def build_group_enrolment_selection(
    custom_filters,
    benefit_plan_id,
    status,
    user=None,
    materialize_selected_ids=True,
):
    _, eligible, assigned, selected, metadata = _build_selection(
        source=GROUP_SOURCE,
        assignment_model=GroupBeneficiary,
        assignment_field="group",
        custom_filters=custom_filters,
        benefit_plan_id=benefit_plan_id,
        status=status,
        user=user,
        materialize_selected_ids=materialize_selected_ids,
    )
    return {
        "groups_assigned_to_selected_programme": assigned,
        "groups_not_assigned_to_selected_programme": selected,
        "group_query_with_filters": eligible,
        "benefit_plan_id": benefit_plan_id,
        "status": status,
        "user": user,
        **metadata,
    }


def build_individual_enrolment_summary(custom_filters, benefit_plan_id, status):
    selection = build_individual_enrolment_selection(
        custom_filters,
        benefit_plan_id,
        status,
    )
    query = selection["individual_query_with_filters"]
    active_assignment = Beneficiary.objects.filter(
        individual_id=OuterRef("pk"),
        status=status,
        is_deleted=False,
    )
    active_selected_assignment = active_assignment.filter(benefit_plan_id=benefit_plan_id)
    query = query.annotate(
        _assigned_to_any_plan=Exists(active_assignment),
        _assigned_to_selected_plan=Exists(active_selected_assignment),
    )
    selected_count = query.count()
    not_assigned_count = query.filter(_assigned_to_any_plan=False).count()
    return {
        "number_of_selected_individuals": selected_count,
        "total_number_of_individuals": Individual.objects.filter(is_deleted=False).count(),
        "number_of_individuals_not_assigned_to_programme": not_assigned_count,
        "number_of_individuals_assigned_to_programme": selected_count - not_assigned_count,
        "number_of_individuals_assigned_to_selected_programme": query.filter(
            _assigned_to_selected_plan=True
        ).count(),
        "number_of_individuals_to_upload": selection["will_enrol"],
        **_summary_metadata(selection),
    }


def build_group_enrolment_summary(custom_filters, benefit_plan_id, status):
    selection = build_group_enrolment_selection(custom_filters, benefit_plan_id, status)
    query = selection["group_query_with_filters"]
    active_assignment = GroupBeneficiary.objects.filter(
        group_id=OuterRef("pk"),
        status=status,
        is_deleted=False,
    )
    active_selected_assignment = active_assignment.filter(benefit_plan_id=benefit_plan_id)
    query = query.annotate(
        _assigned_to_any_plan=Exists(active_assignment),
        _assigned_to_selected_plan=Exists(active_selected_assignment),
    )
    selected_count = query.count()
    not_assigned_count = query.filter(_assigned_to_any_plan=False).count()
    return {
        "number_of_selected_groups": selected_count,
        "total_number_of_groups": Group.objects.filter(is_deleted=False).count(),
        "number_of_groups_not_assigned_to_programme": not_assigned_count,
        "number_of_groups_assigned_to_programme": selected_count - not_assigned_count,
        "number_of_groups_assigned_to_selected_programme": query.filter(
            _assigned_to_selected_plan=True
        ).count(),
        "number_of_groups_to_upload": selection["will_enrol"],
        **_summary_metadata(selection),
    }


def _summary_metadata(selection):
    return {
        "pool_size": selection["pool_size"],
        "cap_applied": selection["cap_applied"],
        "will_enrol": selection["will_enrol"],
        "enrolment_ranking": selection["ranking"],
        "percentage": selection["percentage"],
    }


class EnrolmentService:
    def __init__(self, user):
        self.user = user

    @register_service_signal("enrolment_service.select_individuals_to_benefit_plan")
    def select_individuals_to_benefit_plan(
        self, custom_filters, benefit_plan_id, status, user=None
    ):
        return build_individual_enrolment_selection(
            custom_filters,
            benefit_plan_id,
            status,
            user or self.user,
        )

    @register_service_signal("enrolment_service.select_groups_to_benefit_plan")
    def select_groups_to_benefit_plan(
        self, custom_filters, benefit_plan_id, status, user=None
    ):
        return build_group_enrolment_selection(
            custom_filters,
            benefit_plan_id,
            status,
            user or self.user,
        )


# American-spelling compatibility with existing callers.
build_individual_enrollment_queryset = build_individual_enrolment_queryset
build_group_enrollment_queryset = build_group_enrolment_queryset
build_individual_enrollment_selection = build_individual_enrolment_selection
build_group_enrollment_selection = build_group_enrolment_selection
build_individual_enrollment_summary = build_individual_enrolment_summary
build_group_enrollment_summary = build_group_enrolment_summary
