"""What a space knows of the changes it applied (``latet.server.spaces._Counts``): every count up to the one it applied
last in a row, a count never heard holding it back, and the beat's check finding it behind at two checks in a row."""

from latet.server import spaces
from latet.server.spaces import _Counts


def test_counts_heard_out_of_order_add_up():
    counts = _Counts(3)
    counts.done(5)
    assert counts.applied == 3  # 4 not heard yet
    counts.done(4)
    assert counts.applied == 5
    counts.done(2)  # older than what was applied: nothing
    counts.done("6")  # not a count: nothing
    counts.done(True)
    assert counts.applied == 5


def test_a_count_never_heard_holds_the_space_back_until_it_is_caught_up():
    counts = _Counts(10)
    for count in (12, 13, 14):
        counts.done(count)
    assert counts.applied == 10  # 11 was lost at its source
    counts.caught_up(12)  # read again from PostgreSQL after its count 12 was read
    assert counts.applied == 14  # what it applied beyond, applied still


def test_the_check_finds_a_space_behind_at_two_checks_in_a_row():
    counts = _Counts(5)
    assert not counts.behind(5)
    assert not counts.behind(6)  # a change committed a moment ago may be on its way
    counts.done(6)
    assert not counts.behind(6)  # it came
    assert not counts.behind(8)
    assert counts.behind(8)  # still not heard at the next check
    counts.caught_up(8)
    assert not counts.behind(8)


def test_a_check_that_finds_a_later_count_waits_one_check_for_it_too():
    counts = _Counts(5)
    assert not counts.behind(6)
    counts.done(6)
    assert not counts.behind(7)  # 6 came; 7 is new at this check
    assert counts.behind(7)


def test_counts_kept_past_a_gap_are_bounded(monkeypatch):
    monkeypatch.setattr(spaces, "_ABOVE_KEPT", 3)
    counts = _Counts(0)
    for count in range(2, 10):
        counts.done(count)
    assert len(counts._above) <= 3
    counts.caught_up(9)
    assert counts.applied == 9 and not counts._above
