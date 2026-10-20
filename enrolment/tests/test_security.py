import copy

from django.test import override_settings

from enrolment.services import (
    EnrolmentService,
    build_group_enrolment_selection,
    build_group_enrolment_summary,
    build_individual_enrolment_selection,
    build_individual_enrolment_summary,
)
from individual.models import Group, Individual
from individual.tests.test_helpers import (
    IndividualGQLTestCase,
    create_group,
    create_individual,
)
from social_protection.apps import SocialProtectionConfig
from social_protection.tests.test_helpers import create_benefit_plan


@override_settings(ROW_SECURITY=True)
class EnrolmentRowSecurityTest(IndividualGQLTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.individual_plan = create_benefit_plan(
            cls.admin_user.username,
            payload_override={
                "type": "INDIVIDUAL",
                "beneficiary_data_schema": {"properties": {}},
                "json_ext": {"advanced_criteria": {"ACTIVE": []}},
            },
        )
        cls.group_plan = create_benefit_plan(
            cls.admin_user.username,
            payload_override={
                "type": "GROUP",
                "beneficiary_data_schema": {"properties": {}},
                "json_ext": {"advanced_criteria": {"ACTIVE": []}},
            },
        )
        cls.individual_a = create_individual(
            cls.admin_user.username, {"location": cls.village_a}
        )
        cls.individual_b = create_individual(
            cls.admin_user.username, {"location": cls.village_b}
        )
        cls.group_a = create_group(
            cls.admin_user.username, {"location": cls.village_a}
        )
        cls.group_b = create_group(
            cls.admin_user.username, {"location": cls.village_b}
        )

    def setUp(self):
        self.original_mandatory_criteria = copy.deepcopy(
            SocialProtectionConfig.mandatory_enrollment_criteria
        )
        SocialProtectionConfig.mandatory_enrollment_criteria = {
            "INDIVIDUAL": {},
            "GROUP": {},
        }

    def tearDown(self):
        SocialProtectionConfig.mandatory_enrollment_criteria = (
            self.original_mandatory_criteria
        )

    def test_individual_preview_summary_ranking_and_confirmation_are_scoped(self):
        selection = build_individual_enrolment_selection(
            [], str(self.individual_plan.id), "ACTIVE", self.dist_a_user
        )
        selected_ids = set(
            selection["individuals_not_assigned_to_selected_programme"]
            .values_list("id", flat=True)
        )
        self.assertIn(self.individual_a.id, selected_ids)
        self.assertNotIn(self.individual_b.id, selected_ids)
        self.assertFalse(
            selection["individuals_not_assigned_to_selected_programme"]
            .filter(id=self.individual_b.id)
            .exists()
        )

        summary = build_individual_enrolment_summary(
            [], str(self.individual_plan.id), "ACTIVE", self.dist_a_user
        )
        authorized_total = Individual.get_queryset(
            Individual.objects.filter(is_deleted=False), self.dist_a_user
        ).distinct().count()
        self.assertEqual(summary["total_number_of_individuals"], authorized_total)

        confirmation = EnrolmentService.select_individuals_to_benefit_plan.__wrapped__(
            EnrolmentService(self.dist_a_user),
            [],
            str(self.individual_plan.id),
            "ACTIVE",
            self.dist_a_user,
        )
        self.assertFalse(
            confirmation["individuals_not_assigned_to_selected_programme"]
            .filter(id=self.individual_b.id)
            .exists()
        )

    def test_group_preview_summary_ranking_and_confirmation_are_scoped(self):
        selection = build_group_enrolment_selection(
            [], str(self.group_plan.id), "ACTIVE", self.dist_a_user
        )
        selected_ids = set(
            selection["groups_not_assigned_to_selected_programme"]
            .values_list("id", flat=True)
        )
        self.assertIn(self.group_a.id, selected_ids)
        self.assertNotIn(self.group_b.id, selected_ids)
        self.assertFalse(
            selection["groups_not_assigned_to_selected_programme"]
            .filter(id=self.group_b.id)
            .exists()
        )

        summary = build_group_enrolment_summary(
            [], str(self.group_plan.id), "ACTIVE", self.dist_a_user
        )
        authorized_total = Group.get_queryset(
            Group.objects.filter(is_deleted=False), self.dist_a_user
        ).distinct().count()
        self.assertEqual(summary["total_number_of_groups"], authorized_total)

        confirmation = EnrolmentService.select_groups_to_benefit_plan.__wrapped__(
            EnrolmentService(self.dist_a_user),
            [],
            str(self.group_plan.id),
            "ACTIVE",
            self.dist_a_user,
        )
        self.assertFalse(
            confirmation["groups_not_assigned_to_selected_programme"]
            .filter(id=self.group_b.id)
            .exists()
        )
