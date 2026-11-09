import copy

from social_protection.tests.test_helpers import create_benefit_plan

from enrolment.apps import EnrolmentConfig


class ProfileRankingMixin:
    """Rank test Phases through their programme profile.

    Phases no longer hold rankings. ``_ranked_plan`` takes the ranking given in
    ``json_ext.enrolment_ranking``, creates the Phase without it and puts it in
    a profile keyed by the Phase's code. Profiles are restored after each test.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(
            setattr, EnrolmentConfig, "programme_profiles",
            copy.deepcopy(EnrolmentConfig.programme_profiles),
        )
        EnrolmentConfig.programme_profiles = {}

    def _ranked_plan(self, username, payload_override):
        payload = copy.deepcopy(payload_override)
        ranking = payload.get("json_ext", {}).pop("enrolment_ranking", None)
        plan = create_benefit_plan(username, payload_override=payload)
        if ranking is not None:
            EnrolmentConfig.programme_profiles = {
                **EnrolmentConfig.programme_profiles,
                plan.code: {"ranking": ranking},
            }
        return plan
