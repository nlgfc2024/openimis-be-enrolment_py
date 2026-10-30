# Household district enrolment

Ports the district and three-count behaviour from nlgfc2024/openimis-be-individual_py#22 into the extracted Enrolment module. Individual retains registry models and access policy; Social Protection retains phases and beneficiary records.

A GROUP phase must explicitly expose `district_id: {"type":"integer"}` in its beneficiary data schema. The optional operator is `district_id__exact__integer=<active R location ID>`. Enrolment validates this reserved operator, intersects active district descendants with the authorized household queryset, and consumes it before invoking the ordinary Individual JSON filter wizard. No household JSON backfill or new model migration is needed. Omission retains access scope. An unauthorized district is rejected, including when there are no matching eligible households.

Current primary-worker eligibility, mandatory criteria, ranking, phase-wide capacity and transactional confirmation/approval remain authoritative. Approval rechecks district scope against current data and staged candidates; it does not apply percentage limits twice.

`groupEnrollmentSummary` adds:

- `verifiedUnenrolledHouseholds`: eligible verified households not assigned in the selected phase/status, before the intake cap. Uses the configured registry verification field/value.
- `phaseTarget`: stored `max_beneficiaries`, including zero or null. It is not remaining capacity and does not change unranked-plan cap semantics.
- `enrolledHouseholds`: distinct current assignments in the phase/status, scoped by user and district, independent of current eligibility and other operator filters.

Deploy with the extracted Individual and Social Protection modules and register `enrolment` in the backend assembly. Move enrolment-owned ModuleConfiguration values to `enrolment`, retaining site overrides and approval event names. Phase creation defaults and phase schemas remain in Social Protection. Preserve VERIFIED criteria; do not add district to mandatory criteria.

## Validation

The complete Enrolment suite passed 90 tests plus 10 subtests against PostgreSQL before the final district approval regression was added. The district suite is rerun separately for that addition. Tests use the assembled schema, current extracted Individual/Social Protection sources, a fresh test database with schema synchronization (`--nomigrations`), test mode, in-memory caches/Celery, and upstream language/district fixtures. Test-only password validation is disabled for upstream fixtures. This is not a production migration rehearsal or staging browser acceptance.
