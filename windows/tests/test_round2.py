# -*- coding: utf-8 -*-
"""第 2 轮审查的回归测试:配置层补齐/两阶段删除/指标勾选口径/容器类型守卫。"""

import json

import pytest

from quotabar.config import ConfigService
from quotabar.models import (
    AVAILABLE_METRIC_LABELS,
    DEFAULT_ENABLED_METRICS,
    ErrorKind,
    PlatformError,
    PlatformType,
)
from quotabar.net import Response
from quotabar.platforms.glm import GLMPlatformService
from quotabar.platforms.minimax import MiniMaxPlatformService
from quotabar.platforms.stepfun import StepFunPlatformService
from quotabar.platforms.tokenrhythm import TokenRhythmPlatformService


def _resp(status: int, text: str = "{}") -> Response:
    return Response(status_code=status, text=text)


def _write_settings(home, payload):
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(payload), encoding="utf-8")


# ------------------------------------------------------------ 每实例配置缺失

def test_instance_without_per_instance_entry_does_not_crash(tmp_path):
    """账号条目在、per_instance 配置缺了:补默认值,不能变成取不到配置的僵尸"""
    home = tmp_path / "home"
    _write_settings(
        home,
        {"instances": [{"id": "minimax_cn-1", "platform_type": "minimax_cn", "display_name": "主力"}]},
    )

    config = ConfigService(home=str(home))
    assert [i.id for i in config.instances()] == ["minimax_cn-1"]
    # 取值不能抛异常(原先 is_enabled/is_pinned 直接 KeyError,应用启动即崩)
    assert config.is_enabled("minimax_cn-1") in (True, False)
    assert config.is_pinned("minimax_cn-1") in (True, False)
    assert config.enabled_metrics("minimax_cn-1") == DEFAULT_ENABLED_METRICS[PlatformType.MINIMAX_CN]


def test_per_instance_value_not_dict_is_replaced(tmp_path):
    """per_instance 的值不是 dict:整条换默认值,不留坏配置"""
    home = tmp_path / "home"
    _write_settings(
        home,
        {
            "instances": [{"id": "minimax_cn-1", "platform_type": "minimax_cn", "display_name": "主力"}],
            "per_instance": {"minimax_cn-1": "坏掉了"},
        },
    )

    config = ConfigService(home=str(home))
    assert config.is_enabled("minimax_cn-1") in (True, False)
    assert config.enabled_metrics("minimax_cn-1") == DEFAULT_ENABLED_METRICS[PlatformType.MINIMAX_CN]


def test_broken_entries_leave_a_corrupt_backup(tmp_path):
    """剔掉了坏条目要留备份(原文件里的东西不能无声消失)"""
    home = tmp_path / "home"
    _write_settings(
        home,
        {
            "instances": [
                {"id": "minimax_cn-1", "platform_type": "minimax_cn", "display_name": "主力"},
                {"id": "坏条目"},
            ]
        },
    )

    ConfigService(home=str(home))
    assert (home / "settings.json.corrupt").exists()


