"""Detaching links from a deleted entity, with a record, and restoring them
on revert (0.2.0).

A row that is no longer valid is deleted together with what points at it
(entity_links, their sync_states baselines, relations), so validate() stays
clean; the detachment is recorded under the same commit and revert puts it
back all-or-nothing.
"""
import os
import tempfile
import unittest

import syncbase

MAPPING = {"title": ("book", "", "title"), "price": ("book", "", "price")}
TABLES = ("book", "person", "entity_links", "sync_states", "relations", "detachments", "commits", "changes")


class DetachFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        b = self.base = syncbase.Base(os.path.join(self.dir.name, "m.sqlite"))
        b.entity("book", fields=["title", "price"])
        b.entity("person", fields=["name"])
        b.source("shop", kind="delta")
        b.commit(b.stage("shop", {"b1": {"title": "Dune", "price": "9"},
                                  "b2": {"title": "Emma", "price": "5"}}, MAPPING))
        with b.transaction("people"):
            self.p = b.insert("person", {"name": "Frank Herbert"})
        b.relate("author", ("book", self.book("b1")), ("person", self.p), reason="test")

    def tearDown(self):
        self.base.close()
        self.dir.cleanup()

    def book(self, key):
        r = self.base.con.execute("SELECT entity_id FROM entity_links WHERE external_key=?", (key,)).fetchone()
        return r[0] if r else None

    def dump(self):
        return {t: [tuple(r) for r in self.base.con.execute("SELECT * FROM %s ORDER BY 1" % t)] for t in TABLES}

    def pointing(self, eid):
        """links/baselines/relations of a book, without ids and timestamps"""
        c = self.base.con
        return (sorted((r["source_id"], r["external_key"], r["role"]) for r in c.execute(
                    "SELECT * FROM entity_links WHERE entity_type='book' AND entity_id=?", (eid,))),
                sorted((r["external_key"], r["field"], r["s0"], r["m0"]) for r in c.execute(
                    "SELECT * FROM sync_states WHERE external_key IN (SELECT external_key FROM entity_links"
                    " WHERE entity_type='book' AND entity_id=?)", (eid,))),
                sorted((r["kind"], r["to_id"], r["reason"]) for r in c.execute(
                    "SELECT * FROM relations WHERE from_type='book' AND from_id=?", (eid,))))

    def delete_b1(self):
        eid = self.book("b1")
        before = self.pointing(eid)
        with self.base.transaction("retire b1") as cid:
            self.base.delete("book", eid, detach=True, reason="retired")
        return eid, before, cid


class Detach(DetachFixture):
    def test_outside_transaction_refused(self):
        before = self.dump()
        with self.assertRaises(RuntimeError):
            self.base.detach("book", self.book("b1"))
        self.assertEqual(self.dump(), before)

    def test_failure_rolls_everything_back(self):
        before = self.dump()
        with self.assertRaises(ValueError):
            with self.base.transaction("retire then fail"):
                self.base.delete("book", self.book("b1"), detach=True)
                self.base.insert("person", {"name": "half-done"})
                raise ValueError("boom")
        self.assertEqual(self.dump(), before)

    def test_delete_detaches_with_record(self):
        eid, before, cid = self.delete_b1()
        self.assertEqual(self.base.validate(), {})                 # no link to a missing entity
        self.assertEqual(self.pointing(eid), ([], [], []))
        d = self.base.detachments("book", eid)
        self.assertEqual((len(d), d[0]["kind"], d[0]["commit_id"], d[0]["reason"]), (1, "detach", cid, "retired"))
        self.assertEqual(len(d[0]["payload"]["links"]), 1)
        self.assertEqual(len(d[0]["payload"]["relations"]), 1)
        self.assertEqual(self.book("b2") is not None, True)        # others untouched

    def test_revert_restores(self):
        eid, before, cid = self.delete_b1()
        self.base.revert(cid)
        self.assertEqual(self.base.values("book", eid)["title"], "Dune")
        self.assertEqual(self.pointing(eid), before)
        self.assertEqual(self.base.validate(), {})
        self.assertEqual([d["kind"] for d in self.base.detachments("book", eid)], ["restore", "detach"])
        # the restored baselines behave as before: same source again is a no-op
        head = self.base.head()
        self.base.commit(self.base.stage("shop", {"b1": {"title": "Dune", "price": "9"}}, MAPPING))
        self.assertEqual(self.base.head(), head)

    def test_revert_change_restores(self):
        eid, before, cid = self.delete_b1()
        ch = self.base.con.execute("SELECT id FROM changes WHERE commit_id=?", (cid,)).fetchone()[0]
        self.base.revert_change(ch)
        self.assertEqual(self.pointing(eid), before)
        self.assertEqual(self.base.validate(), {})

    def test_revert_refused_when_key_taken(self):
        eid, before, cid = self.delete_b1()
        # the same external record comes back and is linked to a new book
        self.base.commit(self.base.stage("shop", {"b1": {"title": "Dune", "price": "9"}}, MAPPING))
        self.assertNotEqual(self.book("b1"), eid)
        state = self.dump()
        for force in (False, True):
            with self.assertRaises(RuntimeError) as cm:
                self.base.revert(cid, force=force)
            self.assertIn("shop/b1", str(cm.exception))
            self.assertEqual(self.dump(), state)                   # nothing half-restored

    def test_revert_refused_when_relation_taken(self):
        # the deleted entity is the target of a relation; the slot (kind, from) is reused meanwhile
        with self.base.transaction("another person"):
            p2 = self.base.insert("person", {"name": "F. Herbert"})
        with self.base.transaction("retire person") as cid:
            self.base.delete("person", self.p, detach=True)
        self.assertEqual(self.base.validate(), {})
        self.base.relate("author", ("book", self.book("b1")), ("person", p2))
        state = self.dump()
        with self.assertRaises(RuntimeError) as cm:
            self.base.revert(cid)
        self.assertIn("relation author", str(cm.exception))
        self.assertEqual(self.dump(), state)

    def test_revert_of_insert_does_not_dangle(self):
        b = self.base.stage("shop", {"b3": {"title": "Kim", "price": "7"}}, MAPPING)
        self.base.commit(b)
        cid = self.base.head()
        eid = self.book("b3")
        self.base.revert(cid)
        self.assertIsNone(self.base.values("book", eid))
        self.assertEqual(self.base.validate(), {})
        self.assertIsNone(self.book("b3"))
        d = self.base.detachments("book", eid)
        self.assertEqual([x["kind"] for x in d], ["detach"])
        # importing it again creates it anew (not "deleted in the master")
        d = self.base.diff(self.base.stage("shop", {"b3": {"title": "Kim", "price": "7"}}, MAPPING))
        self.assertEqual(d["records"][0]["roles"][0]["status"], "new")



