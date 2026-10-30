# -*- coding: utf-8 -*-
"""多账号编排层:对齐 macOS 版 PlatformManager。

- 每账号实例持一个独立 service 对象(服务内缓存天然按账号隔离)
- fetch_all 并发拉取所有「已启用且已配置」的实例
- 单实例取数失败不影响其他实例(FetchResult 分别携带 data / error)
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, Optional

from .config import ConfigService
from .models import PlatformConfigData, PlatformError, PlatformType, PlatformUsageData
from .net import NetworkFunc, requests_network
from .platforms.glm import GLMPlatformService
from .platforms.minimax import MiniMaxPlatformService
from .platforms.stepfun import StepFunPlatformService
from .platforms.stepfun_refresh import (
    needs_refresh,
    parse_credential,
    refresh_credential,
)
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
        #: 每实例一把续期锁:并发 fetch_all 与单账号刷新不能对同一账号双发 RefreshToken
        self._refresh_locks: Dict[str, threading.Lock] = {}
        #: 续期失败冷却(秒):refresh token 失效后不再每个周期白打一次 RefreshToken
        self._refresh_cooldown: Dict[str, float] = {}
        #: 冷却世代:用户保存新凭据时递增,在途失败的刷新不再重新武装冷却
        self._refresh_generation: Dict[str, int] = {}
        self._REFRESH_COOLDOWN_SECONDS = 300.0

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
        # Stepfun 会话预刷新:access 票快过期时先换新凭据再发主请求。
        # 刷新失败不阻断主流程,旧凭据若已失效会走既有 401 -> 重新登录提示。
        if inst.platform_type == PlatformType.STEPFUN:
            self._refresh_stepfun_if_needed(instance_id)
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

    def _refresh_stepfun_if_needed(self, instance_id: str) -> None:
        """Stepfun 自动续期:本地判断 exp,快过期才发 RefreshToken。

        - 每实例一把锁:fetch_all 的线程池与单账号刷新可能同时进来,只发一次;
        - 锁内重读凭据:别的线程可能已经刷新过,不必重复;
        - CAS 写回:只有当前凭据仍是本次刷新用的旧串才落盘。用户在刷新在途时
          保存了新凭据时,别人的更新优先,不能被旧凭据刷出的串覆盖。
        """
        credential = self._config.api_key(instance_id)
        parsed = parse_credential(credential)
        if not needs_refresh(parsed):
            return
        if self._refresh_on_cooldown(instance_id):
            return
        with self._lock:
            lock = self._refresh_locks.setdefault(instance_id, threading.Lock())
        with lock:
            credential = self._config.api_key(instance_id)
            parsed = parse_credential(credential)
            if not needs_refresh(parsed):
                return
            generation = self._refresh_generation_of(instance_id)
            fresh = refresh_credential(credential, self._network, parsed=parsed)
            if not fresh:
                self._mark_refresh_cooldown(instance_id, generation)
                return
            try:
                # 原子 CAS:比较与写入在同一把锁内完成。刷新在途期间用户换了
                # key/重新登录,则放弃写回(别人的更新优先),不会把用户的新
                # 凭据覆盖成旧凭据刷出的串。
                # KeyError:实例在刷新途中被删,跳过即可。
                self._config.compare_and_set_api_key(instance_id, credential, fresh)
            except (OSError, TypeError, UnicodeEncodeError, KeyError):
                # 落盘失败:不阻断本次主请求;下次刷新会再试
                pass

    def _refresh_on_cooldown(self, instance_id: str) -> bool:
        with self._lock:
            return time.time() < self._refresh_cooldown.get(instance_id, 0.0)

    def _refresh_generation_of(self, instance_id: str) -> int:
        with self._lock:
            return self._refresh_generation.get(instance_id, 0)

    def _mark_refresh_cooldown(self, instance_id: str, generation: int) -> None:
        with self._lock:
            # 只有"发起刷新时那个世代"才武装:期间用户存了新凭据(世代已变)
            # 就不冷却,避免刚换的 key 被在途失败拖进 5 分钟
            if self._refresh_generation.get(instance_id, 0) != generation:
                return
            self._refresh_cooldown[instance_id] = time.time() + self._REFRESH_COOLDOWN_SECONDS

    def clear_refresh_cooldown(self, instance_id: str) -> None:
        """用户保存/重登新凭据:解除冷却并推进世代(在途失败不再重新武装)"""
        with self._lock:
            self._refresh_cooldown.pop(instance_id, None)
            self._refresh_generation[instance_id] = self._refresh_generation.get(instance_id, 0) + 1

    def remove_instance(self, instance_id: str) -> None:
        """账号删除后清理其 service(连带缓存),防已删账号的对象常驻内存"""
        with self._lock:
            self._services.pop(instance_id, None)
            self._refresh_locks.pop(instance_id, None)
            self._refresh_cooldown.pop(instance_id, None)
            self._refresh_generation.pop(instance_id, None)

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
