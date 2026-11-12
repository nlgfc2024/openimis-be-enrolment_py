# openIMIS Enrolment module

This module owns programme-neutral enrolment eligibility, ranking, preview,
capacity, and confirmation orchestration. Registry data remains in `individual`
and Phase/beneficiary records remain in `social_protection`.

## Configuration

The module accepts the following `MODULE_CONFIGURATION` keys:

- `enable_maker_checker_logic_enrollment`: stage confirmations for approval.
- `default_beneficiary_status`: status assigned to newly enrolled beneficiaries.
- `registry_gender_field`, `registry_primary_worker_field`,
  `registry_household_verification_field`, `registry_verified_household_value`,
  `registry_individual_verification_field` and
  `registry_verified_individual_value`: registry field mappings used by ranking
  and the built-in eligibility rules (defaults `gender`, `primary_worker`,
  `validation_status` and `VERIFIED`).
- `bulk_enrollment_batch_size`: persistence batch size.
- `district_location_type`: the location type that counts as a district
  (default `R`), one of the location module's `location_types`. Derived
  district values report the name of the nearest location of this type, and
  the household district filter selects it. District names in score mappings
  and in `exact` or `iexact` conditions must name an active location of this
  type. Both are checked when the configuration is saved.
- `max_enrolment_score`: highest score a weighted ranking may award (default
  `100`). A ranking is rejected if its components' maximum points add up to
  more than this, or if its `weighted_score.normalise_to` is above it.
- `programme_profiles`: eligibility and ranking rules shared by a programme's
  Phases (see below; default `{}`).
- `require_programme_profile`: refuse to enrol into a Phase that matches no
  programme profile and has no `default` to fall back on (default `false`).

The individual and group maker-checker validation event names can be overridden
with `validation_enrollment` and `validation_group_enrollment`.

`mandatory_enrollment_criteria` is retired; programme profiles replace it. A
configuration that still sets criteria under it is rejected on save, and one
already stored is logged on load, because those criteria are no longer applied.
Move them to a profile's `eligibility.criteria`, or to a Phase's
`advanced_criteria` for a single beneficiary status. The empty
`{"INDIVIDUAL": {}, "GROUP": {}}` shape is still accepted.

## Built-in eligibility

Some rules apply to every Phase and need no configuration:

- GROUP Phases enrol households that are VERIFIED and have exactly one verified
  primary worker.
- INDIVIDUAL Phases enrol VERIFIED individuals who do not belong to an active
  household.

Programme profiles, Phase criteria and operator filters can only narrow the
candidates further.

## Programme profiles

`programme_profiles` holds eligibility and ranking rules that apply to every
enrolment into a programme, so they don't have to be copied into each Phase.
Profiles are keyed by `BenefitPlan.code`, matched without regard to case. A
Phase whose code has no profile uses `default`; without a `default` it uses only
its own criteria and no ranking, unless `require_programme_profile` is on.

A profile does not name a Phase type; its content decides which types can use
it. A `household` or member subject, or a ranking that reads a household member,
only suits GROUP Phases. A `self` subject or a `SELF` quota source only suits
INDIVIDUAL Phases. A profile with neither suits both. A profile that suits
neither type is rejected when the configuration is saved, and a Phase whose type
its profile does not suit fails when enrolment runs. On an instance with both
Phase types, keep GROUP-only rules out of `default` and key them by the GROUP
Phases' codes instead.

```json
"programme_profiles": {
  "RMEP": {
    "description": "Rural Micro Enterprise Programme",
    "eligibility": {
      "criteria": [
        {"subject": "primary_worker", "field": "business_experience", "filter": "exact", "type": "string", "value": "Yes"}
      ]
    },
    "ranking": {
      "*": {
        "order_by": [{"subject": "head", "field": "household_pmt_score", "cast": "float", "direction": "asc", "nulls": "last"}]
      }
    }
  }
}
```

