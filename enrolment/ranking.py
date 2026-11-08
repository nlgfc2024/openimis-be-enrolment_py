import json
import math
from datetime import date, timedelta

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import DataError
from django.db.models import (
    Case,
    CharField,
    DateField,
    ExpressionWrapper,
    F,
    FloatField,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast

from core.custom_filters import CustomFilterWizardStorage
from enrolment.apps import EnrolmentConfig
from enrolment.criteria import (
    FILTERS_BY_TYPE,
    VALUE_TYPES,
    criterion_to_condition,
    is_number,
)
from enrolment.membership import (
    HEAD_SUBJECT,
    MEMBER_SUBJECTS,
    PRIMARY_WORKER_SUBJECT,
    SELF_SUBJECT,
    authoritative_primary_worker_memberships,
)


SUPPORTED_CASTS = {
    "int": IntegerField,
    "float": FloatField,
    "date": DateField,
    "str": CharField,
}
SUPPORTED_DIRECTIONS = {"asc", "desc"}
SUPPORTED_NULLS = {"first", "last"}
WEIGHTED_SCORE_FIELD = "_weighted_score"
PRIMARY_WORKER_SOURCE = "PRIMARY_WORKER"
HOUSEHOLD_HEAD_SOURCE = "HOUSEHOLD_HEAD"
SELF_SOURCE = "SELF"
# The subject each selection_strategy.source names.
SOURCE_SUBJECTS = {
    SELF_SOURCE: SELF_SUBJECT,
    PRIMARY_WORKER_SOURCE: PRIMARY_WORKER_SUBJECT,
    HOUSEHOLD_HEAD_SOURCE: HEAD_SUBJECT,
}
# Household members whose json_ext a GROUP order_by item may read. A sort needs
# one value per household, so any_member waits for a best-member aggregate.
ORDER_BY_MEMBER_SUBJECTS = {HEAD_SUBJECT, PRIMARY_WORKER_SUBJECT}
SHORTHAND_QUOTA_KEYS = {
    "female_percentage",
    "youth_percentage",
    "female_value",
    "youth_min_age",
    "youth_max_age",
}


def load_ranking_spec(benefit_plan, status):
    """Return the status-specific ranking configuration stored on a Phase."""
    json_ext = benefit_plan.json_ext or {}
    if isinstance(json_ext, str):
        try:
            json_ext = json.loads(json_ext)
        except (TypeError, ValueError) as exc:
            raise ValidationError("BenefitPlan json_ext must be valid JSON.") from exc
    if not isinstance(json_ext, dict):
        raise ValidationError("BenefitPlan json_ext must be an object.")

    rankings = json_ext.get("enrolment_ranking")
    if rankings is None:
        return None
    if not isinstance(rankings, dict):
        raise ValidationError("enrolment_ranking must be an object keyed by beneficiary status.")
    ranking = rankings.get(status, rankings.get("*"))
    if ranking is None:
        return None
    if not isinstance(ranking, dict):
        raise ValidationError(f"enrolment_ranking.{status} must be an object.")
    return ranking


def _validate_model_path(model, path, extra_fields=None, schema_fields=None):
    if not isinstance(path, str) or not path:
        raise ValidationError("Ranking fields must be non-empty strings.")
    if path in (extra_fields or set()) or path in (schema_fields or set()):
        return
    parts = path.split("__")
    if parts[0] == "json_ext" and len(parts) == 2 and parts[1]:
        if schema_fields and parts[1] not in schema_fields:
            raise ValidationError(
                f"Ranking field {path} is not defined in beneficiary_data_schema."
            )
        return
    if len(parts) != 1:
        raise ValidationError(
            f"Ranking field {path} cannot cross a related or multi-valued path."
        )
    try:
        field = model._meta.get_field(path)
    except (FieldDoesNotExist, AttributeError) as exc:
        raise ValidationError(f"Unsupported ranking field: {path}.") from exc
    if not field.concrete or field.many_to_many or field.one_to_many:
        raise ValidationError(f"Unsupported ranking field: {path}.")


def _normalise_order_item(
    item, model, index, extra_fields=None, schema_fields=None
):
    if isinstance(item, str):
        direction = "desc" if item.startswith("-") else "asc"
        field = item[1:] if item.startswith("-") else item
        result = {"field": field, "direction": direction}
    elif isinstance(item, dict):
        unexpected = set(item) - {
            "subject", "field", "direction", "cast", "nulls",
        }
        if unexpected:
            raise ValidationError(
                f"Unsupported enrolment_ranking.order_by[{index}] keys: "
                + ", ".join(sorted(unexpected))
            )
        result = dict(item)
        result.setdefault("direction", "asc")
    else:
        raise ValidationError(f"enrolment_ranking.order_by[{index}] must be a string or object.")

    subject = result.pop("subject", None)
    if model._meta.model_name == "individual":
        if subject not in {None, SELF_SUBJECT}:
            raise ValidationError(
                f"INDIVIDUAL enrolment_ranking.order_by[{index}].subject "
                "must be self."
            )
        subject = None
    elif subject is not None and subject not in ORDER_BY_MEMBER_SUBJECTS:
        raise ValidationError(
            f"GROUP enrolment_ranking.order_by[{index}].subject must be "
            "head or primary_worker."
        )

    if subject is None:
        _validate_model_path(
            model,
            result.get("field"),
            extra_fields,
            schema_fields,
        )
    else:
        # Member values come from Individual.json_ext, which the Phase's
        # beneficiary_data_schema does not describe.
        field = result.get("field")
        if not isinstance(field, str) or not field or "__" in field:
            raise ValidationError(
                f"enrolment_ranking.order_by[{index}].field must be a json_ext "
                f"key on the {subject}."
            )
        result["subject"] = subject
    if result["direction"] not in SUPPORTED_DIRECTIONS:
        raise ValidationError(f"Unsupported ranking direction: {result['direction']}.")
    if result.get("cast") not in ({None} | set(SUPPORTED_CASTS)):
        raise ValidationError(f"Unsupported ranking cast: {result.get('cast')}.")
    if result.get("nulls") not in ({None} | SUPPORTED_NULLS):
        raise ValidationError(f"Unsupported ranking null placement: {result.get('nulls')}.")
    return result


def _positive_number(value, path):
    if not is_number(value) or value <= 0:
        raise ValidationError(f"{path} must be a positive number.")


def _validate_score_source(score_source, model):
    if score_source is None:
        return
    if not isinstance(score_source, dict):
        raise ValidationError("enrolment_ranking.score_source must be an object.")
    unexpected = set(score_source) - {"type", "data_field"}
    if unexpected:
        raise ValidationError(
            "Unsupported enrolment_ranking.score_source keys: "
            + ", ".join(sorted(unexpected))
        )
    if score_source.get("type") != PRIMARY_WORKER_SOURCE:
        raise ValidationError(
            "enrolment_ranking.score_source.type must be PRIMARY_WORKER."
        )
    if model._meta.model_name != "group":
        raise ValidationError(
            "PRIMARY_WORKER score_source is only supported for GROUP enrollment."
        )
    if score_source.get("data_field") != "individual.json_ext":
        raise ValidationError(
            "PRIMARY_WORKER score_source.data_field must be individual.json_ext."
        )


def _validate_scoring(scoring, path):
    if not isinstance(scoring, dict):
        raise ValidationError(f"{path} must be an object.")
    scoring_type = scoring.get("type")
    if scoring_type == "mapping":
        unexpected = set(scoring) - {
            "type", "values", "default", "maximum_points"
        }
        if unexpected:
            raise ValidationError(
                f"Unsupported {path} keys: {', '.join(sorted(unexpected))}."
            )
        values = scoring.get("values")
        if not isinstance(values, dict) or not values:
            raise ValidationError(f"{path}.values must be a non-empty object.")
        if not all(
            isinstance(key, str) and is_number(points)
            for key, points in values.items()
        ):
            raise ValidationError(
                f"{path}.values must map strings to numeric points."
            )
        if not is_number(scoring.get("default", 0)):
            raise ValidationError(f"{path}.default must be numeric.")
        maximum_points = scoring.get(
            "maximum_points",
            max([scoring.get("default", 0), *values.values()]),
        )
        configured_points = [scoring.get("default", 0), *values.values()]
    elif scoring_type == "numeric_bands":
        unexpected = set(scoring) - {
            "type", "bands", "default", "maximum_points"
        }
        if unexpected:
            raise ValidationError(
                f"Unsupported {path} keys: {', '.join(sorted(unexpected))}."
            )
        bands = scoring.get("bands")
        if not isinstance(bands, list) or not bands:
            raise ValidationError(f"{path}.bands must be a non-empty list.")
        points = []
        for index, band in enumerate(bands):
            band_path = f"{path}.bands[{index}]"
            if not isinstance(band, dict):
                raise ValidationError(f"{band_path} must be an object.")
            unexpected_band = set(band) - {"min", "max", "points"}
            if unexpected_band:
                raise ValidationError(
                    f"Unsupported {band_path} keys: "
                    f"{', '.join(sorted(unexpected_band))}."
                )
            if "points" not in band or not is_number(band["points"]):
                raise ValidationError(f"{band_path}.points must be numeric.")
            if "min" not in band and "max" not in band:
                raise ValidationError(
                    f"{band_path} must define min, max, or both."
                )
            for bound in ("min", "max"):
                if bound in band and not is_number(band[bound]):
                    raise ValidationError(f"{band_path}.{bound} must be numeric.")
            if (
                "min" in band
                and "max" in band
                and band["min"] > band["max"]
            ):
                raise ValidationError(f"{band_path}.min cannot exceed max.")
            points.append(band["points"])
        if not is_number(scoring.get("default", 0)):
            raise ValidationError(f"{path}.default must be numeric.")
        maximum_points = scoring.get(
            "maximum_points",
            max([scoring.get("default", 0), *points]),
        )
        configured_points = [scoring.get("default", 0), *points]
    else:
        raise ValidationError(
            f"{path}.type must be mapping or numeric_bands."
        )
    _positive_number(maximum_points, f"{path}.maximum_points")
    if any(points < 0 for points in configured_points):
        raise ValidationError(f"{path} points cannot be negative.")
    if maximum_points < max(configured_points):
        raise ValidationError(
            f"{path}.maximum_points cannot be smaller than configured points."
        )


def _validate_weighted_score(weighted_score):
    if not isinstance(weighted_score, dict):
        raise ValidationError("enrolment_ranking.weighted_score must be an object.")
    unexpected = set(weighted_score) - {"normalise_to", "components"}
    if unexpected:
        raise ValidationError(
            "Unsupported enrolment_ranking.weighted_score keys: "
            + ", ".join(sorted(unexpected))
        )
    normalise_to = weighted_score.get("normalise_to", 100)
    _positive_number(
        normalise_to,
        "enrolment_ranking.weighted_score.normalise_to",
    )
    max_score = EnrolmentConfig.max_enrolment_score
    if normalise_to > max_score:
        raise ValidationError(
            "enrolment_ranking.weighted_score.normalise_to cannot exceed "
            f"{max_score:g}, the maximum enrolment score."
        )
    components = weighted_score.get("components")
    if not isinstance(components, list) or not components:
        raise ValidationError(
            "enrolment_ranking.weighted_score.components must be a non-empty list."
        )
    fields = set()
    for index, component in enumerate(components):
        path = f"enrolment_ranking.weighted_score.components[{index}]"
        if not isinstance(component, dict):
            raise ValidationError(f"{path} must be an object.")
        unexpected_component = set(component) - {"field", "weight", "scoring"}
        if unexpected_component:
            raise ValidationError(
                f"Unsupported {path} keys: "
                f"{', '.join(sorted(unexpected_component))}."
            )
        field = component.get("field")
        if not isinstance(field, str) or not field:
            raise ValidationError(f"{path}.field must be a non-empty string.")
        if field in fields:
            raise ValidationError(f"Duplicate weighted score field: {field}.")
        fields.add(field)
        _positive_number(component.get("weight"), f"{path}.weight")
        _validate_scoring(component.get("scoring"), f"{path}.scoring")
    total_points = sum(
        _maximum_points(component["scoring"]) for component in components
    )
    if total_points > max_score:
        raise ValidationError(
            "enrolment_ranking.weighted_score components add up to "
            f"{total_points:g} points, more than the maximum enrolment score "
            f"of {max_score:g}."
        )


def _normalise_selection_strategy(selection_strategy, model):
    if not isinstance(selection_strategy, dict):
        raise ValidationError(
            "enrolment_ranking.selection_strategy must be an object."
        )
    allowed = {"source", "quotas", *SHORTHAND_QUOTA_KEYS}
    unexpected = set(selection_strategy) - allowed
    if unexpected:
        raise ValidationError(
            "Unsupported enrolment_ranking.selection_strategy keys: "
            + ", ".join(sorted(unexpected))
        )

    source = selection_strategy.get("source")
    if model._meta.model_name == "individual":
        if source != SELF_SOURCE:
            raise ValidationError(
                "INDIVIDUAL selection_strategy.source must be SELF."
            )
    elif source not in {PRIMARY_WORKER_SOURCE, HOUSEHOLD_HEAD_SOURCE}:
        raise ValidationError(
            "GROUP selection_strategy.source must be PRIMARY_WORKER or "
            "HOUSEHOLD_HEAD."
        )

    if "quotas" in selection_strategy:
        shorthand = SHORTHAND_QUOTA_KEYS & set(selection_strategy)
        if shorthand:
            raise ValidationError(
                "enrolment_ranking.selection_strategy.quotas cannot be combined "
                "with " + ", ".join(sorted(shorthand)) + "."
            )
        quotas = _normalise_quotas(selection_strategy["quotas"], source)
    else:
        quotas = _shorthand_quotas(selection_strategy)

    return {"source": source, "quotas": quotas}


def _shorthand_quotas(selection_strategy):
    """Translate the female/youth shorthand into female then youth buckets."""
    female = selection_strategy.get("female_percentage", 0)
    youth = selection_strategy.get("youth_percentage", 0)
    for key, value in (
        ("female_percentage", female),
        ("youth_percentage", youth),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 100
        ):
            raise ValidationError(
                f"enrolment_ranking.selection_strategy.{key} must be "
                "between 0 and 100."
            )
    if female + youth > 100:
        raise ValidationError(
            "enrolment_ranking.selection_strategy percentages cannot exceed 100."
        )
    if female == 0 and youth == 0:
        raise ValidationError(
            "enrolment_ranking.selection_strategy must configure a non-zero quota."
        )

    female_value = selection_strategy.get("female_value", "Female")
    if not isinstance(female_value, str) or not female_value.strip():
        raise ValidationError(
            "enrolment_ranking.selection_strategy.female_value must be a "
            "non-empty string."
        )

    youth_min = selection_strategy.get("youth_min_age", 18)
    youth_max = selection_strategy.get("youth_max_age", 35)
    if (
        type(youth_min) is not int
        or type(youth_max) is not int
        or not 0 <= youth_min <= youth_max <= 120
    ):
        raise ValidationError(
            "enrolment_ranking.selection_strategy youth ages must be integers "
            "with 0 <= minimum <= maximum <= 120."
        )

    return [
        {
            "key": "female",
            "percentage": female,
            "when": [{
                "field": EnrolmentConfig.registry_gender_field,
                "filter": "iexact",
                "type": "string",
                "value": female_value,
            }],
        },
        {
            "key": "youth",
            "percentage": youth,
            "when": [
                {"derived": "age", "filter": "gte", "value": youth_min},
                {"derived": "age", "filter": "lte", "value": youth_max},
            ],
        },
    ]


def _normalise_quotas(quotas, source):
    path = "enrolment_ranking.selection_strategy.quotas"
    if not isinstance(quotas, list) or not quotas:
        raise ValidationError(f"{path} must be a non-empty list.")

    normalised = []
    keys = set()
    total_percentage = 0
    for index, quota in enumerate(quotas):
        quota_path = f"{path}[{index}]"
        if not isinstance(quota, dict):
            raise ValidationError(f"{quota_path} must be an object.")
        unexpected = set(quota) - {"key", "when", "percentage", "count"}
        if unexpected:
            raise ValidationError(
                f"Unsupported {quota_path} keys: " + ", ".join(sorted(unexpected))
            )

        key = quota.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ValidationError(f"{quota_path}.key must be a non-empty string.")
        if key in keys:
            raise ValidationError(f"{path} key {key} is used more than once.")
        keys.add(key)

        if ("percentage" in quota) == ("count" in quota):
            raise ValidationError(
                f"{quota_path} must set exactly one of percentage or count."
            )
        bucket = {"key": key}
        if "percentage" in quota:
            percentage = quota["percentage"]
            if not is_number(percentage) or not 0 < percentage <= 100:
                raise ValidationError(
                    f"{quota_path}.percentage must be above 0 and at most 100."
                )
            total_percentage += percentage
            bucket["percentage"] = percentage
        else:
            count = quota["count"]
            if type(count) is not int or count < 1:
                raise ValidationError(
                    f"{quota_path}.count must be a positive integer."
                )
            bucket["count"] = count

        conditions = quota.get("when")
        if not isinstance(conditions, list) or not conditions:
            raise ValidationError(f"{quota_path}.when must be a non-empty list.")
        bucket["when"] = [
            _normalise_quota_condition(
                condition, f"{quota_path}.when[{condition_index}]", source
            )
            for condition_index, condition in enumerate(conditions)
        ]
        normalised.append(bucket)

    if total_percentage > 100:
        raise ValidationError(f"{path} percentages cannot exceed 100.")
    return normalised


def _normalise_quota_condition(condition, path, source):
    if not isinstance(condition, dict):
        raise ValidationError(f"{path} must be an object.")
    unexpected = set(condition) - {
        "subject", "field", "derived", "filter", "type", "value",
    }
    if unexpected:
        raise ValidationError(
            f"Unsupported {path} keys: " + ", ".join(sorted(unexpected))
        )

    subject = condition.get("subject")
    expected_subject = SOURCE_SUBJECTS[source]
    if subject is not None and subject != expected_subject:
        raise ValidationError(
            f"{path}.subject must be {expected_subject} when the "
            f"selection_strategy.source is {source}."
        )
    if ("field" in condition) == ("derived" in condition):
        raise ValidationError(f"{path} must set exactly one of field or derived.")

    if "derived" in condition:
        derived = DERIVED_QUOTA_VALUES.get(condition["derived"])
        if derived is None:
            raise ValidationError(
                f"{path}.derived {condition['derived']} is not supported; "
                "supported values: " + ", ".join(sorted(DERIVED_QUOTA_VALUES)) + "."
            )
        normalise, _ = derived
        return normalise(condition, path)

    filter_name = condition.get("filter")
    value = condition.get("value")
    field = condition["field"]
    if (
        not isinstance(field, str)
        or not field.strip()
        or "__" in field
        or "=" in field
    ):
        raise ValidationError(f"{path}.field must be a json_ext key.")
    value_type = condition.get("type")
    if value_type not in VALUE_TYPES:
        raise ValidationError(
            f"{path}.type must be one of "
            + ", ".join(sorted(VALUE_TYPES)) + "."
        )
    if filter_name not in FILTERS_BY_TYPE[value_type]:
        raise ValidationError(
            f"{path}.filter {filter_name} is not allowed for {value_type}."
        )
    if not VALUE_TYPES[value_type](value):
        raise ValidationError(f"{path}.value must be of type {value_type}.")
    return {
        "field": field,
        "filter": filter_name,
        "type": value_type,
        "value": value,
    }


def _normalise_age_condition(condition, path):
    filter_name = condition.get("filter")
    value = condition.get("value")
    if condition.get("type", "integer") != "integer":
        raise ValidationError(f"{path}.type must be integer for age.")
    if filter_name not in FILTERS_BY_TYPE["integer"]:
        raise ValidationError(f"{path}.filter {filter_name} is not allowed for age.")
    if type(value) is not int or not 0 <= value <= 120:
        raise ValidationError(f"{path}.value must be an age from 0 to 120.")
    return {"derived": "age", "filter": filter_name, "value": value}


def validate_ranking_spec(ranking, model, beneficiary_schema=None):
    if not isinstance(ranking, dict):
        raise ValidationError("enrolment_ranking status entries must be objects.")
    unexpected = set(ranking) - {
        "order_by",
        "tie_breaker",
        "limit",
        "score_source",
        "weighted_score",
        "selection_strategy",
    }
    if unexpected:
        raise ValidationError(
            "Unsupported enrolment_ranking keys: " + ", ".join(sorted(unexpected))
        )
    weighted_score = ranking.get("weighted_score")
    score_source = ranking.get("score_source")
    if score_source is not None and weighted_score is None:
        raise ValidationError(
            "enrolment_ranking.score_source requires weighted_score."
        )
    _validate_score_source(score_source, model)
    if weighted_score is not None:
        _validate_weighted_score(weighted_score)
    if ranking.get("selection_strategy") is not None:
        _normalise_selection_strategy(ranking["selection_strategy"], model)

    order_by = ranking.get("order_by", [])
    if not isinstance(order_by, list):
        raise ValidationError("enrolment_ranking.order_by must be a list.")
    properties = (
        beneficiary_schema.get("properties", {})
        if isinstance(beneficiary_schema, dict)
        else {}
    )
    schema_fields = set(properties) if isinstance(properties, dict) else set()
    extra_fields = {WEIGHTED_SCORE_FIELD} if weighted_score is not None else set()
    normalised = [
        _normalise_order_item(
            item,
            model,
            index,
            extra_fields,
            schema_fields,
        )
        for index, item in enumerate(order_by)
    ]
    if weighted_score is not None and not any(
        item["field"] == WEIGHTED_SCORE_FIELD and not item.get("subject")
        for item in normalised
    ):
        raise ValidationError(
            "enrolment_ranking.order_by must include _weighted_score when "
            "weighted_score is configured."
        )

    tie_breaker = ranking.get("tie_breaker", "id")
    _validate_model_path(model, tie_breaker, schema_fields=schema_fields)
    normalised = [
        item for item in normalised
        if item.get("subject") or item["field"] != tie_breaker
    ]
    normalised.append({"field": tie_breaker, "direction": "asc"})

    limit = ranking.get("limit", {})
    if limit is None:
        limit = {}
    if not isinstance(limit, dict):
        raise ValidationError("enrolment_ranking.limit must be an object.")
    unexpected_limit = set(limit) - {"percentage", "respect_max_beneficiaries"}
    if unexpected_limit:
        raise ValidationError(
            "Unsupported enrolment_ranking.limit keys: " + ", ".join(sorted(unexpected_limit))
        )
    percentage = limit.get("percentage")
    if percentage is not None and (
        isinstance(percentage, bool)
        or not isinstance(percentage, (int, float))
        or not 1 <= percentage <= 100
    ):
        raise ValidationError("enrolment_ranking.limit.percentage must be between 1 and 100.")
    respect_max = limit.get("respect_max_beneficiaries", True)
    if not isinstance(respect_max, bool):
        raise ValidationError("enrolment_ranking.limit.respect_max_beneficiaries must be boolean.")
    if weighted_score is not None and not score_source and schema_fields:
        for index, component in enumerate(weighted_score["components"]):
            if component["field"] not in schema_fields:
                raise ValidationError(
                    "enrolment_ranking.weighted_score.components"
                    f"[{index}].field {component['field']} is not defined in "
                    "beneficiary_data_schema."
                )
    return normalised, percentage, respect_max


def benefit_plan_ranking_validation_errors(data, existing=None):
    """Shared Social Protection validator-registry callback."""
    from individual.models import Group, Individual
    from social_protection.models import BeneficiaryStatus, BenefitPlan

    if existing is not None and not hasattr(existing, "json_ext"):
        existing = BenefitPlan.objects.filter(id=existing).first()

    json_ext = data.get("json_ext")
    if json_ext is None and existing is not None:
        json_ext = existing.json_ext
    if isinstance(json_ext, str):
        try:
            json_ext = json.loads(json_ext)
        except (TypeError, ValueError):
            return []  # Social Protection owns base json_ext validation.
    if not isinstance(json_ext, dict) or "enrolment_ranking" not in json_ext:
        return []
    rankings = json_ext["enrolment_ranking"]
    if not isinstance(rankings, dict):
        return [{"message": "enrolment_ranking must be an object."}]

    plan_type = data.get("type") or getattr(existing, "type", "INDIVIDUAL")
    model = Group if plan_type == "GROUP" else Individual
    schema = data.get("beneficiary_data_schema")
    if schema is None and existing is not None:
        schema = existing.beneficiary_data_schema
    errors = []
    supported_statuses = set(BeneficiaryStatus.values) | {"*"}
    for status, ranking in rankings.items():
        if not isinstance(status, str) or not status:
            errors.append({"message": "enrolment_ranking status keys must be strings."})
            continue
        if status not in supported_statuses:
            errors.append({
                "message": f"enrolment_ranking has unsupported status {status}."
            })
            continue
        try:
            validate_ranking_spec(ranking, model, schema)
        except ValidationError as exc:
            errors.extend(
                {"message": f"enrolment_ranking.{status}: {message}"}
                for message in exc.messages
            )
    return errors


def _maximum_points(scoring):
    if scoring.get("maximum_points") is not None:
        return scoring["maximum_points"]
    if scoring["type"] == "mapping":
        points = list(scoring["values"].values())
    else:
        points = [band["points"] for band in scoring["bands"]]
    return max([scoring.get("default", 0), *points])


def _score_source_expression(field, score_source):
    if not score_source:
        return KeyTextTransform(field, "json_ext")

    primary_worker = authoritative_primary_worker_memberships().filter(
        group_id=OuterRef("pk")
    ).order_by("id")
    value = KeyTextTransform(field, "individual__json_ext")
    return Subquery(
        primary_worker.annotate(_score_value=value).values("_score_value")[:1],
        output_field=CharField(),
    )


def build_weighted_score(queryset, weighted_score, score_source=None):
    """Annotate a normalized score in SQL without loading the pool into Python."""
    source_annotations = {}
    for index, component in enumerate(weighted_score["components"]):
        expression = _score_source_expression(component["field"], score_source)
        if component["scoring"]["type"] == "numeric_bands":
            expression = Cast(expression, output_field=FloatField())
        source_annotations[f"_enrolment_score_source_{index}"] = expression
    scored = queryset.annotate(**source_annotations)

    component_annotations = {}
    for index, component in enumerate(weighted_score["components"]):
        scoring = component["scoring"]
        source_alias = f"_enrolment_score_source_{index}"
        if scoring["type"] == "mapping":
            cases = [
                When(**{source_alias: value}, then=Value(float(points)))
                for value, points in scoring["values"].items()
            ]
        else:
            cases = []
            for band in scoring["bands"]:
                conditions = {}
                if "min" in band:
                    conditions[f"{source_alias}__gte"] = band["min"]
                if "max" in band:
                    conditions[f"{source_alias}__lte"] = band["max"]
                cases.append(
                    When(**conditions, then=Value(float(band["points"])))
                )
        component_annotations[f"_enrolment_score_component_{index}"] = Case(
            *cases,
            default=Value(float(scoring.get("default", 0))),
            output_field=FloatField(),
        )
    scored = scored.annotate(**component_annotations)

    weighted_total = Value(0.0, output_field=FloatField())
    total_weight = 0.0
    for index, component in enumerate(weighted_score["components"]):
        weight = float(component["weight"])
        total_weight += weight
        weighted_total += (
            F(f"_enrolment_score_component_{index}")
            * Value(weight / float(_maximum_points(component["scoring"])))
        )
    final_score = weighted_total * Value(
        float(weighted_score.get("normalise_to", 100)) / total_weight
    )
    return scored.annotate(
        **{
            WEIGHTED_SCORE_FIELD: ExpressionWrapper(
                final_score,
                output_field=FloatField(),
            )
        }
    )


def _years_ago_boundary(today, years):
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, day=28)


