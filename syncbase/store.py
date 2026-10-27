"""Base: the SQLite store, entity/source registry and commit scope.

Management tables follow the logical data model of spec chapter 9 and are
kept separate from the business (master) tables.  Master tables are plain
relational tables; registering an entity attaches history triggers so that
every row change is recorded in ``changes`` (spec 4.2/4.3), whichever code
path wrote it.
"""
import contextlib
import datetime
import json
import os
import shutil
import sqlite3

# AUTOINCREMENT: the id of a discarded (empty) commit is never handed out again.
_COMMITS_COLUMNS = """(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'manual', batch_id INTEGER, note TEXT NOT NULL DEFAULT '',
    parent INTEGER)"""

# Columns that record a commit id; a migrated database starts numbering after all of them.
_COMMIT_REFS = (("commits", "id"), ("commits", "parent"), ("changes", "commit_id"),
                ("detachments", "commit_id"), ("batches", "base_commit"),
                ("export_runs", "db_version"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'full',
    on_vanish TEXT NOT NULL DEFAULT '"ignore"', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_records (
    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, batch_id INTEGER NOT NULL,
    external_key TEXT NOT NULL, payload TEXT NOT NULL,
    observed_at TEXT NOT NULL, accepted_at TEXT);
CREATE INDEX IF NOT EXISTS source_records_key ON source_records (source_id, external_key);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, mapping TEXT NOT NULL,
    created_at TEXT NOT NULL, base_commit INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'staged');
CREATE TABLE IF NOT EXISTS entity_links (
    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, external_key TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '', entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_id, external_key, role));
CREATE INDEX IF NOT EXISTS entity_links_entity ON entity_links (entity_type, entity_id);
CREATE TABLE IF NOT EXISTS sync_states (
    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, external_key TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '', field TEXT NOT NULL,
    s0 TEXT, m0 TEXT, updated_at TEXT NOT NULL,
    UNIQUE (source_id, external_key, role, field));
CREATE TABLE IF NOT EXISTS commits """ + _COMMITS_COLUMNS + """;
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY, commit_id INTEGER, ts TEXT NOT NULL,
    entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, op TEXT NOT NULL,
    old TEXT, new TEXT);
CREATE INDEX IF NOT EXISTS changes_entity ON changes (entity_type, entity_id);
CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, source_id TEXT NOT NULL,
    external_key TEXT NOT NULL, role TEXT NOT NULL DEFAULT '', field TEXT NOT NULL,
    entity_type TEXT, entity_id INTEGER,
    s0 TEXT, s1 TEXT, m0 TEXT, m1 TEXT,
    resolution TEXT, value TEXT, note TEXT NOT NULL DEFAULT '', resolved_at TEXT,
    UNIQUE (batch_id, external_key, role, field));
