from django.test import TestCase, override_settings

from core.test_helpers import LogInHelper
from enrolment.apps import EnrolmentConfig
from enrolment.services import build_group_enrolment_selection, build_individual_enrolment_selection
from individual.tests.test_helpers import create_group_with_individual, create_individual
from social_protection.tests.test_helpers import create_benefit_plan


@override_settings(ROW_SECURITY=False)
class EnrolmentDataSourceTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def setUp(self):
        self.original_criteria = EnrolmentConfig.mandatory_enrollment_criteria
        EnrolmentConfig.mandatory_enrollment_criteria = {"INDIVIDUAL": {}, "GROUP": {}}

    def tearDown(self):
        EnrolmentConfig.mandatory_enrollment_criteria = self.original_criteria

    def _plan(self, plan_type, data_source=None):
        json_ext = {"advanced_criteria": {"ACTIVE": []}}
        if data_source:
            json_ext["data_source"] = data_source
        return create_benefit_plan(self.user.username, payload_override={"type": plan_type, "json_ext": json_ext})

    def _individual(self, data_source=None):
        json_ext = {"data_source": data_source} if data_source else {}
        return create_individual(self.user.username, payload_override={"json_ext": json_ext})

    def _verified_group(self, data_source=None):
        _, group, membership = create_group_with_individual(self.user.username)
        membership.json_ext = {"primary_worker": True}
        membership.save(username=self.user.username)
        group.refresh_from_db()
        group.json_ext = {
            **(group.json_ext or {}),
            "validation_status": "VERIFIED",
            **({"data_source": data_source} if data_source else {}),
        }
        group.save(username=self.user.username)
        return group

    def _eligible_individuals(self, plan):
        selection = build_individual_enrolment_selection([], str(plan.id), "ACTIVE", self.user)
        return set(selection["individual_query_with_filters"].values_list("id", flat=True))

    def _eligible_groups(self, plan):
        selection = build_group_enrolment_selection([], str(plan.id), "ACTIVE", self.user)
        return set(selection["group_query_with_filters"].values_list("id", flat=True))

    def test_tagged_plan_only_selects_individuals_from_its_source(self):
        pwp = self._individual("pwp")
        self._individual("sctp")
        self._individual()

        self.assertEqual(self._eligible_individuals(self._plan("INDIVIDUAL", "pwp")), {pwp.id})

    def test_untagged_plan_selects_default_source_individuals(self):
        untagged = self._individual()
        ubr = self._individual("ubr")
        self._individual("pwp")

        self.assertEqual(self._eligible_individuals(self._plan("INDIVIDUAL")), {untagged.id, ubr.id})

    def test_tagged_plan_only_selects_households_from_its_source(self):
        sctp = self._verified_group("sctp")
        self._verified_group("pwp")
        self._verified_group()

        self.assertEqual(self._eligible_groups(self._plan("GROUP", "sctp")), {sctp.id})

    def test_untagged_plan_selects_default_source_households(self):
        untagged = self._verified_group()
        self._verified_group("sctp")

        self.assertEqual(self._eligible_groups(self._plan("GROUP")), {untagged.id})
