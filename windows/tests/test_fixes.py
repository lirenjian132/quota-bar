# -*- coding: utf-8 -*-
"""第 1 轮全量审查的回归测试:配置层结构自愈/写盘回滚、HTTP 口径、
字段类型守卫、渲染口径。"""

import json
import os

import pytest

from quotabar.config import ConfigService, _TEMPLATE_FALLBACK, _resources_dir
from quotabar.manager import PlatformManager
from quotabar.models import (
    LABEL_BALANCE,
    LABEL_FIVE_HOUR,
    ErrorKind,
    PlatformError,
    PlatformType,
    UsageMetric,
)
from quotabar.net import Request, Response
from quotabar.platforms._base import send, to_float
from quotabar.platforms.glm import GLMPlatformService
from quotabar.platforms.minimax import MiniMaxPlatformService
from quotabar.platforms.stepfun import StepFunPlatformService
from quotabar.render import metric_detail_text, metric_value_text, reset_countdown


def _resp(status: int, text: str = "{}") -> Response:
    return Response(status_code=status, text=text)


# ------------------------------------------------------------ 配置层:结构自愈

def test_settings_root_not_dict_does_not_crash(tmp_path):
    """settings.json 根是数组:启动不能崩,要有 .corrupt 备份"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    config = ConfigService(home=str(home))
    assert config.instances() == []
    assert (home / "settings.json.corrupt").exists()


def test_settings_broken_container_types_do_not_crash(tmp_path):
    """instances 是 dict / globals 是 list:字段类型不对要自愈,不能启动即崩"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"instances": {"a": 1}, "globals": []}), encoding="utf-8"
    )

    config = ConfigService(home=str(home))
    assert config.instances() == []
    assert config.display_mode() == "used"


def test_broken_instance_entry_does_not_take_down_others(tmp_path):
    """一条坏账号条目只剔除自己,其余账号照常可用"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps(
            {
                "instances": [
                    {"id": "minimax_cn-1", "platform_type": "minimax_cn", "display_name": "主力"},
                    {"id": "坏条目"},
                    {"id": "x-1", "platform_type": "不存在的平台", "display_name": "?"},
                ]
            }
        ),
        encoding="utf-8",
    )

    config = ConfigService(home=str(home))
    assert [i.id for i in config.instances()] == ["minimax_cn-1"]


def test_keys_file_root_not_dict_falls_back(tmp_path):
    """keys.json 根不是 dict:凭据读成空,不能崩"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "keys.json").write_text(json.dumps(["sk-1"]), encoding="utf-8")

    config = ConfigService(home=str(home))
    assert config.api_key("minimax_cn-1") == ""


# ------------------------------------------------------------ 配置层:写盘失败回滚

