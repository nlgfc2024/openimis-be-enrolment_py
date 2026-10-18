from core.custom_filters import CustomFilterWizardStorage
from individual.models import Group, Individual

from enrolment.criteria import merge_enrolment_criteria


class IndividualCandidateSource:
    phase_type = "INDIVIDUAL"

    def eligible_queryset(self, custom_filters, benefit_plan_id, status):
        filters = merge_enrolment_criteria(
            custom_filters,
            benefit_plan_id,
            status,
            self.phase_type,
        )
        query = Individual.objects.filter(is_deleted=False)
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

    def eligible_queryset(self, custom_filters, benefit_plan_id, status):
        filters = merge_enrolment_criteria(
            custom_filters,
            benefit_plan_id,
            status,
            self.phase_type,
        )
        query = Group.objects.filter(is_deleted=False)
        return CustomFilterWizardStorage.build_custom_filters_queryset(
            "individual",
            "Group",
            filters,
            query,
        ).distinct()


INDIVIDUAL_SOURCE = IndividualCandidateSource()
GROUP_SOURCE = GroupCandidateSource()
