import graphene
import graphene_django_optimizer as gql_optimizer

from django.contrib.auth.models import AnonymousUser

from individual.apps import IndividualConfig
from individual.gql_queries import GroupGQLType, IndividualGQLType
from core.schema import OrderedDjangoFilterConnectionField

from enrolment.gql_mutations import (
    ConfirmGroupEnrolmentMutation,
    ConfirmIndividualEnrolmentMutation,
)
from enrolment.gql_types import (
    GroupSummaryEnrolmentGQLType,
    IndividualSummaryEnrolmentGQLType,
)
from enrolment.services import (
    build_group_enrolment_selection,
    build_group_enrolment_summary,
    build_individual_enrolment_selection,
    build_individual_enrolment_summary,
)


def _check_permissions(user, permissions):
    if (
        type(user) is AnonymousUser
        or not getattr(user, "id", None)
        or not user.has_perms(permissions)
    ):
        raise PermissionError("Unauthorized")


class Query(graphene.ObjectType):
    enrolment_individual_candidate = OrderedDjangoFilterConnectionField(
        IndividualGQLType,
        customFilters=graphene.List(of_type=graphene.String),
        benefitPlanId=graphene.String(required=True),
        status=graphene.String(required=True),
    )
    enrolment_group_candidate = OrderedDjangoFilterConnectionField(
        GroupGQLType,
        customFilters=graphene.List(of_type=graphene.String),
        benefitPlanId=graphene.String(required=True),
        status=graphene.String(required=True),
    )
    individual_enrollment_summary = graphene.Field(
        IndividualSummaryEnrolmentGQLType,
        customFilters=graphene.List(of_type=graphene.String),
        benefitPlanId=graphene.String(required=True),
        status=graphene.String(required=True),
    )
    group_enrollment_summary = graphene.Field(
        GroupSummaryEnrolmentGQLType,
        customFilters=graphene.List(of_type=graphene.String),
        benefitPlanId=graphene.String(required=True),
        status=graphene.String(required=True),
    )

    def resolve_individual_enrollment_summary(self, info, **kwargs):
        _check_permissions(
            info.context.user,
            IndividualConfig.gql_individual_search_perms,
        )
        return IndividualSummaryEnrolmentGQLType(**build_individual_enrolment_summary(
            kwargs.get("customFilters"),
            kwargs.get("benefitPlanId"),
            kwargs.get("status"),
        ))

    def resolve_enrolment_individual_candidate(self, info, **kwargs):
        _check_permissions(
            info.context.user,
            IndividualConfig.gql_individual_search_perms,
        )
        selection = build_individual_enrolment_selection(
            kwargs.get("customFilters"),
            kwargs.get("benefitPlanId"),
            kwargs.get("status"),
            info.context.user,
            materialize_selected_ids=False,
        )
        return gql_optimizer.query(
            selection["individuals_not_assigned_to_selected_programme"],
            info,
        )

    def resolve_group_enrollment_summary(self, info, **kwargs):
        _check_permissions(
            info.context.user,
            IndividualConfig.gql_group_search_perms,
        )
        return GroupSummaryEnrolmentGQLType(**build_group_enrolment_summary(
            kwargs.get("customFilters"),
            kwargs.get("benefitPlanId"),
            kwargs.get("status"),
        ))

    def resolve_enrolment_group_candidate(self, info, **kwargs):
        _check_permissions(
            info.context.user,
            IndividualConfig.gql_group_search_perms,
        )
        selection = build_group_enrolment_selection(
            kwargs.get("customFilters"),
            kwargs.get("benefitPlanId"),
            kwargs.get("status"),
            info.context.user,
            materialize_selected_ids=False,
        )
        return gql_optimizer.query(
            selection["groups_not_assigned_to_selected_programme"],
            info,
        )


class Mutation(graphene.ObjectType):
    confirm_individual_enrollment = ConfirmIndividualEnrolmentMutation.Field()
    confirm_group_enrollment = ConfirmGroupEnrolmentMutation.Field()
