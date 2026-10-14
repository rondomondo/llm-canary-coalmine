import pytest

import ramp
from loadgen import Sample


def _ok(name: str, total_s: float, count: int) -> list[Sample]:
    return [Sample(200, name, total_s, None) for _ in range(count)]


def test_parse_steps_accepts_increasing_percentages() -> None:
    assert ramp.parse_steps("5, 25,50,100") == [5, 25, 50, 100]


@pytest.mark.parametrize("bad", ["a,b", "0,10", "10,101", "10,10", "50,25", ""])
def test_parse_steps_rejects_invalid_input(bad: str) -> None:
    with pytest.raises(ValueError):
        ramp.parse_steps(bad)


def test_error_rate_counts_non_2xx_and_transport_errors() -> None:
    samples = _ok("a", 1.0, 6) + [Sample(429, "a", 1.0, None), Sample(0, "error:ConnectError", 1.0, None)]
    assert ramp.error_rate(samples) == pytest.approx(0.25)


def test_error_rate_of_no_samples_is_zero() -> None:
    assert ramp.error_rate([]) == 0.0


def test_backend_p95_needs_minimum_successful_samples() -> None:
    assert ramp.backend_p95(_ok("llm-new", 1.0, ramp.MIN_GATE_SAMPLES - 1), "llm-new") is None
    assert ramp.backend_p95(_ok("llm-new", 1.0, ramp.MIN_GATE_SAMPLES), "llm-new") == 1.0


def test_backend_p95_ignores_failed_responses() -> None:
    samples = _ok("llm-new", 1.0, 5) + [Sample(500, "llm-new", 9.0, None)]
    assert ramp.backend_p95(samples, "llm-new") == 1.0


def test_gate_passes_when_latency_is_within_ratio() -> None:
    samples = _ok("llm-stable", 0.8, 20) + _ok("llm-new", 1.0, 10)
    result = ramp.evaluate_gate(samples, baseline_p95=0.8, new_name="llm-new", max_error_rate=0.02, max_p95_ratio=1.5)
    assert result.passed


def test_gate_fails_when_new_backend_is_too_slow() -> None:
    samples = _ok("llm-stable", 0.8, 20) + _ok("llm-new", 1.3, 10)
    result = ramp.evaluate_gate(samples, baseline_p95=0.8, new_name="llm-new", max_error_rate=0.02, max_p95_ratio=1.25)
    assert not result.passed
    assert "llm-new p95" in result.reason


def test_gate_fails_on_error_rate_before_looking_at_latency() -> None:
    samples = _ok("llm-new", 0.1, 8) + [Sample(500, "llm-new", 0.1, None), Sample(500, "llm-new", 0.1, None)]
    result = ramp.evaluate_gate(samples, baseline_p95=1.0, new_name="llm-new", max_error_rate=0.05, max_p95_ratio=2.0)
    assert not result.passed
    assert "error rate" in result.reason


def test_gate_skips_latency_check_with_too_few_new_samples() -> None:
    samples = _ok("llm-stable", 0.8, 20) + _ok("llm-new", 9.0, 2)
    result = ramp.evaluate_gate(samples, baseline_p95=0.8, new_name="llm-new", max_error_rate=0.02, max_p95_ratio=1.25)
    assert result.passed
    assert "skipped" in result.reason
