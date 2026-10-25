"""Unit tests for the three-way field merge (spec 5.3, 5.5, 5.7)."""
import unittest

from syncbase import merge


def f(s0, s1, m0, m1, policy="auto", decided=merge.MISSING):
    return merge.three_way_field(s0, s1, m0, m1, policy, decided)


class ThreeWayField(unittest.TestCase):
    def test_table_5_3(self):
        # source unchanged / master unchanged
        self.assertEqual(f("a", "a", "x", "x")[0], merge.UNCHANGED)
        # source unchanged / master changed -> keep master
        self.assertEqual(f("a", "a", "x", "y")[0], merge.MASTER_KEPT)
        # source changed / master unchanged -> update candidate, auto-applied
        st, ap, v = f("a", "b", "x", "x")
        self.assertEqual((st, ap, v), (merge.SOURCE_UPDATE, True, "b"))
        # both changed to the same value
        self.assertEqual(f("a", "b", "x", "b")[0], merge.AGREED)
        # both changed, different values
        st, ap, _ = f("a", "b", "x", "y")
        self.assertEqual((st, ap), (merge.CONFLICT, False))

    def test_s0_m0_may_differ(self):
        # S0 != M0 (spec 5.2): same source value again is not a change.
        self.assertEqual(f("F. Herbert", "F. Herbert", "Frank Herbert", "Frank Herbert")[0],
                         merge.UNCHANGED)

    def test_policies(self):
        self.assertEqual(f("a", "b", "x", "y", "source_wins"), (merge.CONFLICT, True, "b"))
        self.assertEqual(f("a", "b", "x", "y", "master_wins")[0], merge.MASTER_KEPT)
        self.assertEqual(f("a", "b", "x", "x", "master_wins")[0], merge.MASTER_KEPT)
        self.assertEqual(f("a", "b", "x", "x", "manual")[0], merge.CONFLICT)

    def test_protected(self):
        # A manually confirmed value blocks silent source updates.
        st, ap, _ = f("a", "b", "x", "x", "protected", decided="x")
        self.assertEqual((st, ap), (merge.CONFLICT, False))
        # Source agreeing with the confirmed value is fine.
        self.assertEqual(f("a", "x", "x", "x", "protected", decided="x")[0],
                         merge.SOURCE_UPDATE)

    def test_missing_values(self):
        # Not provided: never compared (spec 5.7).
        self.assertEqual(f("a", merge.MISSING, "x", "x")[0], merge.NOT_PROVIDED)
        # Explicit empty string is a real change.
        self.assertEqual(f("a", "", "a", "a")[0], merge.SOURCE_UPDATE)
        # DELETE clears the value.
        st, ap, v = f("a", merge.DELETE, "a", "a")
        self.assertEqual((st, v), (merge.SOURCE_UPDATE, ""))

    def test_no_base(self):
        # No recorded base (spec 12.4): empty master accepts, filled master conflicts.
        self.assertEqual(f(merge.MISSING, "b", merge.MISSING, "")[:1], (merge.SOURCE_UPDATE,))
        self.assertEqual(f(merge.MISSING, "b", merge.MISSING, "y")[0], merge.CONFLICT)
        self.assertEqual(f(merge.MISSING, "b", merge.MISSING, "b")[0], merge.AGREED)

    def test_dict_form(self):
        out = merge.three_way({"a": "1"}, {"a": "2", "b": "x"}, {"a": "1"}, {"a": "1", "b": ""})
        self.assertEqual(out["a"][0], merge.SOURCE_UPDATE)
        self.assertEqual(out["b"][0], merge.SOURCE_UPDATE)   # no base, empty master


if __name__ == "__main__":
    unittest.main()
