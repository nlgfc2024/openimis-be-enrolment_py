"""Programme profiles: eligibility rules shared by every Phase of a programme.

Profiles live in the ``programme_profiles`` module configuration, keyed by
``BenefitPlan.code`` (matched case-insensitively) with ``default`` as the
fallback. A Phase that matches no profile keeps its own criteria only.

A profile does not name a Phase type. The types it suits follow from its
content (for example, a ``head`` criterion only suits GROUP Phases), and a
profile that suits neither is rejected when the configuration is saved.
"""
from django.core.exceptions import ValidationError
from django.db.models import Exists, OuterRef

from core.custom_filters import CustomFilterWizardStorage

from enrolment.apps import EnrolmentConfig
from enrolment.criteria import FILTERS_BY_TYPE, VALUE_TYPES, criterion_to_condition
from enrolment.membership import CANDIDATE_SUBJECTS, MEMBER_SUBJECTS
from enrolment.ranking import ranking_errors


DEFAULT_PROFILE = "default"
PROFILE_KEYS = {"description", "eligibility", "ranking"}
ELIGIBILITY_KEYS = {"criteria"}
CRITERION_KEYS = {"subject", "field", "filter", "type", "value"}
SUBJECTS = set(CANDIDATE_SUBJECTS.values()) | set(MEMBER_SUBJECTS)
PHASE_TYPES = tuple(CANDIDATE_SUBJECTS)


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
        profile_errors = []
        description = profile.get("description")
        if description is not None and not isinstance(description, str):
            profile_errors.append(f"{path}.description must be a string.")
        profile_errors.extend(
            _eligibility_errors(profile.get("eligibility", {}), f"{path}.eligibility")
        )
        if not unexpected and not profile_errors:
            profile_errors.extend(_unsuitable_profile_errors(profile, path))
        errors.extend(profile_errors)
    return errors


def _unsuitable_profile_errors(profile, path):
    """Report a profile that no Phase type can use, with each type's reasons."""
    reasons = {
        phase_type: phase_type_errors(profile, phase_type)
        for phase_type in PHASE_TYPES
    }
    if not all(reasons.values()):
        return []
    if reasons["GROUP"] == reasons["INDIVIDUAL"]:
        return [f"{path}: {_sentence(reasons['GROUP'])}"]
    return [f"{path} suits neither Phase type. " + " ".join(
        f"{phase_type} Phases: {_sentence(errors)}"
        for phase_type, errors in reasons.items()
    )]


def _sentence(errors):
    return "; ".join(error.rstrip(".") for error in errors) + "."


def phase_type_errors(profile, phase_type):
    """Return why Phases of ``phase_type`` cannot use ``profile``.

    Rankings are checked without a Phase's beneficiary_data_schema, which only
    enrolment has; it re-checks them against the Phase's schema.
    """
    from individual.models import Group, Individual

    subjects = {CANDIDATE_SUBJECTS[phase_type]}
    if phase_type == "GROUP":
        subjects |= set(MEMBER_SUBJECTS)
    errors = []
    criteria = (profile.get("eligibility") or {}).get("criteria") or []
    for index, criterion in enumerate(criteria):
        subject = criterion.get("subject")
        if subject is not None and subject not in subjects:
            errors.append(f"eligibility.criteria[{index}] uses subject {subject}")
    if "ranking" in profile:
        model = Group if phase_type == "GROUP" else Individual
        errors.extend(ranking_errors(profile["ranking"], model, path="ranking"))
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


def profile_ranking_spec(benefit_plan, status):
    """Return the Phase's profile ranking for ``status`` (or ``*``), if any."""
    _, profile = resolve_programme_profile(benefit_plan)
    rankings = (profile or {}).get("ranking") or {}
    return rankings.get(status, rankings.get("*"))


def apply_profile_eligibility(queryset, benefit_plan, phase_type):
    """Restrict candidates to the eligibility rules of the Phase's profile."""
    key, profile = resolve_programme_profile(benefit_plan)
    if profile is None:
        return queryset
    errors = phase_type_errors(profile, phase_type)
    if errors:
        raise ValidationError(
            f"Programme profile {key} cannot be used by {phase_type} Phases: "
            + _sentence(errors)
        )
    eligibility = profile.get("eligibility") or {}
    candidate_subject = CANDIDATE_SUBJECTS[phase_type]
    conditions_by_subject = {}
    for criterion in eligibility.get("criteria") or []:
        subject = criterion.get("subject", candidate_subject)
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
