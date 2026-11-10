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
    _age_condition_q,
    _annotate_selection_source,
    _normalise_selection_strategy,
    _quota_q,
    calculate_cap,
    build_order_by,
    build_weighted_score,
    rank_and_cap_queryset,
    validate_ranking_spec,
)
from individual.models import Group, GroupIndividual, Individual
from enrolment.schema import Query
from enrolment.tests.helpers import ProfileRankingMixin, create_verified_individual
from individual.custom_filters import GroupCustomFilterWizard, IndividualCustomFilterWizard
from individual.tests.test_helpers import (
    add_individual_to_group,
    create_group,
    create_individual,
)
from location.models import Location
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

    def _ranking_with_points(self, *points, normalise_to=100):
        return {
            "weighted_score": {
                "normalise_to": normalise_to,
                "components": [
                    {
                        "field": f"item_{index}",
                        "weight": item_points,
                        "scoring": {
                            "type": "numeric_bands",
                            "bands": [{"min": 0, "points": item_points}],
                        },
                    }
                    for index, item_points in enumerate(points)
                ],
            },
            "order_by": [{"field": "_weighted_score", "direction": "desc"}],
        }

    def test_weighted_ranking_rejects_points_above_maximum_score(self):
        with self.assertRaisesMessage(
            ValidationError,
            "components add up to 102 points, more than the maximum "
            "enrolment score of 100",
        ):
            validate_ranking_spec(self._ranking_with_points(60, 42), Individual)

    def test_weighted_ranking_accepts_points_equal_to_maximum_score(self):
        order_items, _, _ = validate_ranking_spec(
            self._ranking_with_points(60, 40),
            Individual,
        )

        self.assertEqual(order_items[0]["field"], "_weighted_score")

    def test_weighted_ranking_counts_declared_maximum_points(self):
        ranking = self._ranking_with_points(60, 30)
        ranking["weighted_score"]["components"][1]["scoring"]["maximum_points"] = 50

        with self.assertRaisesMessage(ValidationError, "add up to 110 points"):
            validate_ranking_spec(ranking, Individual)

    def test_weighted_ranking_rejects_normalise_to_above_maximum_score(self):
        with self.assertRaisesMessage(
            ValidationError,
            "normalise_to cannot exceed 100, the maximum enrolment score",
        ):
            validate_ranking_spec(
                self._ranking_with_points(60, 40, normalise_to=150),
                Individual,
            )

    @patch.object(EnrolmentConfig, "max_enrolment_score", 65)
    def test_weighted_ranking_uses_configured_maximum_score(self):
        with self.assertRaisesMessage(ValidationError, "maximum enrolment score of 65"):
            validate_ranking_spec(
                self._ranking_with_points(40, 30, normalise_to=65),
                Individual,
            )

    def test_module_config_rejects_invalid_maximum_score(self):
        from enrolment.apps import DEFAULT_CONFIG

        for value in (0, -1, True, "100", float("inf")):
            with self.subTest(value=value):
                with self.assertRaisesMessage(
                    ValidationError,
                    "max_enrolment_score must be a positive number.",
                ):
                    EnrolmentConfig._validate_config(
                        {**DEFAULT_CONFIG, "max_enrolment_score": value}
                    )

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

    def test_group_selection_strategy_accepts_primary_worker_or_head(self):
        for source in ("PRIMARY_WORKER", HOUSEHOLD_HEAD_SOURCE):
            validate_ranking_spec({
                "selection_strategy": {
                    "source": source,
                    "female_percentage": 40,
                    "youth_percentage": 40,
                },
            }, Group)

        with self.assertRaisesMessage(
            ValidationError,
            "source must be PRIMARY_WORKER or HOUSEHOLD_HEAD",
        ):
            validate_ranking_spec({
                "selection_strategy": {
                    "source": "SELF",
                    "female_percentage": 40,
                },
            }, Group)

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

        strategy = _normalise_selection_strategy({
            "source": HOUSEHOLD_HEAD_SOURCE,
            "female_percentage": 40,
            "youth_percentage": 40,
        }, Group)
        candidates = _annotate_selection_source(
            Group.objects.all(),
            HOUSEHOLD_HEAD_SOURCE,
        )
        for quota in strategy["quotas"]:
            candidates = candidates.filter(_quota_q(quota["when"]))

        sql = str(candidates.query)
        self.assertIn("individual_individual", sql)
        self.assertIn("gender", sql)
        self.assertIn("dob", sql.lower())
        self.assertIn("HEAD", sql)

    def test_shorthand_quotas_become_female_then_youth_buckets(self):
        strategy = _normalise_selection_strategy({
            "source": "SELF",
            "female_percentage": 60,
            "youth_percentage": 40,
            "youth_min_age": 18,
            "youth_max_age": 35,
        }, Individual)

        self.assertEqual(
            [(quota["key"], quota["percentage"]) for quota in strategy["quotas"]],
            [("female", 60), ("youth", 40)],
        )
        self.assertEqual(strategy["quotas"][0]["when"][0]["value"], "Female")
        self.assertEqual(
            [(c["filter"], c["value"]) for c in strategy["quotas"][1]["when"]],
            [("gte", 18), ("lte", 35)],
        )

    def _quota_strategy(self, quotas, source="SELF", **extra):
        return {"source": source, "quotas": quotas, **extra}

    def _business_quota(self, **overrides):
        return {
            "key": "business",
            "percentage": 50,
            "when": [{
                "field": "business_type",
                "filter": "exact",
                "type": "string",
                "value": "Retail",
            }],
            **overrides,
        }

    def test_configured_quotas_are_normalised_in_order(self):
        strategy = _normalise_selection_strategy(self._quota_strategy([
            self._business_quota(),
            {
                "key": "older",
                "count": 10,
                "when": [{
                    "subject": "self",
                    "derived": "age",
                    "filter": "gte",
                    "value": 60,
                }],
            },
        ]), Individual)

        self.assertEqual(
            [quota["key"] for quota in strategy["quotas"]],
            ["business", "older"],
        )
        self.assertEqual(strategy["quotas"][0]["percentage"], 50)
        self.assertEqual(strategy["quotas"][1]["count"], 10)
        self.assertEqual(
            strategy["quotas"][1]["when"],
            [{"derived": "age", "filter": "gte", "value": 60}],
        )

    def test_configured_quotas_reject_invalid_buckets(self):
        cases = [
            (
                self._quota_strategy([self._business_quota()], female_percentage=40),
                "cannot be combined with female_percentage",
            ),
            (self._quota_strategy([]), "quotas must be a non-empty list"),
            (
                self._quota_strategy([
                    self._business_quota(),
                    self._business_quota(key="other", percentage=60),
                ]),
                "percentages cannot exceed 100",
            ),
            (
                self._quota_strategy([
                    self._business_quota(),
                    self._business_quota(percentage=10),
                ]),
                "key business is used more than once",
            ),
            (
                self._quota_strategy([self._business_quota(count=5)]),
                "exactly one of percentage or count",
            ),
            (
                self._quota_strategy([
                    {"key": "business", "count": 0, "when": [{}]},
                ]),
                "count must be a positive integer",
            ),
            (
                self._quota_strategy([self._business_quota(when=[])]),
                "when must be a non-empty list",
            ),
            (
                self._quota_strategy(
                    [self._business_quota()], fill_remaining=False
                ),
                "Unsupported enrolment_ranking.selection_strategy keys: fill_remaining",
            ),
        ]
        for strategy, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesMessage(ValidationError, message):
                    _normalise_selection_strategy(strategy, Individual)

    def test_age_condition_filters_the_given_dob_field(self):
        sql = str(Individual.objects.filter(
            _age_condition_q("dob", "gte", 18)
        ).query)
        self.assertIn('"dob" <=', sql)

        sql = str(Individual.objects.filter(
            _age_condition_q("dob", "exact", 30)
        ).query)
        self.assertIn('"dob" BETWEEN', sql)

    def test_configured_quota_conditions_are_validated(self):
        def with_condition(condition, source="SELF", model=Individual):
            _normalise_selection_strategy(self._quota_strategy(
                [self._business_quota(when=[condition])], source=source,
            ), model)

        cases = [
            (
                {"subject": "head", "field": "gender", "filter": "exact",
                 "type": "string", "value": "Female"},
                "subject must be self when the selection_strategy.source is SELF",
            ),
            (
                {"derived": "income", "filter": "exact", "value": "High"},
                "derived income is not supported; supported values: age, district",
            ),
            (
                {"derived": "district", "filter": "exact", "type": "integer", "value": 1},
                "type must be string for district",
            ),
            (
                {"derived": "district", "filter": "gte", "value": "Zomba"},
                "filter gte is not allowed for district",
            ),
            (
                {"derived": "district", "filter": "iexact", "value": " "},
                "value must be a district name",
            ),
            (
                {"field": "gender", "derived": "age", "filter": "gte", "value": 1},
                "exactly one of field or derived",
            ),
            (
                {"field": "gender__isnull", "filter": "exact",
                 "type": "boolean", "value": True},
                "field must be a json_ext key",
            ),
            (
                {"field": "registered", "filter": "exact",
                 "type": "date", "value": "2026-01-01"},
                "type must be one of",
            ),
            (
                {"field": "gender", "filter": "gte",
                 "type": "string", "value": "Female"},
                "filter gte is not allowed for string",
            ),
            (
                {"field": "workers", "filter": "gte",
                 "type": "integer", "value": "5"},
                "value must be of type integer",
            ),
            (
                {"derived": "age", "filter": "gte", "value": 18.5},
                "value must be an age from 0 to 120",
            ),
        ]
        for condition, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesMessage(ValidationError, message):
                    with_condition(condition)

        with_condition(
            {"subject": "head", "field": "gender", "filter": "iexact",
             "type": "string", "value": "Female"},
            source=HOUSEHOLD_HEAD_SOURCE,
            model=Group,
        )
        with_condition({"derived": "district", "filter": "iexact", "value": "Zomba"})

    def test_configured_quota_compiles_field_condition_on_primary_worker(self):
        strategy = _normalise_selection_strategy(self._quota_strategy(
            [self._business_quota()], source="PRIMARY_WORKER",
        ), Group)
        candidates = _annotate_selection_source(
            Group.objects.all(), "PRIMARY_WORKER",
        ).filter(_quota_q(strategy["quotas"][0]["when"]))

        sql = str(candidates.query)
        self.assertIn("business_type", sql)
        self.assertIn("primary_worker", sql)

    def test_profile_ranking_uses_status_then_wildcard(self):
        from enrolment.profiles import profile_ranking_spec

        profiles = {"default": {"ranking": {
            "*": {"order_by": ["id"]}, "ACTIVE": {"order_by": ["-id"]},
        }}}
        plan = Mock(code="PWP", json_ext={})
        with patch.object(EnrolmentConfig, "programme_profiles", profiles):
            self.assertEqual(profile_ranking_spec(plan, "ACTIVE")["order_by"], ["-id"])
            self.assertEqual(profile_ranking_spec(plan, "POTENTIAL")["order_by"], ["id"])

    def test_a_phase_still_holding_a_ranking_cannot_enrol(self):
        queryset = MagicMock(model=Individual)
        queryset.count.return_value = 3
        plan = Mock(
            code="PWP-1",
            json_ext={"enrolment_ranking": {"ACTIVE": {"order_by": ["-id"]}}},
            max_beneficiaries=1,
        )
        with patch.object(EnrolmentConfig, "programme_profiles", {}):
            with self.assertRaisesMessage(
                ValidationError, "Phase PWP-1 still has an enrolment_ranking",
            ):
                rank_and_cap_queryset(
                    queryset, plan, "ACTIVE", 0, materialize_selected_ids=False,
                )
            queryset.count.assert_not_called()

            # The empty ranking the Phase form sends to clear it does not block.
            plan.json_ext = {"enrolment_ranking": {}}
            _, metadata = rank_and_cap_queryset(
                queryset, plan, "ACTIVE", 0, materialize_selected_ids=False,
            )
        self.assertEqual(metadata["will_enrol"], 3)

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

    def test_group_ordering_reads_household_head_json_ext(self):
        order_items, _, _ = validate_ranking_spec({
            "order_by": [{
                "subject": "head",
                "field": "household_pmt_score",
                "cast": "float",
                "nulls": "last",
            }],
        }, Group, {"properties": {"other": {"type": "string"}}})

        self.assertEqual(order_items[0]["subject"], "head")
        self.assertEqual(order_items[0]["direction"], "asc")
        sql = str(build_order_by(Group.objects.all(), order_items).query)
        self.assertIn("household_pmt_score", sql)
        self.assertIn("HEAD", sql)
        self.assertIn("double precision", sql)
        self.assertIn("NULLS LAST", sql)

    def test_group_ordering_reads_primary_worker_json_ext(self):
        order_items, _, _ = validate_ranking_spec({
            "order_by": [{
                "subject": "primary_worker",
                "field": "business_period",
                "direction": "desc",
            }],
        }, Group)

        sql = str(build_order_by(Group.objects.all(), order_items).query)
        self.assertIn("business_period", sql)
        self.assertIn("primary_worker", sql)

    def test_member_ordering_subjects_are_validated(self):
        cases = [
            (
                {"subject": "head", "field": "household_pmt_score"},
                Individual,
                "subject must be self",
            ),
            (
                {"subject": "self", "field": "household_pmt_score"},
                Group,
                "subject must be head or primary_worker",
            ),
            (
                {"subject": "head", "field": "json_ext__household_pmt_score"},
                Group,
                "must be a json_ext key on the head",
            ),
        ]
        for item, model, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesMessage(ValidationError, message):
                    validate_ranking_spec({"order_by": [item]}, model)

        order_items, _, _ = validate_ranking_spec({
            "order_by": [{"subject": "self", "field": "dob"}],
        }, Individual)
        self.assertNotIn("subject", order_items[0])

    def test_member_ordering_does_not_satisfy_weighted_score_ordering(self):
        with self.assertRaisesMessage(ValidationError, "must include _weighted_score"):
            validate_ranking_spec({
                "weighted_score": {
                    "components": [{
                        "field": "business_period",
                        "weight": 1,
                        "scoring": {"type": "mapping", "values": {"Yes": 1}},
                    }],
                },
                "order_by": [{"subject": "head", "field": "_weighted_score"}],
            }, Group)

    def test_ranking_rejects_multi_valued_relation_paths(self):
        with self.assertRaisesMessage(ValidationError, "multi-valued path"):
            validate_ranking_spec(
                {"order_by": ["groupindividuals__individual__dob"]},
                Group,
            )

    def test_phase_validator_rejects_a_phase_ranking(self):
        from enrolment.ranking import benefit_plan_ranking_validation_errors

        errors = benefit_plan_ranking_validation_errors({
            "type": "GROUP",
            "json_ext": {"enrolment_ranking": {"ACTIVE": {"order_by": ["id"]}}},
        })
        self.assertEqual(len(errors), 1)
        self.assertIn("A Phase can no longer have an enrolment_ranking", errors[0]["message"])
        self.assertIn("programme_profiles.<Phase code or default>.ranking", errors[0]["message"])

        # Clearing the ranking, or an update that leaves json_ext out, is accepted.
        self.assertEqual(benefit_plan_ranking_validation_errors(
            {"json_ext": {"enrolment_ranking": {}}}
        ), [])
        self.assertEqual(benefit_plan_ranking_validation_errors(
            {"name": "Renamed"}, Mock(json_ext={"enrolment_ranking": {"*": {}}})
        ), [])

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
        benefit_plan = Mock(code="PWP", json_ext={}, max_beneficiaries=None)
        profiles = {"default": {"ranking": {"*": {"order_by": [{
            "field": "json_ext__household_wealth_quintile",
            "cast": "int",
        }]}}}}

        with patch.object(EnrolmentConfig, "programme_profiles", profiles):
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
class AuthoritativeEnrollmentTest(ProfileRankingMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()

    def _verified_group(self, json_ext=None, individual_json_ext=None, dob=None):
        group_json = dict(json_ext or {})
        group_json.setdefault("validation_status", "VERIFIED")
        group = create_group(self.user.username, {"json_ext": group_json})
        individual_data = {"json_ext": individual_json_ext or {}}
        if dob is not None:
            individual_data["dob"] = dob
        individual = create_verified_individual(self.user.username, individual_data)
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
        benefit_plan = self._ranked_plan(
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
        create_verified_individual(self.user.username, {
            "dob": date(date.today().year - 50, 1, 1),
            "json_ext": {"gender": "Male"},
        })
        selected = []
        for age in (25, 30):
            selected.append(create_verified_individual(self.user.username, {
                "dob": date(date.today().year - age, 1, 1),
                "json_ext": {"gender": "Male"},
            }))
        for age in (40, 45, 50):
            selected.append(create_verified_individual(self.user.username, {
                "dob": date(date.today().year - age, 1, 1),
                "json_ext": {"gender": "Female"},
            }))
        return benefit_plan, {individual.id for individual in selected}

    def _weighted_quota_plan(self, cap):
        return self._ranked_plan(
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
        return create_verified_individual(self.user.username, {
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
        eligible = create_verified_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Lilongwe",
            }
        })
        create_verified_individual(self.user.username, {
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
        selected = create_verified_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Lilongwe",
            }
        })
        create_verified_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Blantyre",
            }
        })
        create_verified_individual(self.user.username, {
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

    def test_profile_and_saved_phase_criteria_are_anded(self):
        self.enterContext(patch.object(EnrolmentConfig, "programme_profiles", {
            "default": {"eligibility": {"criteria": [{
                "field": "district",
                "filter": "exact",
                "type": "string",
                "value": "Lilongwe",
            }]}},
        }))
        benefit_plan = self._benefit_plan("INDIVIDUAL")
        selected = create_verified_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Lilongwe",
            }
        })
        create_verified_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Blantyre",
            }
        })
        create_verified_individual(self.user.username, {
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

    def test_individual_phase_enrols_only_verified_individuals(self):
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "INDIVIDUAL",
            "json_ext": {"advanced_criteria": {"ACTIVE": []}},
        })
        verified = create_verified_individual(self.user.username)
        create_verified_individual(self.user.username, {
            "json_ext": {"validation_status": "PENDING"},
        })
        create_individual(self.user.username, {"json_ext": {}})

        result = build_individual_enrollment_queryset(
            custom_filters=[],
            benefit_plan_id=str(benefit_plan.id),
            status="ACTIVE",
            user=self.user,
        )

        self.assertEqual(set(result.values_list("id", flat=True)), {verified.id})

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

    def test_group_profile_rule_cannot_be_removed_from_phase_or_payload(self):
        self.enterContext(patch.object(EnrolmentConfig, "programme_profiles", {
            "default": {"eligibility": {"criteria": [{
                "subject": "household",
                "field": "validation_status",
                "filter": "exact",
                "type": "string",
                "value": "VERIFIED",
            }]}},
        }))
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
        benefit_plan = self._ranked_plan(self.user.username, payload_override={
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
        ranked = create_verified_individual(self.user.username, {"json_ext": {"score": "9"}})
        second = create_verified_individual(self.user.username, {"json_ext": {"score": "10"}})
        create_verified_individual(self.user.username, {"json_ext": {"score": "100"}})

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
        benefit_plan = self._ranked_plan(self.user.username, payload_override={
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
        first = create_verified_individual(self.user.username)
        second = create_verified_individual(self.user.username)
        create_verified_individual(self.user.username)

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
        benefit_plan = self._ranked_plan(self.user.username, payload_override={
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
        existing = create_verified_individual(self.user.username)
        create_verified_individual(self.user.username)
        create_verified_individual(self.user.username)
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
        benefit_plan = self._ranked_plan(self.user.username, payload_override={
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
        benefit_plan = self._ranked_plan(self.user.username, payload_override={
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
        benefit_plan = self._ranked_plan(self.user.username, payload_override={
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

    def test_group_quotas_can_use_household_head_gender_and_dob(self):
        benefit_plan = self._ranked_plan(self.user.username, payload_override={
            "type": "GROUP",
            "max_beneficiaries": 2,
            "beneficiary_data_schema": {"properties": {}},
            "json_ext": {
                "advanced_criteria": {"ACTIVE": []},
                "enrolment_ranking": {
                    "ACTIVE": {
                        "order_by": ["id"],
                        "selection_strategy": {
                            "source": "HOUSEHOLD_HEAD",
                            "female_percentage": 50,
                            "youth_percentage": 50,
                        },
                    }
                }
            },
        })

        def household(head_gender, head_age):
            # The primary worker is always an older man, so only the head's
            # gender and age can place the household in a quota.
            group = self._verified_group(
                individual_json_ext={"gender": "Male"},
                dob=date(date.today().year - 50, 1, 1),
            )
            GroupIndividual.objects.filter(group=group).update(
                role=GroupIndividual.Role.SON
            )
            head = create_verified_individual(self.user.username, {
                "dob": date(date.today().year - head_age, 1, 1),
                "json_ext": {"gender": head_gender},
            })
            add_individual_to_group(self.user.username, head, group)
            # Saving a membership copies the head's json_ext onto the household
            # and drops validation_status, so verify the household again.
            group.refresh_from_db()
            group.json_ext = {**(group.json_ext or {}), "validation_status": "VERIFIED"}
            group.save(username=self.user.username)
            return group

        household("Male", 50)
        young_head = household("Male", 25)
        female_head = household("Female", 45)

        selection = build_group_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        self.assertEqual(selection["will_enrol"], 2)
        self.assertEqual(
            set(
                selection["groups_not_assigned_to_selected_programme"]
                .values_list("id", flat=True)
            ),
            {young_head.id, female_head.id},
        )

    def test_group_ranking_orders_by_household_head_pmt_score(self):
        self._assert_orders_by_head_pmt_score(str)

    def test_group_ranking_orders_by_numeric_household_head_pmt_score(self):
        # Household validation stores the PMT score as a JSON number (D6).
        self._assert_orders_by_head_pmt_score(
            lambda score: int(score) if score.isdigit() else float(score)
        )

    def _assert_orders_by_head_pmt_score(self, stored):
        """Rank households by the head's PMT score, stored with ``stored``."""
        benefit_plan = self._ranked_plan(self.user.username, payload_override={
            "type": "GROUP",
            "max_beneficiaries": 3,
            "beneficiary_data_schema": {"properties": {}},
            "json_ext": {
                "advanced_criteria": {"ACTIVE": []},
                "enrolment_ranking": {
                    "ACTIVE": {
                        "order_by": [{
                            "subject": "head",
                            "field": "household_pmt_score",
                            "cast": "float",
                            "direction": "asc",
                            "nulls": "last",
                        }],
                    }
                },
            },
        })

        def household(pmt_score):
            json_ext = {}
            if pmt_score is not None:
                json_ext["household_pmt_score"] = pmt_score
            return self._verified_group(individual_json_ext=json_ext)

        household(None)
        richer = household(stored("40.5"))
        poorest = household(stored("8.25"))
        poorer = household(stored("12"))
        household(stored("55"))

        selection = build_group_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        # Lowest PMT first; a household without a score sorts last and loses
        # its place to scored households.
        self.assertEqual(
            list(
                selection["groups_not_assigned_to_selected_programme"]
                .values_list("id", flat=True)
            ),
            [poorest.id, poorer.id, richer.id],
        )
        self.assertEqual(selection["pool_size"], 5)

    def test_district_quotas_place_individuals_by_their_location(self):
        def village_in(district_name):
            def location(kind, parent=None):
                # Only the district level carries the district's name.
                name = district_name if kind == "R" else f"{district_name} {kind}"
                return Location.objects.create(
                    type=kind, code=f"{district_name}-{kind}", name=name,
                    parent=parent, audit_user_id=-1,
                )
            return location("V", location("W", location("D", location("R"))))

        zomba, blantyre = village_in("DQ-Zomba"), village_in("DQ-Blantyre")
        benefit_plan = self._ranked_plan(self.user.username, payload_override={
            "type": "INDIVIDUAL",
            "max_beneficiaries": 2,
            "beneficiary_data_schema": {
                "properties": {"quota_scope": {"type": "string"}}
            },
            "json_ext": {
                "advanced_criteria": {"ACTIVE": [{
                    "field": "quota_scope",
                    "filter": "exact",
                    "type": "string",
                    "value": "district-quotas",
                }]},
                "enrolment_ranking": {"ACTIVE": {
                    "order_by": ["id"],
                    "selection_strategy": {
                        "source": "SELF",
                        "quotas": [
                            {
                                "key": name,
                                "count": 1,
                                "when": [{
                                    "derived": "district",
                                    "filter": "iexact",
                                    "value": name,
                                }],
                            }
                            for name in ("dq-zomba", "dq-blantyre")
                        ],
                    },
                }},
            },
        })

        def applicant(village):
            return create_verified_individual(self.user.username, {
                "location": village,
                "json_ext": {"quota_scope": "district-quotas"},
            })

        first_zomba = applicant(zomba)
        applicant(zomba)
        in_blantyre = applicant(blantyre)

        selection = build_individual_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        # Ranked by id alone, both Zomba applicants would take the two places;
        # each district's quota gives one to Blantyre instead.
        self.assertEqual(
            set(selection["selected_ids"]), {first_zomba.id, in_blantyre.id},
        )

    def test_configured_count_quota_shortfall_goes_to_next_ranked(self):
        benefit_plan = self._ranked_plan(self.user.username, payload_override={
            "type": "INDIVIDUAL",
            "max_beneficiaries": 4,
            "beneficiary_data_schema": {
                "properties": {"quota_scope": {"type": "string"}}
            },
            "json_ext": {
                "advanced_criteria": {"ACTIVE": [{
                    "field": "quota_scope",
                    "filter": "exact",
                    "type": "string",
                    "value": "city-quotas",
                }]},
                "enrolment_ranking": {
                    "ACTIVE": {
                        "order_by": ["id"],
                        "selection_strategy": {
                            "source": "SELF",
                            "quotas": [
                                {
                                    "key": city.lower(),
                                    "count": 2,
                                    "when": [{
                                        "field": "city",
                                        "filter": "exact",
                                        "type": "string",
                                        "value": city,
                                    }],
                                }
                                for city in ("Zomba", "Mzuzu")
                            ],
                        },
                    }
                },
            },
        })

        def applicant(city):
            return create_verified_individual(self.user.username, {
                "json_ext": {"quota_scope": "city-quotas", "city": city},
            })

        zomba = [applicant("Zomba") for _ in range(3)]
        mzuzu = applicant("Mzuzu")
        applicant("Lilongwe")

        selection = build_individual_enrollment_selection(
            [], str(benefit_plan.id), "ACTIVE", self.user
        )

        # Mzuzu has one applicant for two places, so the spare place goes to
        # the next ranked applicant: the third Zomba applicant, ahead of
        # Lilongwe by id. The Phase still reaches its target of 4.
        self.assertEqual(selection["will_enrol"], 4)
        self.assertEqual(
            set(selection["selected_ids"]),
            {zomba[0].id, zomba[1].id, mzuzu.id, zomba[2].id},
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
        active = create_verified_individual(
            self.user.username, {"json_ext": {"test_scope": "assignment-individuals"}}
        )
        other_status = create_verified_individual(
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
        create_verified_individual(self.user.username, {"json_ext": {"validation_status": "VERIFIED"}})

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
            create_verified_individual(self.user.username, {"json_ext": {"validation_status": "VERIFIED"}})
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
