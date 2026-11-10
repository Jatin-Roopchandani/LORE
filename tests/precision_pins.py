"""Pins for golden tests dumped before prose-precision (2026-11-09).

A golden that predates these switches runs with them off: REASON_FIRST and
VERDICT_GUARD (the check's field order and its re-ask), DEDUP_SENTENCES, and
ATTRIBUTION_FIDELITY, whose rule is now in the claims prompt too."""


def before_prose_precision(monkeypatch) -> None:
    from lore.query import synthesize
    monkeypatch.setattr(synthesize, "REASON_FIRST", False)
    monkeypatch.setattr(synthesize, "VERDICT_GUARD", False)
    monkeypatch.setattr(synthesize, "DEDUP_SENTENCES", False)
    monkeypatch.setattr(synthesize, "ATTRIBUTION_FIDELITY", False)
