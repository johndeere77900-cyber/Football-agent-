import random

import pytest

import backtest


def make_candidates(count):
    return list(range(count))


def test_sampling_returns_requested_number():
    candidates = make_candidates(100)

    result = backtest._sample_backtest_candidates(
        candidates,
        20,
        seed=42,
    )

    assert len(result) == 20


def test_sampling_returns_all_when_candidate_set_is_small():
    candidates = make_candidates(10)

    result = backtest._sample_backtest_candidates(
        candidates,
        20,
        seed=42,
    )

    assert result == candidates


def test_sampling_is_reproducible_with_same_seed():
    candidates = make_candidates(100)

    first = backtest._sample_backtest_candidates(
        candidates,
        20,
        seed=42,
    )

    second = backtest._sample_backtest_candidates(
        candidates,
        20,
        seed=42,
    )

    assert first == second


def test_different_seeds_can_produce_different_samples():
    candidates = make_candidates(100)

    first = backtest._sample_backtest_candidates(
        candidates,
        20,
        seed=42,
    )

    second = backtest._sample_backtest_candidates(
        candidates,
        20,
        seed=99,
    )

    assert first != second


def test_sampling_does_not_duplicate_candidates():
    candidates = make_candidates(100)

    result = backtest._sample_backtest_candidates(
        candidates,
        50,
        seed=42,
    )

    assert len(result) == len(set(result))


def test_sampling_selects_from_original_candidate_set():
    candidates = make_candidates(100)

    result = backtest._sample_backtest_candidates(
        candidates,
        25,
        seed=42,
    )

    assert set(result).issubset(set(candidates))


def test_sampling_does_not_modify_original_candidates():
    candidates = make_candidates(100)
    original = candidates.copy()

    backtest._sample_backtest_candidates(
        candidates,
        20,
        seed=42,
    )

    assert candidates == original


def test_sampling_rejects_zero_sample_size():
    with pytest.raises(ValueError):
        backtest._sample_backtest_candidates(
            make_candidates(10),
            0,
        )


def test_sampling_rejects_negative_sample_size():
    with pytest.raises(ValueError):
        backtest._sample_backtest_candidates(
            make_candidates(10),
            -1,
        )


def test_sampling_rejects_non_integer_sample_size():
    with pytest.raises(ValueError):
        backtest._sample_backtest_candidates(
            make_candidates(10),
            5.5,
        )


def test_sampling_rejects_boolean_sample_size():
    with pytest.raises(ValueError):
        backtest._sample_backtest_candidates(
            make_candidates(10),
            True,
        )


def test_sampling_does_not_change_global_random_state():
    random.seed(12345)

    expected_first = random.random()
    expected_second = random.random()

    random.seed(12345)

    before = random.random()

    backtest._sample_backtest_candidates(
        make_candidates(100),
        20,
        seed=42,
    )

    after = random.random()

    assert before == expected_first
    assert after == expected_second