def _age_range_dob(today, minimum_age, maximum_age):
    latest = _years_ago_boundary(today, minimum_age)
    earliest = _years_ago_boundary(today, maximum_age + 1) + timedelta(days=1)
    return earliest, latest


def _household_member(subject):
    """Return the outer household's membership for a member ``subject``."""
    return MEMBER_SUBJECTS[subject]().filter(
        group_id=OuterRef("pk")
    ).order_by("id")


def _annotate_selection_source(queryset, source):
    """Annotate the person whose attributes place a candidate in a quota."""
    if source == SELF_SOURCE:
        return queryset.annotate(
            _selection_individual=F("id"),
            _selection_dob=F("dob"),
        )

    member = _household_member(SOURCE_SUBJECTS[source])
    return queryset.annotate(
        _selection_individual=Subquery(member.values("individual_id")[:1]),
        _selection_dob=Subquery(
            member.values("individual__dob")[:1],
            output_field=DateField(),
        ),
    )


def _age_condition_q(dob_field, filter_name, age):
    """Translate an age comparison into a date-of-birth range on ``dob_field``."""
    today = date.today()
    if filter_name == "gt":
        filter_name, age = "gte", age + 1
    elif filter_name == "lt":
        filter_name, age = "lte", age - 1
    if filter_name == "gte":
        return Q(**{f"{dob_field}__lte": _years_ago_boundary(today, age)})
    if filter_name == "lte":
        return Q(**{f"{dob_field}__gte": _age_range_dob(today, 0, age)[0]})
    return Q(**{f"{dob_field}__range": _age_range_dob(today, age, age)})


