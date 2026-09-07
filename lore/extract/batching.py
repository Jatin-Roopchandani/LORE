"""Pure batching helper, split out so it is testable off-GPU.

`VllmProvider.prime()` (kaggle/extract/extract_kernel.py) uses this to slice
a work unit's prompts into fixed-size groups before handing each group to
vLLM, decoupling vLLM's concurrency (VRAM-bound) from work-unit size (a
scheduling/amortisation concern). The vLLM call itself cannot run off-GPU;
this slicing logic can and does.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import TypeVar

T = TypeVar("T")


def batched(items: Sequence[T] | Iterable[T], size: int) -> Iterator[list[T]]:
    """Yield consecutive slices of `items`, each of length `size`.

    The final slice is short (never padded, never dropped) if `items` does
    not divide evenly. An empty input yields nothing. `size` must be a
    positive integer; a misconfigured size raises immediately rather than
    looping forever or silently returning everything in one batch.
    """
    if size < 1:
        raise ValueError(f"batch size must be >= 1, got {size}")
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start : start + size]
