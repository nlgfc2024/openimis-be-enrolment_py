import logging

from core.models import User
from django.core.exceptions import ValidationError
from django.db import transaction

from individual.models import Individual, IndividualDataSource
from social_protection.models import (
    Beneficiary,
    BenefitPlan,
    BenefitPlanDataUploadRecords,
    GroupBeneficiary,
)
from social_protection.services import BeneficiaryImportService
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task
from tasks_management.services import TaskService

from enrolment.apps import EnrolmentConfig
from enrolment.membership import authoritative_primary_worker_memberships
from enrolment.persistence import (
    EnrollmentPersistenceService,
    calculate_percentage_of_invalid_items,
)


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


def _start_approval_workflow(
    user,
    upload_record,
    status,
    business_event,
    label,
    custom_filters,
):
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
            "custom_filters": list(custom_filters or []),
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
def confirm_individual_enrolment(custom_filters, benefit_plan_id, status, user):
    benefit_plan = BenefitPlan.objects.select_for_update().get(id=benefit_plan_id)
    from enrolment.services import build_individual_enrolment_selection

    result = build_individual_enrolment_selection(
        custom_filters,
        benefit_plan_id,
        status,
        user,
        benefit_plan=benefit_plan,
    )
    individuals = result["individuals_not_assigned_to_selected_programme"]
    if not individuals.exists():
        result["confirmation"] = {
            "mode": "noop",
            "benefit_plan_id": str(benefit_plan.id),
        }
        return result

    persistence = EnrollmentPersistenceService(user)
    if EnrolmentConfig.enable_maker_checker_logic_enrollment:
        business_event = EnrolmentConfig.validation_enrollment
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
            custom_filters,
        )
        result["confirmation"] = {
            "mode": "maker_checker",
            "benefit_plan_id": str(benefit_plan.id),
            "task": task_result,
        }
        return result

    persistence.persist_individuals(benefit_plan.id, status, individuals)
    result["confirmation"] = {
        "mode": "direct",
        "benefit_plan_id": str(benefit_plan.id),
    }
    return result


@transaction.atomic
def confirm_group_enrolment(custom_filters, benefit_plan_id, status, user):
    benefit_plan = BenefitPlan.objects.select_for_update().get(id=benefit_plan_id)
    from enrolment.services import build_group_enrolment_selection

    result = build_group_enrolment_selection(
        custom_filters,
        benefit_plan_id,
        status,
        user,
        benefit_plan=benefit_plan,
    )
    groups = result["groups_not_assigned_to_selected_programme"]
    if not groups.exists():
        result["confirmation"] = {
            "mode": "noop",
            "benefit_plan_id": str(benefit_plan.id),
        }
        return result

    persistence = EnrollmentPersistenceService(user)
    if EnrolmentConfig.enable_maker_checker_logic_enrollment:
        business_event = EnrolmentConfig.validation_group_enrollment
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
            custom_filters,
        )
        result["confirmation"] = {
            "mode": "maker_checker",
            "benefit_plan_id": str(benefit_plan.id),
            "task": task_result,
        }
        return result

    persistence.persist_groups(benefit_plan.id, status, groups)
    result["confirmation"] = {
        "mode": "direct",
        "benefit_plan_id": str(benefit_plan.id),
    }
    return result


def _validate_approval_selection(staged_ids, eligible, assigned, selected, label):
    staged_ids = set(staged_ids)
    eligible_ids = set(eligible.values_list("id", flat=True))
    assigned_ids = set(assigned.values_list("id", flat=True))
    selected_ids = set(selected.values_list("id", flat=True))
    if eligible_ids != staged_ids:
        missing = staged_ids - eligible_ids
        raise ValidationError(
            f"Staged {label} records are no longer eligible: "
            + ", ".join(map(str, sorted(missing, key=str)))
        )
    if assigned_ids:
        raise ValidationError(
            f"Staged {label} records are already enrolled in this BenefitPlan: "
            + ", ".join(map(str, sorted(assigned_ids, key=str)))
        )
    if selected_ids != staged_ids:
        raise ValidationError(
            f"The remaining BenefitPlan capacity no longer admits every staged {label}."
        )


