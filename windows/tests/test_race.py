# -*- coding: utf-8 -*-
"""竞态回归测试:换 key 后旧请求晚到,不得污染界面数据与 service 缓存。"""

import time

from quotabar.config import ConfigService
from quotabar.manager import PlatformManager
from quotabar.models import PlatformType
from quotabar.platforms._base import ServiceCache
from tests.conftest import FakeNetwork, json_response

GLM_LIMITS = [
    {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 20, "nextResetTime": 1780329600000}
]


def glm_ok() -> dict:
    return {"code": 200, "msg": "ok", "success": True, "data": {"limits": GLM_LIMITS}}


def test_cache_stale_write_discarded():
    """取数期间缓存被清(换 key):旧代次的写回被丢弃,不污染缓存"""
    cache = ServiceCache()
    ver = cache.begin()          # 旧 key 请求开始,记下代次
    cache.clear()                # 取数在途时用户换了 key → 清缓存(代次推进)
    cache.write("旧账号的数据", ver)  # 旧 key 的响应此刻才回来
    assert cache.read(300) is None  # 旧数据没进缓存

    # 新代次正常写入
    ver2 = cache.begin()
    cache.write("新账号的数据", ver2)
    assert cache.read(300) == "新账号的数据"


def test_cache_write_without_version_always_accepted():
    """不带代次的写(测试/兼容路径)不受影响"""
    cache = ServiceCache()
    cache.clear()
    cache.write("v")
    assert cache.read(10) == "v"


def test_manager_remove_instance_releases_service(tmp_path):
    """删除账号后 service 被释放:再次取同 id(理论不会发生)会新建 service 而非复用旧缓存"""
    cfg = ConfigService(home=str(tmp_path))
    a = cfg.add_instance(PlatformType.GLM_CN)
    cfg.set_api_key(a.id, "k1")
    net = FakeNetwork(json_response(glm_ok()), json_response(glm_ok()))
    mgr = PlatformManager(cfg, network=net)
    mgr.fetch_usage(a.id)
    svc = mgr.service_for(a.id)
    mgr.remove_instance(a.id)
    assert mgr.service_for(a.id) is not svc  # 新对象,旧缓存不再持有
