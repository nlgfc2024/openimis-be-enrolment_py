import graphene

from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext as _

from core.gql.gql_mutations.base_mutation import (
    BaseHistoryModelCreateMutationMixin,
    BaseMutation,
)
from core.schema import OpenIMISMutation
from individual.apps import IndividualConfig
from individual.models import Group, Individual
from social_protection.apps import SocialProtectionConfig

from enrolment.services import EnrolmentService
from enrolment.apps import EnrolmentConfig


class ConfirmEnrolmentInputType(OpenIMISMutation.Input):
    custom_filters = graphene.List(required=False, of_type=graphene.String)
    benefit_plan_id = graphene.String(required=True, max_length=255)
    status = graphene.String(required=True, max_length=255)


class _ConfirmEnrolmentMixin:
    @classmethod
    def _clean_input(cls, data):
        data.pop("client_mutation_id", None)
        data.pop("client_mutation_label", None)
        return (
            data.pop("custom_filters", None),
            data.pop("benefit_plan_id", None),
            data.pop("status", EnrolmentConfig.default_beneficiary_status),
        )


class ConfirmIndividualEnrolmentMutation(
    _ConfirmEnrolmentMixin,
    BaseHistoryModelCreateMutationMixin,
    BaseMutation,
):
    _mutation_class = "ConfirmIndividualEnrollmentMutation"
    _mutation_module = "enrolment"
    _model = Individual

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(IndividualConfig.gql_individual_create_perms):
            raise PermissionDenied(_("unauthorized"))
        if not user.has_perms(
            SocialProtectionConfig.gql_beneficiary_create_perms
            + SocialProtectionConfig.gql_benefit_plan_update_perms
        ):
            raise PermissionDenied(_("unauthorized"))

    @classmethod
    def _mutate(cls, user, **data):
        custom_filters, benefit_plan_id, status = cls._clean_input(data)
        EnrolmentService(user).select_individuals_to_benefit_plan(
            custom_filters,
            benefit_plan_id,
            status,
            user,
        )
        return None

    class Input(ConfirmEnrolmentInputType):
        pass


class ConfirmGroupEnrolmentMutation(
    _ConfirmEnrolmentMixin,
    BaseHistoryModelCreateMutationMixin,
    BaseMutation,
):
    _mutation_class = "ConfirmGroupEnrollmentMutation"
    _mutation_module = "enrolment"
    _model = Group

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(IndividualConfig.gql_group_create_perms):
            raise PermissionDenied(_("unauthorized"))
        if not user.has_perms(
            SocialProtectionConfig.gql_beneficiary_create_perms
            + SocialProtectionConfig.gql_benefit_plan_update_perms
        ):
            raise PermissionDenied(_("unauthorized"))

    @classmethod
    def _mutate(cls, user, **data):
        custom_filters, benefit_plan_id, status = cls._clean_input(data)
        EnrolmentService(user).select_groups_to_benefit_plan(
            custom_filters,
            benefit_plan_id,
            status,
            user,
        )
        return None

    class Input(ConfirmEnrolmentInputType):
        pass


# Existing API class names remain importable during the transition.
ConfirmIndividualEnrollmentMutation = ConfirmIndividualEnrolmentMutation
ConfirmGroupEnrollmentMutation = ConfirmGroupEnrolmentMutation
