from django.contrib.auth.models import AnonymousUser
from django.db.models import Count, Exists, OuterRef

from core.custom_filters import CustomFilterWizardStorage
from individual.models import Group, Individual

from enrolment.criteria import merge_enrolment_criteria
from enrolment.district import apply_district_filters, scoped_groups
from enrolment.membership import (
    active_household_memberships,
    primary_worker_membership_filter,
)


class IndividualCandidateSource:
    phase_type = "INDIVIDUAL"

    def authorized_queryset(self, user=None):
        queryset = Individual.objects.filter(is_deleted=False)
        return Individual.get_queryset(queryset, user or AnonymousUser()).distinct()

    def eligible_queryset(self, custom_filters, benefit_plan_id, status, user=None):
        # Row security must be applied before criteria, ranking, counting, or
        # pagination so no later operation can widen the authorized population.
        query = self.authorized_queryset(user)
        filters = merge_enrolment_criteria(
            custom_filters,
            benefit_plan_id,
            status,
            self.phase_type,
        )
        active_membership = active_household_memberships().filter(
            individual_id=OuterRef("pk")
        )
        query = query.annotate(
            _has_active_household_membership=Exists(active_membership)
        ).filter(_has_active_household_membership=False)
        return CustomFilterWizardStorage.build_custom_filters_queryset(
            "individual",
            "Individual",
            filters,
            query,
        ).distinct()


class GroupCandidateSource:
    phase_type = "GROUP"

    def authorized_queryset(self, user=None):
        queryset = Group.objects.filter(is_deleted=False)
        return scoped_groups(queryset, user).distinct()

    def eligible_queryset(self, custom_filters, benefit_plan_id, status, user=None):
        query = self.authorized_queryset(user).annotate(
            _authoritative_primary_workers=Count(
                "groupindividuals",
                filter=primary_worker_membership_filter(),
            )
        ).filter(_authoritative_primary_workers=1)
        filters = merge_enrolment_criteria(
            custom_filters,
            benefit_plan_id,
            status,
            self.phase_type,
        )
        query, filters = apply_district_filters(query, filters, user)
        return CustomFilterWizardStorage.build_custom_filters_queryset(
            "individual",
            "Group",
            filters,
            query,
        ).distinct()


INDIVIDUAL_SOURCE = IndividualCandidateSource()
GROUP_SOURCE = GroupCandidateSource()
