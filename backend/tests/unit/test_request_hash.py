import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.bookings.idempotency import (
    IdempotencyKeyInvalid,
    IdempotencyKeyRequired,
    _compute_request_hash,
    _normalize_path,
    _parse_idempotency_key,
)
from app.bookings.schemas import BookingCreate, Cancel


def test_identical_payload_identical_hash() -> None:
    res_id = uuid.uuid4()
    s = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    e = datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc)

    b1 = BookingCreate(resource_id=res_id, starts_at=s, ends_at=e)
    b2 = BookingCreate(resource_id=res_id, starts_at=s, ends_at=e)

    h1 = _compute_request_hash("POST", "/api/v1/bookings", b1)
    h2 = _compute_request_hash("POST", "/api/v1/bookings", b2)
    assert h1 == h2
    assert len(h1) == 64


def test_different_payload_different_hash() -> None:
    s = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    e = datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc)

    b1 = BookingCreate(resource_id=uuid.uuid4(), starts_at=s, ends_at=e)
    b2 = BookingCreate(resource_id=uuid.uuid4(), starts_at=s, ends_at=e)

    h1 = _compute_request_hash("POST", "/api/v1/bookings", b1)
    h2 = _compute_request_hash("POST", "/api/v1/bookings", b2)
    assert h1 != h2


def test_timezone_equivalent_instants_same_hash() -> None:
    res_id = uuid.uuid4()
    # 10:00 UTC vs 06:00 EDT (-04:00) represents the exact same instant
    tz_neg4 = timezone(timedelta(hours=-4))
    s_utc = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    e_utc = datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc)

    s_edt = datetime(2026, 7, 1, 6, 0, 0, tzinfo=tz_neg4)
    e_edt = datetime(2026, 7, 1, 7, 0, 0, tzinfo=tz_neg4)

    b_utc = BookingCreate(resource_id=res_id, starts_at=s_utc, ends_at=e_utc)
    b_edt = BookingCreate(resource_id=res_id, starts_at=s_edt, ends_at=e_edt)

    h_utc = _compute_request_hash("POST", "/api/v1/bookings", b_utc)
    h_edt = _compute_request_hash("POST", "/api/v1/bookings", b_edt)
    assert h_utc == h_edt


def test_json_key_order_and_header_casing_do_not_change_hash() -> None:
    res_id = uuid.uuid4()
    s = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    e = datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc)

    # Reconstructed models from differently-ordered dictionaries
    d1 = {"resource_id": res_id, "starts_at": s, "ends_at": e}
    d2 = {"ends_at": e, "resource_id": res_id, "starts_at": s}

    b1 = BookingCreate.model_validate(d1)
    b2 = BookingCreate.model_validate(d2)

    h1 = _compute_request_hash("POST", "/api/v1/bookings", b1)
    h2 = _compute_request_hash("POST", "/api/v1/bookings", b2)
    assert h1 == h2

    # Method casing normalized: post vs POST
    h_lower_method = _compute_request_hash("post", "/api/v1/bookings", b1)
    assert h_lower_method == h1


def test_uuid_path_casing_normalized() -> None:
    res_id = uuid.uuid4()
    s = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    e = datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc)
    b = BookingCreate(resource_id=res_id, starts_at=s, ends_at=e)

    sample_uuid = uuid.uuid4()
    path_lower = f"/api/v1/resources/{str(sample_uuid).lower()}/bookings"
    path_upper = f"/api/v1/resources/{str(sample_uuid).upper()}/bookings"

    assert _normalize_path(path_upper) == _normalize_path(path_lower)
    assert _compute_request_hash("POST", path_upper, b) == _compute_request_hash(
        "POST", path_lower, b
    )

    # Different path or method produces different hash
    h_original = _compute_request_hash("POST", "/api/v1/bookings", b)
    h_diff_path = _compute_request_hash("POST", "/api/v1/bookings/other", b)
    h_diff_method = _compute_request_hash("PATCH", "/api/v1/bookings", b)

    assert h_original != h_diff_path
    assert h_original != h_diff_method


def test_defaults_applied_consistently() -> None:
    c1 = Cancel()
    c2 = Cancel(reason="")
    h1 = _compute_request_hash("POST", "/api/v1/bookings/1/cancel", c1)
    h2 = _compute_request_hash("POST", "/api/v1/bookings/1/cancel", c2)
    assert h1 == h2


def test_idempotency_key_parsing() -> None:
    # Absent / blank -> IdempotencyKeyRequired
    with pytest.raises(IdempotencyKeyRequired):
        _parse_idempotency_key(None)

    with pytest.raises(IdempotencyKeyRequired):
        _parse_idempotency_key("")

    with pytest.raises(IdempotencyKeyRequired):
        _parse_idempotency_key("   ")

    # Malformed -> IdempotencyKeyInvalid
    with pytest.raises(IdempotencyKeyInvalid):
        _parse_idempotency_key("not-a-uuid")

    with pytest.raises(IdempotencyKeyInvalid):
        _parse_idempotency_key("12345")

    # Non-v4 UUID (e.g. v1, v5) -> IdempotencyKeyInvalid
    v1_key = str(uuid.uuid1())
    with pytest.raises(IdempotencyKeyInvalid):
        _parse_idempotency_key(v1_key)

    v5_key = str(uuid.uuid5(uuid.NAMESPACE_DNS, "example.com"))
    with pytest.raises(IdempotencyKeyInvalid):
        _parse_idempotency_key(v5_key)

    # Valid v4 accepted (lowercase, uppercase, stripped)
    v4_key = uuid.uuid4()
    assert _parse_idempotency_key(str(v4_key)) == v4_key
    assert _parse_idempotency_key(str(v4_key).upper()) == v4_key
    assert _parse_idempotency_key(f"  {str(v4_key)}  ") == v4_key
