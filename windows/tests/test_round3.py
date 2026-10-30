# -*- coding: utf-8 -*-
"""第 3 轮对抗审查的回归测试:locale 类型校验、删账号回滚、入口开关原子性、
parse_json 根类型、NaN 防护、编码与代理字符。"""

import json
import math

import pytest

from quotabar.config import ConfigService
from quotabar.models import ErrorKind, PlatformError, PlatformType
from quotabar.net import Response
from quotabar.platforms._base import parse_json, to_float


def _resp(status: int, text: str = "{}") -> Response:
    return Response(status_code=status, text=text)


# ------------------------------------------------------------ locale 类型校验

@pytest.mark.parametrize("bad", [["zh-Hans"], {"a": 1}, 5, None, "fr-FR"])
def test_locale_falls_back_to_default(tmp_path, bad):
    """locale 被写坏:必须回落默认语言,不能把不可哈希的值当 i18n 键去查表"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"instances": [], "globals": {"locale": bad}}), encoding="utf-8"
    )

    config = ConfigService(home=str(home))
    assert config.locale() in ("en", "zh-Hans")


def test_locale_valid_values_are_kept(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"instances": [], "globals": {"locale": "en"}}), encoding="utf-8"
    )
    assert ConfigService(home=str(home)).locale() == "en"


# ------------------------------------------------------------ 删账号的两阶段回滚

def test_delete_instance_survives_settings_write_failure(monkeypatch, tmp_path):
    """第一步落盘(settings)就失败:删除中止,账号与凭据原封不动"""
    config = ConfigService(home=str(tmp_path / "home"))
    inst = config.add_instance(PlatformType.MINIMAX_CN)
    config.set_api_key(inst.id, "sk-secret")

    def always_fail(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(config, "_atomic_write", always_fail)
    with pytest.raises(OSError):
        config.delete_instance(inst.id)

    # 账号必须还在(回滚语义:要么都成、要么都不成)
    assert config.instance(inst.id) is not None
    assert config.api_key(inst.id) == "sk-secret"


def test_delete_instance_rollback_restores_account_when_recovery_succeeds(monkeypatch, tmp_path):
    """settings 写成功、keys 写失败、恢复写成功:账号与凭据完整回到删除前"""
    import os

    config = ConfigService(home=str(tmp_path / "home"))
    inst = config.add_instance(PlatformType.MINIMAX_CN)
    config.set_api_key(inst.id, "sk-secret")

    real_write = config._atomic_write
    calls: list[str] = []

    def scripted(path, data):
        calls.append(os.path.basename(path))
        if len(calls) == 2:  # 第 2 次写 = keys.json(删除凭据):失败
            raise OSError("disk full")
        return real_write(path, data)  # 恢复那次写放行

    monkeypatch.setattr(config, "_atomic_write", scripted)
    with pytest.raises(OSError):
        config.delete_instance(inst.id)

    # 精确走到目标分支:settings(删) → keys(败) → settings(恢复)
    assert calls == ["settings.json", "keys.json", "settings.json"]
    assert config.instance(inst.id) is not None
    assert config.api_key(inst.id) == "sk-secret"
    # 磁盘上也完整恢复(重开一个实例从盘上读)
    reloaded = ConfigService(home=str(tmp_path / "home"))
    assert reloaded.instance(inst.id) is not None
    assert reloaded.api_key(inst.id) == "sk-secret"


def test_delete_instance_rollback_survives_partial_failure(monkeypatch, tmp_path):
    """settings 写成功、keys 写失败、恢复写也失败:磁盘删除已生效,内存跟随磁盘"""
    import os

    config = ConfigService(home=str(tmp_path / "home"))
    inst = config.add_instance(PlatformType.MINIMAX_CN)
    config.set_api_key(inst.id, "sk-secret")

    real_write = config._atomic_write
    calls: list[str] = []

    def scripted(path, data):
        calls.append(os.path.basename(path))
        if len(calls) in (2, 3):  # keys 写失败 + 恢复 settings 也失败
            raise OSError("disk full")
        return real_write(path, data)

    monkeypatch.setattr(config, "_atomic_write", scripted)
    with pytest.raises(OSError):
        config.delete_instance(inst.id)

    # 以前的写法对第一次非 keys 写入就抛错,根本没进 keys 阶段——这里钉死写序
    assert calls == ["settings.json", "keys.json", "settings.json"]
    # 第三级降级的语义是「内存跟磁盘走」:磁盘上删除已生效、恢复又写不进去,
    # 内存也保持删除态(否则重启后账号凭空消失,内存与磁盘分叉)
    assert config.instance(inst.id) is None
    # 凭据从内存清掉(磁盘 keys.json 还留着,等下次落盘清理)——
    # 旧断言「凭据仍在」描述的是从未执行到的分支
    assert config.api_key(inst.id) == ""


# ------------------------------------------------------------ 入口开关的原子性

def test_set_entry_visibility_writes_once(tmp_path, monkeypatch):
    """两个入口开关一起改必须一次落盘(分两次写第二步失败会留下「双关」)"""
    config = ConfigService(home=str(tmp_path / "home"))
    calls = []
    real_write = config._atomic_write

    def counting_write(path, data):
        calls.append(path)
        return real_write(path, data)

    monkeypatch.setattr(config, "_atomic_write", counting_write)
    config.set_entry_visibility(overlay=False, tray=True)

    assert len(calls) == 1
    assert config.overlay_enabled() is False
    assert config.tray_icon_enabled() is True


# ------------------------------------------------------------ parse_json / to_float

def test_parse_json_rejects_non_object_root():
    """根不是对象:报「响应无效」,不漏 AttributeError"""
    for text in ("[]", '"x"', "5", "null", "true"):
        with pytest.raises(PlatformError) as e:
            parse_json(text)
        assert e.value.kind == ErrorKind.INVALID_RESPONSE


def test_parse_json_still_reports_bad_syntax():
    with pytest.raises(PlatformError) as e:
        parse_json("<html>502</html>")
    assert e.value.kind == ErrorKind.DECODING


def test_to_float_rejects_nan_and_infinity():
    """NaN / Infinity 不能进渲染层:int(NaN) 会抛 ValueError,int(inf) 抛 OverflowError"""
    assert to_float(float("nan")) is None
    assert to_float(float("inf")) is None
    assert to_float("nan") is None
    assert to_float("Infinity") is None
    assert math.isfinite(to_float(1.5))


def test_platform_error_message_is_always_str():
    """错误消息会当 i18n 键查表:必须是字符串,否则不可哈希的值会炸掉详情窗"""
    err = PlatformError(ErrorKind.API, {"code": 10013})
    assert isinstance(err.message, str)


# ------------------------------------------------------------ 编码与代理字符

def test_utf8_bom_settings_is_readable(tmp_path):
    """带 BOM 的 settings.json(记事本等产物)要能读,不能当损坏"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"instances": [{"id": "minimax_cn-1", "platform_type": "minimax_cn", "display_name": "主力"}]}
    )
    (home / "settings.json").write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))

    config = ConfigService(home=str(home))
    assert [i.id for i in config.instances()] == ["minimax_cn-1"]
    assert not (home / "settings.json.corrupt").exists()


def test_surrogate_in_display_name_is_sanitized(tmp_path):
    """显示名里的代理字符会让之后每次落盘都失败:读入时要清掉"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"instances": [{"id": "minimax_cn-1", "platform_type": "minimax_cn", "display_name": "主\ud800力"}]}
    )
    (home / "settings.json").write_text(payload, encoding="utf-8")

    config = ConfigService(home=str(home))
    name = config.instance("minimax_cn-1").display_name
    name.encode("utf-8")  # 不再抛 UnicodeEncodeError
    # 之后的写入也要能成功
    config.set_display_mode("remaining")
    assert config.display_mode() == "remaining"
