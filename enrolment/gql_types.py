import graphene


class IndividualSummaryEnrolmentGQLType(graphene.ObjectType):
    number_of_selected_individuals = graphene.String()
    total_number_of_individuals = graphene.String()
    number_of_individuals_not_assigned_to_programme = graphene.String()
    number_of_individuals_assigned_to_programme = graphene.String()
    number_of_individuals_assigned_to_selected_programme = graphene.String()
    number_of_individuals_to_upload = graphene.String()
    pool_size = graphene.Int()
    cap_applied = graphene.Int()
    will_enrol = graphene.Int()
    enrolment_ranking = graphene.JSONString()
    percentage = graphene.Float()


class GroupSummaryEnrolmentGQLType(graphene.ObjectType):
    number_of_selected_groups = graphene.String()
    total_number_of_groups = graphene.String()
    number_of_groups_not_assigned_to_programme = graphene.String()
    number_of_groups_assigned_to_programme = graphene.String()
    number_of_groups_assigned_to_selected_programme = graphene.String()
    number_of_groups_to_upload = graphene.String()
    pool_size = graphene.Int()
    cap_applied = graphene.Int()
    will_enrol = graphene.Int()
    enrolment_ranking = graphene.JSONString()
    percentage = graphene.Float()


# American-spelling compatibility.
IndividualSummaryEnrollmentGQLType = IndividualSummaryEnrolmentGQLType
GroupSummaryEnrollmentGQLType = GroupSummaryEnrolmentGQLType
