# -*- coding: utf-8 -*-
"""第 4 轮对抗审查的回归测试:非有限值、消毒覆盖面、类型兜底、渲染层兜底。"""

import json

import pytest

from quotabar.config import ConfigService
from quotabar.models import ErrorKind, PlatformError, PlatformType
from quotabar.net import Response
from quotabar.platforms._base import to_float
from quotabar.platforms.stepfun import StepFunPlatformService
from quotabar.platforms.tokenrhythm import TokenRhythmPlatformService
from quotabar.render import format_balance, metric_detail_text, metric_value_text, tray_text
from quotabar.models import LABEL_FIVE_HOUR, UsageMetric


def _resp(status: int, text: str = "{}") -> Response:
    return Response(status_code=status, text=text)


# ------------------------------------------------------------ 非有限值

def test_to_float_rejects_huge_int():
    """超出 float 上限的巨整数:转不动要返回 default,不能抛 OverflowError"""
    assert to_float(10 ** 400) is None
    assert to_float(10 ** 309) is None


def test_stepfun_rate_overflow_is_rejected():
    """rate * 100 溢出成 inf:要报响应无效,不能把 inf 带进渲染层炸掉界面"""
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
    body = {"status": 1, "plan_credit_rate_limit": {"subscription_credit_left_rate": 1e308}}
    net = lambda req: _resp(200, json.dumps(body))
    with pytest.raises(PlatformError) as e:
        service._fetch_rate_limit(cfg, net, {})
    assert e.value.kind == ErrorKind.INVALID_RESPONSE


def test_tokenrhythm_nan_balance_is_invalid_response():
    """"nan" 余额不是合法数字:要报响应无效,不能显示成 nan 还判健康"""
    service = TokenRhythmPlatformService()
    with pytest.raises(PlatformError) as e:
        service._parse_main({"code": 0, "data": {"availableBalanceCny": "nan"}})
    assert e.value.kind == ErrorKind.INVALID_RESPONSE


def test_render_handles_non_finite_values():
    """渲染层兜底:非有限值显示 --,不能抛 OverflowError/ValueError"""
    inf_metric = UsageMetric(LABEL_FIVE_HOUR, float("inf"), 100, "%")
    assert tray_text(inf_metric, "used") == "--"
    assert metric_detail_text(inf_metric, "zh-Hans") == "--"
    assert format_balance(float("inf")) == "--"
    # 离谱比例(1e308 / 1e-308)也不能撑爆界面
    weird = UsageMetric("mcp_monthly", 1e308, 1e-308, "times")
    assert metric_value_text(weird, "used", "zh-Hans") == "--"


# ------------------------------------------------------------ 消毒覆盖面

def test_keys_with_surrogate_are_sanitized(tmp_path):
    """凭据里的代理字符:一个坏字符会让这个文件的所有写盘都失败"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "keys.json").write_text(
        json.dumps({"minimax_cn-1": "sk-\ud800bad", "glm_cn-1": "sk-good"}),
        encoding="utf-8",
    )

    config = ConfigService(home=str(home))
    # 坏的那条被清干净,好的一条不受影响
    assert config.api_key("glm_cn-1") == "sk-good"
    config.api_key("minimax_cn-1").encode("utf-8")  # 不再抛 UnicodeEncodeError
    # 之后的写盘要能成功
    inst = config.add_instance(PlatformType.GLM_CN)
    config.set_api_key(inst.id, "sk-new")


def test_per_instance_with_surrogate_is_sanitized(tmp_path):
    """每实例配置里的代理字符:同样会让之后的每次写盘失败"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps(
            {
                "instances": [
                    {"id": "minimax_cn-1", "platform_type": "minimax_cn", "display_name": "主力"}
                ],
                "per_instance": {"minimax_cn-1": {"api_base_url": "https://x/\ud800y"}},
            }
        ),
        encoding="utf-8",
    )

    config = ConfigService(home=str(home))
    config.set_display_mode("remaining")  # 写盘要能成功
    assert config.display_mode() == "remaining"


def test_instance_id_with_surrogate_is_sanitized(tmp_path):
    """账号 id 里的代理字符:同样要清掉"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps(
            {"instances": [{"id": "minimax_cn-\ud8001", "platform_type": "minimax_cn", "display_name": "x"}]}
        ),
        encoding="utf-8",
    )

    config = ConfigService(home=str(home))
    ids = [i.id for i in config.instances()]
    assert len(ids) == 1
    ids[0].encode("utf-8")
    config.set_display_mode("remaining")


# ------------------------------------------------------------ 类型兜底

def test_enabled_metrics_not_a_list_falls_back(tmp_path):
    """enabled_metrics 被写成数字:不能抛 TypeError 让应用起不来"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps(
            {
                "instances": [
                    {"id": "glm_cn-1", "platform_type": "glm_cn", "display_name": "GLM"}
                ],
                "per_instance": {"glm_cn-1": {"enabled": True, "enabled_metrics": 5}},
            }
        ),
        encoding="utf-8",
    )

    config = ConfigService(home=str(home))
    picked = config.enabled_metrics("glm_cn-1")
    assert 1 <= len(picked) <= 2


def test_enabled_metrics_more_than_two_falls_back(tmp_path):
    """落盘了 3 个 label:回退平台默认(对齐 macOS 的 ≤2 判定)"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps(
            {
                "instances": [
                    {"id": "glm_cn-1", "platform_type": "glm_cn", "display_name": "GLM"}
                ],
                "per_instance": {
                    "glm_cn-1": {
                        "enabled": True,
                        "enabled_metrics": ["five_hour", "weekly_limit", "mcp_monthly"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = ConfigService(home=str(home))
    assert len(config.enabled_metrics("glm_cn-1")) <= 2


def test_display_mode_whitelist(config):
    """display_mode 只认两个值,其它回落默认(与 macOS 一致)"""
    assert config.display_mode() == "used"
    config._settings["globals"]["display_mode"] = "垃圾值"
    assert config.display_mode() == "used"
    config._settings["globals"]["display_mode"] = "remaining"
    assert config.display_mode() == "remaining"


def test_deeply_nested_settings_does_not_crash(tmp_path):
    """深层嵌套的 JSON:不能抛 RecursionError 让应用起不来"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text("[" * 900 + "]" * 900, encoding="utf-8")

    config = ConfigService(home=str(home))
    assert config.instances() == []
