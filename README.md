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
- `max_enrolment_score`: highest score a weighted ranking may award (default
  `100`). A ranking is rejected if its components' maximum points add up to
  more than this, or if its `weighted_score.normalise_to` is above it.
- `programme_profiles`: eligibility rules shared by a programme's Phases (see
  below; default `{}`).

The individual and group maker-checker validation event names can be overridden
with `validation_enrollment` and `validation_group_enrollment`.

## Programme profiles

`programme_profiles` holds eligibility rules that apply to every enrolment into
a programme, so they don't have to be copied into each Phase. Profiles are keyed
by `BenefitPlan.code`, matched without regard to case. A Phase whose code has no
profile uses `default`; without a `default` it uses only its own criteria.

```json
"programme_profiles": {
  "RMEP": {
    "description": "Rural Micro Enterprise Programme",
    "eligibility": {
      "criteria": [
        {"subject": "household", "field": "validation_status", "filter": "exact", "type": "string", "value": "VERIFIED"},
        {"subject": "primary_worker", "field": "business_experience", "filter": "exact", "type": "string", "value": "Yes"}
      ]
    }
  }
}
```

- A profile may contain `description` and `eligibility`.
- Profiles don't set a registry source. Enrolment already keeps only records
  whose `json_ext.data_source` matches the Phase's own `json_ext.data_source`.
  A Phase without one uses `ubr`, which also matches untagged records. On an
  instance with several programmes, set it on each Phase (for example `pwp` for
  RMEP and `sctp` for UPG).
- The profile's criteria are combined with the Phase's `advanced_criteria`, the
  mandatory criteria and the operator's filters. A candidate must meet all of
  them.
- A criterion is a `json_ext` `field` with `filter`, `type` (`string`,
  `integer`, `numeric` or `boolean`) and `value`. `subject` names the record it
  reads. Without a `subject`, the criterion reads the candidate itself.
  - INDIVIDUAL Phases: `self`.
  - GROUP Phases: `household`, or a member's `json_ext`: `primary_worker` (the
    verified primary worker), `head` (the member with role `HEAD`) or
    `any_member`.
  - Criteria with the same member subject must all be met by one member. For
    example, two `any_member` criteria need a single member who meets both.
- Only top-level keys are merged with the module defaults, so supply the whole
  `programme_profiles` block when overriding it. The block is validated when
  the configuration is saved. A subject that doesn't suit the Phase type is
  rejected when enrolment runs.

## Ranking and confirmation

Ranking paths may reference a concrete model field or a scalar `json_ext` key.
Related and other multi-valued paths are rejected. A benefit plan without a
ranking configuration retains the legacy behaviour and does not apply
`max_beneficiaries` as a selection cap.

Confirmation locks the benefit plan before the final eligibility, ranking,
capacity, and persistence steps. Maker-checker approval repeats those checks
against current registry and beneficiary data in the same transaction.

## Optional household district filter

Set `enable_household_district_filter: true` in the backend `enrolment`
ModuleConfiguration to enable the reserved `district_id__exact__integer=<R ID>`
operator. The default is false. Only GROUP enrolment supports it; backend
validation rejects district conditions when disabled even if sent directly by API
clients or included in saved criteria. Active District (R), location permissions,
primary-worker eligibility and approval-time revalidation still apply.

Set the same boolean in the exposed frontend `fe-enrolment` module configuration
to offer the picker. The frontend setting controls presentation only; it cannot
enable an operator disabled by the backend. Do not expose backend configuration
records merely to configure the frontend.

No Phase schema or Social Protection creation-default edits are required.
`district_id` is a location relationship filter, not beneficiary JSON data.
