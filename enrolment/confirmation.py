import logging

from core.models import User
from django.core.exceptions import ValidationError
from django.db import transaction

from individual.models import Group, GroupIndividual, Individual
from social_protection.apps import SocialProtectionConfig
from social_protection.models import BenefitPlan, BenefitPlanDataUploadRecords
from social_protection.services import (
    BeneficiaryImportService,
    EnrollmentPersistenceService,
)
from social_protection.utils import calculate_percentage_of_invalid_items
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task
from tasks_management.services import TaskService


logger = logging.getLogger(__name__)


def _ensure_no_pending_enrolment(benefit_plan_id, business_event, label):
    # Task.entity_id is textual while upload-record IDs are UUIDs, so evaluate
    # the IDs before the second query instead of generating a cross-type SQL
    # subquery that PostgreSQL cannot compare.
    pending_record_ids = list(Task.objects.filter(
        source="import_valid_items",
        business_event=business_event,
        status__in=[Task.Status.RECEIVED, Task.Status.ACCEPTED],
        is_deleted=False,
    ).values_list("entity_id", flat=True))
    if pending_record_ids and BenefitPlanDataUploadRecords.objects.filter(
        id__in=pending_record_ids,
        benefit_plan_id=benefit_plan_id,
        is_deleted=False,
    ).exists():
        raise ValidationError(
            f"A {label} enrolment confirmation is already awaiting approval "
            "for this BenefitPlan."
        )


def _start_approval_workflow(user, upload_record, status, business_event, label):
    task_result = TaskService(user).create({
        "source": "import_valid_items",
        "entity": upload_record,
        "status": Task.Status.RECEIVED,
        "executor_action_event": TasksManagementConfig.default_executor_event,
        "business_event": business_event,
        "json_ext": {
            "source_name": upload_record.data_upload.source_name,
            "workflow": upload_record.workflow,
            "percentage_of_invalid_items": (
                calculate_percentage_of_invalid_items(upload_record.id)
            ),
            "data_upload_id": str(upload_record.data_upload_id),
            "benefit_plan_id": str(upload_record.benefit_plan_id),
            "beneficiary_status": status,
        },
    })
    if not task_result or not task_result.get("success"):
        raise ValidationError(
            (task_result or {}).get("message")
            or (task_result or {}).get("detail")
            or f"Unable to initiate the {label} enrolment approval workflow."
        )
    return task_result


@transaction.atomic
def on_confirm_enrolment_of_individual(**kwargs):
    result = kwargs["result"]
    benefit_plan_id = result["benefit_plan_id"]
    status = result["status"]
    user = result["user"]

    # Lock before evaluating the selected queryset. A concurrent confirmation
    # will then re-evaluate its assignment exclusion after the first commits.
    benefit_plan = BenefitPlan.objects.select_for_update().get(id=benefit_plan_id)
    individuals = result["individuals_not_assigned_to_selected_programme"]
    if not individuals.exists():
        return {"mode": "noop", "benefit_plan_id": str(benefit_plan.id)}

    persistence = EnrollmentPersistenceService(user)
    if SocialProtectionConfig.enable_maker_checker_logic_enrollment:
        business_event = SocialProtectionConfig.validation_enrollment
        _ensure_no_pending_enrolment(
            benefit_plan.id,
            business_event,
            "individual",
        )
        upload_record = persistence.stage_individuals(benefit_plan, individuals)
        task_result = _start_approval_workflow(
            user,
            upload_record,
            status,
            business_event,
            "individual",
        )
        return {
            "mode": "maker_checker",
            "benefit_plan_id": str(benefit_plan.id),
            "task": task_result,
        }

    persistence.persist_individuals(benefit_plan.id, status, individuals)
    return {"mode": "direct", "benefit_plan_id": str(benefit_plan.id)}


@transaction.atomic
def on_confirm_enrolment_of_group(**kwargs):
    result = kwargs["result"]
    benefit_plan_id = result["benefit_plan_id"]
    status = result["status"]
    user = result["user"]

    benefit_plan = BenefitPlan.objects.select_for_update().get(id=benefit_plan_id)
    groups = result["groups_not_assigned_to_selected_programme"]
    if not groups.exists():
        return {"mode": "noop", "benefit_plan_id": str(benefit_plan.id)}

    persistence = EnrollmentPersistenceService(user)
    if SocialProtectionConfig.enable_maker_checker_logic_enrollment:
        business_event = SocialProtectionConfig.validation_group_enrollment
        _ensure_no_pending_enrolment(
            benefit_plan.id,
            business_event,
            "group",
        )
        upload_record = persistence.stage_groups(benefit_plan, groups)
        task_result = _start_approval_workflow(
            user,
            upload_record,
            status,
            business_event,
            "group",
        )
        return {
            "mode": "maker_checker",
            "benefit_plan_id": str(benefit_plan.id),
            "task": task_result,
        }

    persistence.persist_groups(benefit_plan.id, status, groups)
    return {"mode": "direct", "benefit_plan_id": str(benefit_plan.id)}


@transaction.atomic
def on_task_complete_enrolment(**kwargs):
    """Complete an approved enrollment task inside the Enrolment module."""
    result = kwargs.get("result")
    if not result or not result.get("success"):
        return
    data = result.get("data") or {}
    task = data.get("task") or {}
    business_event = task.get("business_event")
    supported_events = {
        SocialProtectionConfig.validation_enrollment,
        SocialProtectionConfig.validation_group_enrollment,
    }
    if (
        business_event not in supported_events
        or task.get("status") != Task.Status.COMPLETED
    ):
        return

    task_data = task.get("json_ext") or {}
    upload_record = None
    try:
        upload_record = BenefitPlanDataUploadRecords.objects.get(
            id=task["entity_id"]
        )
        benefit_plan = BenefitPlan.objects.select_for_update().get(
            id=task_data["benefit_plan_id"]
        )
        user = User.objects.get(id=data["user"]["id"])
        upload_id = task_data["data_upload_id"]
        status = task_data["beneficiary_status"]
        persistence = EnrollmentPersistenceService(user)

        if business_event == SocialProtectionConfig.validation_enrollment:
            individuals = Individual.objects.filter(
                individualdatasource__upload_id=upload_id,
            ).distinct()
            persistence.persist_individuals(
                benefit_plan.id,
                status,
                individuals,
            )
            BeneficiaryImportService(user).synchronize_data_for_reporting(
                upload_id=upload_id,
                benefit_plan=str(benefit_plan.id),
            )
        else:
            groups = Group.objects.filter(
                groupindividuals__is_deleted=False,
                groupindividuals__role=GroupIndividual.Role.HEAD,
                groupindividuals__individual__individualdatasource__upload_id=(
                    upload_id
                ),
            ).distinct()
            persistence.persist_groups(
                benefit_plan.id,
                status,
                groups,
            )
    except Exception as exc:
        if upload_record:
            data_upload = upload_record.data_upload
            data_upload.status = data_upload.Status.FAIL
            data_upload.error = {"Task Resolve": str(exc)}
            data_upload.save(username=data_upload.user_updated.username)
        logger.error(
            "Error while completing enrollment approval task",
            exc_info=exc,
        )
