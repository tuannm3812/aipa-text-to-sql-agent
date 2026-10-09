import pytest

from text_to_sql_agent.evaluation_v2 import Interval, bootstrap_ci, paired_bootstrap_ci
from text_to_sql_agent.evaluation_v2.stats import percentile


def test_percentile_endpoints_and_interpolation() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 1.0) == 5.0
    assert percentile(values, 0.5) == 3.0
    assert percentile(values, 0.125) == pytest.approx(1.5)
    assert percentile([7.0], 0.975) == 7.0


def test_percentile_tails_on_101_values() -> None:
    values = [float(i) for i in range(101)]  # position q*100 lands exactly on a rank
    assert percentile(values, 0.025) == pytest.approx(2.5)
    assert percentile(values, 0.975) == pytest.approx(97.5)


def test_percentile_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        percentile([], 0.5)
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)


def test_deterministic_under_seed() -> None:
    data = [True] * 30 + [False] * 20
    assert bootstrap_ci(data, seed=3) == bootstrap_ci(data, seed=3)


def test_point_interval_and_order() -> None:
    iv = bootstrap_ci([True] * 60 + [False] * 40)
    assert isinstance(iv, Interval)
    assert iv.point == pytest.approx(0.6)
    assert iv.n == 100
    assert iv.low < iv.point < iv.high
    assert iv.width == pytest.approx(iv.high - iv.low)
    assert iv.low >= 0.0 and iv.high <= 1.0


@pytest.mark.parametrize("value", [True, False])
def test_degenerate_rates_collapse(value: bool) -> None:
    iv = bootstrap_ci([value] * 25)
    assert iv.low == iv.high == iv.point == float(value)
    assert iv.width == 0.0


def test_more_cases_give_narrower_interval() -> None:
    small = bootstrap_ci([True] * 5 + [False] * 5)
    large = bootstrap_ci([True] * 50 + [False] * 50)
    assert large.width < small.width


def test_empty_input_raises() -> None:
    with pytest.raises(ValueError):
        bootstrap_ci([])
    with pytest.raises(ValueError):
        paired_bootstrap_ci([], [])


def test_paired_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        paired_bootstrap_ci([True], [True, False])


def test_paired_identical_is_centred_on_zero() -> None:
    data = [True, False] * 50
    iv = paired_bootstrap_ci(data, data)
    assert iv.point == 0.0
    assert iv.low == iv.high == 0.0


def test_paired_clear_loss_excludes_zero() -> None:
    old = [True] * 20 + [False] * 80
    new = [False] * 20 + [False] * 80  # loses 20, gains none
    iv = paired_bootstrap_ci(new, old)
    assert iv.point == pytest.approx(-0.2)
    assert iv.high < 0


def test_paired_noise_includes_zero_and_is_deterministic() -> None:
    old = [True] * 50 + [False] * 50
    new = [True] * 48 + [False] * 2 + [True] * 2 + [False] * 48
    iv = paired_bootstrap_ci(new, old, seed=1)
    assert iv.low < 0 < iv.high
    assert iv == paired_bootstrap_ci(new, old, seed=1)


def test_different_seeds_move_at_least_one_bound_at_realistic_n() -> None:
    """Bounds sit on a grid of achievable rates, so two seeds can coincide at
    small n; at n = 1,500 the grid is ~0.0007 wide and five seeds cannot all
    agree on both bounds unless the seed is being ignored.
    """
    successes = [i % 3 != 0 for i in range(1_500)]
    bounds = {
        (bootstrap_ci(successes, seed=seed).low, bootstrap_ci(successes, seed=seed).high)
        for seed in range(5)
    }
    assert len(bounds) > 1


def test_non_boolean_indicators_are_coerced_not_summed() -> None:
    assert bootstrap_ci([2, 0, 2]).point == pytest.approx(2 / 3)
    assert bootstrap_ci([2, 0, 2]).high <= 1.0


def test_a_single_resample_is_rejected() -> None:
    with pytest.raises(ValueError):
        bootstrap_ci([True, False], resamples=1)
