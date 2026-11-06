"""Programme profiles: eligibility rules shared by every Phase of a programme.

Profiles live in the ``programme_profiles`` module configuration, keyed by
``BenefitPlan.code`` (matched case-insensitively) with ``default`` as the
fallback. A Phase that matches no profile keeps its own criteria only.
"""
from django.core.exceptions import ValidationError
from django.db.models import Exists, OuterRef

from core.custom_filters import CustomFilterWizardStorage

from enrolment.apps import EnrolmentConfig
from enrolment.criteria import FILTERS_BY_TYPE, criterion_to_condition
from enrolment.membership import (
    active_household_memberships,
    authoritative_primary_worker_memberships,
    household_head_memberships,
)


DEFAULT_PROFILE = "default"
PROFILE_KEYS = {"description", "eligibility"}
ELIGIBILITY_KEYS = {"criteria"}
CRITERION_KEYS = {"subject", "field", "filter", "type", "value"}
# Subjects that name the candidate record itself, by Phase type.
CANDIDATE_SUBJECTS = {"GROUP": "household", "INDIVIDUAL": "self"}
# Household members a GROUP criterion may read, by subject.
MEMBER_SUBJECTS = {
    "primary_worker": authoritative_primary_worker_memberships,
    "head": household_head_memberships,
    "any_member": active_household_memberships,
}
SUBJECTS = set(CANDIDATE_SUBJECTS.values()) | set(MEMBER_SUBJECTS)
# Value types the individual module's custom filter wizards can cast.
VALUE_TYPES = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: type(value) is int,
    "numeric": lambda value: (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    ),
    "boolean": lambda value: isinstance(value, bool),
}


def validate_programme_profiles(profiles):
    """Return the configuration errors in a ``programme_profiles`` block."""
    if not isinstance(profiles, dict):
        return ["programme_profiles must be an object keyed by BenefitPlan code."]
    errors = []
    codes = set()
    for code, profile in profiles.items():
        path = f"programme_profiles.{code}"
        if not isinstance(code, str) or not code.strip():
            errors.append("programme_profiles keys must be non-empty strings.")
            continue
        if code.upper() in codes:
            errors.append(f"{path} repeats a code that differs only in case.")
        codes.add(code.upper())
        if not isinstance(profile, dict):
            errors.append(f"{path} must be an object.")
            continue
        unexpected = set(profile) - PROFILE_KEYS
        if unexpected:
            errors.append(
                f"Unsupported {path} keys: " + ", ".join(sorted(unexpected))
            )
        description = profile.get("description")
        if description is not None and not isinstance(description, str):
            errors.append(f"{path}.description must be a string.")
        errors.extend(
            _eligibility_errors(profile.get("eligibility", {}), f"{path}.eligibility")
        )
    return errors


def _eligibility_errors(eligibility, path):
    if not isinstance(eligibility, dict):
        return [f"{path} must be an object."]
    errors = []
    unexpected = set(eligibility) - ELIGIBILITY_KEYS
    if unexpected:
        errors.append(f"Unsupported {path} keys: " + ", ".join(sorted(unexpected)))
    criteria = eligibility.get("criteria", [])
    if not isinstance(criteria, list):
        return errors + [f"{path}.criteria must be a list."]
    for index, criterion in enumerate(criteria):
        errors.extend(_criterion_errors(criterion, f"{path}.criteria[{index}]"))
    return errors


def _criterion_errors(criterion, path):
    if not isinstance(criterion, dict):
        return [f"{path} must be an object."]
    unexpected = set(criterion) - CRITERION_KEYS
    if unexpected:
        return [f"Unsupported {path} keys: " + ", ".join(sorted(unexpected))]
    subject = criterion.get("subject")
    if subject is not None and subject not in SUBJECTS:
        return [f"{path}.subject must be one of " + ", ".join(sorted(SUBJECTS)) + "."]
    field = criterion.get("field")
    if (
        not isinstance(field, str)
        or not field.strip()
        or "__" in field
        or "=" in field
    ):
        return [f"{path}.field must be a json_ext key."]
    value_type = criterion.get("type")
    if value_type not in VALUE_TYPES:
        return [f"{path}.type must be one of " + ", ".join(sorted(VALUE_TYPES)) + "."]
    filter_name = criterion.get("filter")
    if filter_name not in FILTERS_BY_TYPE[value_type]:
        return [f"{path}.filter {filter_name} is not allowed for {value_type}."]
    if not VALUE_TYPES[value_type](criterion.get("value")):
        return [f"{path}.value must be of type {value_type}."]
    return []


def resolve_programme_profile(benefit_plan):
    """Return ``(key, profile)`` for a Phase, or ``(None, None)`` if none applies."""
    profiles = EnrolmentConfig.programme_profiles or {}
    code = str(benefit_plan.code or "").upper()
    for key, profile in profiles.items():
        if key.upper() == code:
            return key, profile
    if DEFAULT_PROFILE in profiles:
        return DEFAULT_PROFILE, profiles[DEFAULT_PROFILE]
    return None, None


def apply_profile_eligibility(queryset, benefit_plan, phase_type):
    """Restrict candidates to the eligibility rules of the Phase's profile."""
    key, profile = resolve_programme_profile(benefit_plan)
    if profile is None:
        return queryset
    eligibility = profile.get("eligibility") or {}
    candidate_subject = CANDIDATE_SUBJECTS[phase_type]
    conditions_by_subject = {}
    for criterion in eligibility.get("criteria") or []:
        subject = criterion.get("subject", candidate_subject)
        if subject != candidate_subject and (
            phase_type != "GROUP" or subject not in MEMBER_SUBJECTS
        ):
            raise ValidationError(
                f"Programme profile {key} uses subject {subject}, which "
                f"{phase_type} Phases do not support."
            )
        conditions_by_subject.setdefault(subject, []).append(
            criterion_to_condition(criterion)
        )

    candidate_conditions = conditions_by_subject.pop(candidate_subject, [])
    if candidate_conditions:
        queryset = CustomFilterWizardStorage.build_custom_filters_queryset(
            "individual",
            "Group" if phase_type == "GROUP" else "Individual",
            candidate_conditions,
            queryset,
        )
    for subject, conditions in conditions_by_subject.items():
        queryset = queryset.filter(_member_matches(subject, conditions))
    return queryset


def _member_matches(subject, conditions):
    """Require one household member of ``subject`` to meet every condition."""
    from individual.models import Individual

    matching = CustomFilterWizardStorage.build_custom_filters_queryset(
        "individual",
        "Individual",
        conditions,
        Individual.objects.filter(is_deleted=False),
    )
    return Exists(
        MEMBER_SUBJECTS[subject]().filter(
            group_id=OuterRef("pk"),
            individual_id__in=matching.values("id"),
        )
    )