CREATE TABLE IF NOT EXISTS field_decisions (
    id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
    field TEXT NOT NULL, value TEXT, manual INTEGER NOT NULL DEFAULT 1,
    source_id TEXT, note TEXT NOT NULL DEFAULT '', decided_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS field_decisions_entity ON field_decisions (entity_type, entity_id, field);
CREATE TABLE IF NOT EXISTS export_profiles (
    profile_id TEXT PRIMARY KEY, definition TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS export_runs (
    id INTEGER PRIMARY KEY, profile_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'prepared',
    prepared_at TEXT NOT NULL, confirmed_at TEXT, actor TEXT NOT NULL DEFAULT '',
    db_version INTEGER NOT NULL, row_count INTEGER NOT NULL,
    file_name TEXT, file_hash TEXT);
CREATE TABLE IF NOT EXISTS export_records (
    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, record_key TEXT NOT NULL, row TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS export_records_run ON export_records (run_id);
CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
    from_type TEXT NOT NULL, from_id INTEGER NOT NULL,
    to_type TEXT NOT NULL, to_id INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    UNIQUE (kind, from_type, from_id));
CREATE INDEX IF NOT EXISTS relations_to ON relations (kind, to_type, to_id);
CREATE TABLE IF NOT EXISTS judgements (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
    a_type TEXT NOT NULL, a_id INTEGER NOT NULL, b_type TEXT NOT NULL, b_id INTEGER NOT NULL,
    verdict TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', compared TEXT,
    actor TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS detachments (
    id INTEGER PRIMARY KEY, commit_id INTEGER, ts TEXT NOT NULL,
    entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
    kind TEXT NOT NULL, ref INTEGER, reason TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS detachments_entity ON detachments (entity_type, entity_id);
CREATE TABLE IF NOT EXISTS sync_context (id INTEGER PRIMARY KEY CHECK (id = 1), commit_id INTEGER);
INSERT OR IGNORE INTO sync_context VALUES (1, NULL);
"""


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def q(name):
    """Quote an SQL identifier."""
    return '"%s"' % str(name).replace('"', '""')


class Entity:
    def __init__(self, name, table, pk, fields, policies, match):
        self.name, self.table, self.pk = name, table, pk
        self.fields = list(fields)
        self.policies = dict(policies or {})
        self.match = match


class Source:
    def __init__(self, source_id, kind, on_vanish):
        self.source_id, self.kind, self.on_vanish = source_id, kind, on_vanish


class Base:
    """One synchronized database: master tables plus management tables."""

    def __init__(self, path, actor="system", backup_dir=None):
        self.path, self.actor = path, actor
        self.backup_dir = backup_dir
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(_SCHEMA)
        self._migrate_commits()
        self.con.execute("UPDATE sync_context SET commit_id = NULL WHERE id = 1")
        self.con.commit()
        self.entities = {}
        self.checks = {}
        self._pending = None            # kept commit of a transaction(commit=False) not yet committed

    def _migrate_commits(self):
        """Rebuild a pre-AUTOINCREMENT ``commits`` table in place (atomic),
        and start numbering after every commit id recorded anywhere, so a
        stale reference to a discarded id never meets a new commit."""
        sql = self.con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='commits'").fetchone()[0]
        if "AUTOINCREMENT" in sql.upper():
            return
        seq = max(self.con.execute("SELECT COALESCE(MAX(%s), 0) FROM %s" % (col, table)).fetchone()[0]
                  for table, col in _COMMIT_REFS)
        self.con.commit()
        self.con.execute("BEGIN")
        try:
            self.con.execute("CREATE TABLE sb_commits_new " + _COMMITS_COLUMNS)
            self.con.execute("INSERT INTO sb_commits_new (id, ts, actor, kind, batch_id, note, parent)"
                             " SELECT id, ts, actor, kind, batch_id, note, parent FROM commits")
            self.con.execute("DROP TABLE commits")
            self.con.execute("ALTER TABLE sb_commits_new RENAME TO commits")
            self.con.execute("DELETE FROM sqlite_sequence WHERE name='commits'")
            self.con.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('commits', ?)", (seq,))
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    # ------------------------------------------------------------ registry

    def entity(self, name, table=None, pk="id", fields=(), policies=None,
               match=None, create=True):
        """Register a master entity type (spec 2.2).

        ``table`` defaults to ``name``.  The table may already exist; with
        ``create=True`` a simple table (INTEGER pk, TEXT fields) is created
        when absent.  History triggers are attached either way.
        """
        table = table or name
        e = Entity(name, table, pk, fields, policies, match)
        if create:
            cols = ", ".join("%s TEXT DEFAULT ''" % q(c) for c in e.fields)
            self.con.execute("CREATE TABLE IF NOT EXISTS %s (%s INTEGER PRIMARY KEY%s)"
                             % (q(table), q(pk), ", " + cols if cols else ""))
        have = [r[1] for r in self.con.execute("PRAGMA table_info(%s)" % q(table))]
        missing = [c for c in e.fields if c not in have]
        for c in missing:
            self.con.execute("ALTER TABLE %s ADD COLUMN %s TEXT DEFAULT ''" % (q(table), q(c)))
        self._triggers(e)
        self.con.commit()
        self.entities[name] = e
        return e

    def source(self, source_id, kind="full", on_vanish="ignore"):
        """Register an input source (spec 5.6).

        ``kind`` is ``"full"`` (each batch is a full snapshot) or ``"delta"``.
        ``on_vanish`` says what to do when a full snapshot no longer contains
        a previously accepted record: ``"ignore"`` or
        ``("set", role, {column: value})`` (e.g. mark as cancelled).  Records
        added directly to the master (no entity link) are never touched.
        """
        self.con.execute(
            "INSERT INTO sources (source_id, kind, on_vanish, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET kind=excluded.kind, on_vanish=excluded.on_vanish",
            (source_id, kind, json.dumps(on_vanish), now()))
        self.con.commit()
        return Source(source_id, kind, on_vanish)

    def get_source(self, source_id):
        r = self.con.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if not r:
            raise KeyError("unknown source: %s" % source_id)
        return Source(r["source_id"], r["kind"], json.loads(r["on_vanish"]))

    # ------------------------------------------------------------ triggers

    def _triggers(self, e):
        obj = lambda alias: "json_object(%s)" % ", ".join(
            "'%s', %s.%s" % (c.replace("'", "''"), alias, q(c)) for c in [e.pk] + e.fields)
        for op, old, new in (("INSERT", "NULL", obj("NEW")),
                             ("UPDATE", obj("OLD"), obj("NEW")),
                             ("DELETE", obj("OLD"), "NULL")):
            tg = "sb_%s_%s" % (e.table, op.lower())
            self.con.execute("DROP TRIGGER IF EXISTS %s" % q(tg))
            self.con.execute(
                "CREATE TRIGGER %s AFTER %s ON %s BEGIN "
                "INSERT INTO changes (commit_id, ts, entity_type, entity_id, op, old, new) "
                "VALUES ((SELECT commit_id FROM sync_context WHERE id=1), "
                "datetime('now','localtime'), '%s', %s.%s, '%s', %s, %s); END"
                % (q(tg), op, q(e.table), e.name.replace("'", "''"),
                   "OLD" if op == "DELETE" else "NEW", q(e.pk), op.lower(), old, new))

    # ------------------------------------------------------------ commits

    def _unconfirmed(self):
        """Ids of commits that exist in this connection but are not
        confirmed history yet: the one a ``transaction()`` is writing, and
        a kept ``transaction(commit=False)`` commit until the caller commits
        (a rollback removes it)."""
        ids = []
        cur = self.con.execute("SELECT commit_id FROM sync_context WHERE id=1").fetchone()[0]
        if cur is not None:
            ids.append(cur)
        if self._pending is not None:
            if self.con.in_transaction:
                ids.append(self._pending)
            else:                       # the caller committed or rolled back
                if not self.con.execute("SELECT 1 FROM commits WHERE id=?", (self._pending,)).fetchone():
                    self._burn(self._pending)
                self._pending = None
        return ids

    def _burn(self, commit_id):
        """Keep a rolled-back commit's id from being handed out again (a
        rollback also undoes the AUTOINCREMENT counter)."""
        self.con.execute("UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name='commits'", (commit_id,))
        self.con.commit()

    def head(self):
        """The latest *confirmed* commit id (the database version, spec 11.2).

        A commit id is confirmed once ``transaction()`` has exited normally,
        the commit was kept (not empty) and the database transaction is
        committed.  Inside an open transaction this is still the previous
        confirmed commit, so versions recorded there (``stage``, ``diff``,
        ``prepare_export``) never point at an id that may be discarded.
        """
        ids = self._unconfirmed() or [0]
        return self.con.execute(
            "SELECT COALESCE(MAX(id), 0) FROM commits WHERE id NOT IN (%s)" % ",".join("?" * len(ids)),
            ids).fetchone()[0]

    def confirmed(self, commit_id):
        """Whether ``commit_id`` is confirmed history (see ``head``).  The id
        yielded by an empty, failed or rolled-back transaction is not, and
        it never will be: ids are not handed out twice."""
        if commit_id in self._unconfirmed():
            return False
        return self.con.execute("SELECT 1 FROM commits WHERE id=?", (commit_id,)).fetchone() is not None

    def _dirty(self):
        """True when an open transaction has written master changes or
        detachments that are not confirmed yet."""
        return any(self.con.execute(
            "SELECT (SELECT COUNT(*) FROM changes WHERE commit_id=?)"
            " + (SELECT COUNT(*) FROM detachments WHERE commit_id=?)", (c, c)).fetchone()[0]
            for c in self._unconfirmed())

    def _autocommit(self):
        """Commit, unless a transaction is open: then join it, so its
        rollback also undoes what was written here."""
        if not self._unconfirmed():
            self.con.commit()

    @contextlib.contextmanager
    def transaction(self, note="", kind="manual", batch_id=None, actor=None, commit=True):
        """Group master changes into one commit (spec 4.2).

        Empty commits are discarded on exit: no recorded master change and
        no detach/restore record (``detachments`` refer to the commit, so a
        commit that only detached links is kept).  On error
        everything is rolled back.  With ``commit=False`` the final
        ``con.commit()`` is skipped, so the caller can inspect the written
        state and then roll back (dry run) or commit itself.
        """
        cur = self.con.execute(
            "INSERT INTO commits (ts, actor, kind, batch_id, note, parent) VALUES (?,?,?,?,?,?)",
            (now(), actor or self.actor, kind, batch_id, note, self.head() or None))
        cid = cur.lastrowid
        self.con.execute("UPDATE sync_context SET commit_id=? WHERE id=1", (cid,))
        try:
            yield cid
            n = self.con.execute(
                "SELECT (SELECT COUNT(*) FROM changes WHERE commit_id=?)"
                " + (SELECT COUNT(*) FROM detachments WHERE commit_id=?)", (cid, cid)).fetchone()[0]
            if not n:
                self.con.execute("DELETE FROM commits WHERE id=?", (cid,))
            self.con.execute("UPDATE sync_context SET commit_id=NULL WHERE id=1")
            if commit:
                self.con.commit()
            elif n:
                self._pending = cid
        except Exception:
            self.con.rollback()
            self.con.execute("UPDATE sync_context SET commit_id=NULL WHERE id=1")
            self._burn(cid)
            raise

    # ------------------------------------------------------------ rows

    def row(self, entity_type, entity_id):
        e = self.entities[entity_type]
        r = self.con.execute("SELECT * FROM %s WHERE %s=?" % (q(e.table), q(e.pk)),
                             (entity_id,)).fetchone()
        return dict(r) if r else None

    def values(self, entity_type, entity_id):
        """Current master values of the registered fields (M1)."""
        r = self.row(entity_type, entity_id)
        if r is None:
            return None
        e = self.entities[entity_type]
        return {f: r.get(f) for f in e.fields}

    def insert(self, entity_type, values):
        e = self.entities[entity_type]
        cols = [c for c in e.fields if c in values]
        cur = self.con.execute(
            "INSERT INTO %s (%s) VALUES (%s)"
            % (q(e.table), ", ".join(q(c) for c in cols), ", ".join("?" * len(cols))),
            [values[c] for c in cols])
        return cur.lastrowid

    def update(self, entity_type, entity_id, values):
        e = self.entities[entity_type]
        cols = [c for c in e.fields if c in values]
        if not cols:
            return
        self.con.execute(
            "UPDATE %s SET %s WHERE %s=?"
            % (q(e.table), ", ".join("%s=?" % q(c) for c in cols), q(e.pk)),
            [values[c] for c in cols] + [entity_id])

    def delete(self, entity_type, entity_id, detach=False, reason=""):
        """Delete one master row.  With ``detach=True`` the links, sync
        baselines and relations pointing at it are first detached with a
        record (``links.detach``), so ``validate()`` stays clean and
        ``revert`` can restore them.  Must then run inside ``transaction()``."""
        e = self.entities[entity_type]
        if detach:
            from .links import detach as _detach
            _detach(self, entity_type, entity_id, reason=reason)
        self.con.execute("DELETE FROM %s WHERE %s=?" % (q(e.table), q(e.pk)), (entity_id,))

    # ------------------------------------------------------------ snapshot

    def snapshot(self, reason=""):
        """Copy the whole database file aside (full restore is a separate
        feature from commit-level revert, spec 4.3)."""
        if not self.backup_dir:
            raise RuntimeError("no backup_dir configured")
        os.makedirs(self.backup_dir, exist_ok=True)
        name = "%s_%s%s.sqlite" % (
            os.path.splitext(os.path.basename(self.path))[0],
            datetime.datetime.now().strftime("%Y%m%d-%H%M%S"),
            "_" + reason if reason else "")
        self.con.commit()
        dst = os.path.join(self.backup_dir, name)
        src = sqlite3.connect(self.path)
        try:
            out = sqlite3.connect(dst)
            with out:
                src.backup(out)
            out.close()
        finally:
            src.close()
        return name

    def close(self):
        self.con.close()
