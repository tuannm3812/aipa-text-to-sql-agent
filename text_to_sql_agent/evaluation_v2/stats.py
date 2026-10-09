"""Bootstrap confidence intervals for evaluation rates.

Standard library only. Both functions resample *cases* with replacement using
``random.Random(seed)``, so a fixed seed gives a reproducible interval.

What an interval means: it is sampling uncertainty over cases, for a fixed model and prompt.
It is not generation variance across repeated runs of the same model.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

ALPHA = 0.05  # two-sided: 2.5 % and 97.5 % percentiles


@dataclass(frozen=True)
class Interval:
    """A point estimate with a 95 % percentile-bootstrap interval over ``n`` cases."""

    point: float
    low: float
    high: float
    n: int

    @property
    def width(self) -> float:
        """Distance from the lower to the upper bound."""
        return self.high - self.low


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """The ``q`` quantile (0 <= q <= 1) of an ascending sequence, by linear interpolation.

    Position is ``q * (m - 1)`` (the common "type 7" definition): q=0 is the minimum, q=1 the
    maximum, and a position between two ranks interpolates between them. Raises ``ValueError``
    on an empty sequence or ``q`` outside [0, 1].
    """
    m = len(sorted_values)
    if m == 0:
        raise ValueError("percentile of an empty sequence")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be in [0, 1], got {q}")
    pos = q * (m - 1)
    lo = int(pos)
    hi = min(lo + 1, m - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _interval(values: Sequence[float], *, resamples: int, seed: int) -> Interval:
    """Percentile bootstrap of the mean of ``values`` (resampled with replacement)."""
    n = len(values)
    if n == 0:
        raise ValueError("cannot compute an interval over zero cases")
    if resamples < 2:  # one resample is a point, not an interval
        raise ValueError("resamples must be at least 2")
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(resamples))
    return Interval(
        point=sum(values) / n,
        low=percentile(means, ALPHA / 2),
        high=percentile(means, 1 - ALPHA / 2),
        n=n,
    )


def bootstrap_ci(successes: Sequence[bool], *, resamples: int = 10_000, seed: int = 0) -> Interval:
    """95 % percentile-bootstrap interval for a success rate.

    Meaning: sampling uncertainty over cases, for a fixed model and prompt - not generation
    variance across repeated runs. Resamples the cases with replacement ``resamples`` times and
    takes the 2.5th and 97.5th percentiles (linear interpolation) of the resampled rates. When
    every case succeeds or none does, ``low == high == point``. Raises ``ValueError`` on empty
    input.
    """
    # bool() first: a stray 2 or "False" must not become a rate of 2.0 or a ValueError
    # from float() - the indicator is correct/not-correct, nothing else.
    return _interval([float(bool(s)) for s in successes], resamples=resamples, seed=seed)


def bootstrap_mean_ci(
    values: Sequence[float], *, resamples: int = 10_000, seed: int = 0
) -> Interval:
    """95 % percentile-bootstrap interval for the mean of per-case values in [0, 1].

    For a per-case fraction such as schema recall, which ``bootstrap_ci`` would coerce to a
    bool. Same resampling, same meaning: sampling uncertainty over cases, for a fixed model and
    prompt. Raises ``ValueError`` on empty input.
    """
    return _interval([float(v) for v in values], resamples=resamples, seed=seed)


def paired_bootstrap_ci(
    new: Sequence[bool], old: Sequence[bool], *, resamples: int = 10_000, seed: int = 0
) -> Interval:
    """95 % percentile-bootstrap interval for ``mean(new) - mean(old)`` over the same cases.

    ``new[i]`` and ``old[i]`` are the two runs' outcomes on case ``i``. Each resample draws case
    indices with replacement and keeps the pairing, so cases both runs get right or wrong cancel
    out. Meaning: sampling uncertainty over cases for fixed models and prompts - not generation
    variance across repeated runs. Raises ``ValueError`` on empty input or unequal lengths.
    """
    if len(new) != len(old):
        raise ValueError(f"paired runs differ in length: {len(new)} vs {len(old)}")
    diffs = [float(bool(a)) - float(bool(b)) for a, b in zip(new, old, strict=True)]
    return _interval(diffs, resamples=resamples, seed=seed)
