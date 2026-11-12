import copy
from datetime import date
from unittest.mock import Mock, patch

from django.apps import apps
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from individual.models import Group, Individual
from individual.tests.test_helpers import (
    IndividualGQLTestCase,
    add_individual_to_group,
    create_group,
    create_individual,
)
from social_protection.models import BenefitPlanDataUploadRecords, GroupBeneficiary
from social_protection.tests.test_helpers import create_benefit_plan
from tasks_management.models import Task
from tasks_management.services import TaskService

from enrolment.apps import DEFAULT_CONFIG, EnrolmentConfig
from enrolment.profiles import (
    apply_profile_eligibility,
    resolve_programme_profile,
    validate_programme_profiles,
)
from enrolment.services import EnrolmentService, build_group_enrolment_selection
from enrolment.tests.helpers import create_village_in


def _age(filter_name, value, **overrides):
    return {"derived": "age", "filter": filter_name, "value": value, **overrides}


def _district(name, **overrides):
    return {"derived": "district", "filter": "exact", "value": name, **overrides}


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
            ({"RMEP": {"phase_type": "GROUP"}}, "Unsupported programme_profiles.RMEP keys: phase_type"),
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
            (_criterion(derived="age"), "must set exactly one of field or derived"),
            ({"filter": "exact", "type": "string", "value": "x"}, "must set exactly one of field or derived"),
            (_age("gte", 18, derived="income"), "supported values: age, district"),
            (_age("gte", "18"), "value must be an age from 0 to 120"),
            (_age("gte", 18, type="string"), "type must be integer for age"),
            (_age("icontains", 18), "filter icontains is not allowed for age"),
            (_district(""), "value must be a district name"),
            (_district("Zomba", filter="gte"), "filter gte is not allowed for district"),
        ]
        for criterion, message in cases:
            with self.subTest(message=message):
                errors = validate_programme_profiles(
                    {"RMEP": {"eligibility": {"criteria": [criterion]}}}
                )
                self.assertTrue(
                    any(message in error for error in errors), errors
                )

    def test_profile_must_suit_a_phase_type(self):
        self.assertEqual(validate_programme_profiles({
            "GROUP_ONLY": {"eligibility": {"criteria": [_criterion(subject="head")]}},
            "INDIVIDUAL_ONLY": {"eligibility": {"criteria": [_criterion(subject="self")]}},
            "EITHER": {"eligibility": {"criteria": [_criterion()]}},
        }), [])

        errors = validate_programme_profiles({"MIXED": {"eligibility": {"criteria": [
            _criterion(subject="household"), _criterion(subject="self"),
        ]}}})
        self.assertEqual(errors, [
            "programme_profiles.MIXED suits neither Phase type. "
            "GROUP Phases: eligibility.criteria[1] uses subject self. "
            "INDIVIDUAL Phases: eligibility.criteria[0] uses subject household."
        ])

    def test_derived_criteria_suit_the_phase_types_that_have_the_value(self):
        self.assertEqual(validate_programme_profiles({
            "UPG": {"eligibility": {"criteria": [
                _age("gte", 18, subject="primary_worker"),
                _age("lte", 60, subject="primary_worker"),
                _district("Zomba", subject="household"),
            ]}},
            # Without a subject, age reads the candidate: INDIVIDUAL Phases only.
            "UMEP": {"eligibility": {"criteria": [_age("gte", 18), _district("Zomba")]}},
        }), [])

        errors = validate_programme_profiles({"UPG": {"eligibility": {"criteria": [
            _age("gte", 18, subject="household"),
        ]}}})
        self.assertEqual(errors, [
            "programme_profiles.UPG suits neither Phase type. "
            "GROUP Phases: eligibility.criteria[0].derived age needs a person: set "
            "subject to head, primary_worker or any_member. "
            "INDIVIDUAL Phases: eligibility.criteria[0] uses subject household."
        ])

    def test_profile_rankings_are_validated_for_the_phase_types_they_suit(self):
        head_pmt = {"order_by": [{
            "subject": "head", "field": "household_pmt_score", "cast": "float",
        }]}
        self_quota = {"selection_strategy": {
            "source": "SELF", "female_percentage": 50, "youth_percentage": 0,
        }}
        self.assertEqual(validate_programme_profiles({
            "PWP": {"ranking": {"*": head_pmt}},
            "UMEP": {"ranking": {"ACTIVE": self_quota}},
        }), [])

        cases = [
            (
                {"ranking": {"SOMETIME": head_pmt}},
                "programme_profiles.RMEP: ranking has unsupported status SOMETIME.",
            ),
            (
                {"ranking": {"*": {"order_by": [], "sort": "desc"}}},
                "programme_profiles.RMEP: ranking.*: Unsupported enrolment_ranking keys: sort.",
            ),
            (
                {
                    "eligibility": {"criteria": [_criterion(subject="self")]},
                    "ranking": {"*": head_pmt},
                },
                "programme_profiles.RMEP suits neither Phase type. GROUP Phases: "
                "eligibility.criteria[0] uses subject self. INDIVIDUAL Phases: "
                "ranking.*: INDIVIDUAL enrolment_ranking.order_by[0].subject must be self.",
            ),
        ]
        for profile, message in cases:
            with self.subTest(message=message):
                self.assertEqual(
                    validate_programme_profiles({"RMEP": profile}), [message]
                )

    def test_module_config_rejects_invalid_profiles(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["programme_profiles"] = {"RMEP": {"phase_type": "GROUP"}}
        with self.assertRaisesMessage(ValidationError, "Unsupported programme_profiles"):
            EnrolmentConfig._validate_config(config)

        EnrolmentConfig._validate_config(copy.deepcopy(DEFAULT_CONFIG))


class ModuleConfigValueTest(SimpleTestCase):
    def _profile_config(self, profile):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["programme_profiles"] = {"default": profile}
        return config

    def test_a_list_or_object_in_place_of_a_name_is_a_validation_error(self):
        mapping = {"type": "mapping", "values": {"a": 1}}

        def component(**keys):
            return {"ranking": {"*": {"weighted_score": {"components": [
                {"field": "x", "weight": 1, "scoring": mapping, **keys}
            ]}}}}

        def quota(**keys):
            return {"ranking": {"*": {"selection_strategy": {"source": "SELF", "quotas": [{
                "key": "k", "count": 1,
                "when": [{"derived": "age", "filter": "gte", "value": 1, **keys}],
            }]}}}}

        def order_by(**keys):
            return {"ranking": {"*": {"order_by": [{"field": "json_ext__x", **keys}]}}}

        def criterion(**keys):
            return {"eligibility": {"criteria": [_criterion(**keys)]}}

        cases = {
            "component subject": component(subject=["head"]),
            "component aggregate": component(subject="any_member", aggregate=["max"]),
            "component derived": {"ranking": {"*": {"weighted_score": {"components": [
                {"derived": [], "weight": 1, "scoring": mapping}
            ]}}}},
            "criterion subject": criterion(subject=["head"]),
            "criterion filter": criterion(filter=["exact"]),
            "criterion type": criterion(type={"string": True}),
            "criterion derived": {"eligibility": {"criteria": [_age("gte", 1, derived=[])]}},
            "quota source": {"ranking": {"*": {"selection_strategy": {
                "source": ["SELF"], "female_percentage": 10,
            }}}},
            "quota filter": quota(filter=["gte"]),
            "quota derived": quota(derived=[]),
            "order_by subject": order_by(subject=["head"]),
            "order_by direction": order_by(direction=["asc"]),
            "order_by cast": order_by(cast=["float"]),
            "order_by nulls": order_by(nulls=["last"]),
        }
        for name, profile in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValidationError):
                    EnrolmentConfig._validate_config(self._profile_config(profile))

    def test_district_location_type_must_be_a_location_type_on_save(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["district_location_type"] = "r"
        with self.assertRaisesMessage(
            ValidationError, "district_location_type r is not a location type"
        ):
            EnrolmentConfig._validate_config(config)
        # A stored value is not checked at startup, when location types may
        # not be loaded yet.
        EnrolmentConfig._validate_config(config, on_load=True)


class DistrictNameValidationTest(TestCase):
    def setUp(self):
        create_village_in("DN-Zomba")

    def _validate(self, profile, on_load=False):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["programme_profiles"] = {"default": profile}
        EnrolmentConfig._validate_config(config, on_load=on_load)

    def _criteria(self, *criteria):
        return {"eligibility": {"criteria": list(criteria)}}

    def test_names_of_active_districts_are_accepted(self):
        self._validate(self._criteria(
            _district("DN-Zomba"),
            _district("dn-zomba", filter="iexact"),
            # Other filters compare parts of names, which are not checked.
            _district("Zomb", filter="icontains"),
        ))

    def test_unknown_district_names_are_rejected_on_save(self):
        district = {"derived": "district", "filter": "exact", "value": "DN-Zomb"}
        cases = {
            "programme_profiles.default.eligibility.criteria[0].value DN-Zomb":
                self._criteria(_district("DN-Zomb")),
            "programme_profiles.default.eligibility.criteria[0].value dn-zomba":
                self._criteria(_district("dn-zomba")),
            "programme_profiles.default.ranking.*.weighted_score.components[0]"
            ".scoring.values DN-Zomb":
                {"ranking": {"*": {
                    "order_by": [{"field": "_weighted_score", "direction": "desc"}],
                    "weighted_score": {"components": [{
                        "derived": "district", "weight": 3,
                        "scoring": {"type": "mapping", "values": {"DN-Zomba": 3, "DN-Zomb": 1}},
                    }]},
                }}},
            "programme_profiles.default.ranking.*.selection_strategy.quotas[0]"
            ".when[0].value DN-Zomb":
                {"ranking": {"*": {"selection_strategy": {"source": "SELF", "quotas": [
                    {"key": "zomba", "count": 1, "when": [district]},
                ]}}}},
        }
        for message, profile in cases.items():
            with self.subTest(message):
                with self.assertRaisesMessage(
                    ValidationError,
                    f"{message} is not the name of an active district (R).",
                ):
                    self._validate(profile)
                self._validate(profile, on_load=True)


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

    def test_required_profile_must_match(self):
        with patch.object(EnrolmentConfig, "programme_profiles", {"RMEP": {}}), \
                patch.object(EnrolmentConfig, "require_programme_profile", True):
            with self.assertRaisesMessage(
                ValidationError,
                "Phase UPG matches no programme profile, and "
                "require_programme_profile is on.",
            ):
                apply_profile_eligibility(Group.objects.all(), Mock(code="UPG"), "GROUP")
            # A Phase that matches a profile is enrolled as usual.
            apply_profile_eligibility(Group.objects.all(), Mock(code="rmep"), "GROUP")

    def test_module_config_requires_a_boolean_profile_requirement(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["require_programme_profile"] = "yes"
        with self.assertRaisesMessage(ValidationError, "require_programme_profile must be boolean"):
            EnrolmentConfig._validate_config(config)

    def test_retired_mandatory_criteria_are_rejected_on_save_and_logged_on_load(self):
        retired = {"INDIVIDUAL": {}, "GROUP": {"ACTIVE": [_criterion()]}}
        app = apps.get_app_config("enrolment")
        instance = Mock(_cfg={"mandatory_enrollment_criteria": retired})
        loaded = {field: getattr(EnrolmentConfig, field) for field in DEFAULT_CONFIG}
        self.addCleanup(lambda: [setattr(EnrolmentConfig, f, v) for f, v in loaded.items()])

        # Saving a configuration that still sets criteria is rejected.
        with self.assertRaisesMessage(
            ValidationError, "mandatory_enrollment_criteria is no longer applied"
        ):
            app._validate_module_config(instance)

        # One already stored does not stop startup or a reload; it is logged.
        stored = {**copy.deepcopy(DEFAULT_CONFIG), "mandatory_enrollment_criteria": retired}
        with patch("core.models.ModuleConfiguration.get_or_default", return_value=stored), \
                patch("enrolment.apps.register_validator"), \
                patch("enrolment.apps.register_reloader"), \
                patch.object(EnrolmentConfig, "_register_benefit_plan_validator"), \
                self.assertLogs("enrolment.apps", "WARNING") as startup_logs:
            app.ready()
        self.assertIn("programme profile's eligibility.criteria", startup_logs.output[0])
        with self.assertLogs("enrolment.apps", "WARNING") as reload_logs:
            app._reload_module_config(instance)
        self.assertIn("programme profile's eligibility.criteria", reload_logs.output[0])

        # The empty shape older configurations carry still saves.
        app._validate_module_config(Mock(_cfg={
            "mandatory_enrollment_criteria": {"INDIVIDUAL": {}, "GROUP": {}},
        }))

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
            ValidationError,
            "Programme profile default cannot be used by GROUP Phases: "
            "eligibility.criteria[0] uses subject self.",
        ):
            self._apply([_criterion(subject="self")], Group, "GROUP")
        with self.assertRaisesMessage(
            ValidationError, "INDIVIDUAL Phases: eligibility.criteria[0] uses subject household"
        ):
            self._apply([_criterion(subject="household")], Individual, "INDIVIDUAL")
        with self.assertRaisesMessage(
            ValidationError, "INDIVIDUAL Phases: eligibility.criteria[0] uses subject primary_worker"
        ):
            self._apply(
                [_criterion(subject="primary_worker")], Individual, "INDIVIDUAL"
            )

    def test_age_without_a_subject_cannot_be_used_by_group_phases(self):
        with self.assertRaisesMessage(
            ValidationError,
            "Programme profile default cannot be used by GROUP Phases: "
            "eligibility.criteria[0].derived age needs a person",
        ):
            self._apply([_age("gte", 18)], Group, "GROUP")


class ProgrammeProfileEnrolmentTest(IndividualGQLTestCase):
    """Profiles applied to real registry records, through preview and approval."""

    def setUp(self):
        self.addCleanup(
            setattr, EnrolmentConfig, "programme_profiles",
            copy.deepcopy(EnrolmentConfig.programme_profiles),
        )
        EnrolmentConfig.programme_profiles = {}

    def _plan(self, token, criteria, ranking=None, **payload):
        json_ext = {"advanced_criteria": {"ACTIVE": [{
            "field": "enrolment_test",
            "filter": "exact",
            "type": "string",
            "value": token,
        }]}}
        plan = create_benefit_plan(self.admin_user.username, payload_override={
            "type": "GROUP",
            "beneficiary_data_schema": {
                "properties": {"enrolment_test": {"type": "string"}}
            },
            "json_ext": json_ext,
            **payload,
        })
        self._set_profile(plan, criteria, ranking)
        return plan

    def _set_profile(self, plan, criteria, ranking=None):
        profile = {"eligibility": {"criteria": criteria}}
        if ranking is not None:
            profile["ranking"] = {"*": ranking}
        EnrolmentConfig.programme_profiles = {plan.code: profile}

    def _household(self, token, members, primary_worker=0, people=None, **group_fields):
        """Create a VERIFIED household from members' json_ext; the first is head.

        ``people`` gives each member's other Individual fields, such as ``dob``.
        """
        username = self.admin_user.username
        group = create_group(username, {
            "json_ext": {"enrolment_test": token}, **group_fields,
        })
        people = people or [{} for _ in members]
        memberships = [
            add_individual_to_group(
                username,
                create_individual(username, {
                    "json_ext": {"enrolment_test": token, **json_ext},
                    **person,
                }),
                group,
                is_head=index == 0,
            )
            for index, (json_ext, person) in enumerate(zip(members, people))
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

    def test_upg_age_limit_reads_the_primary_worker(self):
        token = "profile-age"
        plan = self._plan(token, [
            _age("gte", 18, subject="primary_worker"),
            _age("lte", 60, subject="primary_worker"),
        ])
        this_year = date.today().year

        def household(primary_worker_age):
            # The head is 40 in both, so only the primary worker's age differs.
            return self._household(
                token, [{}, {}], primary_worker=1,
                people=[{"dob": date(this_year - 40, 1, 1)},
                        {"dob": date(this_year - primary_worker_age, 1, 1)}],
            )

        working_age = household(30)
        household(65)

        self.assertEqual(self._eligible(plan), {working_age.id})

    def test_district_criterion_reads_where_the_household_lives(self):
        token = "profile-district"
        plan = self._plan(token, [_district("PD-Zomba", subject="household")])
        in_zomba = self._household(token, [{}], location=create_village_in("PD-Zomba"))
        self._household(token, [{}], location=create_village_in("PD-Dedza"))

        self.assertEqual(self._eligible(plan), {in_zomba.id})

    def _households_by_head_pmt(self, token):
        richer = self._household(token, [{"household_pmt_score": 30}])
        poorer = self._household(token, [{"household_pmt_score": 10}])
        return richer, poorer

    def test_profile_ranking_applies_when_the_phase_has_none(self):
        token = "profile-ranking"
        plan = self._plan(token, [], ranking={"order_by": [{
            "subject": "head", "field": "household_pmt_score", "cast": "float",
        }]}, max_beneficiaries=1)
        _, poorer = self._households_by_head_pmt(token)

        # Lowest PMT first, so the household created second takes the place.
        self.assertEqual(self._eligible(plan), {poorer.id})

    def _submit_then_narrow_the_profile(self, token):
        """Submit two households, then narrow the profile so one no longer fits."""
        experienced = _criterion(
            subject="primary_worker", field="business_experience", value="Yes",
        )
        plan = self._plan(token, [experienced])
        retail = self._household(
            token, [{"business_experience": "Yes", "sector": "Retail"}]
        )
        farming = self._household(
            token, [{"business_experience": "Yes", "sector": "Farming"}]
        )
        self.assertEqual(self._eligible(plan), {retail.id, farming.id})

        EnrolmentService(self.admin_user).select_groups_to_benefit_plan(
            [], str(plan.id), "ACTIVE", self.admin_user
        )
        record = BenefitPlanDataUploadRecords.objects.get(
            benefit_plan=plan, workflow="Enrollment", is_deleted=False
        )
        task = Task.objects.get(entity_id=str(record.id), is_deleted=False)
        self._set_profile(plan, [
            experienced,
            _criterion(subject="primary_worker", field="sector", value="Retail"),
        ])
        self.assertEqual(self._eligible(plan), {retail.id})
        return plan, task, {retail.id, farming.id}

    @patch.object(EnrolmentConfig, "enable_maker_checker_logic_enrollment", True)
    def test_approval_uses_the_profile_the_enrolment_was_submitted_with(self):
        plan, task, submitted = self._submit_then_narrow_the_profile("profile-approval")
        self.assertEqual(task.json_ext["programme_profile"]["key"], plan.code)

        TaskService(self.admin_user).complete_task({"id": task.id})

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertEqual(
            set(GroupBeneficiary.objects.filter(
                benefit_plan=plan, is_deleted=False
            ).values_list("group_id", flat=True)),
            submitted,
        )

    @patch.object(EnrolmentConfig, "enable_maker_checker_logic_enrollment", True)
    def test_a_task_without_a_profile_snapshot_uses_the_current_profile(self):
        # Tasks submitted before snapshots were kept are re-checked as before.
        plan, task, _ = self._submit_then_narrow_the_profile("profile-no-snapshot")
        json_ext = dict(task.json_ext)
        del json_ext["programme_profile"]
        Task.objects.filter(id=task.id).update(json_ext=json_ext)

        TaskService(self.admin_user).complete_task({"id": task.id})

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertFalse(
            GroupBeneficiary.objects.filter(
                benefit_plan=plan, is_deleted=False
            ).exists()
        )
