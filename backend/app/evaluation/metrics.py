"""Quality metrics. Pure functions: no I/O, no randomness, so results are reproducible."""

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any


# --------------------------------------------------------------------------- value matching
def normalize(value: Any) -> Any:
    """Canonical form used to compare an extracted value with the annotation.

    Text is case/whitespace/accent-insensitive; numbers compare to the cent; ``None`` and
    empty strings are both "no value".
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).casefold()
    return text.strip(" .,;:")


def values_match(expected: Any, predicted: Any) -> bool:
    return normalize(expected) == normalize(predicted)


# --------------------------------------------------------------------------- field scoring
@dataclass
class Counts:
    """tp: correct value; fp: a value was given but it is wrong or should be absent;
    fn: an expected value was missed or wrong. A wrong value is both an FP and an FN."""

    tp: int = 0
    fp: int = 0
    fn: int = 0

    def add(self, other: "Counts") -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None:
            return None
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def as_dict(self) -> dict[str, Any]:
        def r(x: float | None) -> float | None:
            return None if x is None else round(x, 4)

        return {
            "tp": self.tp, "fp": self.fp, "fn": self.fn,
            "precision": r(self.precision), "recall": r(self.recall), "f1": r(self.f1),
        }


def score_field(expected: Any, predicted: Any) -> Counts:
    exp, pred = normalize(expected), normalize(predicted)
    if exp is None and pred is None:
        return Counts()  # correctly absent: neither a hit nor a miss
    if exp is None:
        return Counts(fp=1)  # hallucinated a value
    if pred is None:
        return Counts(fn=1)  # missed
    if exp == pred:
        return Counts(tp=1)
    return Counts(fp=1, fn=1)


def score_document(truth: dict[str, Any], predicted: dict[str, Any]) -> dict[str, Counts]:
    """Per-field counts. Predicted keys outside the annotation count as false positives."""
    scores = {name: score_field(value, predicted.get(name)) for name, value in truth.items()}
    for name, value in predicted.items():
        if name not in truth and normalize(value) is not None:
            scores[name] = Counts(fp=1)
    return scores


def aggregate(per_doc: list[dict[str, Counts]]) -> dict[str, Any]:
    by_field: dict[str, Counts] = defaultdict(Counts)
    total = Counts()
    for doc in per_doc:
        for name, counts in doc.items():
            by_field[name].add(counts)
            total.add(counts)
    macro = [c.f1 for c in by_field.values() if c.f1 is not None]
    return {
        "micro": total.as_dict(),
        "macro_f1": round(sum(macro) / len(macro), 4) if macro else None,
        "per_field": {name: by_field[name].as_dict() for name in sorted(by_field)},
    }


# --------------------------------------------------------------------------- text error rates
def edit_distance(a: list[str] | str, b: list[str] | str) -> int:
    """Levenshtein distance over characters or tokens."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, start=1):
        current = [i]
        for j, y in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate, whitespace-insensitive. 0 = identical."""
    ref, hyp = squash(reference), squash(hypothesis)
    return edit_distance(ref, hyp) / max(1, len(ref))


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate, whitespace-insensitive."""
    ref, hyp = squash(reference).split(), squash(hypothesis).split()
    return edit_distance(ref, hyp) / max(1, len(ref))


# --------------------------------------------------------------------------- confidence & review
@dataclass
class Observation:
    """One extracted field with the facts needed to judge confidence and review routing."""

    doc_id: str
    field: str
    correct: bool
    confidence: float | None
    flagged: bool  # would be routed to a human by the validation layer


BINS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.0001))


def calibration(observations: list[Observation]) -> dict[str, Any]:
    """Does a stated confidence mean what it says? Reports accuracy per confidence bin and
    the expected calibration error (ECE: mean |confidence - accuracy| weighted by bin size)."""
    rated = [o for o in observations if o.confidence is not None]
    bins = []
    ece = 0.0
    for low, high in BINS:
        members = [o for o in rated if low <= (o.confidence or 0.0) < high]
        if not members:
            bins.append({"range": [low, min(high, 1.0)], "n": 0, "accuracy": None, "mean_confidence": None})
            continue
        accuracy = sum(o.correct for o in members) / len(members)
        mean_conf = sum(o.confidence or 0.0 for o in members) / len(members)
        ece += len(members) / len(rated) * abs(mean_conf - accuracy)
        bins.append(
            {
                "range": [low, min(high, 1.0)], "n": len(members),
                "accuracy": round(accuracy, 4), "mean_confidence": round(mean_conf, 4),
            }
        )
    return {"n": len(rated), "ece": round(ece, 4) if rated else None, "bins": bins}


def review_routing(observations: list[Observation]) -> dict[str, Any]:
    """What the human-review gate buys.

    * ``error_capture_rate`` -- share of wrong fields routed to a person
    * ``review_load``        -- share of all fields routed to a person
    * ``auto_accept_accuracy`` -- accuracy of the fields that are *not* routed (the risk that remains)
    * ``raw_accuracy``       -- accuracy with no review at all
    """
    total = len(observations)
    wrong = [o for o in observations if not o.correct]
    flagged = [o for o in observations if o.flagged]
    accepted = [o for o in observations if not o.flagged]

    def ratio(n: int, d: int) -> float | None:
        return round(n / d, 4) if d else None

    return {
        "fields": total,
        "wrong": len(wrong),
        "flagged": len(flagged),
        "raw_accuracy": ratio(total - len(wrong), total),
        "error_capture_rate": ratio(sum(o.flagged for o in wrong), len(wrong)),
        "review_load": ratio(len(flagged), total),
        "auto_accept_accuracy": ratio(sum(o.correct for o in accepted), len(accepted)),
        "silent_errors": [f"{o.doc_id}:{o.field}" for o in wrong if not o.flagged],
    }


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[index], 4)
