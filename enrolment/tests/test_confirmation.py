import copy
from django.core.exceptions import ValidationError
from unittest.mock import patch

from core.models.base_mutation import MutationLog
from enrolment.apps import EnrolmentConfig
from enrolment.services import EnrolmentService
from enrolment.schema import Mutation as EnrolmentMutation, Query as EnrolmentQuery
from individual.schema import Mutation as IndividualMutation, Query as IndividualQuery
from individual.tests.test_helpers import (
    BaseTestContext,
    IndividualGQLTestCase,
    create_group_with_individual,
    create_individual,
)
from social_protection.models import (
    Beneficiary,
    BenefitPlanDataUploadRecords,
    GroupBeneficiary,
)
from social_protection.tests.test_helpers import create_benefit_plan
from tasks_management.models import Task
from tasks_management.services import TaskService
from openIMIS.schema import schema as assembled_schema


class EnrolmentConfirmationTest(IndividualGQLTestCase):
    def setUp(self):
        self.original_mandatory_criteria = copy.deepcopy(
            EnrolmentConfig.mandatory_enrollment_criteria
        )
        EnrolmentConfig.mandatory_enrollment_criteria = {
            "INDIVIDUAL": {},
            "GROUP": {},
        }

    def tearDown(self):
        EnrolmentConfig.mandatory_enrollment_criteria = (
            self.original_mandatory_criteria
        )

    def _plan(self, plan_type, token):
        return create_benefit_plan(
            self.admin_user.username,
            payload_override={
                "type": plan_type,
                "beneficiary_data_schema": {
                    "properties": {"enrolment_test": {"type": "string"}}
                },
                "json_ext": {
                    "advanced_criteria": {
                        "ACTIVE": [{
                            "field": "enrolment_test",
                            "filter": "exact",
                            "type": "string",
                            "value": token,
                        }]
                    }
                },
            },
        )

    def _verified_group(self, token):
        individual, group, membership = create_group_with_individual(
            self.admin_user.username,
            group_override={
                "json_ext": {
                    "enrolment_test": token,
                    "validation_status": "VERIFIED",
                }
            },
            individual_override={"json_ext": {"enrolment_test": token}},
        )
        membership.json_ext = {"primary_worker": True}
        membership.save(username=self.admin_user.username)
        group.refresh_from_db()
        group.json_ext = {
            **(group.json_ext or {}),
            "enrolment_test": token,
            "validation_status": "VERIFIED",
        }
        group.save(username=self.admin_user.username)
        return individual, group, membership

    @patch.object(
        EnrolmentConfig, "enable_maker_checker_logic_enrollment", False
    )
    def test_direct_individual_confirmation_persists_once(self):
        plan = self._plan("INDIVIDUAL", "direct-individual")
        individual = create_individual(
            self.admin_user.username,
            {"json_ext": {"enrolment_test": "direct-individual"}},
        )
        service = EnrolmentService(self.admin_user)

        service.select_individuals_to_benefit_plan(
            [], str(plan.id), "ACTIVE", self.admin_user
        )
        service.select_individuals_to_benefit_plan(
            [], str(plan.id), "ACTIVE", self.admin_user
        )

        beneficiaries = Beneficiary.objects.filter(
            individual=individual,
            benefit_plan=plan,
            status="ACTIVE",
            is_deleted=False,
        )
        self.assertEqual(beneficiaries.count(), 1)

    @patch.object(
        EnrolmentConfig, "enable_maker_checker_logic_enrollment", False
    )
    def test_direct_group_confirmation_persists_once(self):
        plan = self._plan("GROUP", "direct-group")
        _, group, _ = self._verified_group("direct-group")
        service = EnrolmentService(self.admin_user)

        service.select_groups_to_benefit_plan(
            [], str(plan.id), "ACTIVE", self.admin_user
        )
        service.select_groups_to_benefit_plan(
            [], str(plan.id), "ACTIVE", self.admin_user
        )

        self.assertEqual(
            GroupBeneficiary.objects.filter(
                group=group,
                benefit_plan=plan,
                status="ACTIVE",
                is_deleted=False,
            ).count(),
            1,
        )

    @patch.object(
        EnrolmentConfig, "enable_maker_checker_logic_enrollment", True
    )
    def test_individual_maker_checker_stages_one_pending_workflow(self):
        plan = self._plan("INDIVIDUAL", "maker-individual")
        individual = create_individual(
            self.admin_user.username,
            {"json_ext": {"enrolment_test": "maker-individual"}},
        )
        service = EnrolmentService(self.admin_user)

        service.select_individuals_to_benefit_plan(
            [], str(plan.id), "ACTIVE", self.admin_user
        )

        self.assertFalse(
            Beneficiary.objects.filter(individual=individual, benefit_plan=plan).exists()
        )
        record = BenefitPlanDataUploadRecords.objects.get(
            benefit_plan=plan, workflow="Enrollment", is_deleted=False
        )
        self.assertTrue(
            Task.objects.filter(
                entity_id=str(record.id),
                business_event=EnrolmentConfig.validation_enrollment,
                status__in=[Task.Status.RECEIVED, Task.Status.ACCEPTED],
                is_deleted=False,
            ).exists()
        )

        with self.assertRaisesMessage(ValidationError, "already awaiting approval"):
            service.select_individuals_to_benefit_plan(
                [], str(plan.id), "ACTIVE", self.admin_user
            )
        self.assertEqual(
            BenefitPlanDataUploadRecords.objects.filter(
                benefit_plan=plan, workflow="Enrollment", is_deleted=False
            ).count(),
            1,
        )

    @patch.object(
        EnrolmentConfig, "enable_maker_checker_logic_enrollment", True
    )
    def test_group_maker_checker_stages_one_pending_workflow(self):
        plan = self._plan("GROUP", "maker-group")
        _, group, _ = self._verified_group("maker-group")
        service = EnrolmentService(self.admin_user)

        service.select_groups_to_benefit_plan(
            [], str(plan.id), "ACTIVE", self.admin_user
        )

        self.assertFalse(
            GroupBeneficiary.objects.filter(group=group, benefit_plan=plan).exists()
        )
        record = BenefitPlanDataUploadRecords.objects.get(
            benefit_plan=plan, workflow="Enrollment", is_deleted=False
        )
        self.assertTrue(
            Task.objects.filter(
                entity_id=str(record.id),
                business_event=EnrolmentConfig.validation_group_enrollment,
                status__in=[Task.Status.RECEIVED, Task.Status.ACCEPTED],
                is_deleted=False,
            ).exists()
        )
        with self.assertRaisesMessage(ValidationError, "already awaiting approval"):
            service.select_groups_to_benefit_plan(
                [], str(plan.id), "ACTIVE", self.admin_user
            )

    @patch.object(
        EnrolmentConfig, "enable_maker_checker_logic_enrollment", True
    )
    @patch(
        "enrolment.confirmation.BeneficiaryImportService."
        "synchronize_data_for_reporting"
    )
    def test_approved_individual_workflow_persists_beneficiary(self, _synchronize):
        plan = self._plan("INDIVIDUAL", "approved-individual")
        individual = create_individual(
            self.admin_user.username,
            {"json_ext": {"enrolment_test": "approved-individual"}},
        )
        EnrolmentService(self.admin_user).select_individuals_to_benefit_plan(
            [], str(plan.id), "ACTIVE", self.admin_user
        )
        record = BenefitPlanDataUploadRecords.objects.get(
            benefit_plan=plan,
            workflow="Enrollment",
            is_deleted=False,
        )
        task = Task.objects.get(
            entity_id=str(record.id),
            business_event=EnrolmentConfig.validation_enrollment,
            is_deleted=False,
        )

        result = TaskService(self.admin_user).complete_task({"id": task.id})

        self.assertTrue(result["success"])
        self.assertTrue(
            Beneficiary.objects.filter(
                individual=individual,
                benefit_plan=plan,
                status="ACTIVE",
                is_deleted=False,
            ).exists()
        )

    @patch.object(
        EnrolmentConfig, "enable_maker_checker_logic_enrollment", True
    )
    @patch(
        "enrolment.confirmation.BeneficiaryImportService."
        "synchronize_data_for_reporting",
        side_effect=ValidationError("reporting failed"),
    )
    def test_approval_failure_rolls_back_beneficiary_and_fails_task(self, _sync):
        plan = self._plan("INDIVIDUAL", "rollback-individual")
        individual = create_individual(
            self.admin_user.username,
            {"json_ext": {"enrolment_test": "rollback-individual"}},
        )
        EnrolmentService(self.admin_user).select_individuals_to_benefit_plan(
            [], str(plan.id), "ACTIVE", self.admin_user
        )
        record = BenefitPlanDataUploadRecords.objects.get(
            benefit_plan=plan, workflow="Enrollment", is_deleted=False
        )
        task = Task.objects.get(entity_id=str(record.id), is_deleted=False)

        TaskService(self.admin_user).complete_task({"id": task.id})

        task.refresh_from_db()
        record.data_upload.refresh_from_db()
        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertEqual(record.data_upload.status, record.data_upload.Status.FAIL)
        self.assertFalse(
            Beneficiary.objects.filter(
                individual=individual, benefit_plan=plan, is_deleted=False
            ).exists()
        )

    @patch.object(
        EnrolmentConfig, "enable_maker_checker_logic_enrollment", True
    )
    def test_approval_rejects_candidate_enrolled_while_pending(self):
        plan = self._plan("INDIVIDUAL", "duplicate-individual")
        individual = create_individual(
            self.admin_user.username,
            {"json_ext": {"enrolment_test": "duplicate-individual"}},
        )
        EnrolmentService(self.admin_user).select_individuals_to_benefit_plan(
            [], str(plan.id), "ACTIVE", self.admin_user
        )
        record = BenefitPlanDataUploadRecords.objects.get(
            benefit_plan=plan, workflow="Enrollment", is_deleted=False
        )
        task = Task.objects.get(entity_id=str(record.id), is_deleted=False)
        Beneficiary(
            individual=individual,
            benefit_plan=plan,
            status="POTENTIAL",
            json_ext={},
        ).save(username=self.admin_user.username)

        TaskService(self.admin_user).complete_task({"id": task.id})

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertEqual(
            Beneficiary.objects.filter(
                individual=individual, benefit_plan=plan, is_deleted=False
            ).count(),
            1,
        )

    @patch.object(
        EnrolmentConfig, "enable_maker_checker_logic_enrollment", True
    )
    def test_approval_rechecks_remaining_capacity_under_plan_lock(self):
        token = "approval-capacity"
        plan = create_benefit_plan(
            self.admin_user.username,
            payload_override={
                "type": "INDIVIDUAL",
                "max_beneficiaries": 1,
                "beneficiary_data_schema": {
                    "properties": {"enrolment_test": {"type": "string"}}
                },
                "json_ext": {
                    "advanced_criteria": {"ACTIVE": [{
                        "field": "enrolment_test",
                        "filter": "exact",
                        "type": "string",
                        "value": token,
                    }]},
                    "enrolment_ranking": {"ACTIVE": {
                        "order_by": ["id"],
                        "limit": {"respect_max_beneficiaries": True},
                    }},
                },
            },
        )
        staged = create_individual(
            self.admin_user.username,
            {"json_ext": {"enrolment_test": token}},
        )
        EnrolmentService(self.admin_user).select_individuals_to_benefit_plan(
            [], str(plan.id), "ACTIVE", self.admin_user
        )
        record = BenefitPlanDataUploadRecords.objects.get(
            benefit_plan=plan, workflow="Enrollment", is_deleted=False
        )
        task = Task.objects.get(entity_id=str(record.id), is_deleted=False)
        other = create_individual(self.admin_user.username)
        Beneficiary(
            individual=other,
            benefit_plan=plan,
            status="ACTIVE",
            json_ext={},
        ).save(username=self.admin_user.username)

        TaskService(self.admin_user).complete_task({"id": task.id})

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertFalse(
            Beneficiary.objects.filter(
                individual=staged, benefit_plan=plan, is_deleted=False
            ).exists()
        )

    @patch.object(
        EnrolmentConfig, "enable_maker_checker_logic_enrollment", True
    )
    def test_approved_group_workflow_persists_group_beneficiary(self):
        plan = self._plan("GROUP", "approved-group")
        _, group, _ = self._verified_group("approved-group")
        EnrolmentService(self.admin_user).select_groups_to_benefit_plan(
            [], str(plan.id), "ACTIVE", self.admin_user
        )
        record = BenefitPlanDataUploadRecords.objects.get(
            benefit_plan=plan,
            workflow="Enrollment",
            is_deleted=False,
        )
        task = Task.objects.get(
            entity_id=str(record.id),
            business_event=EnrolmentConfig.validation_group_enrollment,
            is_deleted=False,
        )

        result = TaskService(self.admin_user).complete_task({"id": task.id})

        self.assertTrue(result["success"])
        self.assertTrue(
            GroupBeneficiary.objects.filter(
                group=group,
                benefit_plan=plan,
                status="ACTIVE",
                is_deleted=False,
            ).exists()
        )

    @patch.object(
        EnrolmentConfig, "enable_maker_checker_logic_enrollment", False
    )
    def test_new_graphql_mutation_confirms_individual_enrolment(self):
        plan = self._plan("INDIVIDUAL", "graphql-individual")
        individual = create_individual(
            self.admin_user.username,
            {"json_ext": {"enrolment_test": "graphql-individual"}},
        )
        result = assembled_schema.execute(
            f'''mutation {{
              confirmIndividualEnrollment(input: {{
                benefitPlanId: "{plan.id}"
                status: "ACTIVE"
              }}) {{
                internalId
              }}
            }}''',
            context_value=BaseTestContext(
                user=self.admin_user, method="POST"
            ).request,
        )
        self.assertIsNone(result.errors)
        mutation_log = MutationLog.objects.get(
            id=result.data["confirmIndividualEnrollment"]["internalId"]
        )
        self.assertEqual(mutation_log.status, MutationLog.SUCCESS)
        self.assertTrue(
            Beneficiary.objects.filter(individual=individual, benefit_plan=plan).exists()
        )

    @patch.object(
        EnrolmentConfig, "enable_maker_checker_logic_enrollment", False
    )
    def test_new_graphql_mutation_confirms_group_enrolment(self):
        plan = self._plan("GROUP", "graphql-group")
        _, group, _ = self._verified_group("graphql-group")
        result = assembled_schema.execute(
            f'''mutation {{
              confirmGroupEnrollment(input: {{
                benefitPlanId: "{plan.id}"
                status: "ACTIVE"
              }}) {{
                internalId
              }}
            }}''',
            context_value=BaseTestContext(
                user=self.admin_user, method="POST"
            ).request,
        )
        self.assertIsNone(result.errors)
        mutation_log = MutationLog.objects.get(
            id=result.data["confirmGroupEnrollment"]["internalId"]
        )
        self.assertEqual(mutation_log.status, MutationLog.SUCCESS)
        self.assertTrue(
            GroupBeneficiary.objects.filter(group=group, benefit_plan=plan).exists()
        )

    @patch.object(
        EnrolmentConfig, "enable_maker_checker_logic_enrollment", True
    )
    def test_graphql_individual_confirmation_starts_approval_workflow(self):
        plan = self._plan("INDIVIDUAL", "graphql-maker-individual")
        individual = create_individual(
            self.admin_user.username,
            {"json_ext": {"enrolment_test": "graphql-maker-individual"}},
        )

        result = assembled_schema.execute(
            f'''mutation {{
              confirmIndividualEnrollment(input: {{
                benefitPlanId: "{plan.id}"
                status: "ACTIVE"
              }}) {{
                internalId
              }}
            }}''',
            context_value=BaseTestContext(
                user=self.admin_user, method="POST"
            ).request,
        )

        self.assertIsNone(result.errors)
        mutation_log = MutationLog.objects.get(
            id=result.data["confirmIndividualEnrollment"]["internalId"]
        )
        self.assertEqual(mutation_log.status, MutationLog.SUCCESS)
        self.assertFalse(
            Beneficiary.objects.filter(
                individual=individual,
                benefit_plan=plan,
            ).exists()
        )
        record = BenefitPlanDataUploadRecords.objects.get(
            benefit_plan=plan,
            workflow="Enrollment",
            is_deleted=False,
        )
        self.assertTrue(
            Task.objects.filter(
                entity_id=str(record.id),
                business_event=EnrolmentConfig.validation_enrollment,
                status__in=[Task.Status.RECEIVED, Task.Status.ACCEPTED],
                is_deleted=False,
            ).exists()
        )

    @patch.object(
        EnrolmentConfig, "enable_maker_checker_logic_enrollment", True
    )
    def test_graphql_group_confirmation_starts_approval_workflow(self):
        plan = self._plan("GROUP", "graphql-maker-group")
        _, group, _ = self._verified_group("graphql-maker-group")

        result = assembled_schema.execute(
            f'''mutation {{
              confirmGroupEnrollment(input: {{
                benefitPlanId: "{plan.id}"
                status: "ACTIVE"
              }}) {{
                internalId
              }}
            }}''',
            context_value=BaseTestContext(
                user=self.admin_user, method="POST"
            ).request,
        )

        self.assertIsNone(result.errors)
        mutation_log = MutationLog.objects.get(
            id=result.data["confirmGroupEnrollment"]["internalId"]
        )
        self.assertEqual(mutation_log.status, MutationLog.SUCCESS)
        self.assertFalse(
            GroupBeneficiary.objects.filter(
                group=group,
                benefit_plan=plan,
            ).exists()
        )
        record = BenefitPlanDataUploadRecords.objects.get(
            benefit_plan=plan,
            workflow="Enrollment",
            is_deleted=False,
        )
        self.assertTrue(
            Task.objects.filter(
                entity_id=str(record.id),
                business_event=(
                    EnrolmentConfig.validation_group_enrollment
                ),
                status__in=[Task.Status.RECEIVED, Task.Status.ACCEPTED],
                is_deleted=False,
            ).exists()
        )

    @patch.object(
        EnrolmentConfig, "enable_maker_checker_logic_enrollment", True
    )
    @patch("enrolment.confirmation.TaskService")
    def test_workflow_creation_failure_rolls_back_staging(
        self, task_service
    ):
        task_service.return_value.create.return_value = {
            "success": False,
            "message": "workflow unavailable",
        }
        plan = self._plan("INDIVIDUAL", "maker-failure")
        create_individual(
            self.admin_user.username,
            {"json_ext": {"enrolment_test": "maker-failure"}},
        )

        with self.assertRaisesMessage(ValidationError, "workflow unavailable"):
            EnrolmentService(self.admin_user).select_individuals_to_benefit_plan(
                [], str(plan.id), "ACTIVE", self.admin_user
            )

        self.assertFalse(
            BenefitPlanDataUploadRecords.objects.filter(
                benefit_plan=plan,
                workflow="Enrollment",
                is_deleted=False,
            ).exists()
        )

    def test_enrolment_schema_is_the_only_runtime_api_owner(self):
        self.assertNotIn("confirm_individual_enrollment", IndividualMutation._meta.fields)
        self.assertNotIn("confirm_group_enrollment", IndividualMutation._meta.fields)
        self.assertNotIn("individual_enrollment_summary", IndividualQuery._meta.fields)
        self.assertNotIn("group_enrollment_summary", IndividualQuery._meta.fields)
        self.assertIn("confirm_individual_enrollment", EnrolmentMutation._meta.fields)
        self.assertIn("confirm_group_enrollment", EnrolmentMutation._meta.fields)
        self.assertIn("individual_enrollment_summary", EnrolmentQuery._meta.fields)
        self.assertIn("group_enrollment_summary", EnrolmentQuery._meta.fields)
