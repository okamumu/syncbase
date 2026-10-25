# Changelog

## 0.1.0 — 2026-10-25

First cut of the data management core (layer 2 of the specification,
`docs/spec.ja.md`), extracted and generalized from the import pipeline of a
conference-management system, where the three-way merge was first built and
battle-tested.

- Three-way field merge with S0/S1/M0/M1 (S0 ≠ M0 supported), field
  policies (`auto`, `source_wins`, `master_wins`, `manual`, `protected`)
  and explicit missing-value semantics (not provided / empty / NULL /
  DELETE).
- Staging pipeline: `stage` → `diff` → `conflicts` → `resolve`
  (source / master / custom value / defer) → `commit`; one transaction per
  batch, idempotent re-commits, optimistic `expected_head` check, deferred
  conflicts keep their baseline and resurface.
- Versioning: trigger-based change capture into `commits`/`changes`,
  `history`, `get_version`, `compare_versions`, non-destructive `revert`,
  whole-file `snapshot`.
- Record linking: `entity_links` with roles, `link`/`unlink`,
  `merge_entities`, `split_entity`, injectable match rules.
- Provenance: every adoption recorded in `field_decisions` (automatic vs
  manual, source, note); `decide()` confirms a value for the `protected`
  policy.
- Validation plugins and built-in link integrity.
- Export management: profiles, prepared runs, diff against the last
  confirmed run, confirm with file hash and database version, run history,
  run-to-run comparison, CSV formula-injection guard.
- Test suite implementing the specification's chapter-14 acceptance
  scenarios (test names carry the scenario numbers).
