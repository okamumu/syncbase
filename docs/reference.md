# API quick reference

All public API is methods on `syncbase.Base`. Signatures below omit the
implicit `self`/`base`. Section numbers refer to `spec.ja.md`; see
`design.md` for the concepts (S0/S1/M0/M1, policies, baselines).

## Setup (store.py)

### `Base(path, actor="system", backup_dir=None)`
Open (and initialize) the database. `actor` is recorded on commits;
`backup_dir` enables `snapshot()`.

### `entity(name, table=None, pk="id", fields=(), policies=None, match=None, create=True) -> Entity`
Register a master entity type and attach history triggers.
- `table` defaults to `name`; `pk` must be a single INTEGER column.
- `fields`: the synchronized columns. Missing ones are added with
  `ALTER TABLE`; with `create=True` a simple table (INTEGER pk, TEXT
  fields) is created when absent.
- `policies`: `{field: policy}` — `"auto"` (default), `"source_wins"`,
  `"master_wins"`, `"manual"`, `"protected"`.
- `match`: `fn(values, base) -> entity_id or None`, used to propose a
  candidate for unlinked records.

### `source(source_id, kind="full", on_vanish="ignore") -> Source`
Register an input source. `kind` is `"full"` (each batch is a complete
snapshot) or `"delta"`. `on_vanish` is `"ignore"` or
`("set", role, {column: value})` applied to linked entities that
disappear from a full snapshot.

### `transaction(note="", kind="manual", batch_id=None, actor=None)` *(context manager)*
Group master changes into one commit. Yields the commit id; empty
commits are discarded, errors roll everything back. **All manual writes
must happen inside this.**

### Row helpers
- `row(entity_type, entity_id) -> dict | None` — whole row.
- `values(entity_type, entity_id) -> dict | None` — registered fields only (M1).
- `insert(entity_type, values) -> entity_id`
- `update(entity_type, entity_id, values)`
- `delete(entity_type, entity_id)`
- `head() -> int` — current database version (latest commit id).
- `snapshot(reason="") -> filename` — copy the whole file to `backup_dir`.
- `close()`

## Import (staging.py)

### `stage(source_id, records, mapping) -> batch_id`
Store one observed batch. `records` is `{external_key: payload}`;
`mapping` routes payload fields: `{payload_field: (entity_type, role, column)}`.
A payload key that is absent means *not provided* (never compared);
`""` = explicit empty, `None` = NULL, `syncbase.DELETE` = explicit clear.

### `diff(batch_id) -> dict`
Read-only classification against the current master. Returns
`{head, records, vanished, ...}`; each record's roles carry a status
(`new` / `update` / `conflict` / `same` / `deleted_master`) and per-field
details (`status`, `apply`, `value`, `s0`, `s1`, `m0`, `m1`).
Keep `head` for the optimistic check in `commit`.

### `conflicts(batch_id) -> list[dict]`
Pending conflicts with any stored resolutions. Each item:
`external_key`, `role`, `entity_type`, `entity_id`, `field`,
`s0/s1/m0/m1`, `auto` (auto-resolvable by policy), `resolution`, `value`.

### `resolve(batch_id, decisions, note="")`
Record conflict decisions. `decisions` maps
`(external_key, role, field)` — or `(external_key, field)` for the
default role — to `"source"` | `"master"` | `("value", v)` | `"defer"`.

### `commit(batch_id, expected_head=None, note="") -> summary`
Apply the batch in one transaction and advance the baselines
(S0 := S1, M0 := as written). Raises `StaleView` when the master moved
past `expected_head`, `Unresolved` when conflicts lack a decision.
Deferred conflicts keep their baseline and surface again next time.
Re-committing the same batch is a no-op. Returns
`{created, updated, same, vanished, deferred, head}`.

## History (versioning.py)

- `history(entity_type=None, entity_id=None, limit=200) -> list[dict]` —
  change history, newest first, joined with commit metadata.
- `commits(limit=200) -> list[dict]`
- `get_version(commit_id) -> {entity_type: {entity_id: row}}` —
  reconstruct the state as of a commit.
- `compare_versions(v1, v2) -> list[dict]` — field-level differences
  between two commits.
- `revert(commit_id, force=False, note=None) -> new_commit_id` —
  apply the inverse changes as a **new** commit; refuses when touched
  values changed since (unless `force`).
- `revert_change(change_id, force=False, note=None) -> new_commit_id` —
  same, but for a single row of `changes` instead of a whole commit.

## Linking (links.py)

- `match_candidates(entity_type, values)` — run the entity's match rule.
- `link(source_id, external_key, entity_type, entity_id, role="")` —
  create or repoint a link.
- `unlink(source_id, external_key, role="")` — remove the link and its
  sync state.
- `links_of(entity_type, entity_id) -> list[dict]`
- `merge_entities(entity_type, keep, drop, note="")` — repoint links and
  decisions from `drop` to `keep`, delete `drop` (one commit).
- `split_entity(entity_type, entity_id, link_ids, values=None, note="") -> new_id` —
  move the given `entity_links.id` rows to a newly created entity.

## Provenance (provenance.py)

- `provenance(entity_type, entity_id, field=None) -> list[dict]` —
  adoption history (`value`, `manual`, `source_id`, `note`, `decided_at`),
  oldest first.
- `decide(entity_type, entity_id, field, value, note="")` — manually
  confirm a value; under the `protected` policy a differing source value
  then becomes a conflict instead of a silent update.

## Validation (validate.py)

- `check(name, fn)` — register a rule; `fn(base)` returns a list of
  findings.
- `validate() -> {check_name: [finding, ...]}` — run built-in link
  integrity plus all registered rules; empty results are omitted.

## Export (export.py)

- `export_profile(profile_id, query, key, columns=None, order_by=None, encoding="utf-8", dialect="excel", ignore_order=True)` —
  declare a profile. `query` is a SELECT over the master; `key` lists the
  columns that identify a row across runs.
- `prepare_export(profile_id) -> run_id` — snapshot candidate rows as a
  *prepared* run (never moves the baseline).
- `export_diff(run_id) -> {added, removed, changed, same, against}` —
  compare a run against the last **confirmed** run of its profile.
- `confirm_export(run_id, path) -> {file, hash, rows}` — write the CSV
  (formula-injection guarded) and record the run as official.
- `export_history(profile_id) -> list[dict]` — all runs, newest first.
- `compare_exports(run_id_1, run_id_2) -> {added, removed, changed}`

## Module-level names

- `syncbase.DELETE` — sentinel: explicit instruction to clear a field.
- `syncbase.MISSING` — sentinel: no value / not provided (internal).
- `syncbase.StaleView`, `syncbase.Unresolved` — exceptions raised by `commit`.
- `syncbase.three_way_field(s0, s1, m0, m1, policy="auto", decided=MISSING) -> (status, apply, value)`
  and `syncbase.three_way(s0, s1, m0, m1, policies=None, decided=None) -> {field: (status, apply, value)}` —
  the pure merge functions, usable standalone.
- Field statuses: `UNCHANGED`, `NOT_PROVIDED`, `MASTER_KEPT`,
  `SOURCE_UPDATE`, `AGREED`, `CONFLICT`.
