# -*- coding: utf-8 -*-
"""编排层测试:取数范围、并发结果、缓存按账号隔离、清缓存。"""

from quotabar.config import ConfigService
from quotabar.manager import PlatformManager
from quotabar.models import ErrorKind, PlatformType
from tests.conftest import FakeNetwork, json_response

GLM_LIMITS = [
    {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 20, "nextResetTime": 1780329600000}
]


def glm_ok() -> dict:
    return {"code": 200, "msg": "ok", "success": True, "data": {"limits": GLM_LIMITS}}


def test_fetch_all_skips_unconfigured_and_disabled(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    cfg.set_api_key(a.id, "k1")
    b = cfg.add_instance(PlatformType.GLM_CN)          # 未配 key → 跳过
    c = cfg.add_instance(PlatformType.MINIMAX_CN)
    cfg.set_api_key(c.id, "k3")
    cfg.set_enabled(c.id, False)                        # 禁用 → 跳过

    net = FakeNetwork(json_response(glm_ok()))
    mgr = PlatformManager(cfg, network=net)
    results = mgr.fetch_all()
    assert [r.instance_id for r in results] == [a.id]
    assert net.call_count == 1


def test_fetch_all_partial_failure_isolated(tmp_path):
    """单实例失败不影响其他实例"""
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    cfg.set_api_key(a.id, "k1")
    b = cfg.add_instance(PlatformType.GLM_CN)
    cfg.set_api_key(b.id, "k2")

    net = FakeNetwork(
        json_response(glm_ok()),
        json_response({}, status_code=401),  # b 未授权
    )
    mgr = PlatformManager(cfg, network=net)
    results = {r.instance_id: r for r in mgr.fetch_all()}
    assert results[a.id].data is not None
    assert results[b.id].error is not None
    assert results[b.id].error.kind == ErrorKind.UNAUTHORIZED


def test_unexpected_exception_wrapped(tmp_path):
    """未预期异常(如网络层抛 requests 异常)被兜住包成 NETWORK 错误"""
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    cfg.set_api_key(a.id, "k1")

    def boom(req):
        raise RuntimeError("dns fail")

    mgr = PlatformManager(cfg, network=boom)
    results = mgr.fetch_all()
    assert results[0].error.kind == ErrorKind.NETWORK
    assert "dns fail" in results[0].error.message


def test_cache_isolated_per_instance(tmp_path):
    """两个账号各自持有 service:缓存不互命中"""
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    cfg.set_api_key(a.id, "k1")
    b = cfg.add_instance(PlatformType.GLM_CN)
    cfg.set_api_key(b.id, "k2")

    net = FakeNetwork(*[json_response(glm_ok()) for _ in range(4)])
    mgr = PlatformManager(cfg, network=net)
    mgr.fetch_usage(a.id)
    mgr.fetch_usage(a.id)   # TTL 内命中缓存
    mgr.fetch_usage(b.id)   # b 有自己的缓存 → 必须发请求
    mgr.fetch_usage(b.id)
    assert net.call_count == 2


def test_clear_all_caches_forces_refetch(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    cfg.set_api_key(a.id, "k1")
    net = FakeNetwork(json_response(glm_ok()), json_response(glm_ok()))
    mgr = PlatformManager(cfg, network=net)
    mgr.fetch_usage(a.id)
    mgr.clear_all_caches()
    mgr.fetch_usage(a.id)
    assert net.call_count == 2


def test_not_configured_raises(tmp_path):
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    mgr = PlatformManager(cfg, network=FakeNetwork())
    try:
        mgr.fetch_usage(a.id)
        assert False, "应抛 PlatformError"
    except Exception as e:
        assert "notConfigured" in str(e)
