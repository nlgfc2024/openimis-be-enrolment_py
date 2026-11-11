import copy

from individual.tests.test_helpers import create_individual
from location.models import Location
from social_protection.tests.test_helpers import create_benefit_plan

from enrolment.apps import EnrolmentConfig


def create_verified_individual(username, payload_override=None):
    """Create an individual the registry marks VERIFIED, as enrolment requires.

    A ``validation_status`` given in ``payload_override["json_ext"]`` is kept, so
    tests can still create PENDING or other unverified individuals.
    """
    data = copy.deepcopy(payload_override or {})
    data["json_ext"] = {
        "validation_status": "VERIFIED",
        **(data.get("json_ext") or {}),
    }
    return create_individual(username, data)


def create_village_in(district_name):
    """Create a district and a village three levels below it.

    Only the district level carries ``district_name``, so a lookup that finds
    it read the district rather than the village or a level in between.
    """
    def location(kind, parent=None):
        name = district_name if kind == "R" else f"{district_name} {kind}"
        return Location.objects.create(
            type=kind, code=f"{district_name}-{kind}", name=name,
            parent=parent, audit_user_id=-1,
        )
    return location("V", location("W", location("D", location("R"))))


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
