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
        # Re-importing the same source does not reconcile the master-only
        # edit (spec 4.4, 5.5): it stays MASTER_KEPT, not UNCHANGED.
        b = self.base.stage("shop", {"b1": {"title": "Dune", "price": "9"}}, MAPPING)
        d = self.base.diff(b)
        f = d["records"][0]["roles"][0]["fields"]["title"]
        self.assertEqual(f["status"], syncbase.MASTER_KEPT)

    def test_02b_master_only_edit_survives_reimports_until_source_changes(self):
        # 1. source and master both A
        self.sync({"b1": {"title": "A", "price": "9"}})
        # 2. master only -> B
        with self.base.transaction("manual fix"):
            self.base.update("book", self.book_id(), {"title": "B"})
        # 3. the same source A, imported several times: B is kept every time
        for _ in range(3):
            b = self.base.stage("shop", {"b1": {"title": "A", "price": "9"}}, MAPPING)
            f = self.base.diff(b)["records"][0]["roles"][0]["fields"]["title"]
            self.assertEqual(f["status"], syncbase.MASTER_KEPT)
            self.base.commit(b)
            self.assertEqual(self.title(), "B")
        st = self.base.con.execute(
            "SELECT s0, m0 FROM sync_states WHERE field='title'").fetchone()
        self.assertEqual((st["s0"], st["m0"]), ('"A"', '"A"'))   # observed A; M0 not moved to B
        # 4. source changes to C: B vs C is a conflict (no protected policy involved)
        self.assertEqual(self.base.entities["book"].policies.get("title"), None)
        b = self.base.stage("shop", {"b1": {"title": "C", "price": "9"}}, MAPPING)
        f = self.base.diff(b)["records"][0]["roles"][0]["fields"]["title"]
        self.assertEqual((f["status"], f["apply"]), (syncbase.CONFLICT, False))
        with self.assertRaises(syncbase.Unresolved):
            self.base.commit(b)

    def _title_conflict_b_vs_c(self):
        """Source A/A synced, master edited to B, source then changes to C.
        Returns the staged batch after checking it holds exactly the
        expected B-vs-C conflict on ``title``."""
        self.sync({"b1": {"title": "A", "price": "9"}})
        with self.base.transaction("manual fix"):
            self.base.update("book", self.book_id(), {"title": "B"})
        self.sync({"b1": {"title": "A", "price": "9"}})          # observed, not reconciled
        b = self.base.stage("shop", {"b1": {"title": "C", "price": "9"}}, MAPPING)
        cs = self.base.conflicts(b)
        self.assertEqual(len(cs), 1)
        c = cs[0]
        self.assertEqual((c["external_key"], c["role"], c["field"]), ("b1", "", "title"))
        self.assertEqual((c["s0"], c["s1"], c["m0"], c["m1"]), ("A", "C", "A", "B"))
        self.assertFalse(c["auto"])
        self.assertIsNone(c["resolution"])
        return b

    def _assert_reimport_keeps(self, source_title, expected, times=3):
        for _ in range(times):
            b = self.base.stage("shop", {"b1": {"title": source_title, "price": "9"}}, MAPPING)
            self.assertEqual(self.base.conflicts(b), [])
            f = self.base.diff(b)["records"][0]["roles"][0]["fields"]["title"]
            self.assertEqual(f["status"], syncbase.UNCHANGED)
            self.base.commit(b)
            self.assertEqual(self.title(), expected)

    def test_02c_conflict_resolved_to_master_is_kept_on_reimport(self):
        b = self._title_conflict_b_vs_c()
        self.base.resolve(b, {("b1", "title"): "master"})
        self.base.commit(b)
        self.assertEqual(self.title(), "B")
        st = self.base.con.execute(
            "SELECT s0, m0 FROM sync_states WHERE field='title'").fetchone()
        self.assertEqual((st["s0"], st["m0"]), ('"C"', '"B"'))   # both sides reconciled
        self._assert_reimport_keeps("C", "B")

    def test_02d_conflict_resolved_to_source_is_kept_on_reimport(self):
        b = self._title_conflict_b_vs_c()
        self.base.resolve(b, {("b1", "title"): "source"})
        self.base.commit(b)
        self.assertEqual(self.title(), "C")
        st = self.base.con.execute(
            "SELECT s0, m0 FROM sync_states WHERE field='title'").fetchone()
        self.assertEqual((st["s0"], st["m0"]), ('"C"', '"C"'))   # both sides reconciled
        self._assert_reimport_keeps("C", "C")

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