def _age_quota_q(condition):
    return _age_condition_q(
        "_selection_dob", condition["filter"], condition["value"]
    )


# Derived values a quota condition may use: name -> (validate and normalise
# the condition, build its filter on the annotated selection source).
DERIVED_QUOTA_VALUES = {
    "age": (_normalise_age_condition, _age_quota_q),
}


def _quota_q(conditions):
    """Combine a quota's conditions on the selection source into one filter."""
    from individual.models import Individual

    query = Q()
    field_conditions = []
    for condition in conditions:
        if "derived" in condition:
            _, condition_q = DERIVED_QUOTA_VALUES[condition["derived"]]
            query &= condition_q(condition)
        else:
            field_conditions.append(criterion_to_condition(condition))
    if field_conditions:
        matching = CustomFilterWizardStorage.build_custom_filters_queryset(
            "individual",
            "Individual",
            field_conditions,
            Individual.objects.filter(is_deleted=False),
        )
        query &= Q(_selection_individual__in=matching.values("id"))
    return query


def _select_quota_ids(ranked, selection_strategy, cap):
    """Fill quota buckets in order, then the remaining places from the ranked list.

    ``ranked`` already carries the configured weighted/deterministic ordering.
    Each bucket and the fallback retain that ordering, and exclusions ensure one
    beneficiary cannot occupy more than one place.
    """
    strategy = _normalise_selection_strategy(
        selection_strategy,
        ranked.model,
    )
    candidates = _annotate_selection_source(ranked, strategy["source"])
    selected_ids = []

    for quota in strategy["quotas"]:
        target = quota.get("count")
        if target is None:
            target = math.floor(cap * quota["percentage"] / 100)
        target = min(target, cap - len(selected_ids))
        if target <= 0:
            continue
        selected_ids.extend(
            candidates.exclude(id__in=selected_ids).filter(
                _quota_q(quota["when"])
            ).values_list("id", flat=True)[:target]
        )

    # Places the buckets leave unfilled always go to the next ranked candidates,
    # so the Phase reaches its target whenever enough candidates are eligible.
    remaining = cap - len(selected_ids)
    if remaining:
        selected_ids.extend(
            candidates.exclude(id__in=selected_ids).values_list(
                "id", flat=True
            )[:remaining]
        )
    return selected_ids


