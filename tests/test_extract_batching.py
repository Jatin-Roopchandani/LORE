import pytest

from lore.extract.batching import batched


def test_batched_splits_exact_multiples_evenly() -> None:
    assert list(batched([1, 2, 3, 4], 2)) == [[1, 2], [3, 4]]


def test_batched_ragged_tail_is_a_short_final_batch() -> None:
    assert list(batched([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]


def test_batched_shorter_than_batch_size_yields_one_batch() -> None:
    assert list(batched([1, 2], 5)) == [[1, 2]]


def test_batched_empty_list_yields_nothing() -> None:
    assert list(batched([], 3)) == []


def test_batched_size_zero_raises() -> None:
    with pytest.raises(ValueError):
        list(batched([1, 2, 3], 0))


def test_batched_size_negative_raises() -> None:
    with pytest.raises(ValueError):
        list(batched([1, 2, 3], -1))
