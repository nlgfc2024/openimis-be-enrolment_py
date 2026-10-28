import copy
from datetime import date
from unittest.mock import MagicMock, Mock, patch

from django.core.exceptions import ValidationError
from django.db import DataError
from django.test import SimpleTestCase, TestCase, override_settings

from core.test_helpers import LogInHelper
from enrolment.criteria import _criterion_to_condition
from enrolment.apps import EnrolmentConfig
from enrolment.services import (
    EnrolmentService,
    build_group_enrollment_queryset,
    build_individual_enrollment_queryset,
    build_individual_enrollment_selection,
    build_group_enrollment_selection,
)
from enrolment.ranking import (
    HOUSEHOLD_HEAD_SOURCE,
    _annotate_selection_source,
    calculate_cap,
    build_order_by,
    build_weighted_score,
    load_ranking_spec,
    rank_and_cap_queryset,
    validate_ranking_spec,
)
from individual.models import Group, Individual
from enrolment.schema import Query
from individual.custom_filters import GroupCustomFilterWizard, IndividualCustomFilterWizard
from individual.tests.test_helpers import (
    add_individual_to_group,
    create_group,
    create_individual,
)
from social_protection.tests.test_helpers import create_benefit_plan
from social_protection.models import Beneficiary, GroupBeneficiary


