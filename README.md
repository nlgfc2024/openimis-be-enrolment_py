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

The individual and group maker-checker validation event names can be overridden
with `validation_enrollment` and `validation_group_enrollment`.

## Ranking and confirmation

Ranking paths may reference a concrete model field or a scalar `json_ext` key.
Related and other multi-valued paths are rejected.

GROUP Phases can also order households by a member's value. Add `subject` to
an `order_by` object: `head` (the member with role `HEAD`) or `primary_worker`
(the verified primary worker). `field` is then a key in that member's
`json_ext`, for example to rank households by the PMT score stored on the head:

```json
{"subject": "head", "field": "household_pmt_score", "cast": "float", "direction": "asc", "nulls": "last"}
```

INDIVIDUAL Phases accept only `subject: self`, which is the same as leaving it
out. A benefit plan without a
ranking configuration retains the legacy behaviour and does not apply
`max_beneficiaries` as a selection cap.

Confirmation locks the benefit plan before the final eligibility, ranking,
capacity, and persistence steps. Maker-checker approval repeats those checks
against current registry and beneficiary data in the same transaction.

## Selection quotas

`enrolment_ranking[status].selection_strategy` reserves places in the ranked
list for female and youth candidates (`female_percentage`, `youth_percentage`).
`source` names the person whose gender and date of birth are checked:

- INDIVIDUAL Phases: `SELF`.
- GROUP Phases: `PRIMARY_WORKER` (the household's verified primary worker) or
  `HOUSEHOLD_HEAD` (the member with role `HEAD`, for female- or youth-headed
  household quotas).

For other quotas, replace the female/youth keys with an ordered `quotas` list:

```json
"selection_strategy": {
  "source": "SELF",
  "quotas": [
    {
      "key": "zomba",
      "count": 87,
      "when": [
        {"field": "city", "filter": "iexact", "type": "string", "value": "Zomba"}
      ]
    },
    {
      "key": "older",
      "percentage": 10,
      "when": [{"derived": "age", "filter": "gte", "value": 60}]
    }
  ],
  "fill_remaining": false
}
```

- Each bucket has a unique `key`, exactly one of `percentage` (of the cap,
  rounded down) or `count`, and a non-empty `when` list. Percentages may add up
  to at most 100.
- `when` conditions all apply to the `source` person. A condition is either a
  `json_ext` `field` with `filter`, `type` (`string`, `integer`, `numeric` or
  `boolean`) and `value`, or `derived: age` with an integer `filter` and
  `value`. An optional `subject` must name the source (`self`,
  `primary_worker` or `head`).
- Buckets fill in order from the ranked list, and nobody takes more than one
  place. A bucket short of candidates leaves its places empty.
- `fill_remaining` (default `true`) gives unfilled places to the next ranked
  candidates. Set it to `false` to keep them empty.
- `quotas` cannot be combined with `female_percentage`, `youth_percentage`,
  `female_value`, `youth_min_age` or `youth_max_age`. That shorthand is
  equivalent to a `female` bucket followed by a `youth` bucket, with
  `fill_remaining: true`.

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
