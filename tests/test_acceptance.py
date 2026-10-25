"""Acceptance scenarios from spec chapter 14, on a toy "book" master.

Numbers in test names refer to the scenario list in the specification.
"""
import os
import tempfile
import unittest

import syncbase

MAPPING = {"title": ("book", "", "title"), "price": ("book", "", "price")}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.base = syncbase.Base(os.path.join(self.dir.name, "m.sqlite"),
                                  backup_dir=os.path.join(self.dir.name, "bk"))
        self.base.entity("book", fields=["title", "price", "status"])
        self.base.source("shop", kind="full",
                         on_vanish=("set", "", {"status": "cancelled"}))

    def tearDown(self):
        self.base.close()
        self.dir.cleanup()

    def sync(self, records, decisions=None):
        b = self.base.stage("shop", records, MAPPING)
        if decisions:
            self.base.resolve(b, decisions)
        return self.base.commit(b)

    def book_id(self, key="b1"):
        for l in (r for r in self.base.con.execute(
                "SELECT * FROM entity_links WHERE external_key=?", (key,))):
            return l["entity_id"]
        raise AssertionError("no link for %s" % key)

    def title(self, key="b1"):
        return self.base.values("book", self.book_id(key))["title"]


class Acceptance(Fixture):
    def test_01_reimport_is_idempotent(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        head = self.base.head()
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        self.assertEqual(self.base.head(), head)            # no new commit

    def test_02_master_only_edit_is_kept(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        with self.base.transaction("manual fix"):
            self.base.update("book", self.book_id(), {"title": "Dune (1965)"})
        s = self.sync({"b1": {"title": "Dune", "price": "9"}})
        self.assertEqual(self.title(), "Dune (1965)")
        self.assertEqual(s["updated"], [])                  # nothing rewritten
        # After reconciliation the master change is acknowledged (spec 4.4).
        b = self.base.stage("shop", {"b1": {"title": "Dune", "price": "9"}}, MAPPING)
        d = self.base.diff(b)
        f = d["records"][0]["roles"][0]["fields"]["title"]
        self.assertEqual(f["status"], syncbase.UNCHANGED)

    def test_03_source_only_change_is_an_update(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        b = self.base.stage("shop", {"b1": {"title": "Dune Messiah", "price": "9"}}, MAPPING)
        d = self.base.diff(b)
        f = d["records"][0]["roles"][0]["fields"]["title"]
        self.assertEqual((f["status"], f["apply"]), (syncbase.SOURCE_UPDATE, True))
        self.base.commit(b)
        self.assertEqual(self.title(), "Dune Messiah")

    def test_04_double_change_is_a_conflict(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        with self.base.transaction("manual fix"):
            self.base.update("book", self.book_id(), {"title": "Dune (fixed)"})
        b = self.base.stage("shop", {"b1": {"title": "DUNE!", "price": "9"}}, MAPPING)
        cs = self.base.conflicts(b)
        self.assertEqual([c["field"] for c in cs], ["title"])
        with self.assertRaises(syncbase.Unresolved):
            self.base.commit(b)
        self.base.resolve(b, {("b1", "title"): "master"})
        self.base.commit(b)
        self.assertEqual(self.title(), "Dune (fixed)")

    def test_05_06_s0_m0_differ_after_linking(self):
        # Link to an entity whose master value already differs from the source.
        with self.base.transaction("seed"):
            eid = self.base.insert("book", {"title": "Frank Herbert's Dune", "price": ""})
        self.base.entities["book"].match = (
            lambda vals, base: eid if "Dune" in (vals.get("title") or "") else None)
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        self.assertEqual(self.title(), "Frank Herbert's Dune")   # kept (not empty)
        # Same source value again: no change reported (spec 14.6).
        b = self.base.stage("shop", {"b1": {"title": "Dune", "price": "9"}}, MAPPING)
        f = self.base.diff(b)["records"][0]["roles"][0]["fields"]["title"]
        self.assertEqual(f["status"], syncbase.UNCHANGED)
        # Master edit after reconciliation plus a source change: conflict (14.5).
        with self.base.transaction("manual fix"):
            self.base.update("book", eid, {"title": "Dune — Herbert"})
        b2 = self.base.stage("shop", {"b1": {"title": "Dune 2", "price": "9"}}, MAPPING)
        f2 = self.base.diff(b2)["records"][0]["roles"][0]["fields"]["title"]
        self.assertEqual(f2["status"], syncbase.CONFLICT)

    def test_07_protected(self):
        self.base.entities["book"].policies["title"] = "protected"
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        self.base.decide("book", self.book_id(), "title", "Dune", "checked against the cover")
        b = self.base.stage("shop", {"b1": {"title": "DUNE", "price": "9"}}, MAPPING)
        f = self.base.diff(b)["records"][0]["roles"][0]["fields"]["title"]
        self.assertEqual(f["status"], syncbase.CONFLICT)     # not silently applied

    def test_08_second_source_conflicts(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        eid = self.book_id()
        self.base.source("import2", kind="full")
        self.base.link("import2", "x9", "book", eid)
        b = self.base.stage("import2", {"x9": {"title": "El Dune", "price": "9"}}, MAPPING)
        f = self.base.diff(b)["records"][0]["roles"][0]["fields"]["title"]
        self.assertEqual(f["status"], syncbase.CONFLICT)     # no base, master not empty

    def test_09_manual_rows_survive_vanishing(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        with self.base.transaction("manual add"):
            self.base.insert("book", {"title": "Hand-made", "price": "0", "status": ""})
        s = self.sync({})                                    # b1 vanished from the feed
        self.assertEqual(s["vanished"], ["b1"])
        self.assertEqual(self.base.values("book", self.book_id())["status"], "cancelled")
        rows = [dict(r) for r in self.base.con.execute("SELECT * FROM book")]
        hand = [r for r in rows if r["title"] == "Hand-made"]
        self.assertEqual(hand[0]["status"], "")              # untouched (spec 5.6)

    def test_10_multi_entity_commit_is_one_transaction(self):
        self.base.entity("author", fields=["name"])
        m = dict(MAPPING, author=("author", "a", "name"))
        b = self.base.stage("shop", {"b1": {"title": "Dune", "price": "9",
                                            "author": "Frank Herbert"}}, m)
        self.base.commit(b)
        cids = {r[0] for r in self.base.con.execute("SELECT DISTINCT commit_id FROM changes")}
        self.assertEqual(len(cids), 1)                       # one commit covers both tables

    def test_11_stale_preview_is_refused(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        b = self.base.stage("shop", {"b1": {"title": "Dune 2", "price": "9"}}, MAPPING)
        head = self.base.diff(b)["head"]
        with self.base.transaction("someone else"):
            self.base.update("book", self.book_id(), {"price": "12"})
        with self.assertRaises(syncbase.StaleView):
            self.base.commit(b, expected_head=head)

    def test_12_recommitting_a_batch_changes_nothing(self):
        b = self.base.stage("shop", {"b1": {"title": "Dune", "price": "9"}}, MAPPING)
        self.base.commit(b)
        head = self.base.head()
        s = self.base.commit(b)
        self.assertEqual((s["created"], s["updated"], self.base.head()), ([], [], head))

    def test_16_provenance_is_tracked(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        with self.base.transaction("manual fix"):
            self.base.update("book", self.book_id(), {"title": "Dune (fixed)"})
        self.sync({"b1": {"title": "DUNE!", "price": "9"}}, {("b1", "title"): "master"})
        prov = self.base.provenance("book", self.book_id(), "title")
        self.assertEqual(prov[0]["manual"], False)           # initial adoption from the source
        self.assertEqual(prov[-1]["manual"], True)           # the human decision
        self.assertIn("master", prov[-1]["note"])

    def test_deferred_conflicts_come_back(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        with self.base.transaction("manual fix"):
            self.base.update("book", self.book_id(), {"title": "Dune (fixed)"})
        b = self.base.stage("shop", {"b1": {"title": "DUNE!", "price": "9"}}, MAPPING)
        self.base.resolve(b, {("b1", "title"): "defer"})
        self.base.commit(b)
        self.assertEqual(self.title(), "Dune (fixed)")       # untouched
        b2 = self.base.stage("shop", {"b1": {"title": "DUNE!", "price": "9"}}, MAPPING)
        cs = self.base.conflicts(b2)                         # still a conflict (spec 4.4)
        self.assertEqual([c["field"] for c in cs], ["title"])


class ExportTests(Fixture):
    def setUp(self):
        super().setUp()
        self.sync({"b1": {"title": "Dune", "price": "9"},
                   "b2": {"title": "Solaris", "price": "7"}})
        self.base.export_profile("books", "SELECT title, price FROM book",
                                 key=["title"], order_by="title")
        self.out = os.path.join(self.dir.name, "books.csv")

    def test_13_14_15_export_flow(self):
        r1 = self.base.prepare_export("books")
        d = self.base.export_diff(r1)
        self.assertEqual(len(d["added"]), 2)                 # first run: everything is new
        # 15: preparing again does not move the baseline.
        r_preview = self.base.prepare_export("books")
        self.assertEqual(len(self.base.export_diff(r_preview)["added"]), 2)
        self.base.confirm_export(r1, self.out)
        self.assertTrue(os.path.exists(self.out))
        # 13: after a change the diff is against the confirmed run.
        with self.base.transaction("price change"):
            self.base.update("book", self.book_id("b1"), {"price": "11"})
        r2 = self.base.prepare_export("books")
        d2 = self.base.export_diff(r2)
        self.assertEqual(d2["against"], r1)
        self.assertEqual(len(d2["changed"]), 1)
        self.assertEqual(d2["changed"][0]["fields"][0]["field"], "price")
        # 14: run history and run-to-run comparison.
        self.base.confirm_export(r2, self.out)
        hist = self.base.export_history("books")
        self.assertEqual([h["status"] for h in hist].count("confirmed"), 2)
        cmp_ = self.base.compare_exports(r1, r2)
        self.assertEqual(len(cmp_["changed"]), 1)

    def test_formula_injection_guard(self):
        with self.base.transaction("evil"):
            self.base.update("book", self.book_id("b1"), {"title": "=cmd()"})
        r = self.base.prepare_export("books")
        self.base.confirm_export(r, self.out)
        with open(self.out, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("'=cmd()", text)


class VersioningTests(Fixture):
    def test_history_and_revert(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        with self.base.transaction("manual fix") as cid:
            self.base.update("book", self.book_id(), {"title": "Dune (fixed)"})
        h = self.base.history("book", self.book_id())
        self.assertEqual(h[0]["note"], "manual fix")
        self.base.revert(cid)
        self.assertEqual(self.title(), "Dune")
        # Reverting is itself a commit; nothing was rewritten.
        self.assertEqual(self.base.commits()[0]["kind"], "revert")

    def test_revert_refuses_stale(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        with self.base.transaction("fix 1") as cid:
            self.base.update("book", self.book_id(), {"title": "A"})
        with self.base.transaction("fix 2"):
            self.base.update("book", self.book_id(), {"title": "B"})
        with self.assertRaises(RuntimeError):
            self.base.revert(cid)                            # value moved on since

    def test_get_version_and_compare(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        v1 = self.base.head()
        with self.base.transaction("manual fix"):
            self.base.update("book", self.book_id(), {"title": "Dune (fixed)"})
        v2 = self.base.head()
        old = self.base.get_version(v1)["book"]
        self.assertEqual(list(old.values())[0]["title"], "Dune")
        diffs = self.base.compare_versions(v1, v2)
        self.assertEqual([d["field"] for d in diffs], ["title"])

    def test_validate_plugin(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        self.base.check("prices", lambda base: [
            r["title"] for r in base.con.execute("SELECT * FROM book WHERE price=''")])
        self.assertEqual(self.base.validate(), {})
        with self.base.transaction("oops"):
            self.base.update("book", self.book_id(), {"price": ""})
        self.assertEqual(self.base.validate(), {"prices": ["Dune"]})


if __name__ == "__main__":
    unittest.main()