class DetachOnly(DetachFixture):
    """detach() without any master change in the same transaction (0.2.0
    regression): the commit must be kept because detachments refer to it."""

    def masters(self):
        return {t: [tuple(r) for r in self.base.con.execute("SELECT * FROM %s ORDER BY 1" % t)]
                for t in ("book", "person")}

    def detach_b1(self):
        eid = self.book("b1")
        before = self.pointing(eid)
        with self.base.transaction("detach only") as cid:
            self.assertIsNotNone(self.base.detach("book", eid, reason="standalone"))
        return eid, before, cid

    def test_1_detach_only_commit_is_kept(self):
        masters = self.masters()
        eid, _, cid = self.detach_b1()
        row = self.base.con.execute("SELECT * FROM commits WHERE id=?", (cid,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["note"], "detach only")
        self.assertEqual(self.base.head(), cid)
        self.assertEqual([d["commit_id"] for d in self.base.detachments("book", eid)], [cid])
        self.assertEqual(self.pointing(eid), ([], [], []))
        self.assertEqual(self.masters(), masters)                 # no master change at all

    def test_2_record_keeps_its_commit_after_unrelated_edit(self):
        eid, _, cid = self.detach_b1()
        with self.base.transaction("unrelated edit") as cid2:
            self.base.update("book", self.book("b2"), {"title": "Emma (1815)"})
        self.assertNotEqual(cid2, cid)
        self.assertEqual([d["commit_id"] for d in self.base.detachments("book", eid)], [cid])
        self.assertEqual(self.base.detachments(commit_id=cid2), [])
        self.assertEqual(self.base.con.execute(
            "SELECT note FROM commits WHERE id=?", (cid,)).fetchone()["note"], "detach only")

    def test_3_revert_restores_without_master_change(self):
        masters = self.masters()
        eid, before, cid = self.detach_b1()
        rid = self.base.revert(cid)
        self.assertEqual(self.pointing(eid), before)               # links, baselines, relations
        self.assertEqual(self.masters(), masters)                  # master values untouched
        self.assertEqual(self.base.validate(), {})
        self.assertEqual([(d["kind"], d["commit_id"]) for d in self.base.detachments("book", eid)],
                         [("restore", rid), ("detach", cid)])
        self.assertEqual(self.base.con.execute(
            "SELECT kind FROM commits WHERE id=?", (rid,)).fetchone()["kind"], "revert")
        self.assertEqual(self.base.con.execute(
            "SELECT COUNT(*) FROM changes WHERE commit_id=?", (rid,)).fetchone()[0], 0)
        # the restored baselines behave as before: same source again is a no-op
        head = self.base.head()
        self.base.commit(self.base.stage("shop", {"b1": {"title": "Dune", "price": "9"}}, MAPPING))
        self.assertEqual(self.base.head(), head)

    def test_4_exception_rolls_back_detach_and_record(self):
        before = self.dump()
        with self.assertRaises(ValueError):
            with self.base.transaction("detach then fail"):
                self.base.detach("book", self.book("b1"))
                raise ValueError("boom")
        self.assertEqual(self.dump(), before)

    def test_5_truly_empty_transaction_is_discarded(self):
        with self.base.transaction("lonely person"):
            lonely = self.base.insert("person", {"name": "nobody points here"})
        before = self.dump()
        with self.base.transaction("nothing"):
            pass
        with self.base.transaction("detach with nothing to detach"):
            self.assertIsNone(self.base.detach("person", lonely))
        self.assertEqual(self.dump(), before)

    def test_6_revert_refused_when_row_gone(self):
        eid, _, cid = self.detach_b1()
        with self.base.transaction("delete it later"):
            self.base.delete("book", eid)
        state = self.dump()
        with self.assertRaises(RuntimeError) as cm:
            self.base.revert(cid)
        self.assertIn("no longer exists", str(cm.exception))
        self.assertEqual(self.dump(), state)                       # nothing half-restored


if __name__ == "__main__":
    unittest.main()
