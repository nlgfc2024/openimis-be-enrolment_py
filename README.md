# openIMIS Enrolment module

This module owns programme-neutral enrolment eligibility, ranking, preview,
capacity, and confirmation orchestration. Registry data remains in `individual`
and Phase/beneficiary records remain in `social_protection`.

## Configuration

The module accepts the following `MODULE_CONFIGURATION` keys:

- `mandatory_enrollment_criteria`: criteria that every candidate must satisfy.
- `enable_maker_checker_logic_enrollment`: stage confirmations for approval.
- `default_beneficiary_status`: status assigned to newly enrolled beneficiaries.
- `registry_gender_field`, `registry_primary_worker_field`,
  `registry_household_verification_field`, and
  `registry_verified_household_value`: registry field mappings used by ranking
  and group eligibility.
- `bulk_enrollment_batch_size`: persistence batch size.

The individual and group maker-checker validation event names can be overridden
with `validation_enrollment` and `validation_group_enrollment`.

## Ranking and confirmation

Ranking paths may reference a concrete model field or a scalar `json_ext` key.
Related and other multi-valued paths are rejected. A benefit plan without a
ranking configuration retains the legacy behaviour and does not apply
`max_beneficiaries` as a selection cap.

Confirmation locks the benefit plan before the final eligibility, ranking,
capacity, and persistence steps. Maker-checker approval repeats those checks
against current registry and beneficiary data in the same transaction.
