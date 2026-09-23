"""Belief engine for live mode — Bayesian updating and expected information gain.

Replay mode keeps its published divergence÷cost heuristic and fixed logit
bumps so the 12-bundle ablation numbers stay reproducible. Live mode uses the
formal versions:

  * Each hypothesis h assigns an outcome label to some observables. Its
    likelihood for an observed label y is (1−ε) if y matches its prediction,
    ε/(k−1) otherwise, and 1/k if it makes no prediction (agnostic), where k is
    the number of possible outcomes and ε is the measurement-error rate.
  * Posteriors update by Bayes' rule.
  * A probe's value is its expected information gain in nats:
        EIG = H(p) − Σ_y P(y) · H(p | y)
    which is positive whenever the hypotheses assign different likelihoods to
    its outcomes — including when only ONE hypothesis commits to a prediction.
    (The replay divergence count scores that case as zero.)
"""
from __future__ import annotations

import math
from typing import Optional

EPSILON = 0.05  # probability a probe's reading contradicts a true hypothesis's prediction


def softmax(logits: dict[str, float]) -> dict[str, float]:
    if not logits:
        return {}
    mx = max(logits.values())
    ex = {k: math.exp(v - mx) for k, v in logits.items()}
    z = sum(ex.values())
    return {k: v / z for k, v in ex.items()}


def entropy(p: dict[str, float]) -> float:
    """Shannon entropy in nats."""
    return -sum(v * math.log(v) for v in p.values() if v > 0)


def kl_divergence(p: dict[str, float], q: dict[str, float]) -> float:
    """KL(p ‖ q) in nats — how far beliefs p moved away from q."""
    return sum(pv * math.log(pv / q[h]) for h, pv in p.items() if pv > 0 and q.get(h, 0) > 0)


def likelihood(prediction: Optional[str], outcome: str, outcomes: tuple[str, ...],
               eps: float = EPSILON) -> float:
    k = len(outcomes)
    if prediction is None:
        return 1.0 / k
    return (1.0 - eps) if prediction == outcome else eps / (k - 1)


def update(posteriors: dict[str, float], predictions: dict[str, Optional[str]], outcome: str,
           outcomes: tuple[str, ...], eps: float = EPSILON) -> dict[str, float]:
    raw = {h: p * likelihood(predictions.get(h), outcome, outcomes, eps) for h, p in posteriors.items()}
    z = sum(raw.values())
    return {h: v / z for h, v in raw.items()} if z > 0 else dict(posteriors)


def expected_information_gain(posteriors: dict[str, float], predictions: dict[str, Optional[str]],
                              outcomes: tuple[str, ...], eps: float = EPSILON) -> float:
    prior_h = entropy(posteriors)
    expected_posterior_h = 0.0
    for y in outcomes:
        p_y = sum(p * likelihood(predictions.get(h), y, outcomes, eps) for h, p in posteriors.items())
        if p_y <= 0:
            continue
        expected_posterior_h += p_y * entropy(update(posteriors, predictions, y, outcomes, eps))
    return max(0.0, prior_h - expected_posterior_h)
