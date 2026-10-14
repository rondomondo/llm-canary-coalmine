import asyncio

import httpx
import pytest

import loadgen
from loadgen import Sample


def test_percentile_uses_nearest_rank() -> None:
    values = [4.0, 1.0, 3.0, 2.0]
    assert loadgen.percentile(values, 50) == 2.0
    assert loadgen.percentile(values, 95) == 4.0
    assert loadgen.percentile(values, 0) == 1.0


def test_percentile_rejects_empty_input() -> None:
    with pytest.raises(ValueError):
        loadgen.percentile([], 50)


def test_parse_headers_strips_whitespace_and_keeps_colons_in_values() -> None:
    parsed = loadgen.parse_headers(["x-llm-canary:  true ", "Authorization: Bearer a:b"])
    assert parsed == {"x-llm-canary": "true", "Authorization": "Bearer a:b"}


@pytest.mark.parametrize("bad", ["no-colon", ": value", "   : value"])
def test_parse_headers_rejects_malformed_entries(bad: str) -> None:
    with pytest.raises(ValueError):
        loadgen.parse_headers([bad])


def test_backend_name_prefers_served_by_header() -> None:
    headers = {"x-served-by": "llm-new", "x-inference-pod": "llm-stable-7d9f8b6c5-abcde"}
    assert loadgen.backend_name(headers) == "llm-new"


@pytest.mark.parametrize(
    ("pod", "expected"),
    [
        ("llm-new-7d9f8b6c5-abcde", "llm-new"),
        ("llm-new-config-92-7d9f8b6c5-abcde", "llm-new-config-92"),
        ("standalone", "standalone"),
    ],
)
def test_backend_name_falls_back_to_deployment_name_from_pod(pod: str, expected: str) -> None:
    assert loadgen.backend_name({"x-inference-pod": pod}) == expected


def test_backend_name_is_unknown_without_headers() -> None:
    assert loadgen.backend_name({}) == loadgen.UNKNOWN_BACKEND


def test_is_success_covers_only_2xx() -> None:
    assert loadgen.is_success(Sample(204, "a", 1.0, None))
    assert not loadgen.is_success(Sample(429, "a", 1.0, None))
    assert not loadgen.is_success(Sample(loadgen.TRANSPORT_ERROR_STATUS, "a", 1.0, None))


def test_build_payload_carries_model_and_stream_flag() -> None:
    payload = loadgen.build_payload("real-model", 8, True)
    assert payload["model"] == "real-model"
    assert payload["max_tokens"] == 8
    assert payload["stream"] is True


def test_summarise_reports_shares_and_statuses() -> None:
    samples = [
        Sample(200, "llm-stable", 1.0, None),
        Sample(200, "llm-stable", 1.2, None),
        Sample(200, "llm-stable", 1.1, None),
        Sample(429, "llm-new", 0.5, None),
    ]
    lines = loadgen.summarise(samples, stream=False)
    assert lines[0] == "status: 200=3, 429=1"
    assert "ttft" not in lines[1]
    assert any(line.startswith("llm-stable") and "75.0%" in line for line in lines)
    assert any(line.startswith("llm-new") and "25.0%" in line for line in lines)


def test_summarise_adds_ttft_columns_when_streaming() -> None:
    lines = loadgen.summarise([Sample(200, "llm-new", 1.0, 0.3)], stream=True)
    assert "ttft p50" in lines[1]
    assert lines[2].rstrip().endswith("0.30")


def test_send_records_backend_header_status_and_ttft() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-served-by": "llm-stable"}, content=b"data: hi\n\n")

    async def go() -> Sample:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await loadgen._send(client, "http://gw/v1/chat/completions", {}, {})

    sample = asyncio.run(go())
    assert (sample.status, sample.served_by) == (200, "llm-stable")
    assert sample.ttft_s is not None and sample.ttft_s <= sample.total_s


def test_send_attributes_by_pod_header_when_route_sets_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-inference-pod": "llm-new-7d9f8b6c5-abcde"}, content=b"ok")

    async def go() -> Sample:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await loadgen._send(client, "http://gw/x", {}, {})

    assert asyncio.run(go()).served_by == "llm-new"


def test_send_maps_transport_errors_to_status_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async def go() -> Sample:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await loadgen._send(client, "http://gw/v1/chat/completions", {}, {})

    sample = asyncio.run(go())
    assert sample.status == loadgen.TRANSPORT_ERROR_STATUS
    assert sample.served_by == "error:ConnectError"
    assert sample.ttft_s is None


def test_send_labels_missing_backend_header_as_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    async def go() -> Sample:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await loadgen._send(client, "http://gw/x", {}, {})

    assert asyncio.run(go()).served_by == loadgen.UNKNOWN_BACKEND
