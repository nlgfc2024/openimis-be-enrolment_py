import copy
from types import SimpleNamespace

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, override_settings

from enrolment.candidate_sources import GROUP_SOURCE, INDIVIDUAL_SOURCE
from enrolment.criteria import merge_enrolment_criteria

from enrolment.services import (
    EnrolmentService,
    build_group_enrolment_selection,
    build_group_enrolment_summary,
    build_individual_enrolment_selection,
    build_individual_enrolment_summary,
)
from enrolment.apps import EnrolmentConfig
from individual.models import Group, Individual
from individual.tests.test_helpers import (
    IndividualGQLTestCase,
    create_group,
    create_individual,
    add_individual_to_group,
)
from social_protection.tests.test_helpers import create_benefit_plan


class CandidateAuthenticationTest(SimpleTestCase):
    def test_reused_phase_still_validates_identity_deletion_type_and_status(self):
        for changes, plan_id, status in (
            ({}, "another-plan", "ACTIVE"),
            ({"is_deleted": True}, "plan", "ACTIVE"),
            ({"type": "INDIVIDUAL"}, "plan", "ACTIVE"),
            ({}, "plan", "INVALID"),
        ):
            phase = SimpleNamespace(**{"pk": "plan", "type": "GROUP", "is_deleted": False, **changes})
            with self.subTest(changes=changes, plan_id=plan_id, status=status):
                with self.assertRaises(ValidationError):
                    merge_enrolment_criteria([], plan_id, status, "GROUP", benefit_plan=phase)

    def test_both_sources_reject_missing_and_anonymous_users_before_querying(self):
        for row_security in (True, False):
            for source in (INDIVIDUAL_SOURCE, GROUP_SOURCE):
                for user in (None, AnonymousUser()):
                    with self.subTest(source=source.phase_type, user=user, row_security=row_security):
                        with override_settings(ROW_SECURITY=row_security):
                            with self.assertRaisesMessage(PermissionDenied, "authenticated user"):
                                source.authorized_queryset(user)
                            with self.assertRaisesMessage(PermissionDenied, "authenticated user"):
                                source.eligible_queryset([], "unused-plan", "ACTIVE", user)


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
            cls.admin_user.username,
            {"location": cls.village_a, "json_ext": {"validation_status": "VERIFIED"}},
        )
        cls.group_b = create_group(
            cls.admin_user.username,
            {"location": cls.village_b, "json_ext": {"validation_status": "VERIFIED"}},
        )
        for group in (cls.group_a, cls.group_b):
            individual = create_individual(
                cls.admin_user.username, {"location": group.location}
            )
            membership = add_individual_to_group(
                cls.admin_user.username, individual, group
            )
            membership.json_ext = {"primary_worker": True}
            membership.save(username=cls.admin_user.username)
            group.refresh_from_db()
            group.json_ext = {
                **(group.json_ext or {}),
                "validation_status": "VERIFIED",
            }
            group.save(username=cls.admin_user.username)

    def setUp(self):
        self.original_mandatory_criteria = copy.deepcopy(
            EnrolmentConfig.mandatory_enrollment_criteria
        )
        EnrolmentConfig.mandatory_enrollment_criteria = {
            "INDIVIDUAL": {},
            "GROUP": {},
        }

    def tearDown(self):
        EnrolmentConfig.mandatory_enrollment_criteria = (
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
