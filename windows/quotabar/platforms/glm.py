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

import math

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
from ._base import ServiceCache, parse_json, send, to_float, to_int

CACHE_TTL = 10.0  # 秒;与 macOS 版一致


class GLMPlatformService:
    platform_type = PlatformType.GLM_CN

    def __init__(self):
        self._cache = ServiceCache()

    def clear_cache(self) -> None:
        self._cache.clear()

    def fetch_usage(self, config: PlatformConfigData, network: NetworkFunc) -> PlatformUsageData:
        # begin 在 read 之前:clear 落在两者之间时,旧凭据的在途结果不会以新代次写回
        ver = self._cache.begin()
        cached = self._cache.read(CACHE_TTL)
        if cached is not None:
            return cached

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
            # 健康度:各项剩余比例都要 ≥15%;比例算不出来(非有限)的条目跳过
            is_healthy=all(
                _remaining_ratio(m) >= 15
                for m in data
                if m.total_value and m.total_value > 0
            ),
        )
        self._cache.write(result, ver)
        return result

    def _parse(self, body: dict) -> list[UsageMetric]:
        # 只信 success 布尔,不看 code 字段
        if body.get("success") is False:
            raise PlatformError(ErrorKind.API, str(body.get("msg") or "GLM request failed"))
        data = body.get("data")
        if not isinstance(data, dict):
            raise PlatformError(ErrorKind.INVALID_RESPONSE)
        limits = data.get("limits")
        if not isinstance(limits, list) or not limits:
            raise PlatformError(ErrorKind.INVALID_RESPONSE)

        metrics: list[UsageMetric] = []
        for item in limits:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "TOKENS_LIMIT":
                used_pct = to_float(item.get("percentage"))
                if used_pct is None:
                    continue  # 缺失/类型异常的条目整条跳过
                remaining = max(0.0, 100 - used_pct)
                # unit 用宽松整数归一化:上游把 6 序列化成 "6" 时不能把周额度误判成 5 小时
                label = LABEL_WEEKLY_LIMIT if to_int(item.get("unit")) == 6 else LABEL_FIVE_HOUR
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
                usage = to_float(item.get("usage"))
                remaining_count = to_float(item.get("remaining"))
                if usage is None or remaining_count is None:
                    continue  # 缺次数字段整条跳过,避免谎报 0 次可用
                metrics.append(
                    UsageMetric(
                        label=LABEL_MCP_MONTHLY,
                        current_value=remaining_count,
                        total_value=usage,
                        unit="times",
                        reset_time=_ms_to_seconds(item.get("nextResetTime")),
                    )
                )
        if not metrics:
            # message 用 i18n key,UI 层 t() 后回退原样显示
            raise PlatformError(ErrorKind.API, "error.glm.emptyLimits")
        # 同 label 去重(保留响应首条,与 Swift 侧一致):上游多返回一条同 unit 的
        # TOKENS_LIMIT 时,详情窗会出现两张互相矛盾的「周限额」卡片
        seen_labels: set[str] = set()
        deduped: list[UsageMetric] = []
        for metric in metrics:
            if metric.label in seen_labels:
                continue
            seen_labels.add(metric.label)
            deduped.append(metric)
        # 固定排序:5 小时 → 周 → MCP 月度
        order = {LABEL_FIVE_HOUR: 0, LABEL_WEEKLY_LIMIT: 1, LABEL_MCP_MONTHLY: 2}
        deduped.sort(key=lambda m: order[m.label])
        return deduped


def _remaining_ratio(metric) -> float:
    """剩余比例(百分比);算不出来(非有限)时返回 -1(视作不健康)"""
    try:
        ratio = metric.current_value / metric.total_value * 100
    except ZeroDivisionError:
        return -1.0
    return ratio if math.isfinite(ratio) else -1.0


def _ms_to_seconds(ts) -> float | None:
    """毫秒时间戳转 Unix 秒;缺失、非数字或非正数返回 None(对齐 Swift 的 ms > 0 判定)。

    另加合理性区间(2020~2096):上游把秒当毫秒/把纳秒当毫秒时会算出 1970 年
    或几百年后的假日期,把倒计时画成几百位数字。
    """
    value = to_float(ts)
    if value is None or value <= 0:
        return None
    seconds = value / 1000.0
    if not (1_577_836_800 <= seconds <= 4_000_000_000):
        return None
    return seconds
