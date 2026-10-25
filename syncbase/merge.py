"""Three-way field merge (spec 5.2, 5.3, 5.5, 5.7).

For each synchronized field, four values take part in the decision:

    S0  the source value at the last synchronization
    S1  the source value observed now
    M0  the master value at the last synchronization/reconciliation
    M1  the current master value

S0 and M0 are *not* assumed to be equal: entity resolution or manual fixes
may have made the master differ from the source record on purpose.

The classification follows the table in spec 5.3:

    source changed | master changed | outcome
    ---------------+----------------+---------------------------
    no             | no             | UNCHANGED
    no             | yes            | MASTER_KEPT
    yes            | no             | SOURCE_UPDATE (candidate)
    yes            | yes, equal     | AGREED
    yes            | yes, differs   | CONFLICT

Field policies (spec 5.5) then decide whether a SOURCE_UPDATE is applied
automatically and whether a CONFLICT can be auto-resolved:

    auto         apply source updates, raise conflicts for double changes
    source_wins  apply source updates, resolve conflicts to the source
    master_wins  keep the master value in both cases
    manual       every source change needs a human decision
    protected    a manually confirmed value exists; never overwrite silently

Missing values (spec 5.7) are kept distinct: a key that is absent from the
observed record means "not provided" and is not compared at all; ``""`` is an
explicit empty string; ``None`` is NULL; the ``DELETE`` sentinel is an
explicit instruction to clear the value.
"""


class _Sentinel:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


#: Absent value (field not provided / no recorded base).
MISSING = _Sentinel("MISSING")
#: Explicit instruction from a source to clear the field.
DELETE = _Sentinel("DELETE")

# Field statuses.
UNCHANGED = "unchanged"
NOT_PROVIDED = "not_provided"
MASTER_KEPT = "master_kept"
SOURCE_UPDATE = "source_update"
AGREED = "agreed"
CONFLICT = "conflict"

POLICIES = ("auto", "source_wins", "master_wins", "manual", "protected")


def _effective(v):
    """Map the DELETE sentinel to an empty value for comparison/apply."""
    return "" if v is DELETE else v


def three_way_field(s0, s1, m0, m1, policy="auto", decided=MISSING):
    """Classify one field.  Returns ``(status, apply, value)``.

    ``apply`` is True when the master should be written with ``value``
    without a human decision.  ``status == CONFLICT`` with ``apply False``
    means a decision is required (spec 5.4).  ``decided`` is a manually
    confirmed value (spec 5.5 "protection of manual decisions"); it only
    matters under the ``protected`` policy.
    """
    if policy not in POLICIES:
        raise ValueError("unknown policy: %r" % (policy,))
    if s1 is MISSING:
        return NOT_PROVIDED, False, MISSING
    s1 = _effective(s1)
    if s0 is MISSING and m0 is MISSING:
        # No base recorded (first contact with a pre-linked row, spec 12.4:
        # never fabricate a base).  An empty master accepts the source value;
        # anything else is a conflict.
        if m1 == s1:
            return AGREED, False, s1
        if m1 in ("", None):
            return SOURCE_UPDATE, policy not in ("manual", "master_wins", "protected"), s1
        return CONFLICT, False, s1
    s_changed = s1 != _effective(s0)
    m_changed = m1 != _effective(m0)
    if policy == "protected" and decided is not MISSING and s1 != decided:
        # A manually confirmed value exists and the source disagrees.
        if not s_changed:
            return MASTER_KEPT, False, m1
        return CONFLICT, False, s1
    if not s_changed:
        return (MASTER_KEPT, False, m1) if m_changed else (UNCHANGED, False, m1)
    if not m_changed:
        if policy in ("manual",):
            return CONFLICT, False, s1
        if policy == "master_wins":
            return MASTER_KEPT, False, m1
        return SOURCE_UPDATE, True, s1
    if s1 == m1:
        return AGREED, False, s1
    if policy == "source_wins":
        return CONFLICT, True, s1
    if policy == "master_wins":
        return MASTER_KEPT, False, m1
    return CONFLICT, False, s1


def three_way(s0, s1, m0, m1, policies=None, decided=None):
    """Classify every field provided by the source (keys of ``s1``).

    All four arguments are mappings ``{field: value}``; a missing key means
    MISSING.  Returns ``{field: (status, apply, value)}``.  Fields that the
    source does not provide are not compared (spec 5.7: in delta feeds an
    absent field is never a deletion).
    """
    policies = policies or {}
    decided = decided or {}
    out = {}
    for f in s1:
        out[f] = three_way_field(
            s0.get(f, MISSING), s1.get(f, MISSING),
            m0.get(f, MISSING), m1.get(f, MISSING),
            policies.get(f, "auto"), decided.get(f, MISSING))
    return out