- A profile may contain `description`, `eligibility` and `ranking`.
- Profiles don't set a registry source. Enrolment already keeps only records
  whose `json_ext.data_source` matches the Phase's own `json_ext.data_source`.
  A Phase without one uses `ubr`, which also matches untagged records. On an
  instance with several programmes, set it on each Phase (for example `pwp` for
  RMEP and `sctp` for UPG).
- The profile's criteria are combined with the Phase's `advanced_criteria` and
  the operator's filters. A candidate must meet all of them.
- A criterion is a `json_ext` `field` with `filter`, `type` (`string`,
  `integer`, `numeric` or `boolean`) and `value`. `subject` names the record it
  reads. Without a `subject`, the criterion reads the candidate itself.
  - INDIVIDUAL Phases: `self`.
  - GROUP Phases: `household`, or a member's `json_ext`: `primary_worker` (the
    verified primary worker), `head` (the member with role `HEAD`) or
    `any_member`.
  - Criteria with the same member subject must all be met by one member. For
    example, two `any_member` criteria need a single member who meets both.
  - In place of `field`, a criterion may set `derived`, with the same rules as
    in quota conditions: `age` (whole years on the day the enrolment is
    submitted, an integer `filter` and `value`) or `district` (the name of the district the subject
    lives in, a string `filter` and `value`). Age needs a person, so on GROUP
    Phases set `subject` to `head`, `primary_worker` or `any_member`. For
    example, UPG's working-age limit:

    ```json
    {"subject": "primary_worker", "derived": "age", "filter": "gte", "value": 18},
    {"subject": "primary_worker", "derived": "age", "filter": "lte", "value": 60}
    ```
- `ranking` holds entries keyed by beneficiary status, or `*` for any status.
  Each entry sets `order_by`, `tie_breaker`, `limit`, `score_source`,
  `weighted_score`, `minimum_score` and `selection_strategy` (see the sections
  below). It is the only place rankings are set; the enrolment summary shows the
  one that applied.
- Profile rankings are checked when the configuration is saved, without a
  Phase's `beneficiary_data_schema`. Name beneficiary data as `json_ext__<key>`
  or through a member `subject`; enrolment checks the ranking again against the
  Phase's schema.
- Only top-level keys are merged with the module defaults, so supply the whole
  `programme_profiles` block when overriding it. The block is validated when
  the configuration is saved.

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
out.

Rankings live in programme profiles. A Phase whose profile has no ranking for
the status (or has no profile) retains the legacy behaviour and does not apply
`max_beneficiaries` as a selection cap.

A Phase can no longer hold its own `enrolment_ranking`. The Phase validator
rejects an update that sets one; an empty `enrolment_ranking`, which the Phase
form sends to clear it, is still accepted. A Phase that still stores a ranking
from earlier cannot enrol: preview and confirmation fail with an error naming
the Phase, rather than enrolling its whole pool uncapped. Move the ranking to
the Phase's profile and clear it from the Phase.

Confirmation locks the benefit plan before the final eligibility, ranking,
capacity, and persistence steps. Maker-checker approval repeats those checks
against current registry and beneficiary data in the same transaction. Ages
are still worked out on the day the enrolment was submitted, so a candidate
who met an age rule then is not refused because approval came later.

The approval task also records the programme profile that applied when the
enrolment was submitted (`json_ext.programme_profile`: its key and content,
or a `null` key if none applied), and approval re-checks against that copy.
Editing a profile while an enrolment awaits approval therefore changes only
later enrolments. Tasks submitted before this was recorded are re-checked
against the current profile.

## Enrolment summary

The individual and group enrolment summaries report, besides the counts:

