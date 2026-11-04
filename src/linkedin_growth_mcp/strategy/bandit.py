"""Thompson sampling over (style, hook) arms using Beta posteriors on post scores in [0, 1]."""
from __future__ import annotations

import random
from dataclasses import dataclass

from .styles import all_arms, arm_key


@dataclass
class ArmStats:
    alpha: float = 1.0  # prior successes
    beta: float = 1.0  # prior failures
    n: int = 0

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)


class StyleBandit:
    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.arms: dict[str, ArmStats] = {arm_key(s, h): ArmStats() for s, h in all_arms()}

    def update(self, style: str, hook: str, score: float) -> None:
        """Fractional Bernoulli update: a score of 0.7 adds 0.7 to alpha and 0.3 to beta."""
        score = min(max(score, 0.0), 1.0)
        a = self.arms[arm_key(style, hook)]
        a.alpha += score
        a.beta += 1.0 - score
        a.n += 1

    def choose(self, exclude: set[str] | None = None, include: set[str] | None = None) -> tuple[str, str]:
        exclude = exclude or set()
        best_key, best_draw = None, -1.0
        for key, st in self.arms.items():
            if key in exclude or (include is not None and key not in include):
                continue
            draw = self.rng.betavariate(st.alpha, st.beta)
            if draw > best_draw:
                best_key, best_draw = key, draw
        if best_key is None:
            raise ValueError("no arms available")
        style, hook = best_key.split("|")
        return style, hook

    def ranking(self) -> list[tuple[str, float, int]]:
        return sorted(((k, s.mean, s.n) for k, s in self.arms.items()), key=lambda t: -t[1])

    @classmethod
    def from_history(cls, rows: list[tuple[str, str, float]], rng: random.Random | None = None) -> "StyleBandit":
        b = cls(rng)
        for style, hook, score in rows:
            b.update(style, hook, score)
        return b