class EnrollmentCriterionNormalizationTest(SimpleTestCase):
    @patch("enrolment.ranking.build_order_by", side_effect=lambda queryset, order: queryset)
    def test_target_without_ranking_keeps_backward_compatible_full_pool(self, build_order):
        queryset = MagicMock(model=Individual)
        queryset.count.return_value = 5876
        phase = Mock(json_ext={}, max_beneficiaries=300)
        _, metadata = rank_and_cap_queryset(
            queryset, phase, "ACTIVE", 0, materialize_selected_ids=False,
        )
        self.assertEqual(metadata["pool_size"], 5876)
        self.assertIsNone(metadata["cap_applied"])
        self.assertEqual(metadata["will_enrol"], 5876)
        build_order.assert_not_called()

    @patch("enrolment.ranking.build_order_by", side_effect=lambda queryset, order: queryset)
    def test_target_without_ranking_does_not_apply_remaining_capacity(self, build_order):
        queryset = MagicMock(model=Individual)
        queryset.count.return_value = 5876
        _, metadata = rank_and_cap_queryset(
            queryset, Mock(json_ext={}, max_beneficiaries=300), "ACTIVE", 20,
            materialize_selected_ids=False,
        )
        self.assertEqual(metadata["will_enrol"], 5876)

    def test_custom_filter_string_cast_decodes_json_quoting(self):
        wizard = IndividualCustomFilterWizard()
        cast_value = wizard._IndividualCustomFilterWizard__cast_value

        self.assertEqual(cast_value('"Karonga"', 'string'), "Karonga")
        self.assertEqual(
            cast_value('"Nkhata \\"Bay\\""', 'string'),
            'Nkhata "Bay"',
        )

    def test_custom_filter_string_cast_preserves_legacy_unquoted_values(self):
        wizard = IndividualCustomFilterWizard()
        cast_value = wizard._IndividualCustomFilterWizard__cast_value

        self.assertEqual(cast_value("Karonga", "string"), "Karonga")

    def test_individual_custom_filter_preserves_equals_in_string_value(self):
        query = Mock()
        query.filter.return_value = query

        result = IndividualCustomFilterWizard().apply_filter_to_queryset(
            ['district__exact__string="a=b"'],
            query,
        )

        query.filter.assert_called_once_with(json_ext__district__exact="a=b")
        self.assertIs(result, query)

    def test_group_custom_filter_preserves_equals_in_string_value(self):
        query = Mock()
        query.filter.return_value = query

        result = GroupCustomFilterWizard().apply_filter_to_queryset(
            ['district__exact__string="a=b"'],
            query,
        )

        query.filter.assert_called_once_with(json_ext__district__exact="a=b")
        self.assertIs(result, query)

    def test_canonical_string_value_is_quoted_for_custom_filter_casting(self):
        condition = _criterion_to_condition({
            "field": "validation_status",
            "filter": "exact",
            "type": "string",
            "value": "VERIFIED",
        })
        self.assertEqual(
            condition,
            'validation_status__exact__string="VERIFIED"',
        )

    def test_existing_condition_is_preserved(self):
        condition = "score__gte__integer=10"
        self.assertEqual(
            _criterion_to_condition({"custom_filter_condition": condition}),
            condition,
        )

    def test_ranking_validation_rejects_unknown_field_and_bad_limit(self):
        with self.assertRaisesMessage(ValidationError, "Unsupported ranking field"):
            validate_ranking_spec(
                {"order_by": ["does_not_exist"]}, Individual
            )
        with self.assertRaisesMessage(ValidationError, "between 1 and 100"):
            validate_ranking_spec(
                {"order_by": ["id"], "limit": {"percentage": 101}},
                Individual,
            )

    def test_cap_uses_ceiling_and_smaller_remaining_capacity(self):
        self.assertEqual(calculate_cap(7, 20, None), (2, True))
        self.assertEqual(calculate_cap(100, 20, 15, 3), (12, True))
        self.assertEqual(calculate_cap(100, 20, 10, 12), (0, True))
        self.assertEqual(calculate_cap(7, None, None), (7, False))

    def test_tie_breaker_is_always_normalised_to_last_position(self):
        order_items, _, _ = validate_ranking_spec(
            {"order_by": ["id", "last_name"], "tie_breaker": "id"},
            Individual,
        )
        self.assertEqual([item["field"] for item in order_items], ["last_name", "id"])

    def test_weighted_ranking_validation_accepts_primary_worker_source(self):
        from individual.models import Group

        order_items, percentage, respect_max = validate_ranking_spec({
            "score_source": {
                "type": "PRIMARY_WORKER",
                "data_field": "individual.json_ext",
            },
            "weighted_score": {
                "normalise_to": 100,
                "components": [{
                    "field": "business_experience",
                    "weight": 100,
                    "scoring": {
                        "type": "mapping",
                        "values": {"Yes": 1},
                        "default": 0,
                    },
                }],
            },
            "order_by": [{"field": "_weighted_score", "direction": "desc"}],
        }, Group)

        self.assertEqual(order_items[0]["field"], "_weighted_score")
        self.assertIsNone(percentage)
        self.assertTrue(respect_max)

    def test_weighted_ranking_validation_requires_score_in_ordering(self):
        with self.assertRaisesMessage(ValidationError, "must include _weighted_score"):
            validate_ranking_spec({
                "weighted_score": {
                    "components": [{
                        "field": "business_period",
                        "weight": 100,
                        "scoring": {
                            "type": "numeric_bands",
                            "bands": [{"min": 0, "points": 1}],
                        },
                    }],
                },
                "order_by": ["id"],
            }, Individual)

    def test_selection_strategy_is_opt_in_and_requires_explicit_source(self):
        with self.assertRaisesMessage(
            ValidationError,
            "source must be SELF",
        ):
            validate_ranking_spec({
                "selection_strategy": {"female_percentage": 40},
            }, Individual)

        order_items, percentage, respect_max = validate_ranking_spec({
            "selection_strategy": {
                "source": "SELF",
                "female_percentage": 40,
                "youth_percentage": 40,
            },
        }, Individual)

        self.assertEqual(order_items[-1]["field"], "id")
        self.assertIsNone(percentage)
        self.assertTrue(respect_max)

    def test_selection_strategy_rejects_percentages_above_one_hundred(self):
        with self.assertRaisesMessage(
            ValidationError,
            "percentages cannot exceed 100",
        ):
            validate_ranking_spec({
                "selection_strategy": {
                    "source": "SELF",
                    "female_percentage": 60,
                    "youth_percentage": 50,
                },
            }, Individual)

    def test_primary_worker_weighted_score_compiles_to_sql(self):
        from individual.models import Group

        scored = build_weighted_score(
            Group.objects.all(),
            {
                "components": [{
                    "field": "business_period",
                    "weight": 100,
                    "scoring": {
                        "type": "numeric_bands",
                        "bands": [{"min": 3, "points": 10}],
                        "default": 0,
                    },
                }],
            },
            {
                "type": "PRIMARY_WORKER",
                "data_field": "individual.json_ext",
            },
        )

        sql = str(scored.query)
        self.assertIn("_weighted_score", sql)
        self.assertIn("individual_individual", sql)
        self.assertIn("business_period", sql)

    def test_household_quota_source_compiles_gender_and_dob_to_sql(self):
        from individual.models import Group

        candidates = _annotate_selection_source(
            Group.objects.all(),
            HOUSEHOLD_HEAD_SOURCE,
        )

        sql = str(candidates.query)
        self.assertIn("individual_individual", sql)
        self.assertIn("gender", sql)
        self.assertIn("dob", sql.lower())
        self.assertIn("HEAD", sql)

    def test_string_json_ext_snapshot_uses_status_then_wildcard(self):
        plan = Mock(json_ext='{"enrolment_ranking":{"*":{"order_by":["id"]},"ACTIVE":{"order_by":["-id"]}}}')
        self.assertEqual(load_ranking_spec(plan, "ACTIVE")["order_by"], ["-id"])
        self.assertEqual(load_ranking_spec(plan, "POTENTIAL")["order_by"], ["id"])

    def test_string_and_object_ordering_compile_as_annotated_distinct_sql(self):
        order_items, _, _ = validate_ranking_spec({
            "order_by": [
                "-dob",
                {
                    "field": "json_ext__score",
                    "cast": "int",
                    "direction": "asc",
                    "nulls": "last",
                },
            ]
        }, Individual)
        query = build_order_by(Individual.objects.all().distinct(), order_items)
        sql = str(query.query)

        self.assertIn("DISTINCT", sql)
        self.assertIn("_enrolment_rank_", sql)
        self.assertIn("NULLS LAST", sql)
        self.assertIn("integer", sql)

    def test_ranking_rejects_multi_valued_relation_paths(self):
        with self.assertRaisesMessage(ValidationError, "multi-valued path"):
            validate_ranking_spec(
                {"order_by": ["groupindividuals__individual__dob"]},
                Group,
            )

    def test_benefit_plan_validator_uses_the_runtime_ranking_validator(self):
        from enrolment.ranking import benefit_plan_ranking_validation_errors

        errors = benefit_plan_ranking_validation_errors({
            "type": "GROUP",
            "beneficiary_data_schema": {"properties": {}},
            "json_ext": {"enrolment_ranking": {"ACTIVE": {
                "order_by": ["groupindividuals__individual__dob"],
            }}},
        })

        self.assertIn("multi-valued path", errors[0]["message"])

    @patch("enrolment.ranking.build_order_by")
    def test_cast_data_error_names_the_configured_field(
        self,
        build_ranking,
    ):
        queryset = MagicMock()
        queryset.count.return_value = 1
        queryset.model = Individual
        ranked = MagicMock()
        build_ranking.return_value = ranked
        ranked.values_list.return_value.__getitem__.return_value.__iter__.side_effect = (
            DataError('invalid input syntax for type integer: "Poorer"')
        )
        benefit_plan = Mock(
            json_ext={
                "enrolment_ranking": {
                    "*": {
                        "order_by": [{
                            "field": "json_ext__household_wealth_quintile",
                            "cast": "int",
                        }]
                    }
                }
            },
            max_beneficiaries=None,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "json_ext__household_wealth_quintile (int)",
        ):
            rank_and_cap_queryset(queryset, benefit_plan, "ACTIVE", 0)

    @patch("enrolment.confirmation.confirm_individual_enrolment")
    def test_confirmation_signal_result_contains_authoritative_queryset(
        self, confirm,
    ):
        authoritative_queryset = Mock()
        not_assigned_queryset = Mock()
        confirm.return_value = {
            "individual_query_with_filters": authoritative_queryset,
            "individuals_not_assigned_to_selected_programme": not_assigned_queryset,
        }

        service = EnrolmentService(Mock())
        result = EnrolmentService.select_individuals_to_benefit_plan.__wrapped__(
            service,
            [],
            "plan-id",
            "ACTIVE",
            service.user,
        )

        self.assertIs(
            result["individual_query_with_filters"],
            authoritative_queryset,
        )
        self.assertIs(
            result["individuals_not_assigned_to_selected_programme"],
            not_assigned_queryset,
        )


