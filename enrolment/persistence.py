import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count

from enrolment.apps import EnrolmentConfig
from enrolment.membership import authoritative_primary_worker_memberships
from individual.models import IndividualDataSource, IndividualDataSourceUpload
from social_protection.models import (
    Beneficiary,
    BenefitPlanDataUploadRecords,
    GroupBeneficiary,
)


def bulk_create_in_batches(manager, objects, batch_size=None):
    """Persist an iterable in bounded batches in the caller's transaction."""
    batch_size = batch_size or EnrolmentConfig.bulk_enrollment_batch_size
    batch = []
    for obj in objects:
        batch.append(obj)
        if len(batch) == batch_size:
            manager.bulk_create(batch, batch_size=batch_size)
            batch = []
    if batch:
        manager.bulk_create(batch, batch_size=batch_size)


def calculate_percentage_of_invalid_items(upload_id):
    sources = IndividualDataSource.objects.filter(
        upload_id=upload_id,
        is_deleted=False,
    )
    valid = sources.filter(validations__validation_errors=[]).count()
    invalid = sources.exclude(validations__validation_errors=[]).count()
    total = valid + invalid
    return round((invalid / total) * 100, 2) if total else 0


class EnrollmentPersistenceService:
    """Enrolment-owned beneficiary and maker-checker persistence."""

    def __init__(self, user):
        self.user = user

    @transaction.atomic
    def persist_individuals(self, benefit_plan_id, status, individuals):
        beneficiaries = (
            Beneficiary(
                individual=individual,
                benefit_plan_id=benefit_plan_id,
                status=status,
                json_ext=individual.json_ext,
                user_created=self.user,
                user_updated=self.user,
                uuid=uuid.uuid4(),
            )
            for individual in individuals.iterator(
                chunk_size=EnrolmentConfig.bulk_enrollment_batch_size
            )
        )
        bulk_create_in_batches(Beneficiary.objects, beneficiaries)

    @transaction.atomic
    def persist_groups(self, benefit_plan_id, status, groups):
        workers = self._primary_workers(groups)
        beneficiaries = (
            GroupBeneficiary(
                group=worker.group,
                benefit_plan_id=benefit_plan_id,
                status=status,
                json_ext=worker.individual.json_ext,
                user_created=self.user,
                user_updated=self.user,
                uuid=uuid.uuid4(),
            )
            for worker in workers.iterator(
                chunk_size=EnrolmentConfig.bulk_enrollment_batch_size
            )
        )
        bulk_create_in_batches(GroupBeneficiary.objects, beneficiaries)

    @transaction.atomic
    def stage_individuals(self, benefit_plan, individuals):
        upload_record = self._create_upload_record(
            benefit_plan,
            source_type="beneficiary import",
        )
        data_sources = (
            IndividualDataSource(
                uuid=uuid.uuid4(),
                user_created=self.user,
                user_updated=self.user,
                upload=upload_record.data_upload,
                individual=individual,
                json_ext=individual.json_ext,
                validations={},
            )
            for individual in individuals.iterator(
                chunk_size=EnrolmentConfig.bulk_enrollment_batch_size
            )
        )
        bulk_create_in_batches(IndividualDataSource.objects, data_sources)
        return upload_record

    @transaction.atomic
    def stage_groups(self, benefit_plan, groups):
        workers = self._primary_workers(groups)
        upload_record = self._create_upload_record(
            benefit_plan,
            source_type="beneficiary group import",
        )
        data_sources = (
            IndividualDataSource(
                uuid=uuid.uuid4(),
                user_created=self.user,
                user_updated=self.user,
                upload=upload_record.data_upload,
                individual=worker.individual,
                json_ext=worker.individual.json_ext,
                validations={},
            )
            for worker in workers.iterator(
                chunk_size=EnrolmentConfig.bulk_enrollment_batch_size
            )
        )
        bulk_create_in_batches(IndividualDataSource.objects, data_sources)
        return upload_record

    def _create_upload_record(self, benefit_plan, source_type):
        from core import datetime

        upload = IndividualDataSourceUpload(
            source_name=f"Enrollment into {benefit_plan.code} {datetime.date.today()}",
            source_type=source_type,
        )
        upload.save(username=self.user.login_name)
        upload_record = BenefitPlanDataUploadRecords(
            data_upload=upload,
            benefit_plan=benefit_plan,
            workflow="Enrollment",
        )
        upload_record.save(username=self.user.username)
        return upload_record

    @staticmethod
    def _primary_workers(groups):
        group_ids = list(groups.values_list("id", flat=True))
        workers = authoritative_primary_worker_memberships().filter(
            group_id__in=group_ids,
        ).select_related("group", "individual")
        counts = dict(
            workers.values("group_id")
            .annotate(member_count=Count("id"))
            .values_list("group_id", "member_count")
        )
        invalid_ids = [group_id for group_id in group_ids if counts.get(group_id) != 1]
        if invalid_ids:
            from individual.models import Group

            labels = list(
                Group.objects.filter(id__in=invalid_ids).values_list("code", flat=True)
            )
            raise ValidationError(
                "Selected groups must each have exactly one active primary worker "
                "in a verified household. Invalid groups: "
                + ", ".join(labels or [str(group_id) for group_id in invalid_ids])
            )
        return workers.order_by("group_id", "id")