def _complete_individual_approval(
    user, benefit_plan, upload_id, status, custom_filters
):
    staged_rows = IndividualDataSource.objects.filter(upload_id=upload_id)
    if not staged_rows.exists() or staged_rows.filter(is_deleted=True).exists():
        raise ValidationError("The staged individual records are missing or deleted.")
    staged_ids = list(
        Individual.objects.filter(
            individualdatasource__upload_id=upload_id,
            individualdatasource__is_deleted=False,
        ).values_list("id", flat=True).distinct()
    )
    from enrolment.services import build_individual_enrolment_selection

    selection = build_individual_enrolment_selection(
        custom_filters,
        benefit_plan.id,
        status,
        user,
        candidate_ids=staged_ids,
        benefit_plan=benefit_plan,
        apply_percentage_limit=False,
    )
    duplicates = Beneficiary.objects.filter(
        benefit_plan=benefit_plan,
        individual_id__in=staged_ids,
        is_deleted=False,
    ).values_list("individual_id", flat=True)
    if duplicates:
        raise ValidationError(
            "Staged individuals are already enrolled in this BenefitPlan: "
            + ", ".join(map(str, duplicates))
        )
    _validate_approval_selection(
        staged_ids,
        selection["individual_query_with_filters"],
        selection["individuals_assigned_to_selected_programme"],
        selection["individuals_not_assigned_to_selected_programme"],
        "individual",
    )
    EnrollmentPersistenceService(user).persist_individuals(
        benefit_plan.id,
        status,
        selection["individuals_not_assigned_to_selected_programme"],
    )
    BeneficiaryImportService(user).synchronize_data_for_reporting(
        upload_id=upload_id,
        benefit_plan=str(benefit_plan.id),
    )


def _complete_group_approval(user, benefit_plan, upload_id, status, custom_filters):
    staged_rows = IndividualDataSource.objects.filter(upload_id=upload_id)
    if not staged_rows.exists() or staged_rows.filter(is_deleted=True).exists():
        raise ValidationError("The staged group records are missing or deleted.")
    staged_worker_ids = list(
        Individual.objects.filter(
            individualdatasource__upload_id=upload_id,
            individualdatasource__is_deleted=False,
        ).values_list("id", flat=True).distinct()
    )
    memberships = authoritative_primary_worker_memberships().filter(
        individual_id__in=staged_worker_ids
    )
    group_ids = list(memberships.values_list("group_id", flat=True))
    if len(group_ids) != len(staged_worker_ids) or len(set(group_ids)) != len(group_ids):
        raise ValidationError(
            "One or more staged group primary workers are no longer authoritative."
        )
    from enrolment.services import build_group_enrolment_selection

    selection = build_group_enrolment_selection(
        custom_filters,
        benefit_plan.id,
        status,
        user,
        candidate_ids=group_ids,
        benefit_plan=benefit_plan,
        apply_percentage_limit=False,
    )
    duplicates = GroupBeneficiary.objects.filter(
        benefit_plan=benefit_plan,
        group_id__in=group_ids,
        is_deleted=False,
    ).values_list("group_id", flat=True)
    if duplicates:
        raise ValidationError(
            "Staged groups are already enrolled in this BenefitPlan: "
            + ", ".join(map(str, duplicates))
        )
    _validate_approval_selection(
        group_ids,
        selection["group_query_with_filters"],
        selection["groups_assigned_to_selected_programme"],
        selection["groups_not_assigned_to_selected_programme"],
        "group",
    )
    EnrollmentPersistenceService(user).persist_groups(
        benefit_plan.id,
        status,
        selection["groups_not_assigned_to_selected_programme"],
    )


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
        EnrolmentConfig.validation_enrollment,
        EnrolmentConfig.validation_group_enrollment,
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
        user = User.objects.get(id=data["user"]["id"])
        upload_id = task_data["data_upload_id"]
        status = task_data["beneficiary_status"]
        custom_filters = task_data.get("custom_filters") or []

        # The nested atomic block creates a savepoint. Catching below can mark
        # the workflow failed without committing beneficiaries written before a
        # later reporting or persistence error.
        with transaction.atomic():
            benefit_plan = BenefitPlan.objects.select_for_update().get(
                id=task_data["benefit_plan_id"]
            )
            if business_event == EnrolmentConfig.validation_enrollment:
                _complete_individual_approval(
                    user, benefit_plan, upload_id, status, custom_filters
                )
            else:
                _complete_group_approval(
                    user, benefit_plan, upload_id, status, custom_filters
                )
    except Exception as exc:
        if upload_record:
            data_upload = upload_record.data_upload
            data_upload.status = data_upload.Status.FAIL
            data_upload.error = {"Task Resolve": str(exc)}
            data_upload.save(username=data_upload.user_updated.username)
        task_id = task.get("id")
        if task_id:
            Task.objects.filter(id=task_id).update(status=Task.Status.FAILED)
        logger.error(
            "Error while completing enrollment approval task",
            exc_info=exc,
        )