class EnrollmentPreviewResolverTest(SimpleTestCase):
    def setUp(self):
        self.info = Mock()
        self.info.context.user = Mock()

    @patch("enrolment.schema.gql_optimizer.query")
    @patch("enrolment.schema.build_individual_enrolment_selection")
    @patch("enrolment.schema._check_permissions")
    def test_individual_preview_is_derived_server_side(
        self, check_permissions, build_selection, optimize
    ):
        selected = Mock()
        build_selection.return_value = {
            "individuals_not_assigned_to_selected_programme": selected,
        }
        optimize.return_value = selected

        result = Query().resolve_enrolment_individual_candidate(
            self.info,
            customFilters=['score__gte__integer=10'],
            benefitPlanId="plan-id",
            status="ACTIVE",
        )

        build_selection.assert_called_once_with(
            ['score__gte__integer=10'],
            "plan-id",
            "ACTIVE",
            self.info.context.user,
            materialize_selected_ids=False,
        )
        optimize.assert_called_once_with(selected, self.info)
        self.assertIs(result, selected)

    @patch("enrolment.schema.gql_optimizer.query")
    @patch("enrolment.schema.build_group_enrolment_selection")
    @patch("enrolment.schema._check_permissions")
    def test_group_preview_is_derived_server_side(
        self, check_permissions, build_selection, optimize
    ):
        selected = Mock()
        build_selection.return_value = {
            "groups_not_assigned_to_selected_programme": selected,
        }
        optimize.return_value = selected

        result = Query().resolve_enrolment_group_candidate(
            self.info,
            customFilters=['score__gte__integer=10'],
            benefitPlanId="plan-id",
            status="POTENTIAL",
        )

        build_selection.assert_called_once_with(
            ['score__gte__integer=10'],
            "plan-id",
            "POTENTIAL",
            self.info.context.user,
            materialize_selected_ids=False,
        )
        optimize.assert_called_once_with(selected, self.info)
        self.assertIs(result, selected)


