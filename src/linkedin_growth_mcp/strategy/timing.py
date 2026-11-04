"""Thompson sampling over posting-hour slots.

The arms are the hours in POST_HOURS. A post's slot is simply the hour it went out, so nothing extra is stored:
history comes from posts.published_at joined with that post's latest metrics.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class SlotStats:
    alpha: float = 1.0
    beta: float = 1.0
    n: int = 0

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)


class SlotBandit:
    def __init__(self, hours: tuple[int, ...], rng: random.Random | None = None):
        if not hours:
            raise ValueError("at least one posting hour is required")
        self.rng = rng or random.Random()
        self.slots: dict[int, SlotStats] = {h: SlotStats() for h in hours}

    def update(self, hour: int, score: float) -> None:
        if hour not in self.slots:
            return  # a slot that is no longer configured teaches us nothing about the ones that are
        score = min(max(score, 0.0), 1.0)
        s = self.slots[hour]
        s.alpha += score
        s.beta += 1.0 - score
        s.n += 1

    def choose(self) -> int:
        return max(self.slots, key=lambda h: self.rng.betavariate(self.slots[h].alpha, self.slots[h].beta))

    def ranking(self) -> list[tuple[int, float, int]]:
        return sorted(((h, s.mean, s.n) for h, s in self.slots.items()), key=lambda t: -t[1])

    @classmethod
    def from_history(cls, hours: tuple[int, ...], rows: list[tuple[int, float]],
                     rng: random.Random | None = None) -> "SlotBandit":
        b = cls(hours, rng)
        for hour, score in rows:
            b.update(hour, score)
        return b
