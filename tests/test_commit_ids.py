"""When a commit id may be referred to as confirmed history.

A commit id is *confirmed* once ``transaction()`` has exited normally, the
commit was kept (not empty) and the database transaction is committed (for
``transaction(commit=False)``: when the caller commits).  Two separate
guarantees follow:

1. Ids are never handed out twice: a discarded empty commit's id is not
   reused by a later commit (``commits`` uses AUTOINCREMENT; older
   databases are migrated on open).
2. Nothing records an unconfirmed id as a database version: ``head()``
   returns the latest confirmed commit, so ``stage``/``diff``/
   ``prepare_export`` inside an open transaction record a confirmed one.
"""
import os
import sqlite3
import tempfile
import unittest

import syncbase

MAPPING = {"title": ("book", "", "title")}


class CommitIds(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "m.sqlite")
        self.open()

    def open(self):
        b = self.base = syncbase.Base(self.path)
        b.entity("book", fields=["title"])
        b.source("shop", kind="delta")
        b.export_profile("books", "SELECT id, title FROM book", key=["id"])
        return b

    def tearDown(self):
        self.base.close()
        self.dir.cleanup()

    def seed(self):
        self.base.commit(self.base.stage("shop", {"b1": {"title": "Dune"}}, MAPPING))
        return self.base.head()

    def note(self, cid):
        r = self.base.con.execute("SELECT note FROM commits WHERE id=?", (cid,)).fetchone()
        return r["note"] if r else None

    def unrelated_edit(self):
        with self.base.transaction("unrelated edit") as cid:
            self.base.update("book", 1, {"title": "Dune (1965)"})
        return cid

    # -- 1. ids are never handed out twice -----------------------------------

    def test_empty_commit_id_is_not_reused(self):
        self.seed()
        with self.base.transaction("empty") as empty:
            pass
        cid = self.unrelated_edit()
        self.assertGreater(cid, empty)
        self.assertFalse(self.base.confirmed(empty))

    def test_failed_transaction_id_is_not_reused(self):
        self.seed()
        with self.assertRaises(ValueError):
            with self.base.transaction("fails") as bad:
                self.base.update("book", 1, {"title": "half-done"})
                raise ValueError("boom")
        cid = self.unrelated_edit()
        self.assertGreater(cid, bad)
        self.assertFalse(self.base.confirmed(bad))

    def test_rolled_back_dry_run_id_is_not_reused(self):
        self.seed()
        with self.base.transaction("dry run", commit=False) as dry:
            self.base.update("book", 1, {"title": "maybe"})
        self.base.con.rollback()
        cid = self.unrelated_edit()
        self.assertGreater(cid, dry)
        self.assertFalse(self.base.confirmed(dry))

    def test_old_commits_table_is_migrated(self):
        h = self.seed()
        self.base.close()
        # recreate the pre-0.2.0 table (no AUTOINCREMENT) and a stale
        # reference to a discarded id, as left by the detach-only bug
        con = sqlite3.connect(self.path)
        con.executescript(
            "ALTER TABLE commits RENAME TO c_old;"
            "CREATE TABLE commits (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '',"
            " kind TEXT NOT NULL DEFAULT 'manual', batch_id INTEGER, note TEXT NOT NULL DEFAULT '', parent INTEGER);"
            "INSERT INTO commits SELECT * FROM c_old; DROP TABLE c_old;")
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
            con.execute("DELETE FROM sqlite_sequence WHERE name='commits'")
        con.execute("INSERT INTO detachments (commit_id, ts, entity_type, entity_id, kind, payload)"
                    " VALUES (?, '', 'book', 1, 'detach', '{}')", (h + 5,))
        con.commit()
        con.close()
        self.open()
        sql = self.base.con.execute("SELECT sql FROM sqlite_master WHERE name='commits'").fetchone()[0]
        self.assertIn("AUTOINCREMENT", sql.upper())
        self.assertEqual(self.note(h), "sync batch 1 from shop")          # rows kept
        self.assertGreater(self.unrelated_edit(), h + 5)                 # never collides with a stale reference

    # -- 2. no unconfirmed id is recorded as a version ------------------------

    def test_stage_in_empty_transaction_records_confirmed_version(self):
        h = self.seed()
        with self.base.transaction("stage inside") as cid:
            self.assertEqual(self.base.head(), h)
            b = self.base.stage("shop", {"b1": {"title": "Dune"}}, MAPPING)
            self.assertEqual(self.base.diff(b)["head"], h)
        base_commit = lambda: self.base.con.execute(
            "SELECT base_commit FROM batches WHERE id=?", (b,)).fetchone()[0]
        self.assertEqual(base_commit(), h)
        self.assertFalse(self.base.confirmed(cid))                        # empty: discarded
        self.assertTrue(self.base.confirmed(h))
        later = self.unrelated_edit()
        self.assertNotEqual(later, h)
        self.assertEqual(base_commit(), h)
        self.assertEqual(self.note(h), "sync batch 1 from shop")          # still the same commit

    def test_prepare_export_in_empty_transaction_records_confirmed_version(self):
        h = self.seed()
        with self.base.transaction("export inside") as cid:
            run = self.base.prepare_export("books")
        version = lambda: self.base.con.execute(
            "SELECT db_version FROM export_runs WHERE id=?", (run,)).fetchone()[0]
        self.assertEqual(version(), h)
        self.assertFalse(self.base.confirmed(cid))
        later = self.unrelated_edit()
        self.assertNotEqual(later, h)
        self.assertEqual(version(), h)
        self.assertEqual(self.note(h), "sync batch 1 from shop")

    def test_prepare_export_refused_with_uncommitted_changes(self):
        h = self.seed()
        runs = len(self.base.export_history("books"))
        with self.assertRaises(RuntimeError):
            with self.base.transaction("edit then export"):
                self.base.update("book", 1, {"title": "half-done"})
                self.base.prepare_export("books")
        self.assertEqual(self.base.head(), h)
        self.assertEqual(self.base.values("book", 1)["title"], "Dune")
        self.assertEqual(len(self.base.export_history("books")), runs)

    def test_stage_rolls_back_with_its_transaction(self):
        h = self.seed()
        batches = self.base.con.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        commits = len(self.base.commits())
        with self.assertRaises(ValueError):
            with self.base.transaction("stage then fail"):
                self.base.stage("shop", {"b1": {"title": "Dune"}}, MAPPING)
                raise ValueError("boom")
        self.assertEqual(self.base.con.execute("SELECT COUNT(*) FROM batches").fetchone()[0], batches)
        self.assertEqual(len(self.base.commits()), commits)               # no orphan empty commit
        self.assertEqual(self.base.head(), h)

    def test_dry_run_commit_is_unconfirmed_until_caller_commits(self):
        h = self.seed()
        with self.base.transaction("dry run", commit=False) as cid:
            self.base.update("book", 1, {"title": "maybe"})
        self.assertEqual(self.base.head(), h)
        self.assertFalse(self.base.confirmed(cid))
        self.base.con.rollback()
        self.assertEqual(self.base.head(), h)
        self.assertFalse(self.base.confirmed(cid))
        with self.base.transaction("kept", commit=False) as cid2:
            self.base.update("book", 1, {"title": "yes"})
        self.assertFalse(self.base.confirmed(cid2))
        self.base.con.commit()
        self.assertEqual(self.base.head(), cid2)
        self.assertTrue(self.base.confirmed(cid2))


if __name__ == "__main__":
    unittest.main()
