# -*- coding: utf-8 -*-
"""GLM 取数服务测试:正常解析、各坑点规避、错误分派。"""

import pytest

from quotabar.config import ConfigService
from quotabar.models import (
    LABEL_FIVE_HOUR,
    LABEL_MCP_MONTHLY,
    LABEL_WEEKLY_LIMIT,
    ErrorKind,
    PlatformError,
    PlatformType,
)
from quotabar.platforms.glm import GLMPlatformService
from tests.conftest import FakeNetwork, json_response


def make_config(tmp_path, **overrides) -> "ConfigService":
    cfg = ConfigService(home=str(tmp_path))
    inst = cfg.add_instance(PlatformType.GLM_CN)
    if "api_key" in overrides:
        cfg.set_api_key(inst.id, overrides.pop("api_key"))
    return cfg, inst


def payload(limits, success=True, msg="success") -> dict:
    return {"code": 200, "msg": msg, "success": success, "data": {"level": "v1", "limits": limits}}


def test_normal_parse(tmp_path):
    cfg, inst = make_config(tmp_path, api_key="raw-key")
    net = FakeNetwork(
        json_response(
            payload(
                [
                    {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 20, "nextResetTime": 1780329600000},
                    {"type": "TOKENS_LIMIT", "unit": 6, "percentage": 10, "nextResetTime": 1780848000000},
                    {"type": "TIME_LIMIT", "usage": 1000, "currentValue": 68, "remaining": 932,
                     "nextResetTime": 1780848000000},
                ]
            )
        )
    )
    data = GLMPlatformService().fetch_usage(cfg.config_data(inst.id), net)

    assert [(m.label, m.current_value, m.total_value, m.unit) for m in data.metrics] == [
        (LABEL_FIVE_HOUR, 80.0, 100, "%"),        # percentage 20 是已用 → 剩余 80
        (LABEL_WEEKLY_LIMIT, 90.0, 100, "%"),     # unit 6 → 周额度
        (LABEL_MCP_MONTHLY, 932.0, 1000, "times"),
    ]
    # 毫秒时间戳已转秒
    assert data.metrics[0].reset_time == 1780329600.0
    # 鉴权头:裸 key,无 Bearer 前缀
    assert net.requests[0].headers["Authorization"] == "raw-key"
    assert data.is_healthy


def test_percentage_is_used_not_remaining(tmp_path):
    """percentage=95(快用完)→ 剩余 5% → 不健康"""
    cfg, inst = make_config(tmp_path, api_key="k")
    net = FakeNetwork(
        json_response(
            payload([{"type": "TOKENS_LIMIT", "unit": 3, "percentage": 95, "nextResetTime": 1780329600000}])
        )
    )
    data = GLMPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    assert data.metrics[0].current_value == 5.0
    assert not data.is_healthy  # 5% < 15% 阈值


def test_missing_fields_skipped(tmp_path):
    """缺 percentage / 缺 usage|remaining 的条目整条跳过,不谎报"""
    cfg, inst = make_config(tmp_path, api_key="k")
    net = FakeNetwork(
        json_response(
            payload(
                [
                    {"type": "TOKENS_LIMIT", "unit": 3, "nextResetTime": 1780329600000},  # 缺 percentage
                    {"type": "TIME_LIMIT", "remaining": 100, "nextResetTime": 1},        # 缺 usage
                ]
            )
        )
    )
    with pytest.raises(PlatformError) as ei:
        GLMPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    assert ei.value.message == "error.glm.emptyLimits"  # i18n key


def test_success_false_beats_code_200(tmp_path):
    """只信 success 布尔,不信 code"""
    cfg, inst = make_config(tmp_path, api_key="k")
    net = FakeNetwork(json_response({"code": 200, "msg": "denied", "success": False}))
    with pytest.raises(PlatformError) as ei:
        GLMPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    assert ei.value.kind == ErrorKind.API
    assert ei.value.message == "denied"


def test_empty_limits_invalid(tmp_path):
    cfg, inst = make_config(tmp_path, api_key="k")
    net = FakeNetwork(json_response(payload([])))
    with pytest.raises(PlatformError) as ei:
        GLMPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    assert ei.value.kind == ErrorKind.INVALID_RESPONSE


def test_401_unauthorized_and_cache(tmp_path):
    cfg, inst = make_config(tmp_path, api_key="k")
    net = FakeNetwork(json_response({}, status_code=401))
    with pytest.raises(PlatformError) as ei:
        GLMPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    assert ei.value.kind == ErrorKind.UNAUTHORIZED

    # 缓存:TTL 内二次取数不再发请求
    net2 = FakeNetwork(
        json_response(payload([{"type": "TOKENS_LIMIT", "unit": 3, "percentage": 1,
                                 "nextResetTime": 1780329600000}])),
        json_response(payload([{"type": "TOKENS_LIMIT", "unit": 3, "percentage": 1,
                                 "nextResetTime": 1780329600000}])),
    )
    svc = GLMPlatformService()
    svc.fetch_usage(cfg.config_data(inst.id), net2)
    svc.fetch_usage(cfg.config_data(inst.id), net2)
    assert net2.call_count == 1
    svc.clear_cache()
    svc.fetch_usage(cfg.config_data(inst.id), net2)
    assert net2.call_count == 2


def test_zero_reset_time_is_none(tmp_path):
    """nextResetTime=0 → reset_time 为 None(对齐 Swift ms>0 判定),不出现 1970 假日期"""
    cfg, inst = make_config(tmp_path, api_key="k")
    net = FakeNetwork(
        json_response(
            payload([{"type": "TOKENS_LIMIT", "unit": 3, "percentage": 10, "nextResetTime": 0}])
        )
    )
    data = GLMPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    assert data.metrics[0].reset_time is None
