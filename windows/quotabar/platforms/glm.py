# -*- coding: utf-8 -*-
"""GLM(智谱)取数服务。移植自 GLMPlatformService.swift。

契约要点:
- GET {api_base_url}(模板里就是完整端点;国际区走 api.z.ai 变体)
- 鉴权:Authorization: <apiKey>(无 Bearer 前缀,auth_prefix 为空串)
- percentage 字段是「已用」百分比,剩余 = 100 - percentage
- nextResetTime 是毫秒时间戳,需 /1000
- TOKENS_LIMIT:unit==6 → 周额度,否则(含 3)→ 5 小时额度
- 缺 percentage 的 TOKENS_LIMIT / 缺 usage|remaining 的 TIME_LIMIT 整条跳过(不谎报)
"""

from __future__ import annotations

from ..models import (
    LABEL_FIVE_HOUR,
    LABEL_MCP_MONTHLY,
    LABEL_WEEKLY_LIMIT,
    ErrorKind,
    PlatformConfigData,
    PlatformError,
    PlatformType,
    PlatformUsageData,
    UsageMetric,
)
from ..net import NetworkFunc, Request
from ._base import ServiceCache, parse_json, send

CACHE_TTL = 10.0  # 秒;与 macOS 版一致


class GLMPlatformService:
    platform_type = PlatformType.GLM_CN

    def __init__(self):
        self._cache = ServiceCache()

    def clear_cache(self) -> None:
        self._cache.clear()

    def fetch_usage(self, config: PlatformConfigData, network: NetworkFunc) -> PlatformUsageData:
        cached = self._cache.read(CACHE_TTL)
        if cached is not None:
            return cached
        ver = self._cache.begin()

        resp = send(
            network,
            Request(
                method="GET",
                url=config.resolved_base_url(),
                headers={"Authorization": config.api_key, "Accept": "application/json"},
                timeout=10.0,
            ),
        )
        data = self._parse(parse_json(resp.text))
        result = PlatformUsageData(
            platform=self.platform_type,
            instance_id=config.instance_id,
            display_name=config.display_name,
            metrics=data,
            is_healthy=all(
                m.current_value / m.total_value * 100 >= 15
                for m in data
                if m.total_value and m.total_value > 0
            ),
        )
        self._cache.write(result, ver)
        return result

    def _parse(self, body: dict) -> list[UsageMetric]:
        # 只信 success 布尔,不看 code 字段
        if body.get("success") is False:
            raise PlatformError(ErrorKind.API, body.get("msg") or "GLM request failed")
        limits = (body.get("data") or {}).get("limits")
        if not limits:
            raise PlatformError(ErrorKind.INVALID_RESPONSE)

        metrics: list[UsageMetric] = []
        for item in limits:
            kind = item.get("type")
            if kind == "TOKENS_LIMIT":
                try:
                    used_pct = float(item["percentage"])
                except (KeyError, TypeError, ValueError):
                    continue  # 缺失/类型异常的条目整条跳过
                remaining = max(0.0, 100 - used_pct)
                label = LABEL_WEEKLY_LIMIT if item.get("unit") == 6 else LABEL_FIVE_HOUR
                metrics.append(
                    UsageMetric(
                        label=label,
                        current_value=remaining,
                        total_value=100,
                        unit="%",
                        reset_time=_ms_to_seconds(item.get("nextResetTime")),
                    )
                )
            elif kind == "TIME_LIMIT":
                if item.get("usage") is None or item.get("remaining") is None:
                    continue  # 缺次数字段整条跳过,避免谎报 0 次可用
                metrics.append(
                    UsageMetric(
                        label=LABEL_MCP_MONTHLY,
                        current_value=float(item["remaining"]),
                        total_value=float(item["usage"]),
                        unit="times",
                        reset_time=_ms_to_seconds(item.get("nextResetTime")),
                    )
                )
        if not metrics:
            # message 用 i18n key,UI 层 t() 后回退原样显示
            raise PlatformError(ErrorKind.API, "error.glm.emptyLimits")
        # 固定排序:5 小时 → 周 → MCP 月度
        order = {LABEL_FIVE_HOUR: 0, LABEL_WEEKLY_LIMIT: 1, LABEL_MCP_MONTHLY: 2}
        metrics.sort(key=lambda m: order[m.label])
        return metrics


def _ms_to_seconds(ts) -> float | None:
    """毫秒时间戳转 Unix 秒;缺失或非正数返回 None(对齐 Swift 的 ms > 0 判定)"""
    if ts is None or ts <= 0:
        return None
    return float(ts) / 1000.0
