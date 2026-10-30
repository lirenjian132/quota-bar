# -*- coding: utf-8 -*-
"""Stepfun 取数服务测试:五头契约、credit/降级双路、订阅状态静默降级、凭据早失败。"""

import pytest

from quotabar.config import ConfigService
from quotabar.models import (
    LABEL_CREDITS,
    LABEL_FIVE_HOUR,
    LABEL_WEEKLY_LIMIT,
    ErrorKind,
    PlatformError,
    PlatformType,
)
from quotabar.net import Response
from quotabar.platforms.stepfun import StepFunPlatformService
from tests.conftest import FakeNetwork, json_response

COOKIE = "Oasis-Webid=w123; Oasis-Token=eyJhbGci.eyJleHAiOjE3OTI1MDI4ODN9.sig...eyJyZWYiOjF9.sig2"


def setup_instance(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    inst = cfg.add_instance(PlatformType.STEPFUN)
    cfg.set_api_key(inst.id, COOKIE)
    return cfg, inst


def rate_payload(**overrides) -> dict:
    body = {
        "status": 1,
        "desc": "",
        "plan_credit_rate_limit": {
            "subscription_credit_left_rate": 0.96886694,
            "subscription_credit_reset_time": "1792502883",
            "topup_credit_left_rate": 0,
        },
        "five_hour_usage_left_rate": 0,
        "weekly_usage_left_rate": 0,
    }
    body.update(overrides)
    return body


def status_payload(**overrides) -> dict:
    # expired_at 动态生成 400 天后的 Unix 秒:写死日期是定时炸弹(过了 3 天健康
    # 阈值那一刻,断言 is_healthy 的测试集体变红)
    import time as _time

    body = {
        "status": 1,
        "desc": "",
        "subscription": {"plan_type": 2, "name": "Pro", "status": 1,
                         "expired_at": str(int(_time.time() + 400 * 86400)), "auto_renew": False},
    }
    body.update(overrides)
    return body


def fetch(tmp_path, *responses, cookie=COOKIE):
    cfg, inst = setup_instance(tmp_path)
    cfg.set_api_key(inst.id, cookie)
    net = FakeNetwork(*[json_response(r) for r in responses])
    data = StepFunPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    return data, net


def test_credit_plan_headers_and_parse(tmp_path):
    data, net = fetch(tmp_path, rate_payload(), status_payload())

    # 主指标:credits,0~1 率转百分比
    assert [(m.label, round(m.current_value, 5)) for m in data.metrics] == [(LABEL_CREDITS, 96.88669)]
    assert data.metrics[0].reset_time == 1792502883.0  # 字符串 Unix 秒正确解析
    assert data.is_healthy

    # 五头契约
    req = net.requests[0]
    assert req.url.endswith("/QueryStepPlanRateLimit")
    assert req.headers["Cookie"] == COOKIE
    assert req.headers["oasis-appid"] == "10300"
    assert req.headers["oasis-platform"] == "web"
    assert req.headers["oasis-webid"] == "w123"  # 从 Cookie 拆出
    assert req.headers["Connect-Protocol-Version"] == "1"
    assert req.body == "{}"


def test_non_credit_plan_fallback(tmp_path):
    """无 credit 率时降级显示 5 小时/周用量率"""
    data, _ = fetch(
        tmp_path,
        rate_payload(plan_credit_rate_limit=None, five_hour_usage_left_rate=0.7,
                     weekly_usage_left_rate=0.8),
        status_payload(),
    )
    assert [m.label for m in data.metrics] == [LABEL_FIVE_HOUR, LABEL_WEEKLY_LIMIT]
    assert data.metrics[0].current_value == 70.0


def test_no_metrics_at_all_invalid(tmp_path):
    with pytest.raises(PlatformError) as ei:
        fetch(tmp_path, rate_payload(plan_credit_rate_limit=None,
                                     five_hour_usage_left_rate=None,
                                     weekly_usage_left_rate=None))
    assert ei.value.kind == ErrorKind.INVALID_RESPONSE


def test_subscription_status_not_one_expired(tmp_path):
    """先信 status 字段:subscription.status != 1 即失效(即使 expired_at 在未来)"""
    data, _ = fetch(tmp_path, rate_payload(),
                    status_payload(subscription={"plan_type": 2, "status": 0, "expired_at": "1893456000"}))
    assert not data.is_healthy


def test_subscription_time_past_expired(tmp_path):
    data, _ = fetch(tmp_path, rate_payload(),
                    status_payload(subscription={"plan_type": 2, "status": 1, "expired_at": "1700000000"}))
    assert not data.is_healthy


def test_subscription_request_failure_degrades(tmp_path):
    """辅请求 500:静默降级,健康度不受影响,余量照常显示"""
    cfg, inst = setup_instance(tmp_path)
    net = FakeNetwork(
        json_response(rate_payload()),
        Response(status_code=500, text="boom"),
    )
    data = StepFunPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    assert data.is_healthy
    assert data.metrics[0].label == LABEL_CREDITS


def test_webid_extraction_case_insensitive():
    assert StepFunPlatformService.extract_webid(
        "other=1; oasis-webid=abc; Oasis-Token=x"
    ) == "abc"
    assert StepFunPlatformService.extract_webid("Oasis-Token=x") == ""


def test_bad_cookie_fails_early_without_request(tmp_path):
    """cookie 里没有 Oasis-Webid:不发请求直接报格式错误"""
    cfg, inst = setup_instance(tmp_path)
    cfg.set_api_key(inst.id, "Oasis-Token=only")
    net = FakeNetwork()
    with pytest.raises(PlatformError) as ei:
        StepFunPlatformService().fetch_usage(cfg.config_data(inst.id), net)
    assert ei.value.message == "error.stepfun.cookieFormat"
    assert net.call_count == 0


def test_ms_timestamp_rejected_by_sanity_window(tmp_path):
    """毫秒漂移的时间戳(>4e9)被判不合理 → reset_time 为 None 而非 1970 假日期"""
    data, _ = fetch(tmp_path,
                    rate_payload(plan_credit_rate_limit={
                        "subscription_credit_left_rate": 0.5,
                        "subscription_credit_reset_time": 1792502883000,
                    }),
                    status_payload())
    assert data.metrics[0].reset_time is None


def test_webid_extraction_is_case_insensitive_upper():
    """cookie 名字大小写不敏感:大写 OASIS-WEBID 也要能拆出来"""
    from quotabar.platforms.stepfun import StepFunPlatformService

    svc = StepFunPlatformService()
    assert svc.extract_webid("OASIS-WEBID=abc123") == "abc123"
    assert svc.extract_webid("Oasis-Webid=abc123") == "abc123"
    assert svc.extract_webid("foo=1; oasis-webid=xyz; bar=2") == "xyz"
