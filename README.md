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

## Prepare district filter configuration

After deploying district-filter support, GROUP phases must expose
`district_id: {"type": "integer"}` in their beneficiary data schema.
Creation defaults only affect new phases; existing phases must be targeted explicitly.
The configuration remains owned by Social Protection.

Export the complete Social Protection module configuration and the phases to
prepare into this wrapper (replace the example with actual exported values):

```json
{
  "module_config": {"benefit_plan_creation_defaults": {}},
  "phases": [{"id": "phase-uuid", "type": "GROUP", "beneficiary_data_schema": {}}]
}
```

From the configured backend application, run:

```sh
python manage.py prepare_enrolment_district_config before.json proposed.json --phase-id phase-uuid
```

Repeat `--phase-id` to select multiple existing GROUP phases. Omit it to prepare
creation defaults only. The command preserves unrelated properties, phase criteria
(including VERIFIED), and existing district field metadata. It rejects conflicting
types, invalid exports, unknown/non-GROUP/duplicate selected phase IDs, and existing
output paths. Repeated preparation is idempotent at the JSON content level.

The handler reads and writes files only and disables system/migration checks;
normal Django application startup still uses the configured application environment.
It does not read or update phase records or ModuleConfiguration. Review the JSON
diff, then apply only the intended defaults/schema additions using supported
administration/API paths. The wrapper is not a bulk API payload. Keep an original
export for rollback, and check for concurrent changes before applying. Do not add
district to mandatory criteria or overwrite existing eligibility rules.
