# syncbase

Sync external data into a master SQLite database, git-style.

`syncbase` is a small, dependency-free framework (Python standard library
only) for the common situation where a "golden" database is fed by external
systems *and* edited by hand: form services, registration CSVs, partner
feeds on one side; deduplication, corrections and manual overrides on the
other. A naive re-import overwrites your fixes; `syncbase` makes every
import a **three-way merge** with conflict detection, keeps a full change
history, and tracks what you exported.

It combines two well-known ideas:

- **Dolt-style versioning** — snapshot, commit, diff, three-way merge,
  conflict detection, version history.
- **MDM-style golden records** — source records, entity resolution, record
  linking, provenance, human override.

## The model in one table

For every synchronized field, four values take part (S0 and M0 are *not*
assumed equal — entity resolution or manual fixes may have made the master
differ from the source on purpose):

| | value |
|---|---|
| S0 | source value at the last sync |
| S1 | source value observed now |
| M0 | master value at the last sync |
| M1 | current master value |

| source changed | master changed | outcome |
|---|---|---|
| no | no | unchanged |
| no | yes | **master change is kept** |
| yes | no | source update (applied per policy) |
| yes | yes, equal | agreed |
| yes | yes, differs | **conflict — a human decides** |

## Quick start

```python
import syncbase

base = syncbase.Base("master.sqlite")
base.entity("book", fields=["title", "price", "status"])
base.source("shop", kind="full", on_vanish=("set", "", {"status": "cancelled"}))

mapping = {"title": ("book", "", "title"), "price": ("book", "", "price")}

# First import: creates the row, links it, records the sync baseline.
batch = base.stage("shop", {"b1": {"title": "Dune", "price": "9"}}, mapping)
base.commit(batch)

# You fix the title by hand; the shop renames it too, differently.
with base.transaction("manual fix"):
    base.update("book", 1, {"title": "Dune (1965)"})
batch = base.stage("shop", {"b1": {"title": "DUNE!", "price": "9"}}, mapping)

preview = base.diff(batch)                        # read-only classification
for c in base.conflicts(batch):                   # both sides changed "title"
    print(c["field"], ":", c["m1"], "vs", c["s1"])
base.resolve(batch, {("b1", "title"): "master"})  # keep your fix
base.commit(batch, expected_head=preview["head"])
```

Everything a batch does — creates, updates, vanishing records — is applied
in **one commit**, recorded field-by-field in the change history, and the
sync baselines (S0/M0) are advanced only at that moment. Deferred conflicts
keep their baseline and surface again on the next import.

## Master tables: yours or auto-created

syncbase does not impose a schema on the master database. `entity()`
attaches history triggers to an existing table of your own design
(`table=` and `pk=` if the names differ; the primary key must be a single
INTEGER column). When the table does not exist, `create=True` (the
default) creates a simple one — INTEGER primary key, all registered
fields as `TEXT DEFAULT ''` — and later adds missing registered fields
with `ALTER TABLE`. If you want real types, NOT NULL or CHECK
constraints, create the table yourself first; only the columns listed in
`fields` are synchronized, merged and recorded in history, any others are
left alone.

## API overview (spec chapter 8)

A quick reference with signatures and return values is in
[`docs/reference.md`](docs/reference.md).

| area | methods on `Base` |
|---|---|
| input | `stage(source, records, mapping)` · `diff(batch)` · `conflicts(batch)` · `resolve(batch, decisions)` · `commit(batch, expected_head=None)` |
| history | `history(entity_type, entity_id)` · `commits()` · `get_version(commit)` · `compare_versions(v1, v2)` · `revert(commit)` · `snapshot(reason)` |
| linking | `match_candidates` · `link` · `unlink` · `detach` · `detachments` · `links_of` · `merge_entities` · `split_entity` |
| provenance | `provenance(entity_type, id, field)` · `decide(...)` (manually confirm a value) |
| validation | `check(name, fn)` · `validate()` |
| export | `export_profile` · `prepare_export` · `export_diff` · `confirm_export` · `export_history` · `compare_exports` |

Field policies (`entity(..., policies={field: policy})`): `auto`,
`source_wins`, `master_wins`, `manual`, `protected` (a manually confirmed
value is never overwritten silently).

Missing-value semantics: a key absent from a staged record means *not
provided* (never compared — safe for delta feeds); `""` is an explicit empty
string; `None` is NULL; `syncbase.DELETE` is an explicit clear instruction.

## Guarantees

- Re-importing the same data is a no-op (idempotent).
- Master-only edits survive re-imports; records added directly to the
  master are never cancelled by a feed that doesn't know them.
- A commit against a preview of an older database version is refused
  (`StaleView`) — pass `expected_head=preview["head"]`.
- `revert` applies the inverse as a *new* commit and refuses when the
  values moved on since (no history rewriting).
- Deleting with `delete(..., detach=True)` takes the links, baselines and
  relations off the row with a record in the same commit, so no live link
  points at a missing entity; `revert` puts them back all-or-nothing, or
  refuses if their place is taken. Tables whose rows get deleted should use
  `INTEGER PRIMARY KEY AUTOINCREMENT` so a deleted id is never handed out
  again (SQLite otherwise reuses the largest one).
- CSV exports are one-way: diffs are computed against the last *confirmed*
  run; previews never move the baseline. Output is guarded against CSV
  formula injection.

## Tests

The test suite implements the acceptance scenarios of the specification
(`docs/spec.ja.md`, chapter 14 — test names carry the scenario numbers):

```sh
python3 -m unittest discover -s tests        # directly
docker compose run --rm test                 # or in Docker
```

## Status

Version 0.1 — extracted from a working conference-management system
(its import pipeline was the first application of this design). The
original specification (in Japanese) is in `docs/spec.ja.md`; an English
design summary is in `docs/design.md`.

## License

MIT — see [LICENSE](LICENSE).
