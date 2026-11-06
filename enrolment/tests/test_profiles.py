import copy
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from individual.models import Group, Individual

from enrolment.apps import DEFAULT_CONFIG, EnrolmentConfig
from enrolment.profiles import (
    apply_profile_eligibility,
    resolve_programme_profile,
    validate_programme_profiles,
)


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
