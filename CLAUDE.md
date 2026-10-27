# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
python3 -m unittest discover -s tests              # run all tests
python3 -m unittest tests.test_merge               # one file
python3 -m unittest tests.test_merge.ThreeWayField.test_table_5_3   # one test
docker compose run --rm test                       # tests in Docker (for non-Mac envs)
```

No dependencies to install: the library is Python standard library only (`requires-python >= 3.9`), and that is a deliberate constraint — do not add third-party dependencies. There is no lint/format tooling configured.

## What this is

A framework that syncs external feeds (CSVs, form services) into a "golden" SQLite master database that is *also* edited by hand. Every import is a git-style **three-way merge** per field, with baselines kept for both sides: S0 (source at last sync), S1 (source now), M0 (master at last sync), M1 (master now). S0 ≠ M0 is normal and expected — entity resolution and manual fixes legitimately diverge the master from the source, so an unchanged source value never touches the master. Both sides changed differently ⇒ a recorded conflict a human resolves.

Canonical documents: `docs/spec.ja.md` is the original Japanese specification (section numbers like "spec 5.3" throughout code comments and test names refer to it); `docs/design.md` is the English summary. Tests implement the spec's chapter-14 acceptance scenarios by number.

## Architecture

`syncbase/store.py` defines class `Base` (SQLite connection, entity/source registry, management-table schema, `transaction()` commit scope). The other modules (`staging`, `versioning`, `links`, `provenance`, `validate`, `export`) are written as free functions taking `base` as first argument, and `__init__.py` attaches them to `Base` as methods — so the public API is all methods on `Base`, but each concern lives in its own file.

Key mechanics that span files:

- **History via triggers** (`store.py`): registering an entity attaches SQLite AFTER INSERT/UPDATE/DELETE triggers to the master table, recording every row change into `changes` as JSON — whichever code path wrote it. `Base.transaction()` groups those changes into one row in `commits` by setting `sync_context.commit_id` for the duration; empty commits are discarded. All write paths (manual edits, batch commits, revert, merge_entities) must run inside `transaction()`.
- **Staging flow** (`staging.py`): `stage()` records raw observations into `source_records` and a `batches` row (never erases past observations) → `diff()` classifies read-only against the current master → `resolve()` records per-conflict decisions → `commit()` applies everything in one transaction and only then advances the baselines in `sync_states`. The two sides move separately: S0 := S1 for every provided field; M0 := the master value as written only for fields that were actually synchronized (unchanged, source updates, agreed, explicitly resolved conflicts). For MASTER_KEPT fields (master-only edit, source unchanged) M0 is **not** advanced — re-importing is an observation, not a reconciliation, so a later source change meets the edit as a conflict (spec 4.4/5.5). Deferred conflicts keep their baseline and resurface on the next import. `commit(batch, expected_head=...)` raises `StaleView` if the master moved since the preview.
- **Missing-value semantics** (`merge.py`): an absent key means *not provided* (never compared — delta feeds are safe), `""` is explicit empty, `None` is NULL, the `DELETE` sentinel is an explicit clear. The `MISSING` sentinel is internal "no value". Preserve these distinctions everywhere.
- **Linking** (`links.py`): `entity_links` maps `(source_id, external_key, role)` → `(entity_type, entity_id)`; one external record can map to several entities via roles (the stage `mapping` is `{source_field: (entity_type, role, master_field)}`). Rows with no entity link were added directly to the master and are never touched by `on_vanish` handling.
- **Field policies**: `auto`, `source_wins`, `master_wins`, `manual`, `protected`; a `decide()`-confirmed value (in `field_decisions`) turns silent updates into conflicts.
- **Export** (`export.py`): one-way; diffs are computed against the last *confirmed* run, `prepare_export` never moves that baseline, CSV output is guarded against formula injection.
- **No history rewriting**: `revert` applies inverse changes as a *new* commit and refuses if values moved on since.
- **Detach with a record** (`links.detach`, `delete(detach=True)`): management tables (links, baselines, relations) have no history triggers, so removing them off a deleted row is recorded in `detachments` under the current commit, and `revert` restores them all-or-nothing. Functions that join a caller's transaction must not call `con.commit()`.

Master tables are the application's own schema; syncbase only requires an INTEGER pk and attaches triggers. SQL identifiers are interpolated via `q()` (quoting), values always via `?` parameters — keep that split.
