from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from enrolment.persistence import (
    EnrollmentPersistenceService,
    bulk_create_in_batches,
)


class BulkEnrollmentBatchTest(TestCase):
    def test_large_iterable_is_written_in_bounded_batches(self):
        manager = Mock()

        bulk_create_in_batches(manager, range(2501), batch_size=1000)

        self.assertEqual(
            [len(call.args[0]) for call in manager.bulk_create.call_args_list],
            [1000, 1000, 501],
        )

    def test_later_batch_failure_rolls_back_with_the_caller(self):
        manager = Mock()
        manager.bulk_create.side_effect = [None, ValidationError("failed")]

        with self.assertRaisesMessage(ValidationError, "failed"):
            bulk_create_in_batches(manager, range(3), batch_size=2)


class EnrollmentPersistenceServiceTest(TestCase):
    def setUp(self):
        self.service = EnrollmentPersistenceService(SimpleNamespace(
            login_name="operator",
            username="operator",
        ))

    @patch("enrolment.persistence.bulk_create_in_batches")
    @patch("enrolment.persistence.Beneficiary")
    def test_persist_individuals_creates_only_supplied_candidates(
        self, beneficiary, bulk_create
    ):
        bulk_create.side_effect = lambda _manager, objects: list(objects)
        selected = Mock()
        selected.iterator.return_value = iter([
            SimpleNamespace(json_ext={"rank": 1}),
            SimpleNamespace(json_ext={"rank": 2}),
        ])

        self.service.persist_individuals("plan-id", "ACTIVE", selected)

        self.assertEqual(beneficiary.call_count, 2)
        bulk_create.assert_called_once()