def test_non_utf8_settings_does_not_crash(tmp_path):
    """settings.json 被非 UTF-8 工具转过码:不能崩在启动阶段"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_bytes(
        '{"instances": [{"id": "minimax_cn-1", "platform_type": "minimax_cn",'
        ' "display_name": "主力"}]}'.encode("gbk")
    )

    config = ConfigService(home=str(home))
    assert config.instances() == []
    assert (home / "settings.json.corrupt").exists()


# ------------------------------------------------------------ 删除账号的两阶段

def test_delete_instance_rolls_back_when_keys_write_fails(monkeypatch, tmp_path):
    """凭据删不掉时,账号要恢复回来(不能变成「删了一半」的错位状态)"""
    config = ConfigService(home=str(tmp_path / "home"))
    inst = config.add_instance(PlatformType.MINIMAX_CN)
    config.set_api_key(inst.id, "sk-secret")

    real_write = config._atomic_write

    def fail_on_keys(path, data):
        if path.endswith("keys.json"):
            raise OSError("disk full")
        return real_write(path, data)

    monkeypatch.setattr(config, "_atomic_write", fail_on_keys)
    with pytest.raises(OSError):
        config.delete_instance(inst.id)

    # 账号还在、凭据还在
    assert config.instance(inst.id) is not None
    assert config.api_key(inst.id) == "sk-secret"


def test_delete_instance_removes_credential(tmp_path):
    """正常删除:账号与凭据都清掉"""
    config = ConfigService(home=str(tmp_path / "home"))
    inst = config.add_instance(PlatformType.MINIMAX_CN)
    config.set_api_key(inst.id, "sk-secret")

    config.delete_instance(inst.id)

    assert config.instance(inst.id) is None
    assert config.api_key(inst.id) == ""
    reloaded = ConfigService(home=str(tmp_path / "home"))
    assert reloaded.api_key(inst.id) == ""


# ------------------------------------------------------------ 指标勾选口径

def test_enabled_metrics_falls_back_to_platform_defaults(config):
    """落盘值全空/全死 label:回退平台默认勾选(与 macOS 一致)"""
    inst = config.add_instance(PlatformType.GLM_CN)
    config._settings["per_instance"][inst.id]["enabled_metrics"] = []
    assert config.enabled_metrics(inst.id) == DEFAULT_ENABLED_METRICS[PlatformType.GLM_CN]

    config._settings["per_instance"][inst.id]["enabled_metrics"] = ["早已下架的label"]
    assert config.enabled_metrics(inst.id) == DEFAULT_ENABLED_METRICS[PlatformType.GLM_CN]


def test_set_enabled_metrics_rejects_three_labels_even_with_dead_one(config):
    """3 个 label 一律拒(先按原始名单查数,再过滤死 label,与 macOS 同序)"""
    inst = config.add_instance(PlatformType.GLM_CN)
    allowed = AVAILABLE_METRIC_LABELS[PlatformType.GLM_CN]
    labels = allowed + ["早已下架的label"]
    with pytest.raises(ValueError):
        config.set_enabled_metrics(inst.id, labels)
    assert config.enabled_metrics(inst.id) == DEFAULT_ENABLED_METRICS[PlatformType.GLM_CN]


# ------------------------------------------------------------ 容器类型守卫

def test_glm_data_not_dict_reports_invalid_response():
    """data 是数组:报响应无效,不漏 AttributeError"""
    service = GLMPlatformService()
    with pytest.raises(PlatformError) as e:
        service._parse({"success": True, "data": []})
    assert e.value.kind == ErrorKind.INVALID_RESPONSE


def test_minimax_model_remains_not_list_reports_invalid_response():
    service = MiniMaxPlatformService()
    with pytest.raises(PlatformError) as e:
        service._pick_general({"model_remains": {"a": 1}})
    assert e.value.kind == ErrorKind.INVALID_RESPONSE


def test_minimax_base_resp_not_dict_reports_invalid_response():
    service = MiniMaxPlatformService()
    with pytest.raises(PlatformError) as e:
        service._pick_general({"base_resp": [], "model_remains": [{"model_name": "general"}]})
    assert e.value.kind == ErrorKind.INVALID_RESPONSE


def test_stepfun_rate_limit_not_dict_reports_invalid_response():
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
    assert e.value.kind == ErrorKind.INVALID_RESPONSE


def test_tokenrhythm_data_not_dict_reports_invalid_response():
    service = TokenRhythmPlatformService()
    with pytest.raises(PlatformError) as e:
        service._parse_main({"code": 0, "data": []})
    assert e.value.kind == ErrorKind.INVALID_RESPONSE


# ------------------------------------------------------------ 位置记忆防护

def test_overlay_pos_with_broken_value_is_ignored(config):
    """overlay_pos 被手改成字符串:当作没记过位置,不能抛 ValueError"""
    config._settings["globals"]["overlay_pos"] = ["abc", "def"]
    assert config.overlay_pos() is None