def calculate_cap(
    pool_size,
    percentage=None,
    max_beneficiaries=None,
    current_enrolment_count=0,
    respect_max_beneficiaries=True,
):
    limits = []
    if percentage is not None:
        limits.append(math.ceil(pool_size * percentage / 100))
    if respect_max_beneficiaries and max_beneficiaries is not None:
        limits.append(max(max_beneficiaries - current_enrolment_count, 0))
    return min(limits) if limits else pool_size, bool(limits)


def build_order_by(queryset, order_items):
    """Annotate ordering values so PostgreSQL DISTINCT queries remain valid."""
    annotations = {}
    ordering = []
    for index, item in enumerate(order_items):
        alias = f"_enrolment_rank_{index}"
        field_name = item["field"]
        expression = F(field_name)
        if item.get("subject"):
            member = _household_member(item["subject"])
            value = KeyTextTransform(field_name, "individual__json_ext")
            expression = Subquery(
                member.annotate(_order_value=value).values("_order_value")[:1],
                output_field=CharField(),
            )
        elif field_name.startswith("json_ext__"):
            expression = KeyTextTransform.from_lookup(field_name)
        elif field_name != WEIGHTED_SCORE_FIELD:
            try:
                queryset.model._meta.get_field(field_name)
            except FieldDoesNotExist:
                # A beneficiary-schema property is stored in json_ext but may be
                # configured without the explicit json_ext__ prefix.
                expression = KeyTextTransform(field_name, "json_ext")
        if item.get("cast"):
            expression = Cast(expression, output_field=SUPPORTED_CASTS[item["cast"]]())
        annotations[alias] = expression
        order_expression = F(alias)
        options = {}
        if item.get("nulls") == "first":
            options["nulls_first"] = True
        elif item.get("nulls") == "last":
            options["nulls_last"] = True
        ordering.append(
            order_expression.desc(**options)
            if item["direction"] == "desc"
            else order_expression.asc(**options)
        )
    return queryset.annotate(**annotations).order_by(*ordering)


