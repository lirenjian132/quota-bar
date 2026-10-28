# -*- coding: utf-8 -*-
"""MiniMax 取数服务测试:三态周额度、时间戳双形态、回退选模型。"""

import pytest

from quotabar.config import ConfigService
from quotabar.models import (
    LABEL_FIVE_HOUR,
    LABEL_WEEKLY_BOOSTED,
    LABEL_WEEKLY_LIMIT,
    LABEL_WEEKLY_UNLIMITED,
    ErrorKind,
    PlatformError,
    PlatformType,
)
from quotabar.platforms.minimax import MiniMaxPlatformService
from tests.conftest import FakeNetwork, json_response


def setup_instance(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    inst = cfg.add_instance(PlatformType.MINIMAX_CN)
    cfg.set_api_key(inst.id, "test-key")
    return cfg, inst


def remains_payload(**overrides) -> dict:
    item = {
        "model_name": "general",
        "current_interval_remaining_percent": 98.0,
        "current_weekly_remaining_percent": 95.0,
        "end_time": 1780329600000,
        "weekly_end_time": 1780848000000,
        "remains_time": 3600000,
        "weekly_remains_time": 86400000,
        "current_weekly_status": 1,
        "weekly_boost_permille": 1000,
    }
    item.update(overrides)
    return {"base_resp": {"status_code": 0, "status_msg": "success"}, "model_remains": [item]}


def fetch(tmp_path, payload):
    cfg, inst = setup_instance(tmp_path)
    net = FakeNetwork(json_response(payload))
    data = MiniMaxPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    return data, net


def test_normal_with_weekly(tmp_path):
    data, net = fetch(tmp_path, remains_payload())
    labels = [(m.label, m.current_value) for m in data.metrics]
    assert labels[0] == (LABEL_FIVE_HOUR, 98.0)
    assert labels[1] == (LABEL_WEEKLY_LIMIT, 95.0)  # status 1 无加成 → 普通态
    # 鉴权:Bearer 前缀
    assert net.requests[0].headers["Authorization"] == "Bearer test-key"
    # 绝对毫秒时间戳
    assert data.metrics[0].reset_time == 1780329600.0
    assert data.is_healthy


def test_weekly_boosted(tmp_path):
    data, _ = fetch(tmp_path, remains_payload(weekly_boost_permille=1500))
    assert data.metrics[1].label == LABEL_WEEKLY_BOOSTED


def test_weekly_unlimited(tmp_path):
    data, _ = fetch(tmp_path, remains_payload(current_weekly_status=3))
    m = data.metrics[1]
    assert m.label == LABEL_WEEKLY_UNLIMITED
    assert m.total_value is None
    assert m.unit == "unlimited"


def test_absolute_ts_zero_falls_back_relative(tmp_path):
    """end_time=0(非正数)视为缺失,退化 now+remains_time(对齐 Swift >0 判定)"""
    data, _ = fetch(
        tmp_path,
        remains_payload(end_time=0, remains_time=3600000),
    )
    import time

    assert abs(data.metrics[0].reset_time - (time.time() + 3600)) < 5


def test_absolute_ts_missing_falls_back_relative(tmp_path):
    """end_time 缺失时退化 now+remains_time(相对毫秒)"""
    data, _ = fetch(
        tmp_path,
        remains_payload(end_time=None, remains_time=3600000),
    )
    import time

    assert abs(data.metrics[0].reset_time - (time.time() + 3600)) < 5


def test_model_name_fallback_to_first(tmp_path):
    """找不到 general 时回退首项,不整体挂掉"""
    payload = remains_payload()
    payload["model_remains"][0]["model_name"] = "renamed"
    data, _ = fetch(tmp_path, payload)
    assert data.metrics[0].current_value == 98.0


def test_base_resp_status_error(tmp_path):
    payload = {
        "base_resp": {"status_code": 1004, "status_msg": "invalid api key"},
        "model_remains": [],
    }
    with pytest.raises(PlatformError) as ei:
        fetch(tmp_path, payload)
    assert ei.value.kind == ErrorKind.API
    assert ei.value.message == "invalid api key"


def test_null_base_resp_tolerated(tmp_path):
    """老接口 base_resp 为 null,跳过检查"""
    payload = remains_payload()
    payload["base_resp"] = None
    data, _ = fetch(tmp_path, payload)
    assert data.metrics


def test_empty_model_remains_invalid(tmp_path):
    payload = {"base_resp": {"status_code": 0}, "model_remains": []}
    with pytest.raises(PlatformError) as ei:
        fetch(tmp_path, payload)
    assert ei.value.kind == ErrorKind.INVALID_RESPONSE


def test_low_five_hour_unhealthy(tmp_path):
    """健康度只看 5 小时剩余:5h=5% 时即使周额度 95% 也不健康"""
    data, _ = fetch(tmp_path, remains_payload(current_interval_remaining_percent=5.0))
    assert not data.is_healthy