class RelationTests(Fixture):
    """Master-to-master record linking and judgement history (spec 6.1/6.4)."""

    def setUp(self):
        super().setUp()
        self.base.entity("person", fields=["name"])
        with self.base.transaction("seed"):
            self.p1 = self.base.insert("person", {"name": "F. Herbert"})
            self.p2 = self.base.insert("person", {"name": "Frank Herbert"})
            self.a1 = self.base.insert("book", {"title": "Dune"})

    def test_relate_and_repoint(self):
        self.base.relate("author", ("book", self.a1), ("person", self.p1), reason="same e-mail")
        r = self.base.relation_of("author", ("book", self.a1))
        self.assertEqual((r["to_id"], r["reason"]), (self.p1, "same e-mail"))
        self.base.relate("author", ("book", self.a1), ("person", self.p2), reason="reassigned")
        self.assertEqual(self.base.relation_of("author", ("book", self.a1))["to_id"], self.p2)
        self.assertEqual(len(self.base.relations("author", ("person", self.p2))), 1)

    def test_judgements_latest_wins_and_reopen(self):
        a, b = ("person", self.p1), ("person", self.p2)
        self.base.judge("person", a, b, "deferred", reason="not sure yet")
        self.assertEqual(self.base.pair_verdicts("person")[frozenset((a, b))][0], "deferred")
        self.base.judge("person", a, b, "different", reason="two people, same name")
        self.assertEqual(self.base.pair_verdicts("person")[frozenset((a, b))][0], "different")
        self.base.judge("person", a, b, "reopen")
        self.assertNotIn(frozenset((a, b)), self.base.pair_verdicts("person"))
        self.assertEqual(len(self.base.judgements("person", a)), 3)   # history is append-only

    def test_merge_repoints_relations(self):
        self.base.relate("author", ("book", self.a1), ("person", self.p1))
        self.base.merge_entities("person", self.p2, self.p1)
        self.assertEqual(self.base.relation_of("author", ("book", self.a1))["to_id"], self.p2)

    def test_merge_repoints_from_side(self):
        # The dropped record's own relation moves to the kept one...
        with self.base.transaction("seed"):
            p3 = self.base.insert("person", {"name": "supervisor"})
        self.base.relate("mentor", ("person", self.p1), ("person", p3))
        self.base.merge_entities("person", self.p2, self.p1)
        self.assertEqual(self.base.relation_of("mentor", ("person", self.p2))["to_id"], p3)

    def test_merge_from_side_keep_wins_on_collision(self):
        # ...unless the kept record already has one of the same kind.
        with self.base.transaction("seed"):
            p3 = self.base.insert("person", {"name": "mentor of keep"})
            p4 = self.base.insert("person", {"name": "mentor of drop"})
        self.base.relate("mentor", ("person", self.p2), ("person", p3))
        self.base.relate("mentor", ("person", self.p1), ("person", p4))
        self.base.merge_entities("person", self.p2, self.p1)
        self.assertEqual(self.base.relation_of("mentor", ("person", self.p2))["to_id"], p3)
        self.assertEqual(len(self.base.relations("mentor")), 1)

    def test_merge_drops_self_relation(self):
        # drop pointed at keep: repointing would make it point at itself.
        self.base.judge("person", ("person", self.p1), ("person", self.p2), "same")
        self.base.relate("same_as", ("person", self.p1), ("person", self.p2))
        self.base.merge_entities("person", self.p2, self.p1)
        self.assertIsNone(self.base.relation_of("same_as", ("person", self.p2)))

    def test_unrelate(self):
        self.base.relate("author", ("book", self.a1), ("person", self.p1))
        self.base.unrelate("author", ("book", self.a1))
        self.assertIsNone(self.base.relation_of("author", ("book", self.a1)))

    def test_judge_rejects_unknown_verdict(self):
        with self.assertRaises(ValueError):
            self.base.judge("person", ("person", self.p1), ("person", self.p2), "maybe")

    def test_validate_reports_dangling_relations(self):
        self.base.relate("author", ("book", self.a1), ("person", self.p1))
        with self.base.transaction("remove person"):
            self.base.delete("person", self.p1)
        findings = self.base.validate().get("relations", [])
        self.assertTrue(any("person/%d does not exist" % self.p1 in f for f in findings))


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

    def test_revert_single_change(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        with self.base.transaction("manual fix"):
            self.base.update("book", self.book_id(), {"title": "A"})
            self.base.update("book", self.book_id(), {"price": "1"})
        ch = [h for h in self.base.history("book", self.book_id())
              if h["op"] == "update" and '"price":"1"' in h["new"]][0]
        self.base.revert_change(ch["id"])
        v = self.base.values("book", self.book_id())
        self.assertEqual((v["title"], v["price"]), ("A", "9"))   # only the price reverted

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


class Transaction(Fixture):
    def test_rollback_on_error(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        head = self.base.head()
        with self.assertRaises(RuntimeError):
            with self.base.transaction("broken"):
                self.base.update("book", self.book_id(), {"title": "X"})
                raise RuntimeError("boom")
        self.assertEqual(self.title(), "Dune")              # change rolled back
        self.assertEqual(self.base.head(), head)            # no commit left behind

    def test_commit_false_lets_caller_roll_back(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        head = self.base.head()
        with self.base.transaction("dry run", commit=False):
            self.base.update("book", self.book_id(), {"title": "X"})
        self.assertEqual(self.title(), "X")                 # visible before rollback
        self.base.con.rollback()
        self.assertEqual(self.title(), "Dune")
        self.assertEqual(self.base.head(), head)


class RelationsCommitFlag(Fixture):
    def test_relate_and_judge_commit_false(self):
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        with self.base.transaction("link work", commit=False):
            self.base.relate("person", ("book", 1), ("book", 2), commit=False)
            self.base.judge("person", ("book", 1), ("book", 2), "same", commit=False)
        self.base.con.rollback()
        self.assertEqual(self.base.relations("person"), [])
        self.assertEqual(self.base.judgements("person"), [])


class RunRows(Fixture):
    def test_run_rows_public(self):
        from syncbase import export
        self.sync({"b1": {"title": "Dune", "price": "9"}})
        export.export_profile(self.base, "books", "select title, price from book", ["title"])
        run = export.prepare_export(self.base, "books")
        rows = self.base.run_rows(run)
        self.assertEqual(rows, [{"title": "Dune", "price": "9"}])
        self.assertNotIn("_pos", rows[0])
