import json

from django.apps import apps
from django.core.exceptions import ValidationError

from individual.apps import IndividualConfig


DEFAULT_BENEFICIARY_STATUS = "POTENTIAL"
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


def load_enrolment_phase(benefit_plan_id, status, expected_type=None):
    if not benefit_plan_id:
        raise ValidationError("A BenefitPlan is required for enrolment.")
    if "social_protection" not in apps.app_configs:
        raise ValidationError("The Social Protection module is required for enrolment.")

    from social_protection.models import BenefitPlan, BeneficiaryStatus

    benefit_plan = BenefitPlan.objects.filter(
        id=benefit_plan_id,
        is_deleted=False,
    ).first()
    if not benefit_plan:
        raise ValidationError("The selected BenefitPlan does not exist.")
    if expected_type and benefit_plan.type != expected_type:
        raise ValidationError(
            f"BenefitPlan type {benefit_plan.type} cannot be used for "
            f"{expected_type} enrolment."
        )
    if status not in BeneficiaryStatus.values:
        raise ValidationError(f"Unsupported beneficiary status: {status}.")
    return benefit_plan


def merge_enrolment_criteria(custom_filters, benefit_plan_id, status, expected_type=None):
    """Return immutable, Phase, and operator criteria as one AND-ed list."""
    operator_filters = list(custom_filters or [])
    benefit_plan = load_enrolment_phase(
        benefit_plan_id,
        status,
        expected_type,
    )
    validate_operator_filters(operator_filters, benefit_plan)

    from social_protection.apps import SocialProtectionConfig

    configured = SocialProtectionConfig.mandatory_enrollment_criteria or {}
    system_criteria = configured.get(expected_type or benefit_plan.type, {}) or {}

    json_ext = benefit_plan.json_ext or {}
    if isinstance(json_ext, str):
        try:
            json_ext = json.loads(json_ext)
        except (TypeError, ValueError):
            json_ext = {}

    criteria = json_ext.get("advanced_criteria", {}) or {}
    if isinstance(criteria, list):
        criteria = {DEFAULT_BENEFICIARY_STATUS: criteria}

    combined_filters = []
    seen = set()
    for entry in (
        list(system_criteria.get(status, []) or [])
        + list(criteria.get(status, []) or [])
    ):
        condition = criterion_to_condition(entry)
        if not condition:
            raise ValidationError("Malformed mandatory enrolment criterion.")
        if condition not in seen:
            combined_filters.append(condition)
            seen.add(condition)
    for condition in operator_filters:
        if condition not in seen:
            combined_filters.append(condition)
            seen.add(condition)
    return combined_filters


def validate_operator_filters(custom_filters, benefit_plan):
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
    properties = schema.get("properties", {})

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
