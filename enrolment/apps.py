import copy
import logging

from django.apps import AppConfig
from django.core.exceptions import ValidationError

from core.module_config_registry import register_reloader, register_validator


logger = logging.getLogger(__name__)


DEFAULT_CONFIG = {
    "mandatory_enrollment_criteria": {"INDIVIDUAL": {}, "GROUP": {}},
    "enable_maker_checker_logic_enrollment": True,
    "enable_household_district_filter": False,
    "validation_enrollment": "validation-enrollment",
    "validation_group_enrollment": "validation-group-enrollment",
    "default_beneficiary_status": "POTENTIAL",
    "registry_gender_field": "gender",
    "registry_primary_worker_field": "primary_worker",
    "registry_household_verification_field": "validation_status",
    "registry_verified_household_value": "VERIFIED",
    "bulk_enrollment_batch_size": 1000,
}


class EnrolmentConfig(AppConfig):
    name = "enrolment"
    verbose_name = "Enrolment"

    mandatory_enrollment_criteria = None
    enable_maker_checker_logic_enrollment = None
    enable_household_district_filter = False
    validation_enrollment = None
    validation_group_enrollment = None
    default_beneficiary_status = None
    registry_gender_field = None
    registry_primary_worker_field = None
    registry_household_verification_field = None
    registry_verified_household_value = None
    bulk_enrollment_batch_size = None

    def ready(self):
        from core.models import ModuleConfiguration

        config = ModuleConfiguration.get_or_default(self.name, DEFAULT_CONFIG)
        self._validate_config(config)
        self._load_config(config)
        register_validator(self.name, self._validate_module_config)
        register_reloader(self.name, self._reload_module_config)
        self._register_benefit_plan_validator()

    @classmethod
    def _load_config(cls, config):
        for field, value in {**DEFAULT_CONFIG, **config}.items():
            if hasattr(cls, field):
                setattr(cls, field, value)

    @classmethod
    def _validate_config(cls, config):
        from enrolment.criteria import validate_mandatory_enrolment_criteria

        validate_mandatory_enrolment_criteria(
            config.get("mandatory_enrollment_criteria", {})
        )
        errors = []
        for field in (
            "validation_enrollment",
            "validation_group_enrollment",
            "default_beneficiary_status",
            "registry_gender_field",
            "registry_primary_worker_field",
            "registry_household_verification_field",
            "registry_verified_household_value",
        ):
            if not isinstance(config.get(field), str) or not config[field].strip():
                errors.append(f"{field} must be a non-empty string.")
        if not isinstance(config.get("enable_maker_checker_logic_enrollment"), bool):
            errors.append("enable_maker_checker_logic_enrollment must be boolean.")
        if not isinstance(config.get("enable_household_district_filter", False), bool):
            errors.append("enable_household_district_filter must be boolean.")
        batch_size = config.get("bulk_enrollment_batch_size")
        if type(batch_size) is not int or batch_size < 1:
            errors.append("bulk_enrollment_batch_size must be a positive integer.")
        if errors:
            raise ValidationError({"config": errors})

    def _merge_with_defaults(self, instance):
        return {**copy.deepcopy(DEFAULT_CONFIG), **instance._cfg}

    def _validate_module_config(self, instance):
        self._validate_config(self._merge_with_defaults(instance))

    def _reload_module_config(self, instance):
        config = self._merge_with_defaults(instance)
        self._validate_config(config)
        self._load_config(config)

    @staticmethod
    def _register_benefit_plan_validator():
        """Register without making Social Protection depend on Enrolment.

        The shared registry is supplied by the companion Social Protection PR.
        Keeping this import optional lets either module start independently while
        coordinated deployments use the single Enrolment-owned validator.
        """
        try:
            from social_protection.validation import register_benefit_plan_validator
        except ImportError:
            logger.info(
                "Social Protection does not expose the BenefitPlan validator "
                "registry; enrolment ranking validation was not registered."
            )
            return

        from enrolment.ranking import benefit_plan_ranking_validation_errors

        register_benefit_plan_validator(
            "enrolment.ranking",
            benefit_plan_ranking_validation_errors,
        )