def _order_item_label(item):
    if item.get("subject"):
        return f"{item['subject']}.{item['field']}"
    return item["field"]


def _cast_error(order_items, exc, weighted_score=None):
    casts = ", ".join(
        f"{_order_item_label(item)} ({item['cast']})"
        for item in order_items
        if item.get("cast")
    )
    if weighted_score:
        weighted_fields = ", ".join(
            component["field"]
            for component in weighted_score.get("components", [])
            if component.get("scoring", {}).get("type") == "numeric_bands"
        )
        casts = ", ".join(filter(None, [casts, weighted_fields]))
    raise ValidationError(
        "Enrollment ranking could not cast stored values for "
        f"{casts}. Check that the data matches beneficiary_data_schema."
    ) from exc


def rank_and_cap_queryset(
    queryset,
    benefit_plan,
    status,
    current_enrolment_count,
    materialize_selected_ids=True,
    apply_percentage_limit=True,
):
    """Apply deterministic ordering and the configured intake ceiling.

    The returned metadata is shared by preview and execution. ``pool_size`` is the
    unassigned eligible pool, while ``will_enrol`` is the sliced queryset size.
    Approval revalidation can disable the percentage limit after it was applied
    during confirmation without disabling the remaining capacity limit.
    """
    ranking = load_ranking_spec(benefit_plan, status)
    pool_size = queryset.count()
    if ranking is None:
        return queryset, {
            "pool_size": pool_size,
            "cap_applied": None,
            "will_enrol": pool_size,
            "ranking": None,
            "percentage": None,
            "selected_ids": None,
        }

    order_items, percentage, respect_max = validate_ranking_spec(
        ranking,
        queryset.model,
        benefit_plan.beneficiary_data_schema,
    )
    weighted_score = ranking.get("weighted_score")
    score_source = ranking.get("score_source")

    def apply_ranking(source_queryset):
        if weighted_score is not None:
            source_queryset = build_weighted_score(
                source_queryset,
                weighted_score,
                score_source,
            )
        return build_order_by(source_queryset, order_items)

    ranked = apply_ranking(queryset)
    cap, has_limit = calculate_cap(
        pool_size,
        percentage if apply_percentage_limit else None,
        benefit_plan.max_beneficiaries,
        current_enrolment_count,
        respect_max,
    )
    will_enrol = min(pool_size, cap)
    selection_strategy = ranking.get("selection_strategy")
    if selection_strategy is not None:
        try:
            selected_ids = _select_quota_ids(
                ranked,
                selection_strategy,
                will_enrol,
            )
        except DataError as exc:
            _cast_error(order_items, exc, weighted_score)
        capped = apply_ranking(queryset.filter(id__in=selected_ids))
        return capped, {
            "pool_size": pool_size,
            "cap_applied": cap if has_limit else None,
            "will_enrol": len(selected_ids),
            "ranking": ranking,
            "percentage": percentage,
            "selected_ids": (
                selected_ids if materialize_selected_ids else None
            ),
        }
    if materialize_selected_ids:
        try:
            ranked_ids = list(ranked.values_list("id", flat=True)[:will_enrol])
        except DataError as exc:
            _cast_error(order_items, exc, weighted_score)
        capped = apply_ranking(queryset.filter(id__in=ranked_ids))
    else:
        # Keep the LIMIT inside a subquery so the outer queryset remains open to
        # row-security, GraphQL filters, and client-requested ordering.  This avoids
        # materialising the complete cohort on every preview page without handing a
        # sliced queryset to the connection field.
        has_numeric_cast = any(item.get("cast") for item in order_items) or any(
            component.get("scoring", {}).get("type") == "numeric_bands"
            for component in (weighted_score or {}).get("components", [])
        )
        if has_numeric_cast and will_enrol:
            try:
                list(ranked.values_list("id", flat=True)[:1])
            except DataError as exc:
                _cast_error(order_items, exc, weighted_score)
        ranked_ids = None
        capped_ids = ranked.values("id")[:will_enrol]
        capped = apply_ranking(queryset.filter(id__in=capped_ids))
    return capped, {
        "pool_size": pool_size,
        "cap_applied": cap if has_limit else None,
        "will_enrol": will_enrol,
        "ranking": ranking,
        "percentage": percentage,
        "selected_ids": ranked_ids,
    }


# British-spelling compatibility for callers introduced during development.
load_enrolment_ranking = load_ranking_spec
validate_and_normalise_ranking = validate_ranking_spec
