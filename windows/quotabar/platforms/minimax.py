# -*- coding: utf-8 -*-
"""MiniMax 取数服务。移植自 MiniMaxPlatformService.swift。

契约要点:
- GET {api_base_url}/coding_plan/remains(国际区 api.minimax.io 变体)
- 鉴权:Authorization: Bearer <apiKey>
- base_resp 可为 null(老接口),非 null 才检查 status_code
- model_remains 选 model_name=="general",找不到回退首项(防 API 改名整体挂掉)
- 周额度三态:current_weekly_status==1 时,weekly_boost_permille>1000 为加成态,
  否则普通态;其它值为无限态(∞)
- 时间戳双形态:end_time 是毫秒绝对值,缺失时退化用 remains_time(相对毫秒)+now
"""

from __future__ import annotations

import time

from ..models import (
    LABEL_FIVE_HOUR,
    LABEL_WEEKLY_BOOSTED,
    LABEL_WEEKLY_LIMIT,
    LABEL_WEEKLY_UNLIMITED,
    ErrorKind,
    PlatformConfigData,
    PlatformError,
    PlatformType,
    PlatformUsageData,
    UsageMetric,
)
from ..net import NetworkFunc, Request
from ._base import ServiceCache, parse_json, send, to_float

CACHE_TTL = 10.0  # 秒;与 macOS 版一致


class MiniMaxPlatformService:
    platform_type = PlatformType.MINIMAX_CN

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
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Accept": "application/json",
                },
                timeout=10.0,
            ),
            body_in_error=True,
        )
        general = self._pick_general(parse_json(resp.text))
        metrics = self._parse_metrics(general)
        # 健康度只看 5 小时剩余(周额度不影响)
        five_hour = next((m for m in metrics if m.label == LABEL_FIVE_HOUR), None)
        healthy = five_hour is None or five_hour.current_value >= 15.0
        result = PlatformUsageData(
            platform=self.platform_type,
            instance_id=config.instance_id,
            display_name=config.display_name,
            metrics=metrics,
            is_healthy=healthy,
        )
        self._cache.write(result, ver)
        return result

    def _pick_general(self, body: dict) -> dict:
        base_resp = body.get("base_resp")
        if base_resp is not None:
            if not isinstance(base_resp, dict):
                raise PlatformError(ErrorKind.INVALID_RESPONSE)
            if base_resp.get("status_code") not in (None, 0):
                msg = base_resp.get("status_msg") or f"status_code {base_resp.get('status_code')}"
                raise PlatformError(ErrorKind.API, str(msg))
        remains = body.get("model_remains")
        if not isinstance(remains, list) or not remains:
            raise PlatformError(ErrorKind.INVALID_RESPONSE)
        for item in remains:
            if isinstance(item, dict) and item.get("model_name") == "general":
                return item
        first = remains[0]
        if not isinstance(first, dict):
            raise PlatformError(ErrorKind.INVALID_RESPONSE)
        return first

    def _parse_metrics(self, item: dict) -> list[UsageMetric]:
        now = time.time()
        # 缺失 → 0(对齐 Swift 的 ?? 0);存在但不是数字 → 响应无效(不谎报)
        five_hour = to_float(item.get("current_interval_remaining_percent"))
        if five_hour is None:
            if item.get("current_interval_remaining_percent") is not None:
                raise PlatformError(ErrorKind.INVALID_RESPONSE)
            five_hour = 0.0
        metrics = [
            UsageMetric(
                label=LABEL_FIVE_HOUR,
                current_value=five_hour,
                total_value=100,
                unit="%",
                reset_time=_reset_time(
                    item.get("end_time"), item.get("remains_time"), now
                ),
            )
        ]
        metrics.append(self._weekly_metric(item, now))
        return metrics

    @staticmethod
    def _weekly_metric(item: dict, now: float) -> UsageMetric:
        if item.get("current_weekly_status") == 1:
            boost_permille = to_float(item.get("weekly_boost_permille")) or 0.0
            label = (
                LABEL_WEEKLY_BOOSTED
                if boost_permille > 1000
                else LABEL_WEEKLY_LIMIT
            )
            weekly = to_float(item.get("current_weekly_remaining_percent"))
            if weekly is None:
                if item.get("current_weekly_remaining_percent") is not None:
                    raise PlatformError(ErrorKind.INVALID_RESPONSE)
                weekly = 0.0
            return UsageMetric(
                label=label,
                current_value=weekly,
                total_value=100,
                unit="%",
                reset_time=_reset_time(
                    item.get("weekly_end_time"), item.get("weekly_remains_time"), now
                ),
            )
        # 无限态:额度不设限,显示 ∞
        return UsageMetric(
            label=LABEL_WEEKLY_UNLIMITED,
            current_value=0,
            total_value=None,
            unit="unlimited",
            reset_time=None,
        )


def _reset_time(absolute_ms, relative_ms, now: float) -> float | None:
    """重置时刻:优先毫秒绝对时间戳(>0 才算数);缺失/非正退化 now + 相对毫秒"""
    absolute = to_float(absolute_ms)
    if absolute is not None and absolute > 0:
        return absolute / 1000.0
    relative = to_float(relative_ms)
    if relative is not None and relative > 0:
        return now + relative / 1000.0
    return None
