from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from core.test_helpers import LogInHelper
from enrolment import locations
from enrolment.profiles import validate_programme_profiles
from enrolment.ranking import allocate_places, normalise_selection_strategy, quota_members
from enrolment.tests.helpers import create_village_in
from individual.models import Group, Individual
from individual.tests.test_helpers import create_individual

QUOTAS = [{"key": "a", "count": 2}, {"key": "b", "percentage": 50}]


class AllocatePlacesTest(SimpleTestCase):
    def test_buckets_fill_in_order_then_the_next_ranked(self):
        selected, fill = allocate_places([1, 2, 3, 4, 5, 6], QUOTAS, {"a": {2, 5}, "b": {1, 5, 6}}, cap=5)

        # "a" takes 2 and 5; "b" reserves floor(5 * 50%) = 2 and, 5 being taken, gets 1 and 6; then 3
        self.assertEqual(selected, [2, 5, 1, 6, 3])
        self.assertEqual(fill, {"buckets": [{"key": "a", "reserved": 2, "filled": 2},
                                            {"key": "b", "reserved": 2, "filled": 2}], "unreserved": 1})

    def test_without_spillover_a_short_bucket_leaves_its_places_empty(self):
        members = {"a": {2}, "b": {6}}
        spilled, _ = allocate_places([1, 2, 3, 4, 5, 6], QUOTAS, members, cap=5)
        strict, fill = allocate_places([1, 2, 3, 4, 5, 6], QUOTAS, members, cap=5, spillover=False)

        self.assertEqual(spilled, [2, 6, 1, 3, 4])
        self.assertEqual(strict, [2, 6])
        self.assertEqual(fill["unreserved"], 0)

    def test_a_bucket_never_reserves_more_than_the_places_left(self):
        selected, fill = allocate_places([1, 2, 3], [{"key": "a", "count": 5}, {"key": "b", "count": 5}],
                                         {"a": {1, 2, 3}, "b": {1, 2, 3}}, cap=2)
        self.assertEqual(selected, [1, 2])
        self.assertEqual([bucket["reserved"] for bucket in fill["buckets"]], [2, 0])


class SelectionStrategyTest(SimpleTestCase):
    def test_spillover_defaults_to_true_and_must_be_a_boolean(self):
        condition = {"field": "biz_city", "filter": "iexact", "type": "string", "value": "325"}
        strategy = {"source": "SELF", "quotas": [{"key": "a", "count": 1, "when": [condition]}]}
        self.assertTrue(normalise_selection_strategy(strategy, Individual)["spillover"])
        self.assertFalse(normalise_selection_strategy({**strategy, "spillover": False}, Individual)["spillover"])
        with self.assertRaisesMessage(ValidationError, "selection_strategy.spillover must be true or false"):
            normalise_selection_strategy({**strategy, "spillover": "no"}, Individual)
        with self.assertRaisesMessage(ValidationError, "GROUP selection_strategy.source must be"):
            normalise_selection_strategy(strategy, Group)


class QuotaMembersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api(username="quotaMembers")

    def test_each_quota_matches_its_conditions(self):
        zomba = create_village_in("QM-Zomba")
        in_zomba = create_individual(self.user.username, {"location": zomba, "json_ext": {"biz_city": "ZA"}})
        in_blantyre = create_individual(self.user.username, {"json_ext": {"biz_city": "325"}})
        quotas = normalise_selection_strategy({"source": "SELF", "quotas": [
            {"key": "blantyre", "count": 1,
             "when": [{"field": "biz_city", "filter": "iexact", "type": "string", "value": "325"}]},
            {"key": "zomba", "count": 1, "when": [{"derived": "district", "filter": "iexact", "value": "QM-Zomba"}]},
        ]}, Individual)["quotas"]

        members = quota_members(Individual.objects.filter(id__in=[in_zomba.id, in_blantyre.id]), quotas)

        self.assertEqual(members, {"blantyre": {in_blantyre.id}, "zomba": {in_zomba.id}})


class LocationCodeConditionTest(TestCase):
    """A location answer matches the code given or, with a registered code history, any code it replaced."""

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api(username="quotaLocationCode")

    def setUp(self):
        histories = list(locations._CODE_HISTORIES)
        self.addCleanup(self._restore, histories)
        locations._CODE_HISTORIES.clear()

    @staticmethod
    def _restore(histories):
        locations._CODE_HISTORIES[:] = histories

    def _quotas(self, **condition):
        return normalise_selection_strategy({"source": "SELF", "quotas": [{"key": "lilongwe", "count": 2, "when": [
            {"derived": "location_code", "answer": "biz_city", "location_type": "R", "value": "LL01", **condition}],
        }]}, Individual)["quotas"]

    def test_former_codes_match_once_a_history_is_registered(self):
        old = create_individual(self.user.username, {"json_ext": {"biz_city": "LL"}})
        new = create_individual(self.user.username, {"json_ext": {"biz_city": "LL01"}})
        create_individual(self.user.username, {"json_ext": {"biz_city": "325"}})
        people = Individual.objects.filter(json_ext__has_key="biz_city")

        self.assertEqual(quota_members(people, self._quotas()), {"lilongwe": {new.id}})
        locations.register_location_code_history(lambda location_type, code: {"LL"} if code == "LL01" else set())
        self.assertEqual(quota_members(people, self._quotas()), {"lilongwe": {old.id, new.id}})
        self.assertEqual(locations.location_codes("R", "LL01"), ["LL", "LL01"])

    def test_the_condition_is_validated(self):
        cases = [({"answer": "biz__city"}, "answer must be the json_ext key"),
                 ({"location_type": "X"}, "location_type must be one of"),
                 ({"value": " "}, "value must be a location code"),
                 ({"filter": "icontains"}, "leave out filter and type")]
        for condition, message in cases:
            with self.subTest(condition=condition), self.assertRaisesMessage(ValidationError, message):
                self._quotas(**condition)

    def test_a_profile_criterion_can_use_it(self):
        profiles = {"UMEP": {"eligibility": {"criteria": [
            {"subject": "self", "derived": "location_code", "answer": "biz_city", "location_type": "R",
             "value": "325"}]}}}
        self.assertEqual(validate_programme_profiles(profiles), [])
