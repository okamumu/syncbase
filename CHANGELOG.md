# Changelog

## 0.2.0 — 2026-10-28

- **Detach with a record** (`links.detach`, `Base.detach`): inside a
  transaction, save an entity's `entity_links`, their `sync_states`
  baselines and the relations from/to it in the new management table
  `detachments`, then remove them. It never commits, so it joins the
  caller's transaction and is rolled back with it. `Base.delete(...,
  detach=True)` does this before deleting the row; `validate()` stays clean.
- **Restore on revert**: `revert` / `revert_change` re-attach what was
  detached when they re-insert a deleted row, and record a `restore`.
  All-or-nothing: refused (even with `force`) if a link key, baseline or
  relation slot is now taken by something else.
- **Behaviour change**: undoing an insert now deletes the row with
  `detach=True`. Before, the link was left pointing at the missing row
  (`validate()` reported it, and re-importing the record showed it as
  deleted in the master); now re-importing creates it anew.
- `detachments(...)`: read the detach/restore records.

## 0.1.3 — 2026-10-27

- **Baseline fix (spec 4.4, 5.5)**: committing a batch no longer advances
  M0 for MASTER_KEPT fields. A master-only edit used to be treated as
  reconciled as soon as the same source was imported again, so a later
  source change overwrote it silently. Now only S0 (the observation) moves;
  the edit stays MASTER_KEPT across re-imports and a source change becomes
  a conflict. Explicit resolutions still advance both sides. No policy
  setting is needed; `protected` remains a separate adoption policy.
- **Behaviour change with several sources**: a value written by another
  source looks like a master-only edit (MASTER_KEPT) to this source, so its
  M0 no longer moves either. When this source later changes the field, the
  other source's update is protected and the change surfaces as a conflict
  instead of overwriting it — expect more conflicts than before in
  multi-source setups.
- Regression tests: sync A/A → master B → re-import A (several times, B
  kept) → source C (conflict); and, as separate tests, resolving that
  conflict to the master and to the source, each kept with no new conflict
  over repeated re-imports.

## 0.1.2 — 2026-10-26

- `relate` / `judge`: `commit=` flag so they can take part in a caller's
  transaction (dry runs included).
- `run_rows(base, run_id)`: public accessor for a run's rows in output
  order, without the internal `_pos` field (also bound as `Base.run_rows`).
- `Base.transaction(commit=False)`: skip the final `con.commit()` so the
  caller can inspect the written state and roll back (dry run) or commit
  itself.  Default behaviour is unchanged.

## 0.1.1 — 2026-10-25

- `revert_change`: undo a single recorded change as a new commit
  (finer-grained than `revert`).
- Relations: master-to-master resolution pointers (`relate`, `unrelate`,
  `relation_of`, `relations`) — one pointer per `(kind, from)`.
- Judgements: append-only resolution judgement history (`judge`,
  `judgements`, `pair_verdicts`) with verdicts `same` / `different` /
  `deferred` / `reopen`; recording `different` keeps settled pairs from
  being proposed again.
- `merge_entities` repoints relations (keep side wins on a `(kind, from)`
  collision; self-pointing relations are dropped).
- `validate()` reports relations whose endpoints no longer exist.
- CI on GitHub Actions; API quick reference in `docs/reference.md`.

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
