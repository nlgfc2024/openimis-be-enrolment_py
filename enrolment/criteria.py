import json
import math

from django.apps import apps
from django.core.exceptions import ValidationError

from individual.apps import IndividualConfig
from enrolment.apps import EnrolmentConfig
from enrolment.district import DISTRICT_FIELD, district_from_condition


FILTERS_BY_TYPE = {
    "string": {
        "exact", "iexact", "startswith", "istartswith", "contains", "icontains",
    },
    "integer": {"exact", "lt", "lte", "gt", "gte"},
    "decimal": {"exact", "lt", "lte", "gt", "gte"},
    "number": {"exact", "lt", "lte", "gt", "gte"},
    "numeric": {"exact", "lt", "lte", "gt", "gte"},
    "date": {"exact", "lt", "lte", "gt", "gte"},
    "boolean": {"exact"},
}


def is_number(value):
    """Return whether ``value`` is a finite int or float, excluding booleans."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def is_one_of(value, choices):
    """Return whether ``value`` is one of ``choices``.

    Configuration values come from JSON, where a list or object in place of a
    name would make a plain membership test raise TypeError.
    """
    try:
        return value in choices
    except TypeError:
        return False


# Value types a configured condition (a profile criterion or a quota condition)
# may use: those the individual module's custom filter wizards can cast. Each
# maps to a check on the configured value; FILTERS_BY_TYPE gives the filters.
VALUE_TYPES = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: type(value) is int,
    "numeric": is_number,
    "boolean": lambda value: isinstance(value, bool),
}


def load_enrolment_phase(benefit_plan_id, status, expected_type=None):
    if not benefit_plan_id:
        raise ValidationError("A BenefitPlan is required for enrolment.")
    if "social_protection" not in apps.app_configs:
        raise ValidationError("The Social Protection module is required for enrolment.")

    from social_protection.models import BenefitPlan

    benefit_plan = BenefitPlan.objects.filter(
        id=benefit_plan_id,
        is_deleted=False,
    ).first()
    if not benefit_plan:
        raise ValidationError("The selected BenefitPlan does not exist.")
    _validate_enrolment_phase(benefit_plan, benefit_plan_id, status, expected_type)
    return benefit_plan


def _validate_enrolment_phase(benefit_plan, benefit_plan_id, status, expected_type):
    from social_protection.models import BeneficiaryStatus

    if benefit_plan.is_deleted or str(benefit_plan.pk) != str(benefit_plan_id):
        raise ValidationError("The selected BenefitPlan does not exist.")
    if expected_type and benefit_plan.type != expected_type:
        raise ValidationError(
            f"BenefitPlan type {benefit_plan.type} cannot be used for "
            f"{expected_type} enrolment."
        )
    if status not in BeneficiaryStatus.values:
        raise ValidationError(f"Unsupported beneficiary status: {status}.")


def merge_enrolment_criteria(
    custom_filters, benefit_plan_id, status, expected_type=None, *, benefit_plan=None,
):
    """Return the Phase's criteria and the operator's filters as one AND-ed list.

    Programme profile criteria are applied separately by the candidate sources.
    """
    operator_filters = list(custom_filters or [])
    if benefit_plan is None:
        benefit_plan = load_enrolment_phase(benefit_plan_id, status, expected_type)
    else:
        # Confirmation supplies a locked Phase; reuse it without bypassing the
        # type/status checks previously performed by the second database load.
        _validate_enrolment_phase(benefit_plan, benefit_plan_id, status, expected_type)
    validate_operator_filters(operator_filters, benefit_plan)

    json_ext = benefit_plan.json_ext or {}
    if isinstance(json_ext, str):
        try:
            json_ext = json.loads(json_ext)
        except (TypeError, ValueError):
            json_ext = {}

    criteria = json_ext.get("advanced_criteria", {}) or {}
    if isinstance(criteria, list):
        criteria = {EnrolmentConfig.default_beneficiary_status: criteria}

    combined_filters = []
    seen = set()
    for entry in criteria.get(status, []) or []:
        condition = criterion_to_condition(entry)
        if not condition:
            raise ValidationError("Malformed enrolment criterion.")
        if condition not in seen:
            combined_filters.append(condition)
            seen.add(condition)
    for condition in operator_filters:
        if condition not in seen:
            combined_filters.append(condition)
            seen.add(condition)
    return combined_filters


def _operator_filter_properties(benefit_plan):
    schema = benefit_plan.beneficiary_data_schema or {}
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except (TypeError, ValueError):
            schema = {}
    if not isinstance(schema, dict) or not schema.get("properties"):
        fallback_schema = IndividualConfig.individual_schema or {}
        if isinstance(fallback_schema, dict):
            schema = fallback_schema
        else:
            try:
                schema = json.loads(fallback_schema)
            except (TypeError, ValueError):
                schema = {}
    return schema.get("properties", {}) if isinstance(schema, dict) else {}


def validate_operator_filters(custom_filters, benefit_plan):
    properties = None
    for condition in custom_filters:
        if not isinstance(condition, str) or "=" not in condition:
            raise ValidationError("Malformed enrolment operator filter.")
        expression = condition.split("=", 1)[0]
        parts = expression.rsplit("__", 2)
        if len(parts) == 3:
            field, filter_name, value_type = parts
        elif len(parts) == 2:
            field, value_type = parts
            filter_name = "exact"
        else:
            raise ValidationError("Malformed enrolment operator filter.")

        if field == DISTRICT_FIELD:
            if benefit_plan.type != "GROUP":
                raise ValidationError("District enrollment filtering is only supported for households.")
            if EnrolmentConfig.enable_household_district_filter is not True:
                raise ValidationError("Household district filtering is disabled.")
            district_from_condition(condition)
            continue

        if properties is None:
            properties = _operator_filter_properties(benefit_plan)
        definition = properties.get(field)
        if not isinstance(definition, dict):
            raise ValidationError(
                f"Enrolment operator filter field {field} is not allowed."
            )
        schema_type = definition.get("type")
        compatible_types = {schema_type}
        if schema_type == "number":
            compatible_types.add("numeric")
        if value_type not in compatible_types:
            raise ValidationError(
                f"Enrolment operator filter type {value_type} does not match {field}."
            )
        if filter_name not in FILTERS_BY_TYPE.get(value_type, set()):
            raise ValidationError(
                f"Enrolment operator filter {filter_name} is not allowed for {field}."
            )


def criterion_to_condition(entry):
    if isinstance(entry, str):
        return entry
    if not isinstance(entry, dict):
        return None
    if entry.get("custom_filter_condition"):
        return entry["custom_filter_condition"]
    field, filter_name, value_type = (
        entry.get("field"),
        entry.get("filter"),
        entry.get("type"),
    )
    if field is None or filter_name is None or value_type is None:
        return None
    value = entry.get("value")
    if value_type == "string":
        value = json.dumps(value)
    return f"{field}__{filter_name}__{value_type}={value}"


# Compatibility aliases for callers using the earlier names.
_load_enrollment_benefit_plan = load_enrolment_phase
merge_mandatory_enrolment_criteria = merge_enrolment_criteria
_validate_operator_filters = validate_operator_filters
_criterion_to_condition = criterion_to_condition
