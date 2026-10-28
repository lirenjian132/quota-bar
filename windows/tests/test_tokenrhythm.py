# -*- coding: utf-8 -*-
"""TokenRhythm 取数服务测试:字符串余额、辅端点到期监控、ISO8601 毫秒。"""

import pytest

from quotabar.config import ConfigService
from quotabar.models import LABEL_BALANCE, ErrorKind, PlatformError, PlatformType
from quotabar.net import Response
from quotabar.platforms.tokenrhythm import TokenRhythmPlatformService, _parse_iso8601
from tests.conftest import FakeNetwork, json_response


def setup_instance(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    inst = cfg.add_instance(PlatformType.TOKENRHYTHM)
    cfg.set_api_key(inst.id, "sess_abc123")
    return cfg, inst


def summary_payload(**overrides) -> dict:
    body = {
        "code": 0,
        "message": "ok",
        "data": {
            "currency": "CNY",
            "availableBalanceCny": "253.76250848",
            "giftAvailableCny": "253.76250848",
            "rechargeBalanceCny": "0.00000000",
            "asOf": "2026-09-19T03:42:16.271Z",
        },
    }
    body.update(overrides)
    return body


def expiring_payload(next_expiry_at="2026-12-26T15:20:56.281Z") -> dict:
    return {"code": 0, "message": "ok", "data": {"summary": {"nextExpiryAt": next_expiry_at}}}


def test_normal_fetch_with_expiry(tmp_path):
    cfg, inst = setup_instance(tmp_path)
    net = FakeNetwork(
        json_response(summary_payload()),
        json_response(expiring_payload()),
    )
    data = TokenRhythmPlatformService().fetch_usage(cfg.config_data(inst.id), net)

    m = data.metrics[0]
    assert m.label == LABEL_BALANCE
    assert abs(m.current_value - 253.76250848) < 1e-9  # 字符串余额解析
    assert m.total_value is None
    assert m.unit == "CNY"
    assert m.reset_time == _parse_iso8601("2026-12-26T15:20:56.281Z")  # 辅端点到期时刻

    # 鉴权:tr_session= 裸值拼接
    req = net.requests[0]
    assert req.headers["Cookie"] == "tr_session=sess_abc123"
    # 辅端点由字符串替换派生
    assert net.requests[1].url.endswith("/wallet/expiring-credits")
    assert data.is_healthy


def test_aux_failure_tolerated(tmp_path):
    """辅端点 500:静默降级,reset_time 为 None,不影响取数"""
    cfg, inst = setup_instance(tmp_path)
    net = FakeNetwork(json_response(summary_payload()), Response(status_code=500, text="x"))
    data = TokenRhythmPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    assert data.metrics[0].current_value > 0
    assert data.metrics[0].reset_time is None


def test_expiring_soon_makes_unhealthy(tmp_path):
    """最早到期 <3 天:即使余额充足也不健康(到期警示优先)"""
    import time

    cfg, inst = setup_instance(tmp_path)
    soon = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 86400))
    net = FakeNetwork(json_response(summary_payload()), json_response(expiring_payload(soon)))
    data = TokenRhythmPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    assert not data.is_healthy


def test_low_balance_unhealthy(tmp_path):
    cfg, inst = setup_instance(tmp_path)
    net = FakeNetwork(
        json_response(summary_payload(data={"availableBalanceCny": "5.0"})),
        json_response(expiring_payload("2027-01-01T00:00:00.000Z")),
    )
    data = TokenRhythmPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    assert not data.is_healthy


def test_bad_balance_string_invalid(tmp_path):
    cfg, inst = setup_instance(tmp_path)
    net = FakeNetwork(json_response(summary_payload(data={"availableBalanceCny": "N/A"})))
    with pytest.raises(PlatformError) as ei:
        TokenRhythmPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    assert ei.value.kind == ErrorKind.INVALID_RESPONSE


def test_code_nonzero_api_error(tmp_path):
    cfg, inst = setup_instance(tmp_path)
    net = FakeNetwork(json_response({"code": "UNAUTHORIZED", "message": "expired"}))
    with pytest.raises(PlatformError) as ei:
        TokenRhythmPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    assert ei.value.kind == ErrorKind.API


def test_parse_iso8601_variants():
    assert _parse_iso8601("2026-09-26T15:20:56.281Z") is not None
    assert _parse_iso8601("2026-09-26T15:20:56Z") is not None      # 无毫秒
    assert _parse_iso8601("2026-09-26T15:20:56+08:00") is not None  # 带时区
    assert _parse_iso8601("garbage") is None
    assert _parse_iso8601(None) is None
    assert _parse_iso8601(12345) is None
