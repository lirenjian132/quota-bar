# -*- coding: utf-8 -*-
"""渲染纯函数层:托盘图标文本、状态点颜色、指标数值格式化。

对齐 macOS 版 Views/StatusBarView.swift 里的 StatusBarViewHelper(抽成纯函数便于单测,
不依赖 Qt 运行时)。
"""

from __future__ import annotations

import time
from typing import Optional

from .models import (
    LABEL_WEEKLY_UNLIMITED,
    PlatformUsageData,
    UsageMetric,
    label_family,
)

COLOR_GRAY = "gray"    # 无数据
COLOR_RED = "red"
COLOR_YELLOW = "yellow"
COLOR_GREEN = "green"


def filter_metrics(metrics: list[UsageMetric], enabled_labels: list[str]) -> list[UsageMetric]:
    """按勾选的 label 过滤指标(最多 2 个),带同族匹配、去重与回退。

    - 同族匹配:勾了 weekly_limit,返回的 weekly_limit_boosted/unlimited 也能命中
    - 去重:勾了两个同族 label(如周额度+周额度加成)时,同一个 metric 只取一次
    - 全无交集:回退前 2 个 metric(防状态栏恒显 "--")
    """
    picked: list[UsageMetric] = []
    seen: set[int] = set()
    for want in enabled_labels[:2]:
        fam = label_family(want)
        for m in metrics:
            if m.label in fam and id(m) not in seen:
                picked.append(m)
                seen.add(id(m))
                break
    if picked:
        return picked[:2]
    return metrics[:2]


def _ratio(metric: UsageMetric) -> Optional[float]:
    """剩余比例(0~1);无 total 的绝对量型返回 None"""
    if metric.total_value is None or metric.total_value <= 0:
        return None
    return metric.current_value / metric.total_value


def status_color(data: Optional[PlatformUsageData]) -> str:
    """状态点颜色;判定顺序与 macOS 版一致,顺序不可变:
    无数据→灰;空 metrics→灰;不健康→红;首指标比例<10%红、<50%黄、否则绿;
    绝对量型(余额)看到期时间 <3 天红、<7 天黄。
    """
    if data is None or not data.metrics:
        return COLOR_GRAY
    if not data.is_healthy:
        return COLOR_RED
    first = data.metrics[0]
    ratio = _ratio(first)
    if ratio is not None:
        if ratio < 0.1:
            return COLOR_RED
        if ratio < 0.5:
            return COLOR_YELLOW
        return COLOR_GREEN
    if first.reset_time is not None:
        days_left = (first.reset_time - time.time()) / 86400
        if days_left < 3:
            return COLOR_RED
        if days_left < 7:
            return COLOR_YELLOW
    return COLOR_GREEN


def format_balance(value: float) -> str:
    """余额缩写:>=1000 显示 "1.2K",否则取整"""
    if value >= 1000:
        return f"{value / 1000:.1f}K"
    return f"{value:.0f}"


def tray_text(metric: Optional[UsageMetric], display_mode: str) -> str:
    """托盘图标数字:百分比向零截断不带 %(已用/剩余口径,对齐 Swift Int())、余额 K 缩写、无限显示 ∞"""
    if metric is None:
        return "--"
    if metric.unit == "unlimited" or metric.label == LABEL_WEEKLY_UNLIMITED:
        return "∞"
    ratio = _ratio(metric)
    if ratio is not None:
        pct = (1 - ratio) * 100 if display_mode == "used" else ratio * 100
        return str(int(pct))
    if metric.unit == "CNY":
        return format_balance(metric.current_value)
    return str(int(metric.current_value))


def format_credits_count(value: float, locale: str) -> str:
    """大数次数格式化,对齐 Swift formatCredits:Int 截断 + 缩写两位小数(中文万/亿,英文 K/M)"""
    v = int(value)
    if locale.startswith("zh"):
        if v >= 100_000_000:
            return f"{v / 1e8:.2f}亿"
        if v >= 10_000:
            return f"{v / 1e4:.2f}万"
        return str(v)
    if v >= 1_000_000:
        return f"{v / 1e6:.2f}M"
    if v >= 1_000:
        return f"{v / 1e3:.2f}K"
    return str(v)


def metric_detail_text(metric: UsageMetric, locale: str) -> str:
    """详情卡片数值文本:
    百分比型 → "剩余: 80%";有 total → "剩余: cur/total 单位";绝对量 → "N 单位"
    """
    ratio = _ratio(metric)
    if metric.unit == "%" and ratio is not None:
        return f"{int(ratio * 100)}%"
    if metric.total_value is not None:
        return f"{int(metric.current_value)}/{format_credits_count(metric.total_value, locale)}"
    return f"{metric.current_value:.0f}"


def reset_countdown(reset_time: float, now: Optional[float] = None) -> tuple[str, Optional[tuple[int, int]]]:
    """重置倒计时。返回 (文案 kind, 载荷):
    - "soon":即将重置(≤1 分钟)
    - "hm":(时, 分),还剩 H 小时 M 分(<24 小时)
    - "days":(天, 小时),跨天(>24 小时,对齐 Swift 的 hours > 24 阈值)
    """
    now = now if now is not None else time.time()
    remain = reset_time - now
    if remain <= 60:
        return "soon", None
    total_min = int(remain // 60)
    days, rest_min = divmod(total_min, 1440)
    if total_min > 24 * 60:
        return "days", (days, rest_min // 60)
    return "hm", divmod(total_min, 60)
