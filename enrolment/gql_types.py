import graphene


class IndividualSummaryEnrolmentGQLType(graphene.ObjectType):
    number_of_selected_individuals = graphene.String()
    total_number_of_individuals = graphene.String()
    number_of_individuals_not_assigned_to_programme = graphene.String()
    number_of_individuals_assigned_to_programme = graphene.String()
    number_of_individuals_assigned_to_selected_programme = graphene.String()
    number_of_individuals_to_upload = graphene.String()
    pool_size = graphene.Int()
    below_minimum_score = graphene.Int()
    programme_profile = graphene.String()
    quota_fill = graphene.JSONString()
    cap_applied = graphene.Int()
    will_enrol = graphene.Int()
    enrolment_ranking = graphene.JSONString()
    percentage = graphene.Float()


class GroupSummaryEnrolmentGQLType(graphene.ObjectType):
    verified_unenrolled_households = graphene.Int()
    phase_target = graphene.Int()
    enrolled_households = graphene.Int()
    number_of_selected_groups = graphene.String()
    total_number_of_groups = graphene.String()
    number_of_groups_not_assigned_to_programme = graphene.String()
    number_of_groups_assigned_to_programme = graphene.String()
    number_of_groups_assigned_to_selected_programme = graphene.String()
    number_of_groups_to_upload = graphene.String()
    pool_size = graphene.Int()
    below_minimum_score = graphene.Int()
    programme_profile = graphene.String()
    quota_fill = graphene.JSONString()
    cap_applied = graphene.Int()
    will_enrol = graphene.Int()
    enrolment_ranking = graphene.JSONString()
    percentage = graphene.Float()


class ProgrammeProfileGQLType(graphene.ObjectType):
    key = graphene.String()
    description = graphene.String()
    criteria = graphene.JSONString()
    ranking = graphene.JSONString()
    ranking_status = graphene.String()
    unsuitable_reasons = graphene.List(graphene.String)
    profile_required = graphene.Boolean()


# American-spelling compatibility.
IndividualSummaryEnrollmentGQLType = IndividualSummaryEnrolmentGQLType
GroupSummaryEnrollmentGQLType = GroupSummaryEnrolmentGQLType