def test_set_api_key_rolls_back_when_disk_write_fails(monkeypatch, tmp_path):
    """写盘失败:内存值回滚 + 异常抛给调用方(不能界面显示已保存、重启后凭据消失)"""
    config = ConfigService(home=str(tmp_path / "home"))
    inst = config.add_instance(PlatformType.MINIMAX_CN)

    def boom(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(config, "_atomic_write", boom)
    with pytest.raises(OSError):
        config.set_api_key(inst.id, "sk-secret")
    assert config.api_key(inst.id) == ""

    # 重新从磁盘读:确实没写进去
    reloaded = ConfigService(home=str(tmp_path / "home"))
    assert reloaded.api_key(inst.id) == ""


def test_settings_write_failure_rolls_back_memory(monkeypatch, tmp_path):
    """设置项写盘失败:内存回滚到上次落盘成功的状态"""
    config = ConfigService(home=str(tmp_path / "home"))
    config.add_instance(PlatformType.MINIMAX_CN)
    assert config.display_mode() == "used"

    def boom(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(config, "_atomic_write", boom)
    with pytest.raises(OSError):
        config.set_display_mode("remaining")
    assert config.display_mode() == "used"


def test_atomic_write_cleans_temp_file_on_failure(monkeypatch, tmp_path):
    """写失败不能留下 .tmp 垃圾(每次失败漏一个)"""
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", boom)
    with pytest.raises(OSError):
        ConfigService._atomic_write(str(tmp_path / "x.json"), {"a": 1})
    assert list(tmp_path.glob("*.tmp")) == []


# ------------------------------------------------------------ 配置层:模板兜底

def test_template_fallback_matches_json_templates():
    """内置兜底取值必须与 Resources 里的模板一致(防止两边悄悄漂移)"""
    for name, expected in _TEMPLATE_FALLBACK.items():
        path = os.path.join(_resources_dir(), "ConfigTemplates", f"{name}.template.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for key, value in expected.items():
            assert data.get(key) == value, f"{name}.{key} 不一致"


def test_template_fallback_used_when_file_missing(monkeypatch, tmp_path):
    """模板文件读不到(打包漏带 Resources)时仍有取数端点,账号不会永远「未配置」"""
    config = ConfigService(home=str(tmp_path / "home"))
    monkeypatch.setattr("quotabar.config._resources_dir", lambda: str(tmp_path / "缺目录"))
    inst = config.add_instance(PlatformType.GLM_CN)
    cfg = config.config_data(inst.id)
    assert cfg.api_base_url == "https://open.bigmodel.cn/api/monitor/usage/quota/limit"


# ------------------------------------------------------------ HTTP 错误口径

def test_403_is_network_error_by_default():
    """403 默认不是凭据问题(WAF 拦截误报成「API Key 无效」会误导用户)"""
    net = lambda req: _resp(403)
    with pytest.raises(PlatformError) as e:
        send(net, Request(method="GET", url="http://example.invalid"))
    assert e.value.kind == ErrorKind.NETWORK


def test_403_unauthorized_when_platform_says_so():
    net = lambda req: _resp(403)
    with pytest.raises(PlatformError) as e:
        send(net, Request(method="GET", url="http://example.invalid"), forbid_as_unauthorized=True)
    assert e.value.kind == ErrorKind.UNAUTHORIZED


def test_stepfun_403_is_unauthorized():
    """Stepfun 的 403 = cookie 过期/被风控拒绝,和 401 一样是凭据问题"""
    from quotabar.models import PlatformConfigData

    cfg = PlatformConfigData(
        platform_type=PlatformType.STEPFUN,
        instance_id="stepfun-1",
        display_name="x",
        api_base_url="https://example.invalid/dash",
        auth_header="Cookie",
        auth_prefix="",
        api_key="Oasis-Webid=w; Oasis-Token=t",
    )
    service = StepFunPlatformService()
    net = lambda req: _resp(403, "forbidden")
    with pytest.raises(PlatformError) as e:
        service.fetch_usage(cfg, net)
    assert e.value.kind == ErrorKind.UNAUTHORIZED


def test_glm_403_is_network_error():
    """GLM 只认 401(与 macOS 版一致)"""
    from quotabar.models import PlatformConfigData

    cfg = PlatformConfigData(
        platform_type=PlatformType.GLM_CN,
        instance_id="glm_cn-1",
        display_name="x",
        api_base_url="https://example.invalid/quota",
        auth_header="Authorization",
        auth_prefix="",
        api_key="k",
    )
    service = GLMPlatformService()
    net = lambda req: _resp(403, "forbidden")
    with pytest.raises(PlatformError) as e:
        service.fetch_usage(cfg, net)
    assert e.value.kind == ErrorKind.NETWORK


# ------------------------------------------------------------ 字段类型守卫

def test_to_float_accepts_numeric_strings_and_rejects_others():
    assert to_float("12.5") == 12.5
    assert to_float(3) == 3.0
    assert to_float(True) is None
    assert to_float("abc") is None
    assert to_float({"a": 1}) is None
    assert to_float(None) is None


def test_glm_wrong_field_types_do_not_leak_raw_exception():
    """字段类型不对(服务端结构漂移):报可读错误,不漏裸异常"""
    service = GLMPlatformService()
    body = {
        "success": True,
        "data": {"limits": [{"type": "TIME_LIMIT", "usage": "abc", "remaining": "def"}]},
    }
    with pytest.raises(PlatformError) as e:
        service._parse(body)
    assert e.value.kind == ErrorKind.API
    assert e.value.message == "error.glm.emptyLimits"


def test_glm_reset_time_wrong_type_is_ignored():
    """nextResetTime 是字符串:当没有重置时间,不崩"""
    service = GLMPlatformService()
    body = {
        "success": True,
        "data": {
            "limits": [
                {
                    "type": "TOKENS_LIMIT",
                    "percentage": 30,
                    "unit": 3,
                    "nextResetTime": "明天",
                }
            ]
        },
    }
    metrics = service._parse(body)
    assert metrics[0].reset_time is None


def test_minimax_wrong_percent_type_reports_invalid_response():
    """百分比字段是 dict:报响应无效,不漏 AttributeError"""
    service = MiniMaxPlatformService()
    item = {"model_name": "general", "current_interval_remaining_percent": {"a": 1}}
    with pytest.raises(PlatformError) as e:
        service._parse_metrics(item)
    assert e.value.kind == ErrorKind.INVALID_RESPONSE


def test_minimax_missing_percent_falls_back_to_zero():
    """字段缺失 → 0(对齐 Swift 的 ?? 0),不是报错"""
    service = MiniMaxPlatformService()
    metrics = service._parse_metrics({"model_name": "general"})
    assert metrics[0].current_value == 0.0


def test_stepfun_rate_limit_wrong_type_reports_invalid_response():
    """plan_credit_rate_limit 是 list:报响应无效,不漏 AttributeError"""
    from quotabar.models import PlatformConfigData

    cfg = PlatformConfigData(
        platform_type=PlatformType.STEPFUN,
        instance_id="stepfun-1",
        display_name="x",
        api_base_url="https://example.invalid/dash",
        auth_header="Cookie",
        auth_prefix="",
        api_key="Oasis-Webid=w; Oasis-Token=t",
    )
    service = StepFunPlatformService()
    body = {"status": 1, "plan_credit_rate_limit": []}
    net = lambda req: _resp(200, json.dumps(body))
    with pytest.raises(PlatformError) as e:
        service._fetch_rate_limit(cfg, net, {})
    assert e.value.kind in (ErrorKind.INVALID_RESPONSE, ErrorKind.API)


# ------------------------------------------------------------ 编排层

def test_fetch_usage_unknown_instance_raises_key_error(config):
    """实例不存在:抛 KeyError,别回落成误导性的「平台未配置」"""
    manager = PlatformManager(config)
    with pytest.raises(KeyError):
        manager.fetch_usage("minimax_cn-999")


# ------------------------------------------------------------ 渲染口径

def test_reset_countdown_switches_to_days_at_25h():
    """切天点对齐 Swift 的 hours > 24(即剩余 25 小时整),不是 24 小时零 1 分"""
    now = 1_000_000.0
    # 剩 24 小时零 1 分:Swift 侧 hours=24 不切天,显示「还剩24小时1分」
    kind, payload = reset_countdown(now + 24 * 3600 + 60, now=now)
    assert kind == "hm"
    assert payload == (24, 1)
    # 剩 25 小时整:Swift 侧 hours=25 切天,显示「1天1小时后重置」
    kind, payload = reset_countdown(now + 25 * 3600, now=now)
    assert kind == "days"
    assert payload == (1, 1)


def test_metric_detail_text_keeps_unit_and_abbreviates():
    """详情数值带单位(对齐 macOS 弹窗),两侧都做大数缩写"""
    assert metric_detail_text(UsageMetric("mcp_monthly", 932, 1000, "times"), "zh-Hans") == "932/1000 times"
    assert metric_detail_text(UsageMetric("mcp_monthly", 932, 8000000000, "times"), "zh-Hans") == "932/80.00亿 times"
    assert metric_detail_text(UsageMetric(LABEL_BALANCE, 253.7, None, "CNY"), "en") == "254 CNY"


def test_metric_value_text_distinguishes_percent_and_absolute():
    """面板/浮窗口径:百分比按显示模式,次数/余额型走详情口径,无限型 ∞"""
    pct = UsageMetric(LABEL_FIVE_HOUR, 80, 100, "%")
    assert metric_value_text(pct, "remaining", "zh-Hans") == "80%"
    assert metric_value_text(pct, "used", "zh-Hans") == "19%"
    times = UsageMetric("mcp_monthly", 932, 1000, "times")
    assert metric_value_text(times, "used", "zh-Hans") == "932/1000 times"
    unlimited = UsageMetric("weekly_limit_unlimited", 0, None, "unlimited")
    assert metric_value_text(unlimited, "used", "zh-Hans") == "∞"
