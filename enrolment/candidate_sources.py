from django.contrib.auth.models import AnonymousUser

from core.custom_filters import CustomFilterWizardStorage
from individual.models import Group, Individual

from enrolment.criteria import merge_enrolment_criteria


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
        # Household membership does not disqualify a person from an individual
        # programme such as RMEP. Programme membership is handled by explicit
        # Phase criteria and assignment checks instead.
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
        return Group.get_queryset(queryset, user or AnonymousUser()).distinct()

    def eligible_queryset(self, custom_filters, benefit_plan_id, status, user=None):
        query = self.authorized_queryset(user)
        filters = merge_enrolment_criteria(
            custom_filters,
            benefit_plan_id,
            status,
            self.phase_type,
        )
        return CustomFilterWizardStorage.build_custom_filters_queryset(
            "individual",
            "Group",
            filters,
            query,
        ).distinct()


INDIVIDUAL_SOURCE = IndividualCandidateSource()
GROUP_SOURCE = GroupCandidateSource()
