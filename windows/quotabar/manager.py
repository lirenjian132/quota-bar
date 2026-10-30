# -*- coding: utf-8 -*-
"""多账号编排层:对齐 macOS 版 PlatformManager。

- 每账号实例持一个独立 service 对象(服务内缓存天然按账号隔离)
- fetch_all 并发拉取所有「已启用且已配置」的实例
- 单实例取数失败不影响其他实例(FetchResult 分别携带 data / error)
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, Optional

from .config import ConfigService
from .models import PlatformConfigData, PlatformError, PlatformType, PlatformUsageData
from .net import NetworkFunc, requests_network
from .platforms.glm import GLMPlatformService
from .platforms.minimax import MiniMaxPlatformService
from .platforms.stepfun import StepFunPlatformService
from .platforms.tokenrhythm import TokenRhythmPlatformService

#: 平台 → service 工厂(对齐 Swift 版 PlatformManager.serviceFactory)
SERVICE_FACTORIES = {
    PlatformType.GLM_CN: GLMPlatformService,
    PlatformType.MINIMAX_CN: MiniMaxPlatformService,
    PlatformType.STEPFUN: StepFunPlatformService,
    PlatformType.TOKENRHYTHM: TokenRhythmPlatformService,
}


@dataclass
class FetchResult:
    """单实例取数结果:data 与 error 二选一"""

    instance_id: str
    data: Optional[PlatformUsageData] = None
    error: Optional[PlatformError] = None


class PlatformManager:
    def __init__(self, config: ConfigService, network: Optional[NetworkFunc] = None):
        self._config = config
        self._network = network or requests_network
        self._services: Dict[str, object] = {}
        self._lock = threading.Lock()

    def service_for(self, instance_id: str):
        """每实例一个 service 对象;缓存与在途状态按账号隔离"""
        with self._lock:
            svc = self._services.get(instance_id)
            if svc is None:
                inst = self._config.instance(instance_id)
                if inst is None:
                    raise KeyError(f"实例不存在: {instance_id}")
                svc = SERVICE_FACTORIES[inst.platform_type]()
                self._services[instance_id] = svc
            return svc

    def fetch_usage(self, instance_id: str) -> PlatformUsageData:
        """拉取单实例用量;失败抛 PlatformError"""
        inst = self._config.instance(instance_id)
        if inst is None:
            # 实例不存在(已删/从未存在):别回落成「平台未配置」——
            # 那是误导性文案,会把用户引去改一个根本不存在的账号配置
            raise KeyError(f"实例不存在: {instance_id}")
        if not self._config.is_configured(instance_id):
            raise _not_configured(inst.platform_type)
        cfg: PlatformConfigData = self._config.config_data(instance_id)
        return self.service_for(instance_id).fetch_usage(cfg, self._network)

    def fetch_all(self) -> list[FetchResult]:
        """并发拉取所有已启用且已配置的实例"""
        targets = [
            it.id
            for it in self._config.instances()
            if self._config.is_enabled(it.id) and self._config.is_configured(it.id)
        ]
        results: list[FetchResult] = []
        if not targets:
            return results
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            futures = {pool.submit(self._fetch_one_safe, iid): iid for iid in targets}
            for fut in futures:
                results.append(fut.result())
        # 保持与实例注册表相同的顺序,UI 端好对齐
        order = {iid: i for i, iid in enumerate(targets)}
        results.sort(key=lambda r: order.get(r.instance_id, 1 << 30))
        return results

    def _fetch_one_safe(self, instance_id: str) -> FetchResult:
        try:
            return FetchResult(instance_id=instance_id, data=self.fetch_usage(instance_id))
        except PlatformError as e:
            return FetchResult(instance_id=instance_id, error=e)
        except Exception as e:  # 未预期异常也兜住,不拖垮整批
            return FetchResult(instance_id=instance_id, error=_wrap_unexpected(e))

    def remove_instance(self, instance_id: str) -> None:
        """账号删除后清理其 service(连带缓存),防已删账号的对象常驻内存"""
        with self._lock:
            self._services.pop(instance_id, None)

    def clear_all_caches(self) -> None:
        """「立即刷新」入口:清所有实例缓存,下次取数强制走网络"""
        with self._lock:
            services = list(self._services.values())
        for svc in services:
            svc.clear_cache()


def _not_configured(platform: PlatformType) -> PlatformError:
    from .models import ErrorKind

    return PlatformError(ErrorKind.NOT_CONFIGURED, str(platform.value))


def _wrap_unexpected(e: Exception) -> PlatformError:
    from .models import ErrorKind

    return PlatformError(ErrorKind.NETWORK, f"{type(e).__name__}: {e}")