- `programmeProfile`: the key of the profile that applied, or null.
- `poolSize`, `capApplied`, `willEnrol` and `belowMinimumScore` (see
  [Weighted scores](#weighted-scores)).
- `quotaFill`, when the ranking has a `selection_strategy`: for each quota
  bucket the places it `reserved` and `filled`, in order, and how many places
  went to the next ranked candidates (`unreserved`). For example
  `{"buckets": [{"key": "female", "reserved": 3, "filled": 2}], "unreserved": 7}`.
- `enrolmentRanking`: the ranking that applied, including any member
  `subject` in `order_by`.

## Weighted scores

`weighted_score.components` lists the score items. Each item reads one value,
turns it into points with `scoring` (`mapping` or `numeric_bands`), and is
weighted into the total. An item may set:

- `field` (a `json_ext` key) or `derived`, but not both:
  - `derived: age`: whole years on the day the enrolment is submitted. Score it with
    `numeric_bands` whose `min` and `max` are whole years. It needs a person,
    so on GROUP Phases set `subject` (or `score_source`).
  - `derived: district`: the name of the district the subject lives in (see
    `district_location_type`). Score it with a `mapping` of district names.
- `subject`, whose value the item reads: `self` on INDIVIDUAL Phases;
  `household`, `head`, `primary_worker` or `any_member` on GROUP Phases.
  Without it, the item reads the ranking's `score_source` (the primary worker)
  when one is set, and otherwise the candidate itself.
- `aggregate: max`, required with `any_member`: every member is scored and the
  household gets its best member's points, or the item's `default` if it has
  no members.
- `label`: a name for the item, used in messages.

```json
{"label": "Labour capacity", "subject": "any_member", "aggregate": "max", "derived": "age", "weight": 4,
 "scoring": {"type": "numeric_bands", "bands": [{"min": 18, "max": 45, "points": 4}, {"min": 46, "max": 55, "points": 3}]}}
```

Bands are checked in order and the first that matches gives the points, so
"N or more" scales can be written as descending lower bounds. An item read from
two different subjects counts as two items.

`minimum_score`, next to `weighted_score` in a ranking entry, leaves out
candidates whose total score is below it. They are cut before the cap,
`limit.percentage` and selection quotas apply, so none of them can take a
place, even a reserved one. The enrolment summary reports how many were cut as
`belowMinimumScore`, and `poolSize` counts only those who reach the minimum.

`minimum_score` applies only to the weighted total (`_weighted_score`), so it
needs `weighted_score` and must be a positive number no higher than
`normalise_to`, the highest total anyone can reach. It does not limit any
single value. For a threshold on one value, such as a PMT score, add an
eligibility criterion to the profile instead:

```json
{"subject": "head", "field": "household_pmt_score", "filter": "lte", "type": "numeric", "value": 35}
```

The two can be combined: a value can have its own criterion, add points to the
weighted score through a `numeric_bands` item, and sort candidates in
`order_by`, while `minimum_score` still applies to the total.

## Selection quotas

A profile ranking's `selection_strategy` reserves places in the ranked
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
  ]
}
```

- Each bucket has a unique `key`, exactly one of `percentage` (of the cap,
  rounded down) or `count`, and a non-empty `when` list. Percentages may add up
  to at most 100.
- `when` conditions all apply to the `source` person. A condition is either a
  `json_ext` `field` with `filter`, `type` (`string`, `integer`, `numeric` or
  `boolean`) and `value`, `derived: age` with an integer `filter` and `value`,
  or `derived: district` with a string `filter` and a district name as
  `value`. An optional `subject` must name the source (`self`,
  `primary_worker` or `head`).
- Buckets fill in order from the ranked list, and nobody takes more than one
  place. Places a bucket cannot fill, and places no bucket reserves, then go to
  the next ranked candidates, so the Phase reaches its target whenever enough
  candidates are eligible.
- `quotas` cannot be combined with `female_percentage`, `youth_percentage`,
  `female_value`, `youth_min_age` or `youth_max_age`. That shorthand is
  equivalent to a `female` bucket followed by a `youth` bucket.

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
