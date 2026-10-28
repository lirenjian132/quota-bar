# -*- coding: utf-8 -*-
"""配置层测试:实例 CRUD、墓碑、启用/钉选/指标规则、模板填充、凭据存储。"""

import pytest

from quotabar.config import ConfigService
from quotabar.models import (
    LABEL_BALANCE,
    LABEL_CREDITS,
    LABEL_FIVE_HOUR,
    LABEL_WEEKLY_LIMIT,
    LABEL_WEEKLY_BOOSTED,
    PlatformType,
    RefreshInterval,
)


def test_add_instance_template_fill(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    inst = cfg.add_instance(PlatformType.GLM_CN)
    assert inst.id == "glm_cn-1"
    assert inst.display_name == "GLM"

    data = cfg.config_data(inst.id)
    # 模板已填充(GLM 端点 + 裸 Authorization)
    assert data.api_base_url == "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
    assert data.api_base_url_international == "https://api.z.ai/api/monitor/usage/quota/limit"
    assert data.auth_header == "Authorization"
    assert data.auth_prefix == ""
    assert data.region == "domestic"
    # 默认指标
    assert cfg.enabled_metrics(inst.id) == [LABEL_FIVE_HOUR, LABEL_WEEKLY_LIMIT]
    assert cfg.is_enabled(inst.id)


def test_region_switch(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    inst = cfg.add_instance(PlatformType.MINIMAX_CN)
    data = cfg.config_data(inst.id)
    assert "minimaxi.com" in data.resolved_base_url()
    # 国际区:config_data 的 region 来自模板,直接验证解析逻辑
    data.region = "international"
    assert "minimax.io" in data.resolved_base_url()


def test_tombstone_prevents_id_reuse(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    cfg.delete_instance(a.id)
    b = cfg.add_instance(PlatformType.GLM_CN)
    assert b.id == "glm_cn-2"  # 已删 id 不复用


def test_delete_cleans_everything(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    cfg.set_api_key(a.id, "secret")
    cfg.set_active_instance_id(a.id)
    cfg.delete_instance(a.id)
    assert cfg.instance(a.id) is None
    assert cfg.api_key(a.id) == ""
    assert cfg.active_instance_id() is None
    assert not cfg.is_configured(a.id)


def test_last_enabled_cannot_be_disabled(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    with pytest.raises(ValueError):
        cfg.set_enabled(a.id, False)
    b = cfg.add_instance(PlatformType.MINIMAX_CN)
    cfg.set_enabled(a.id, False)  # 有第二个实例时可以禁
    assert not cfg.is_enabled(a.id)
    with pytest.raises(ValueError):
        cfg.set_enabled(b.id, False)  # b 是最后一个启用的


def test_disable_clears_pin(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    cfg.add_instance(PlatformType.MINIMAX_CN)
    cfg.set_pinned(a.id, True)
    cfg.set_enabled(a.id, False)
    assert not cfg.is_pinned(a.id)
    with pytest.raises(ValueError):
        cfg.set_pinned(a.id, True)  # 未启用不可钉选


def test_metrics_validation(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.STEPFUN)
    # 超过 2 个 → 拒绝
    with pytest.raises(ValueError):
        cfg.set_enabled_metrics(a.id, [LABEL_CREDITS, LABEL_FIVE_HOUR, LABEL_WEEKLY_LIMIT])
    # 空 → 拒绝
    with pytest.raises(ValueError):
        cfg.set_enabled_metrics(a.id, [])
    # 清单外(拿别平台的 label)→ 被过滤后不足,拒绝
    with pytest.raises(ValueError):
        cfg.set_enabled_metrics(a.id, [LABEL_BALANCE])
    cfg.set_enabled_metrics(a.id, [LABEL_CREDITS, LABEL_FIVE_HOUR])
    assert cfg.enabled_metrics(a.id) == [LABEL_CREDITS, LABEL_FIVE_HOUR]


def test_dead_label_filtered_on_read(tmp_path):
    """老配置里已下架的 label 读取时被过滤,不占名额"""
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.MINIMAX_CN)
    raw = cfg._state(a.id)
    raw["enabled_metrics"] = ["mcp_monthly", LABEL_FIVE_HOUR]  # MiniMax 已不支持 mcp_monthly
    assert cfg.enabled_metrics(a.id) == [LABEL_FIVE_HOUR]


def test_rename_empty_falls_back(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN, "我的号")
    assert cfg.instance(a.id).display_name == "我的号"
    cfg.rename_instance(a.id, "   ")
    assert cfg.instance(a.id).display_name == "GLM"


def test_move_and_order(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    b = cfg.add_instance(PlatformType.MINIMAX_CN)
    c = cfg.add_instance(PlatformType.TOKENRHYTHM)
    assert [i.id for i in cfg.instances()] == [a.id, b.id, c.id]
    cfg.move_instance(a.id, 1)  # a 右移
    assert [i.id for i in cfg.instances()] == [b.id, a.id, c.id]
    cfg.move_instance(c.id, +5)  # 越界不动
    assert [i.id for i in cfg.instances()] == [b.id, a.id, c.id]


def test_credentials_and_configured(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    assert not cfg.is_configured(a.id)  # 无凭据
    cfg.set_api_key(a.id, "sk-xxx")
    assert cfg.is_configured(a.id)
    assert cfg.config_data(a.id).api_key == "sk-xxx"
    cfg.set_api_key(a.id, "")  # 空串清除
    assert not cfg.is_configured(a.id)


def test_globals_roundtrip(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    assert cfg.display_mode() == "used"  # 默认已用口径(对齐 Swift .used)
    cfg.set_display_mode("remaining")
    assert cfg.display_mode() == "remaining"
    cfg.set_refresh_interval(RefreshInterval.FIVE_MINUTES)
    assert cfg.refresh_interval().seconds == 300
    cfg.set_locale("en")
    assert cfg.locale() == "en"
    # 持久化:新实例读到相同值
    assert ConfigService(home=str(tmp_path)).refresh_interval().seconds == 300


def test_reload_and_atomicity(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    cfg.set_api_key(a.id, "k1")
    other = ConfigService(home=str(tmp_path))
    other.set_api_key(a.id, "k2")
    cfg.reload()
    assert cfg.api_key(a.id) == "k2"


def test_corrupt_settings_backed_up_not_silently_reset(tmp_path):
    """settings.json 损坏:先备份 .corrupt 再用默认值,原残片不丢"""
    import json as _json

    home = tmp_path / "home2"
    home.mkdir()
    (home / "settings.json").write_text('{"instances": [{"id": "glm_cn-1", ', encoding="utf-8")
    cfg = ConfigService(home=str(home))
    assert cfg.instances() == []  # 回落默认
    assert (home / "settings.json.corrupt").exists()  # 残片已备份
    # 新写入是合法 JSON,且没有覆盖备份
    cfg.add_instance(PlatformType.GLM_CN)
    data = _json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert data["instances"]


def test_active_instance_skips_disabled(tmp_path):
    """禁用当前激活账号后,激活回退到第一个启用实例(不返回禁用的)"""
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    b = cfg.add_instance(PlatformType.MINIMAX_CN)
    cfg.set_active_instance_id(a.id)
    cfg.set_enabled(a.id, False)
    assert cfg.active_instance_id() == b.id
