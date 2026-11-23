from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from core.test_helpers import LogInHelper
from enrolment.ranking import (
    WEIGHTED_SCORE_FIELD,
    _maximum_points,
    _validate_scoring,
    ages_on,
    build_weighted_score,
    component_points,
    maximum_points,
    validate_scoring,
)
from enrolment.tests.helpers import create_village_in
from individual.models import Individual
from individual.tests.test_helpers import create_individual

TURNOVER = {"field": "turnover", "weight": 15, "scoring": {"type": "numeric_bands", "default": 1, "bands": [
    {"min": 5000001, "points": 15}, {"min": 3000000, "max": 5000000, "points": 12}]}}
MARKET = {"field": "market", "weight": 25, "scoring": {"type": "mapping", "values": {
    "stable": 25, "irregular": 12, "true": 3, "7": 4}}}
AGE = {"derived": "age", "weight": 10, "scoring": {"type": "numeric_bands", "bands": [
    {"min": 18, "max": 35, "points": 10}, {"min": 36, "points": 4}]}}
DISTRICT = {"derived": "district", "weight": 5, "scoring": {"type": "mapping", "values": {"Zomba": 5}}}


class ScoringHelpersTest(SimpleTestCase):
    def test_validate_scoring_is_public_and_the_private_name_still_works(self):
        self.assertIs(_validate_scoring, validate_scoring)
        self.assertIs(_maximum_points, maximum_points)
        validate_scoring(TURNOVER["scoring"])
        with self.assertRaisesMessage(ValidationError, "turnover.bands must be a non-empty list."):
            validate_scoring({"type": "numeric_bands", "bands": []}, "turnover")

    def test_maximum_points(self):
        self.assertEqual(maximum_points(TURNOVER["scoring"]), 15)
        self.assertEqual(maximum_points({**MARKET["scoring"], "maximum_points": 30}), 30)

    def test_numeric_bands_first_matching_band_inclusive(self):
        cases = [(5000001, 15), (5000000, 12), (3000000, 12), ("4000000", 12), (2999999, 1), (None, 1)]
        for value, points in cases:
            with self.subTest(value=value):
                self.assertEqual(component_points(TURNOVER, value), points)

    def test_numeric_bands_refuse_what_sql_cannot_cast(self):
        for value in ("abc", "", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                component_points(TURNOVER, value)

    def test_mapping_compares_json_text(self):
        cases = [("stable", 25), ("Stable", 0), (True, 3), (7, 4), ("unknown", 0), (None, 0)]
        for value, points in cases:
            with self.subTest(value=value):
                self.assertEqual(component_points(MARKET, value), points)

    def test_age_counts_birthdays_on_the_reference_day(self):
        with ages_on(date(2026, 11, 23)):
            cases = [(date(2008, 11, 23), 10), (date(2008, 11, 24), 0), (date(1990, 11, 24), 10),
                     (date(1990, 11, 23), 4), (datetime(2000, 1, 1, 8, 30), 10)]
            for dob, points in cases:
                with self.subTest(dob=dob):
                    self.assertEqual(component_points(AGE, dob), points)
        with self.assertRaises(ValueError):
            component_points(AGE, "2000-01-01")


class ScoringHelpersMatchSqlTest(TestCase):
    """component_points gives what build_weighted_score works out for the same values."""

    def _sql_points(self, component, json_ext=None, dob=None, location=None):
        individual = create_individual(self.username, {
            "json_ext": json_ext or {}, **({"dob": dob} if dob else {}),
            **({"location": location} if location else {})})
        one = {**component, "weight": maximum_points(component["scoring"])}
        weighted = {"normalise_to": one["weight"], "components": [one]}
        scored = build_weighted_score(Individual.objects.filter(id=individual.id), weighted)
        return round(scored.values_list(WEIGHTED_SCORE_FIELD, flat=True).get(), 6)

    @classmethod
    def setUpTestData(cls):
        cls.username = LogInHelper().get_or_create_user_api().username

    def test_numeric_bands(self):
        for value in (6000000, 5000001, 5000000, 3000000, "4500000", 100, None):
            with self.subTest(value=value):
                json_ext = {} if value is None else {"turnover": value}
                self.assertEqual(self._sql_points(TURNOVER, json_ext), component_points(TURNOVER, value))

    def test_mapping(self):
        for value in ("stable", "irregular", "Stable", True, 7, None):
            with self.subTest(value=value):
                json_ext = {} if value is None else {"market": value}
                self.assertEqual(self._sql_points(MARKET, json_ext), component_points(MARKET, value))

    def test_age(self):
        with ages_on(date(2026, 11, 23)):
            for dob in (date(2008, 11, 23), date(2008, 11, 24), date(1990, 11, 24), date(1990, 11, 23)):
                with self.subTest(dob=dob):
                    self.assertEqual(self._sql_points(AGE, dob=dob), component_points(AGE, dob))

    def test_district(self):
        village = create_village_in("Zomba")
        self.assertEqual(self._sql_points(DISTRICT, location=village), component_points(DISTRICT, "Zomba"))
        self.assertEqual(component_points(DISTRICT, "Zomba"), 5)
