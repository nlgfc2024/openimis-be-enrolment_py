import copy
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from individual.models import Group, Individual
from individual.tests.test_helpers import (
    IndividualGQLTestCase,
    add_individual_to_group,
    create_group,
    create_individual,
)
from social_protection.tests.test_helpers import create_benefit_plan

from enrolment.apps import DEFAULT_CONFIG, EnrolmentConfig
from enrolment.profiles import (
    apply_profile_eligibility,
    resolve_programme_profile,
    validate_programme_profiles,
)
from enrolment.services import build_group_enrolment_selection


def _criterion(**overrides):
    return {
        "field": "validation_status",
        "filter": "exact",
        "type": "string",
        "value": "VERIFIED",
        **overrides,
    }


class ProgrammeProfileValidationTest(SimpleTestCase):
    def test_valid_profiles_have_no_errors(self):
        self.assertEqual(validate_programme_profiles({
            "RMEP": {
                "description": "Rural Micro Enterprise Programme",
                "eligibility": {"criteria": [_criterion(subject="household")]},
            },
            "UPG": {"eligibility": {"criteria": [
                _criterion(subject=subject) for subject in ("primary_worker", "head", "any_member")
            ]}},
            "default": {"eligibility": {"criteria": [_criterion()]}},
        }), [])

    def test_invalid_profiles_are_reported(self):
        cases = [
            ([], "must be an object keyed by BenefitPlan code"),
            ({"RMEP": {}, "rmep": {}}, "differs only in case"),
            ({"RMEP": []}, "programme_profiles.RMEP must be an object"),
            ({"RMEP": {"ranking": {}}}, "Unsupported programme_profiles.RMEP keys: ranking"),
            ({"RMEP": {"description": 1}}, "description must be a string"),
            ({"RMEP": {"eligibility": []}}, "eligibility must be an object"),
            (
                {"RMEP": {"eligibility": {"exclude_household_members": True}}},
                "Unsupported programme_profiles.RMEP.eligibility keys",
            ),
            ({"RMEP": {"eligibility": {"criteria": {}}}}, "criteria must be a list"),
        ]
        for profiles, message in cases:
            with self.subTest(message=message):
                errors = validate_programme_profiles(profiles)
                self.assertTrue(
                    any(message in error for error in errors), errors
                )

    def test_invalid_criteria_are_reported(self):
        cases = [
            (_criterion(subject="village"), "subject must be one of"),
            (_criterion(field="validation_status__isnull"), "field must be a json_ext key"),
            (_criterion(field="   "), "field must be a json_ext key"),
            (_criterion(type="date"), "type must be one of"),
            (_criterion(filter="gte"), "filter gte is not allowed for string"),
            (_criterion(type="integer", filter="gte", value="5"), "value must be of type integer"),
            (_criterion(type="numeric", filter="gte", value=float("inf")), "value must be of type numeric"),
            (_criterion(derived="age"), "Unsupported"),
        ]
        for criterion, message in cases:
            with self.subTest(message=message):
                errors = validate_programme_profiles(
                    {"RMEP": {"eligibility": {"criteria": [criterion]}}}
                )
                self.assertTrue(
                    any(message in error for error in errors), errors
                )

    def test_module_config_rejects_invalid_profiles(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["programme_profiles"] = {"RMEP": {"ranking": {}}}
        with self.assertRaisesMessage(ValidationError, "Unsupported programme_profiles"):
            EnrolmentConfig._validate_config(config)

        EnrolmentConfig._validate_config(copy.deepcopy(DEFAULT_CONFIG))


class ProgrammeProfileResolutionTest(SimpleTestCase):
    profiles = {
        "RMEP": {"eligibility": {"criteria": [_criterion(value="RMEP")]}},
        "default": {"eligibility": {"criteria": [_criterion()]}},
    }

    def test_profile_matches_benefit_plan_code_case_insensitively(self):
        with patch.object(EnrolmentConfig, "programme_profiles", self.profiles):
            key, profile = resolve_programme_profile(Mock(code="rmep"))
        self.assertEqual(key, "RMEP")
        self.assertIs(profile, self.profiles["RMEP"])

    def test_unmatched_code_uses_default_profile(self):
        with patch.object(EnrolmentConfig, "programme_profiles", self.profiles):
            self.assertEqual(resolve_programme_profile(Mock(code="UPG"))[0], "default")

    def test_no_profile_applies_without_match_or_default(self):
        with patch.object(EnrolmentConfig, "programme_profiles", {"RMEP": {}}):
            self.assertEqual(resolve_programme_profile(Mock(code="UPG")), (None, None))
            queryset = Group.objects.all()
            self.assertIs(
                apply_profile_eligibility(queryset, Mock(code="UPG"), "GROUP"),
                queryset,
            )


class ProgrammeProfileEligibilityTest(SimpleTestCase):
    def _apply(self, criteria, model, phase_type):
        profiles = {"default": {"eligibility": {"criteria": criteria}}}
        with patch.object(EnrolmentConfig, "programme_profiles", profiles):
            return apply_profile_eligibility(
                model.objects.all(), Mock(code="PWP"), phase_type
            )

    def test_household_criteria_filter_the_group_record(self):
        sql = str(self._apply(
            [_criterion(subject="household")], Group, "GROUP"
        ).query)
        self.assertIn("individual_group", sql)
        self.assertIn("validation_status", sql)

    def test_criteria_without_subject_filter_the_candidate_record(self):
        sql = str(self._apply([_criterion()], Individual, "INDIVIDUAL").query)
        self.assertIn("validation_status", sql)

    def test_member_criteria_require_a_matching_household_member(self):
        for subject, marker in (
            ("primary_worker", "primary_worker"),
            ("head", "HEAD"),
            ("any_member", "individual_groupindividual"),
        ):
            with self.subTest(subject=subject):
                sql = str(self._apply(
                    [_criterion(subject=subject, field="business_experience", value="Yes")],
                    Group,
                    "GROUP",
                ).query)
                self.assertIn("EXISTS", sql)
                self.assertIn("business_experience", sql)
                self.assertIn(marker, sql)

    def test_criteria_with_the_same_member_subject_share_one_member(self):
        same = str(self._apply([
            _criterion(subject="any_member", field="savings", value="Yes"),
            _criterion(subject="any_member", field="willing", value="Yes"),
        ], Group, "GROUP").query)
        separate = str(self._apply([
            _criterion(subject="any_member", field="savings", value="Yes"),
            _criterion(subject="head", field="willing", value="Yes"),
        ], Group, "GROUP").query)

        self.assertEqual(same.count("EXISTS"), 1)
        self.assertEqual(separate.count("EXISTS"), 2)

    def test_subject_must_suit_the_phase_type(self):
        with self.assertRaisesMessage(
            ValidationError, "uses subject self, which GROUP Phases"
        ):
            self._apply([_criterion(subject="self")], Group, "GROUP")
        with self.assertRaisesMessage(
            ValidationError, "uses subject household, which INDIVIDUAL Phases"
        ):
            self._apply([_criterion(subject="household")], Individual, "INDIVIDUAL")
        with self.assertRaisesMessage(
            ValidationError, "uses subject primary_worker, which INDIVIDUAL Phases"
        ):
            self._apply(
                [_criterion(subject="primary_worker")], Individual, "INDIVIDUAL"
            )


class ProgrammeProfileEnrolmentTest(IndividualGQLTestCase):
    """Profiles applied to real registry records."""

    def setUp(self):
        for name, value in (
            ("mandatory_enrollment_criteria", {"INDIVIDUAL": {}, "GROUP": {}}),
            ("programme_profiles", {}),
        ):
            self.addCleanup(
                setattr, EnrolmentConfig, name,
                copy.deepcopy(getattr(EnrolmentConfig, name)),
            )
            setattr(EnrolmentConfig, name, value)

    def _plan(self, token, criteria):
        plan = create_benefit_plan(self.admin_user.username, payload_override={
            "type": "GROUP",
            "beneficiary_data_schema": {
                "properties": {"enrolment_test": {"type": "string"}}
            },
            "json_ext": {"advanced_criteria": {"ACTIVE": [{
                "field": "enrolment_test",
                "filter": "exact",
                "type": "string",
                "value": token,
            }]}},
        })
        self._set_profile(plan, criteria)
        return plan

    def _set_profile(self, plan, criteria):
        EnrolmentConfig.programme_profiles = {
            plan.code: {"eligibility": {"criteria": criteria}}
        }

    def _household(self, token, members, primary_worker=0):
        """Create a VERIFIED household from members' json_ext; the first is head."""
        username = self.admin_user.username
        group = create_group(username, {"json_ext": {"enrolment_test": token}})
        memberships = [
            add_individual_to_group(
                username,
                create_individual(username, {
                    "json_ext": {"enrolment_test": token, **json_ext},
                }),
                group,
                is_head=index == 0,
            )
            for index, json_ext in enumerate(members)
        ]
        memberships[primary_worker].json_ext = {"primary_worker": True}
        memberships[primary_worker].save(username=username)
        # Saving a membership copies the head's json_ext onto the household
        # and drops validation_status, so verify the household last.
        group.refresh_from_db()
        group.json_ext = {
            **(group.json_ext or {}),
            "enrolment_test": token,
            "validation_status": "VERIFIED",
        }
        group.save(username=username)
        return group

    def _eligible(self, plan):
        selection = build_group_enrolment_selection(
            [], str(plan.id), "ACTIVE", self.admin_user
        )
        return set(
            selection["groups_not_assigned_to_selected_programme"]
            .values_list("id", flat=True)
        )

    def test_rmep_profile_reads_the_primary_worker(self):
        token = "profile-rmep"
        plan = self._plan(token, [
            _criterion(subject="household"),
            _criterion(
                subject="primary_worker",
                field="business_experience",
                value="Yes",
            ),
        ])
        # The head's answer is the opposite of the primary worker's in both
        # households, so only a criterion that reads the primary worker
        # selects the first one.
        experienced = self._household(
            token,
            [{"business_experience": "No"}, {"business_experience": "Yes"}],
            primary_worker=1,
        )
        self._household(
            token,
            [{"business_experience": "Yes"}, {"business_experience": "No"}],
            primary_worker=1,
        )

        self.assertEqual(self._eligible(plan), {experienced.id})

    def test_any_member_criteria_must_be_met_by_one_member(self):
        token = "profile-any-member"
        plan = self._plan(token, [
            _criterion(
                subject="any_member", field="savings_experience", value="Yes",
            ),
            _criterion(
                subject="any_member",
                field="willingness_to_participate",
                value="Yes",
            ),
        ])
        one_member = self._household(token, [
            {},
            {"savings_experience": "Yes", "willingness_to_participate": "Yes"},
        ])
        self._household(token, [
            {"savings_experience": "Yes"},
            {"willingness_to_participate": "Yes"},
        ])

        self.assertEqual(self._eligible(plan), {one_member.id})
