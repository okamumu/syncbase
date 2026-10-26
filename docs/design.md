# Design summary

This is an English summary of the original specification (`spec.ja.md`,
"汎用データ同期・履歴管理フレームワーク 仕様書（案）" v0.1). Section
numbers refer to that document.

## Three layers (§2)

1. **Input/Output adapters** — convert external data (CSV, form services,
   APIs) to and from the internal model. *Not part of syncbase*; each
   application writes its own adapter and calls `stage()` with flattened
   records.
2. **Data management core** — what syncbase implements: versioning, diff,
   three-way merge, reconciliation (record linking), provenance,
   validation, export history.
3. **Master database** — the application's own relational tables. syncbase
   attaches history triggers to them but does not impose a schema.

## Data categories (§3)

- **Source data** — raw observed records, kept per source as history
  (`source_records`; re-staging never erases past observations).
- **Master data** — the golden records, edited by entity resolution and by
  hand.
- **Synchronization state** — the per-field baselines S0 (last source
  value) and M0 (last reconciled master value), in `sync_states`.
- **Version / export history** — `commits`, `changes`, `export_runs`,
  `export_records`.

## The merge (§5)

Per field: S0/S1/M0/M1 as in the README table. Key points:

- **S0 ≠ M0 is normal** (§5.2): resolution and manual fixes legitimately
  make the master differ from the source record. A source value that did
  not change is therefore *never* a reason to touch the master.
- Field **policies** (§5.5) separate *detecting* a change from *deciding*
  whether to adopt it: `auto`, `source_wins`, `master_wins`, `manual`,
  `protected` (a `decide()`-confirmed value turns silent updates into
  conflicts).
- Missing values (§5.7): absent key = not provided (safe for delta feeds),
  `""` = explicit empty, `None` = NULL, `DELETE` sentinel = explicit clear.
- Records that vanish from a full snapshot are handled by the source's
  `on_vanish` rule; rows that were added directly to the master (no entity
  link) are never touched (§5.6).

## Baseline update timing (§4.4)

Baselines move only when a batch is **committed** (after conflicts are
resolved). The source side and the master side move separately:

- S0 := S1 for every provided field (the source was observed).
- M0 := the master value as written, for fields that were synchronized:
  unchanged, source updates, agreed values and explicitly resolved
  conflicts.
- For a MASTER_KEPT field (master-only edit, source unchanged) M0 is **not**
  advanced. Re-importing the same source is only an observation; it does not
  reconcile the edit. When the source later changes, the edit meets it as a
  conflict instead of being overwritten silently (§5.5). This holds under the
  default `auto` policy; `protected` / manual decisions are a separate,
  additional adoption policy.
- Deferred conflicts keep their old baseline, so unresolved differences
  surface again on the next import. Editing the master never advances a
  source's baseline.

## Versioning (§4)

Every row change — whichever code wrote it — is captured by SQLite
triggers into `changes`, grouped into `commits` by `Base.transaction()`.
`revert` applies the inverse changes as a new commit and refuses when the
touched values have changed since (no history rewriting). Whole-file
snapshots (`Base.snapshot`) are a separate, coarser recovery mechanism.

## Reconciliation (§6)

`entity_links` maps `(source_id, external_key, role)` to
`(entity_type, entity_id)`; one external record may link to several
entities (e.g. a form response = one talk + N authors, flattened into
roles by the adapter). Linking is independent of value updates;
`merge_entities` / `split_entity` repoint links inside one commit.
Match rules are injected per entity (`entity(..., match=fn)`).

## Export (§7)

One-way: a two-way diff against the last *confirmed* run of the same
profile. `prepare_export` snapshots candidate rows without moving the
baseline; `confirm_export` writes the CSV (formula-injection guarded),
records hash, row count and the database version (`commits` head).

## Safety (§11)

- One batch = one transaction (§11.1).
- Optimistic concurrency: `commit(batch, expected_head=...)` refuses when
  the master moved after the preview (§11.2).
- Idempotency: re-committing a batch is a no-op (§11.3).
- Validation plugins: `check(name, fn)` + built-in link integrity (§11.4).

## Principles (§17)

1. Source data and the golden master are separate things.
2. Keep sync baselines for *both* sides.
3. Detecting a change and deciding to adopt it are separate steps.
4. Never overwrite conflicting changes silently.
5. Resolution results and their rationale are traceable.
6. Inputs, outputs and master updates all leave history.
7. The core does not depend on any particular master schema.
8. A conference-management system is the first application and the
   test of generality.