@override_settings(ROW_SECURITY=False)
class AuthoritativeEnrollmentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()

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

    def _verified_group(self, json_ext=None, individual_json_ext=None, dob=None):
        group_json = dict(json_ext or {})
        group_json.setdefault("validation_status", "VERIFIED")
        group = create_group(self.user.username, {"json_ext": group_json})
        individual_data = {"json_ext": individual_json_ext or {}}
        if dob is not None:
            individual_data["dob"] = dob
        individual = create_individual(self.user.username, individual_data)
        membership = add_individual_to_group(
            self.user.username, individual, group
        )
        membership.json_ext = {"primary_worker": True}
        membership.save(username=self.user.username)
        group.refresh_from_db()
        group.json_ext = {**(group.json_ext or {}), **group_json}
        group.save(username=self.user.username)
        return group

    def _benefit_plan(self, benefit_plan_type):
        return create_benefit_plan(self.user.username, payload_override={
            "type": benefit_plan_type,
            "beneficiary_data_schema": {
                "properties": {
                    "validation_status": {"type": "string"},
                    "district": {"type": "string"},
                }
            },
            "json_ext": {
                "advanced_criteria": {
                    "ACTIVE": [{
                        "field": "validation_status",
                        "filter": "exact",
                        "type": "string",
                        "value": "VERIFIED",
                    }]
                }
            },
        })

    def _youth_quota_fixture(self):
        benefit_plan = create_benefit_plan(
            self.user.username,
            payload_override={
                "type": "INDIVIDUAL",
                "max_beneficiaries": 5,
                "beneficiary_data_schema": {"properties": {}},
                "json_ext": {
                    "advanced_criteria": {"ACTIVE": []},
                    "enrolment_ranking": {
                        "ACTIVE": {
                            "order_by": ["id"],
                            "selection_strategy": {
                                "source": "SELF",
                                "female_percentage": 60,
                                "youth_percentage": 40,
                                "female_value": "Female",
                                "youth_min_age": 18,
                                "youth_max_age": 35,
                            },
                        }
                    }
                },
            },
        )
        create_individual(self.user.username, {
            "dob": date(date.today().year - 50, 1, 1),
            "json_ext": {"gender": "Male"},
        })
        selected = []
        for age in (25, 30):
            selected.append(create_individual(self.user.username, {
                "dob": date(date.today().year - age, 1, 1),
                "json_ext": {"gender": "Male"},
            }))
        for age in (40, 45, 50):
            selected.append(create_individual(self.user.username, {
                "dob": date(date.today().year - age, 1, 1),
                "json_ext": {"gender": "Female"},
            }))
        return benefit_plan, {individual.id for individual in selected}

    def _weighted_quota_plan(self, cap):
        return create_benefit_plan(
            self.user.username,
            payload_override={
                "type": "INDIVIDUAL",
                "max_beneficiaries": cap,
                "beneficiary_data_schema": {
                    "properties": {"quota_rank": {"type": "string"}}
                },
                "json_ext": {
                    "advanced_criteria": {"ACTIVE": []},
                    "enrolment_ranking": {
                        "ACTIVE": {
                            "weighted_score": {
                                "components": [{
                                    "field": "quota_rank",
                                    "weight": 100,
                                    "scoring": {
                                        "type": "mapping",
                                        "values": {
                                            str(rank): rank
                                            for rank in range(1, 8)
                                        },
                                        "default": 0,
                                        "maximum_points": 7,
                                    },
                                }],
                            },
                            "order_by": ["-_weighted_score"],
                            "tie_breaker": "id",
                            "selection_strategy": {
                                "source": "SELF",
                                "female_percentage": 60,
                                "youth_percentage": 40,
                                "female_value": "Female",
                                "youth_min_age": 18,
                                "youth_max_age": 35,
                            },
                        }
                    }
                },
            },
        )

    def _quota_candidate(self, rank, gender="Male", age=50):
        return create_individual(self.user.username, {
            "dob": date(date.today().year - age, 1, 1),
            "json_ext": {
                "gender": gender,
                "quota_rank": str(rank),
            },
        })

    def _selected_individual_ids(self, benefit_plan):
        selection = build_individual_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )
        selected_ids = selection["selected_ids"]
        queryset_ids = list(
            selection["individuals_not_assigned_to_selected_programme"]
            .values_list("id", flat=True)
        )
        self.assertEqual(len(selected_ids), len(set(selected_ids)))
        self.assertLessEqual(len(selected_ids), benefit_plan.max_beneficiaries)
        return selection, queryset_ids

    def test_small_quota_cap_uses_floor_and_weighted_fallback(self):
        benefit_plan = self._weighted_quota_plan(cap=2)
        highest = self._quota_candidate(7)
        female = self._quota_candidate(6, gender="Female")
        self._quota_candidate(5, age=25)

        selection, selected_ids = self._selected_individual_ids(benefit_plan)

        self.assertEqual(selection["will_enrol"], 2)
        self.assertEqual(selected_ids, [highest.id, female.id])

    def test_quota_shortfall_for_females_uses_weighted_fallback(self):
        benefit_plan = self._weighted_quota_plan(cap=5)
        highest = self._quota_candidate(7)
        youth_high = self._quota_candidate(6, age=25)
        youth_low = self._quota_candidate(5, age=30)
        fallback = self._quota_candidate(4)
        female = self._quota_candidate(3, gender="Female")
        excluded = self._quota_candidate(2)

        _, selected_ids = self._selected_individual_ids(benefit_plan)

        self.assertEqual(
            selected_ids,
            [highest.id, youth_high.id, youth_low.id, fallback.id, female.id],
        )
        self.assertNotIn(excluded.id, selected_ids)

    def test_quota_shortfall_for_youth_uses_weighted_fallback(self):
        benefit_plan = self._weighted_quota_plan(cap=5)
        highest = self._quota_candidate(7)
        female_high = self._quota_candidate(6, gender="Female")
        youth = self._quota_candidate(5, age=25)
        female_middle = self._quota_candidate(4, gender="Female")
        excluded = self._quota_candidate(3)
        female_low = self._quota_candidate(2, gender="Female")
        lower_female = self._quota_candidate(1, gender="Female")

        _, selected_ids = self._selected_individual_ids(benefit_plan)

        self.assertEqual(
            selected_ids,
            [
                highest.id,
                female_high.id,
                youth.id,
                female_middle.id,
                female_low.id,
            ],
        )
        self.assertNotIn(excluded.id, selected_ids)
        self.assertNotIn(lower_female.id, selected_ids)

    def test_female_youth_occupies_only_the_female_quota(self):
        benefit_plan = self._weighted_quota_plan(cap=5)
        excluded_highest = self._quota_candidate(7)
        female_youth = self._quota_candidate(6, gender="Female", age=25)
        female_middle = self._quota_candidate(5, gender="Female")
        female_low = self._quota_candidate(4, gender="Female")
        youth_high = self._quota_candidate(3, age=25)
        youth_low = self._quota_candidate(2, age=30)
        self._quota_candidate(1)

        selection, selected_ids = self._selected_individual_ids(benefit_plan)

        self.assertEqual(
            selected_ids,
            [
                female_youth.id,
                female_middle.id,
                female_low.id,
                youth_high.id,
                youth_low.id,
            ],
        )
        self.assertEqual(selection["selected_ids"].count(female_youth.id), 1)
        self.assertNotIn(excluded_highest.id, selected_ids)

    @override_settings(USE_TZ=False)
    def test_youth_quota_preview_works_without_timezones(self):
        benefit_plan, expected_ids = self._youth_quota_fixture()

        selection = build_individual_enrollment_selection(
            [],
            str(benefit_plan.id),
            "ACTIVE",
            self.user,
            materialize_selected_ids=False,
        )

        self.assertIsNone(selection["selected_ids"])
        self.assertEqual(
            set(
                selection["individuals_not_assigned_to_selected_programme"]
                .values_list("id", flat=True)
            ),
            expected_ids,
        )

    @override_settings(USE_TZ=False)
    def test_youth_quota_confirmation_works_without_timezones(self):
        benefit_plan, expected_ids = self._youth_quota_fixture()
        service = EnrolmentService(self.user)

        select_for_confirmation = (
            EnrolmentService.select_individuals_to_benefit_plan.__wrapped__
        )
        selection = select_for_confirmation(
            service, [], str(benefit_plan.id), "ACTIVE", self.user
        )

        self.assertEqual(set(selection["selected_ids"]), expected_ids)
        self.assertEqual(
            set(
                selection["individuals_not_assigned_to_selected_programme"]
                .values_list("id", flat=True)
            ),
            expected_ids,
        )

    def test_individual_confirmation_cannot_omit_phase_criteria(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")
        eligible = create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Lilongwe",
            }
        })
        create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "PENDING",
                "district": "Lilongwe",
            }
        })

        result = build_individual_enrollment_queryset(
            custom_filters=[],
            benefit_plan_id=str(benefit_plan.id),
            status="ACTIVE",
            user=self.user,
        )

        selected_ids = set(result.values_list("id", flat=True))
        self.assertEqual(selected_ids, {eligible.id})

    def test_operator_filter_can_only_narrow_phase_results(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")
        selected = create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Lilongwe",
            }
        })
        create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Blantyre",
            }
        })
        create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "PENDING",
                "district": "Lilongwe",
            }
        })

        result = build_individual_enrollment_queryset(
            custom_filters=['district__exact__string="Lilongwe"'],
            benefit_plan_id=str(benefit_plan.id),
            status="ACTIVE",
            user=self.user,
        )

        selected_ids = set(result.values_list("id", flat=True))
        self.assertEqual(selected_ids, {selected.id})

    def test_mandatory_and_saved_phase_criteria_are_anded(self):
        EnrolmentConfig.mandatory_enrollment_criteria = {
            "INDIVIDUAL": {
                "ACTIVE": [{
                    "field": "district",
                    "filter": "exact",
                    "type": "string",
                    "value": "Lilongwe",
                }]
            },
            "GROUP": {},
        }
        benefit_plan = self._benefit_plan("INDIVIDUAL")
        selected = create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Lilongwe",
            }
        })
        create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Blantyre",
            }
        })
        create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "PENDING",
                "district": "Lilongwe",
            }
        })

        result = build_individual_enrollment_queryset(
            custom_filters=[],
            benefit_plan_id=str(benefit_plan.id),
            status="ACTIVE",
            user=self.user,
        )

        self.assertEqual(set(result.values_list("id", flat=True)), {selected.id})

    def test_rejects_operator_filter_outside_phase_schema(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")

        with self.assertRaisesMessage(ValidationError, "is not allowed"):
            build_individual_enrollment_queryset(
                custom_filters=['unconfigured__exact__string="value"'],
                benefit_plan_id=str(benefit_plan.id),
                status="ACTIVE",
                user=self.user,
            )

    def test_group_confirmation_uses_group_json_ext(self):
        benefit_plan = self._benefit_plan("GROUP")
        eligible = self._verified_group({"validation_status": "VERIFIED"})
        pending = self._verified_group({"validation_status": "PENDING"})

        result = build_group_enrollment_queryset(
            custom_filters=[],
            benefit_plan_id=str(benefit_plan.id),
            status="ACTIVE",
            user=self.user,
        )

        selected_ids = set(result.values_list("id", flat=True))
        self.assertIn(eligible.id, selected_ids)
        self.assertNotIn(pending.id, selected_ids)

    def test_group_immutable_rule_cannot_be_removed_from_phase_or_payload(self):
        EnrolmentConfig.mandatory_enrollment_criteria = {
            "INDIVIDUAL": {},
            "GROUP": {
                "POTENTIAL": [{
                    "field": "validation_status",
                    "filter": "exact",
                    "type": "string",
                    "value": "VERIFIED",
                }]
            },
        }
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "GROUP",
            "beneficiary_data_schema": {
                "properties": {"validation_status": {"type": "string"}}
            },
            "json_ext": {"advanced_criteria": {"POTENTIAL": []}},
        })
        eligible = self._verified_group({"validation_status": "VERIFIED"})
        not_verified = self._verified_group({"validation_status": "NOT_VERIFIED"})

        result = build_group_enrollment_queryset(
            custom_filters=[],
            benefit_plan_id=str(benefit_plan.id),
            status="POTENTIAL",
            user=self.user,
        )

        result_ids = set(result.values_list("id", flat=True))
        self.assertIn(eligible.id, result_ids)
        self.assertNotIn(not_verified.id, result_ids)

        attempted_replacement = build_group_enrollment_queryset(
            custom_filters=[
                'validation_status__exact__string="NOT_VERIFIED"'
            ],
            benefit_plan_id=str(benefit_plan.id),
            status="POTENTIAL",
            user=self.user,
        )
        self.assertFalse(attempted_replacement.exists())

    def test_rejects_benefit_plan_type_mismatch(self):
        group_plan = self._benefit_plan("GROUP")

        with self.assertRaisesMessage(ValidationError, "cannot be used"):
            build_individual_enrollment_queryset(
                custom_filters=[],
                benefit_plan_id=str(group_plan.id),
                status="ACTIVE",
                user=self.user,
            )

    def test_numeric_json_ranking_and_percentage_cap_are_applied_together(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "INDIVIDUAL",
            "max_beneficiaries": 10,
            "beneficiary_data_schema": {
                "properties": {"score": {"type": "integer"}}
            },
            "json_ext": {
                "advanced_criteria": {"ACTIVE": []},
                "enrolment_ranking": {
                    "*": {
                        "order_by": [{
                            "field": "json_ext__score",
                            "cast": "int",
                            "direction": "asc",
                            "nulls": "last",
                        }],
                        "tie_breaker": "id",
                        "limit": {
                            "percentage": 50,
                            "respect_max_beneficiaries": True,
                        },
                    }
                }
            },
        })
        ranked = create_individual(self.user.username, {"json_ext": {"score": "9"}})
        second = create_individual(self.user.username, {"json_ext": {"score": "10"}})
        create_individual(self.user.username, {"json_ext": {"score": "100"}})

        selection = build_individual_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        self.assertEqual(selection["pool_size"], 3)
        self.assertEqual(selection["will_enrol"], 2)
        self.assertEqual(selection["cap_applied"], 2)
        self.assertEqual(
            list(selection["individuals_not_assigned_to_selected_programme"].values_list("id", flat=True)),
            [ranked.id, second.id],
        )
        self.assertFalse(selection["individuals_not_assigned_to_selected_programme"].query.is_sliced)
        self.assertEqual(selection["selected_ids"], [ranked.id, second.id])

    def test_preview_caps_with_filterable_subquery_without_materialising_ids(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "INDIVIDUAL",
            "max_beneficiaries": 10,
            "beneficiary_data_schema": {
                "properties": {"score": {"type": "integer"}}
            },
            "json_ext": {
                "advanced_criteria": {"ACTIVE": []},
                "enrolment_ranking": {
                    "*": {
                        "order_by": ["id"],
                        "limit": {"percentage": 50},
                    }
                }
            },
        })
        first = create_individual(self.user.username)
        second = create_individual(self.user.username)
        create_individual(self.user.username)

        selection = build_individual_enrollment_selection(
            [],
            str(benefit_plan.id),
            "ACTIVE",
            self.user,
            materialize_selected_ids=False,
        )
        preview = selection["individuals_not_assigned_to_selected_programme"]

        self.assertFalse(preview.query.is_sliced)
        self.assertIsNone(selection["selected_ids"])
        self.assertEqual(list(preview.values_list("id", flat=True)), [first.id, second.id])
        self.assertEqual(preview.filter(id=first.id).count(), 1)
        self.assertEqual(
            list(preview.order_by("-id").values_list("id", flat=True)),
            [second.id, first.id],
        )

    def test_remaining_status_capacity_limits_cumulative_enrollment(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "INDIVIDUAL",
            "max_beneficiaries": 2,
            "json_ext": {
                "advanced_criteria": {"ACTIVE": []},
                "enrolment_ranking": {
                    "ACTIVE": {
                        "order_by": ["id"],
                        "limit": {"percentage": 100},
                    }
                }
            },
        })
        existing = create_individual(self.user.username)
        create_individual(self.user.username)
        create_individual(self.user.username)
        beneficiary = Beneficiary(
            individual=existing,
            benefit_plan=benefit_plan,
            status="ACTIVE",
            json_ext={},
        )
        beneficiary.save(username=self.user.username)

        selection = build_individual_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        self.assertEqual(selection["pool_size"], 2)
        self.assertEqual(selection["cap_applied"], 1)
        self.assertEqual(selection["will_enrol"], 1)

    def test_group_ranking_cap_and_nulls_last(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "GROUP",
            "beneficiary_data_schema": {
                "properties": {
                    "score": {"type": "integer"},
                    "test_scope": {"type": "string"},
                }
            },
            "json_ext": {
                "advanced_criteria": {"POTENTIAL": [{
                    "field": "test_scope",
                    "filter": "exact",
                    "type": "string",
                    "value": "group-ranking",
                }]},
                "enrolment_ranking": {
                    "*": {
                        "order_by": [{
                            "field": "json_ext__score",
                            "cast": "int",
                            "direction": "asc",
                            "nulls": "last",
                        }],
                        "limit": {"percentage": 50},
                    }
                }
            },
        })
        first = self._verified_group({"score": "9", "test_scope": "group-ranking"})
        second = self._verified_group({"score": "10", "test_scope": "group-ranking"})
        self._verified_group({"test_scope": "group-ranking"})

        selection = build_group_enrollment_selection(
            [], str(benefit_plan.id), "POTENTIAL", self.user
        )

        self.assertEqual(selection["will_enrol"], 2)
        self.assertEqual(
            list(selection["groups_not_assigned_to_selected_programme"].values_list("id", flat=True))[:2],
            [first.id, second.id],
        )

    def test_group_weighted_ranking_reads_primary_worker_business_data(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "GROUP",
            "beneficiary_data_schema": {
                "properties": {"test_scope": {"type": "string"}}
            },
            "json_ext": {
                "advanced_criteria": {"ACTIVE": [{
                    "field": "test_scope",
                    "filter": "exact",
                    "type": "string",
                    "value": "weighted-groups",
                }]},
                "enrolment_ranking": {
                    "ACTIVE": {
                        "score_source": {
                            "type": "PRIMARY_WORKER",
                            "data_field": "individual.json_ext",
                        },
                        "weighted_score": {
                            "normalise_to": 100,
                            "components": [
                                {
                                    "field": "type_of_business",
                                    "weight": 50,
                                    "scoring": {
                                        "type": "mapping",
                                        "values": {"Priority": 10, "Other": 6},
                                        "default": 0,
                                        "maximum_points": 10,
                                    },
                                },
                                {
                                    "field": "business_period",
                                    "weight": 50,
                                    "scoring": {
                                        "type": "numeric_bands",
                                        "bands": [
                                            {"min": 0, "max": 2.99, "points": 2},
                                            {"min": 3, "points": 10},
                                        ],
                                        "default": 0,
                                        "maximum_points": 10,
                                    },
                                },
                            ],
                        },
                        "order_by": [{
                            "field": "_weighted_score",
                            "direction": "desc",
                        }],
                        "tie_breaker": "id",
                        "limit": {"percentage": 50},
                    }
                }
            },
        })

        def household(business_type, business_period):
            return self._verified_group(
                json_ext={"test_scope": "weighted-groups"},
                individual_json_ext={
                    "type_of_business": business_type,
                    "business_period": business_period,
                },
            )

        highest = household("Priority", 5)
        second = household("Other", 4)
        household("Other", 1)

        selection = build_group_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        self.assertEqual(selection["pool_size"], 3)
        self.assertEqual(selection["will_enrol"], 2)
        self.assertEqual(
            list(
                selection["groups_not_assigned_to_selected_programme"]
                .values_list("id", flat=True)
            ),
            [highest.id, second.id],
        )

    def test_group_quotas_use_primary_worker_gender_and_dob_with_phase_cap(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "GROUP",
            "max_beneficiaries": 2,
            "beneficiary_data_schema": {"properties": {}},
            "json_ext": {
                "advanced_criteria": {"ACTIVE": []},
                "enrolment_ranking": {
                    "ACTIVE": {
                        "order_by": ["id"],
                        "selection_strategy": {
                            "source": "PRIMARY_WORKER",
                            "female_percentage": 50,
                            "youth_percentage": 50,
                            "female_value": "Female",
                            "youth_min_age": 18,
                            "youth_max_age": 35,
                        },
                    }
                }
            },
        })

        def household(gender, age):
            return self._verified_group(
                individual_json_ext={"gender": gender},
                dob=date(date.today().year - age, 1, 1),
            )

        household("Male", 50)
        young = household("Male", 25)
        female = household("Female", 45)

        selection = build_group_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        self.assertEqual(selection["cap_applied"], 2)
        self.assertEqual(selection["will_enrol"], 2)
        self.assertEqual(
            set(
                selection["groups_not_assigned_to_selected_programme"]
                .values_list("id", flat=True)
            ),
            {young.id, female.id},
        )

    def test_group_selection_only_excludes_active_assignment_for_requested_status(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "GROUP",
            "beneficiary_data_schema": {
                "properties": {"test_scope": {"type": "string"}}
            },
            "json_ext": {"advanced_criteria": {"ACTIVE": [{
                "field": "test_scope",
                "filter": "exact",
                "type": "string",
                "value": "assignment-groups",
            }]}},
        })
        active = self._verified_group({"test_scope": "assignment-groups"})
        other_status = self._verified_group({"test_scope": "assignment-groups"})
        deleted = self._verified_group({"test_scope": "assignment-groups"})

        for group, assignment_status, is_deleted in (
            (active, "ACTIVE", False),
            (other_status, "POTENTIAL", False),
            (deleted, "ACTIVE", True),
        ):
            assignment = GroupBeneficiary(
                group=group,
                benefit_plan=benefit_plan,
                status=assignment_status,
                is_deleted=is_deleted,
                json_ext={},
            )
            assignment.save(username=self.user.username)

        selection = build_group_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        self.assertEqual(selection["pool_size"], 2)
        self.assertEqual(
            list(selection["groups_not_assigned_to_selected_programme"]
                 .order_by("id").values_list("id", flat=True)),
            sorted([other_status.id, deleted.id]),
        )

    def test_individual_selection_only_excludes_active_assignment_for_requested_status(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "INDIVIDUAL",
            "beneficiary_data_schema": {
                "properties": {"test_scope": {"type": "string"}}
            },
            "json_ext": {"advanced_criteria": {"ACTIVE": [{
                "field": "test_scope",
                "filter": "exact",
                "type": "string",
                "value": "assignment-individuals",
            }]}},
        })
        active = create_individual(
            self.user.username, {"json_ext": {"test_scope": "assignment-individuals"}}
        )
        other_status = create_individual(
            self.user.username, {"json_ext": {"test_scope": "assignment-individuals"}}
        )

        for individual, assignment_status in (
            (active, "ACTIVE"),
            (other_status, "POTENTIAL"),
        ):
            assignment = Beneficiary(
                individual=individual,
                benefit_plan=benefit_plan,
                status=assignment_status,
                json_ext={},
            )
            assignment.save(username=self.user.username)

        selection = build_individual_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        self.assertEqual(selection["pool_size"], 1)
        self.assertEqual(
            list(selection["individuals_not_assigned_to_selected_programme"]),
            [other_status],
        )

    def test_missing_ranking_preserves_uncapped_behavior(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")
        benefit_plan.max_beneficiaries = None
        benefit_plan.save(username=self.user.username)
        create_individual(self.user.username, {"json_ext": {"validation_status": "VERIFIED"}})

        selection = build_individual_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        self.assertIsNone(selection["ranking"])
        self.assertIsNone(selection["cap_applied"])
        self.assertEqual(selection["pool_size"], selection["will_enrol"])

    def test_phase_target_is_ignored_until_ranking_is_configured(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")
        benefit_plan.max_beneficiaries = 1
        benefit_plan.save(username=self.user.username)
        for _ in range(3):
            create_individual(self.user.username, {"json_ext": {"validation_status": "VERIFIED"}})
        selection = build_individual_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user,
        )
        self.assertEqual(selection["pool_size"], 3)
        self.assertEqual(selection["will_enrol"], 3)
        self.assertIsNone(selection["cap_applied"])
        self.assertEqual(
            selection["individuals_not_assigned_to_selected_programme"].count(),
            3,
        )

    def test_rejects_unknown_status(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")

        with self.assertRaisesMessage(ValidationError, "Unsupported"):
            build_individual_enrollment_queryset(
                custom_filters=[],
                benefit_plan_id=str(benefit_plan.id),
                status="UNKNOWN",
                user=self.user,
            )
