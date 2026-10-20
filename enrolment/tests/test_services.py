from django.test import TestCase, override_settings

from core.test_helpers import LogInHelper
from enrolment.services import build_individual_enrolment_selection
from individual.tests.test_helpers import create_group_with_individual, create_individual
from social_protection.tests.test_helpers import create_benefit_plan


@override_settings(ROW_SECURITY=False)
class IndividualEnrolmentSelectionTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def test_deleted_group_releases_individual_for_enrolment(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            'type': 'INDIVIDUAL',
        })
        _, group, group_individual = create_group_with_individual(self.user.username)
        create_individual(self.user.username)

        selection = build_individual_enrolment_selection(
            [], str(benefit_plan.id), 'ACTIVE', self.user,
        )
        self.assertEqual(selection['individual_query_with_filters'].count(), 1)

        group.delete(user=self.user)
        group_individual.delete(user=self.user)

        selection = build_individual_enrolment_selection(
            [], str(benefit_plan.id), 'ACTIVE', self.user,
        )
        self.assertEqual(selection['individual_query_with_filters'].count(), 2)
        self.assertEqual(
            selection['individuals_not_assigned_to_selected_programme'].count(),
            2,
        )
