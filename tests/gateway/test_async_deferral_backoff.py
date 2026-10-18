from gateway.run_notifications import DeferralBackoff


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_unseen_group_is_ready_and_refusal_spaces_retries():
    clock = Clock()
    b = DeferralBackoff(clock)
    key = ("deleg_a",)
    assert b.ready(key)
    assert b.deferred(key) == (4.0, 1)
    assert not b.ready(key)
    clock.now += 3.9
    assert not b.ready(key)
    clock.now += 0.2
    assert b.ready(key)


def test_delay_doubles_and_is_capped():
    clock = Clock()
    b = DeferralBackoff(clock)
    key = ("deleg_a",)
    delays = [b.deferred(key)[0] for _ in range(10)]
    assert delays[:4] == [4.0, 8.0, 16.0, 32.0]
    assert delays[-1] == DeferralBackoff.MAX_SECONDS


def test_success_clears_and_groups_are_independent():
    clock = Clock()
    b = DeferralBackoff(clock)
    a, c = ("deleg_a",), ("deleg_c",)
    b.deferred(a)
    assert b.ready(c)
    b.clear(a)
    assert b.ready(a)
    assert b.deferred(a) == (4.0, 1)
