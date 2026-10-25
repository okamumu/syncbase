"""The README quick-start, verbatim as far as possible, must actually run."""
import os
import tempfile
import unittest

import syncbase


class ReadmeQuickStart(unittest.TestCase):
    def test_quick_start(self):
        with tempfile.TemporaryDirectory() as d:
            base = syncbase.Base(os.path.join(d, "master.sqlite"))
            base.entity("book", fields=["title", "price", "status"])
            base.source("shop", kind="full",
                        on_vanish=("set", "", {"status": "cancelled"}))
            mapping = {"title": ("book", "", "title"), "price": ("book", "", "price")}

            batch = base.stage("shop", {"b1": {"title": "Dune", "price": "9"}}, mapping)
            base.commit(batch)

            with base.transaction("manual fix"):
                base.update("book", 1, {"title": "Dune (1965)"})
            batch = base.stage("shop", {"b1": {"title": "DUNE!", "price": "9"}}, mapping)

            preview = base.diff(batch)
            cs = base.conflicts(batch)
            self.assertEqual([c["field"] for c in cs], ["title"])
            base.resolve(batch, {("b1", "title"): "master"})
            base.commit(batch, expected_head=preview["head"])
            self.assertEqual(base.values("book", 1)["title"], "Dune (1965)")
            base.close()


if __name__ == "__main__":
    unittest.main()
