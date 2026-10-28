# -*- coding: utf-8 -*-
"""配置与存储层:合并 macOS 版的 ConfigService / PlatformInstanceStore /
PlatformConfigStore / FileKeyStore 四个存储的职责。

- settings.json:账号注册表 + 全局配置 + 每实例非密配置
- keys.json:凭据(API key / cookie),与 macOS 版 FileKeyStore 对应
- 数据目录:%APPDATA%\\QuotaBar;测试用 QUOTABAR_HOME 环境变量重定向隔离
- 写入均为原子写(临时文件 + os.replace)
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Optional

from .models import (
    AVAILABLE_METRIC_LABELS,
    DEFAULT_ENABLED_METRICS,
    PlatformInstance,
    PlatformType,
    RefreshInterval,
)

SETTINGS_FILE = "settings.json"
KEYS_FILE = "keys.json"

_DISPLAY_USED = "used"
_DISPLAY_REMAINING = "remaining"


def default_home() -> str:
    """数据目录:QUOTABAR_HOME 覆盖(测试隔离),默认 %APPDATA%\\QuotaBar"""
    env = os.environ.get("QUOTABAR_HOME")
    if env:
        return env
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, "QuotaBar")


def _resources_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Resources")


class ConfigService:
    """全部配置读写的唯一入口;自身保证线程安全与落盘原子性"""

    def __init__(self, home: Optional[str] = None):
        self._home = home or default_home()
        self._settings_path = os.path.join(self._home, SETTINGS_FILE)
        self._keys_path = os.path.join(self._home, KEYS_FILE)
        self._lock = threading.RLock()
        self._settings = self._load_json(self._settings_path)
        if self._settings is None:
            self._settings = self._default_settings()
        self._keys: dict[str, str] = self._load_json(self._keys_path) or {}
        self._normalize()

    def _normalize(self) -> None:
        """兼容旧数据:确保字段齐全(__init__ 与 reload 共用)"""
        self._settings.setdefault("instances", [])
        self._settings.setdefault("tombstones", [])
        self._settings.setdefault("globals", {})
        self._settings.setdefault("per_instance", {})

    # ------------------------------------------------------------ 基础读写

    @staticmethod
    def _load_json(path: str):
        """读 JSON;文件不存在返回 None;内容损坏先备份 .corrupt 再返回 None。

        损坏时若直接静默回落默认值,后续任何一次保存都会把残缺状态覆盖回磁盘,
        原配置永久丢失——所以必须先留备份。
        """
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except OSError:
            return None
        except json.JSONDecodeError:
            backup = path + ".corrupt"
            try:
                os.replace(path, backup)
                print(f"[QuotaBar] 配置文件损坏,已备份到 {backup},使用默认配置", file=sys.stderr)
            except OSError:
                pass
            return None

    @staticmethod
    def _atomic_write(path: str, data) -> None:
        """原子写:临时文件名带 pid+线程 id,防多进程/多线程同时写时互相截断"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _save_settings(self) -> None:
        self._atomic_write(self._settings_path, self._settings)

    def _save_keys(self) -> None:
        self._atomic_write(self._keys_path, self._keys)

    @staticmethod
    def _default_settings() -> dict:
        return {
            "instances": [],
            "tombstones": [],
            "globals": {
                "display_mode": _DISPLAY_USED,
                "refresh_interval": RefreshInterval.ONE_MINUTE.value,
                "locale": "zh-Hans",
                "active_instance_id": None,
            },
            "per_instance": {},
        }

    def reload(self) -> None:
        """重新从磁盘加载(测试用)"""
        with self._lock:
            self._settings = self._load_json(self._settings_path)
            if self._settings is None:
                self._settings = self._default_settings()
            self._keys = self._load_json(self._keys_path) or {}
            self._normalize()

    # ------------------------------------------------------------ 账号实例注册表

    def instances(self) -> list[PlatformInstance]:
        with self._lock:
            return [
                PlatformInstance(
                    id=it["id"],
                    platform_type=PlatformType(it["platform_type"]),
                    display_name=it["display_name"],
                )
                for it in self._settings["instances"]
            ]

    def instance(self, instance_id: str) -> Optional[PlatformInstance]:
        for it in self.instances():
            if it.id == instance_id:
                return it
        return None

    def _next_id(self, platform: PlatformType) -> str:
        """生成不与现存实例及墓碑冲突的 id(已删 id 永不复用,防凭据撞 key)"""
        used = set()
        for it in self._settings["instances"]:
            used.add(it["id"])
        used.update(self._settings["tombstones"])
        prefix = platform.value + "-"
        n = 1
        while f"{prefix}{n}" in used:
            n += 1
        return f"{prefix}{n}"

    def _template_name(self, platform: PlatformType) -> str:
        # 模板文件名与平台 raw id 的差异映射(与 Swift 版 fillDefaultsFromTemplateIfNeeded 一致)
        if platform == PlatformType.MINIMAX_CN:
            return "minimax"
        if platform == PlatformType.GLM_CN:
            return "glm"
        return platform.value

    def _load_template(self, platform: PlatformType) -> dict:
        path = os.path.join(_resources_dir(), "ConfigTemplates", f"{self._template_name(platform)}.template.json")
        data = self._load_json(path)
        return data or {}

    def add_instance(self, platform: PlatformType, display_name: Optional[str] = None) -> PlatformInstance:
        """添加账号实例:自动启用、按模板填充取数配置、默认勾选指标"""
        with self._lock:
            inst = PlatformInstance(
                id=self._next_id(platform),
                platform_type=platform,
                display_name=(display_name or "").strip() or platform.display_name(),
            )
            self._settings["instances"].append(
                {"id": inst.id, "platform_type": platform.value, "display_name": inst.display_name}
            )
            tpl = self._load_template(platform)
            self._settings["per_instance"][inst.id] = {
                "enabled": True,
                "pinned": False,
                "enabled_metrics": list(DEFAULT_ENABLED_METRICS[platform]),
                "api_base_url": tpl.get("api_base_url", ""),
                "api_base_url_international": tpl.get("api_base_url_international", ""),
                "auth_header": tpl.get("auth_header", ""),
                "auth_prefix": tpl.get("auth_prefix", ""),
                "region": tpl.get("region", "domestic"),
            }
            self._save_settings()
            return inst

    def rename_instance(self, instance_id: str, new_name: str) -> None:
        """重命名;留空回退平台默认名"""
        with self._lock:
            for it in self._settings["instances"]:
                if it["id"] == instance_id:
                    name = new_name.strip()
                    it["display_name"] = name or PlatformType(it["platform_type"]).display_name()
                    self._save_settings()
                    return
        raise KeyError(f"实例不存在: {instance_id}")

    def delete_instance(self, instance_id: str) -> None:
        """删除实例:清配置与凭据,写墓碑防 id 复用"""
        with self._lock:
            before = len(self._settings["instances"])
            self._settings["instances"] = [it for it in self._settings["instances"] if it["id"] != instance_id]
            if len(self._settings["instances"]) == before:
                raise KeyError(f"实例不存在: {instance_id}")
            self._settings["per_instance"].pop(instance_id, None)
            if instance_id not in self._settings["tombstones"]:
                self._settings["tombstones"].append(instance_id)
            if self._settings["globals"].get("active_instance_id") == instance_id:
                remain = self._settings["instances"]
                self._settings["globals"]["active_instance_id"] = remain[0]["id"] if remain else None
            self._keys.pop(instance_id, None)
            self._save_settings()
            self._save_keys()

    def move_instance(self, instance_id: str, offset: int) -> None:
        """排序:-1 左移 / +1 右移;越界不动"""
        with self._lock:
            order = [it["id"] for it in self._settings["instances"]]
            idx = order.index(instance_id)
            new_idx = idx + offset
            if new_idx < 0 or new_idx >= len(order):
                return
            order[idx], order[new_idx] = order[new_idx], order[idx]
            self._settings["instances"].sort(key=lambda it: order.index(it["id"]))
            self._save_settings()


    def _find_instance(self, instance_id: str) -> dict:
        """按 id 找实例条目;不存在抛 KeyError(裸 next() 会抛 StopIteration,调用方接不住)"""
        for it in self._settings["instances"]:
            if it["id"] == instance_id:
                return it
        raise KeyError(f"实例不存在: {instance_id}")

    # ------------------------------------------------------------ 每实例状态

    def _state(self, instance_id: str) -> dict:
        st = self._settings["per_instance"].get(instance_id)
        if st is None:
            raise KeyError(f"实例配置不存在: {instance_id}")
        return st

    def is_enabled(self, instance_id: str) -> bool:
        with self._lock:
            return bool(self._state(instance_id).get("enabled", False))

    def set_enabled(self, instance_id: str, enabled: bool) -> None:
        """禁用实例;禁止禁用最后一个启用实例;禁用时自动取消钉选"""
        with self._lock:
            st = self._state(instance_id)
            if not enabled:
                enabled_ids = [
                    it["id"]
                    for it in self._settings["instances"]
                    if self._settings["per_instance"].get(it["id"], {}).get("enabled")
                ]
                if instance_id in enabled_ids and len(enabled_ids) <= 1:
                    raise ValueError("至少需要启用一个平台")
                st["pinned"] = False
            st["enabled"] = enabled
            self._save_settings()

    def is_pinned(self, instance_id: str) -> bool:
        with self._lock:
            return bool(self._state(instance_id).get("pinned", False))

    def set_pinned(self, instance_id: str, pinned: bool) -> None:
        with self._lock:
            st = self._state(instance_id)
            if pinned and not st.get("enabled"):
                raise ValueError("未启用的实例不可钉选")
            st["pinned"] = pinned
            self._save_settings()

    def enabled_metrics(self, instance_id: str) -> list[str]:
        with self._lock:
            inst = self._find_instance(instance_id)
            allowed = AVAILABLE_METRIC_LABELS[PlatformType(inst["platform_type"])]
            raw = self._state(instance_id).get("enabled_metrics") or []
            # 死 label 过滤:老配置里已下架的 label 不占勾选名额
            return [m for m in raw if m in allowed]

    def set_enabled_metrics(self, instance_id: str, labels: list[str]) -> None:
        """勾选指标:必须 1~2 个、都在该平台清单内"""
        with self._lock:
            inst = self._find_instance(instance_id)
            allowed = AVAILABLE_METRIC_LABELS[PlatformType(inst["platform_type"])]
            cleaned = [l for l in labels if l in allowed]
            if not 1 <= len(cleaned) <= 2:
                raise ValueError("每账号最多勾选 2 个指标")
            self._state(instance_id)["enabled_metrics"] = cleaned
            self._save_settings()

    # ------------------------------------------------------------ 凭据与取数配置

    def api_key(self, instance_id: str) -> str:
        with self._lock:
            return self._keys.get(instance_id, "")

    def set_api_key(self, instance_id: str, key: str) -> None:
        """写入凭据;空串等于清除"""
        with self._lock:
            if key:
                self._keys[instance_id] = key
            else:
                self._keys.pop(instance_id, None)
            self._save_keys()

    def is_configured(self, instance_id: str) -> bool:
        with self._lock:
            st = self._settings["per_instance"].get(instance_id)
            return bool(st and st.get("api_base_url") and self._keys.get(instance_id))

    def config_data(self, instance_id: str) -> "PlatformConfigData":
        """拼装服务层取数配置"""
        from .models import PlatformConfigData

        with self._lock:
            inst = self._find_instance(instance_id)
            st = self._state(instance_id)
            return PlatformConfigData(
                platform_type=PlatformType(inst["platform_type"]),
                instance_id=instance_id,
                display_name=inst["display_name"],
                api_base_url=st.get("api_base_url", ""),
                auth_header=st.get("auth_header", ""),
                auth_prefix=st.get("auth_prefix", ""),
                api_key=self._keys.get(instance_id, ""),
                region=st.get("region"),
                api_base_url_international=st.get("api_base_url_international") or None,
            )

    # ------------------------------------------------------------ 全局配置

    def display_mode(self) -> str:
        with self._lock:
            mode = self._settings["globals"].get("display_mode", _DISPLAY_USED)
            return mode if mode in (_DISPLAY_USED, _DISPLAY_REMAINING) else _DISPLAY_USED

    def set_display_mode(self, mode: str) -> None:
        with self._lock:
            self._settings["globals"]["display_mode"] = mode
            self._save_settings()

    def refresh_interval(self) -> RefreshInterval:
        with self._lock:
            return RefreshInterval.from_raw(self._settings["globals"].get("refresh_interval"))

    def set_refresh_interval(self, interval: RefreshInterval) -> None:
        with self._lock:
            self._settings["globals"]["refresh_interval"] = interval.value
            self._save_settings()

    def locale(self) -> str:
        with self._lock:
            return self._settings["globals"].get("locale", "zh-Hans")

    def set_locale(self, locale: str) -> None:
        with self._lock:
            self._settings["globals"]["locale"] = locale
            self._save_settings()

    def active_instance_id(self) -> Optional[str]:
        """激活实例:已记录且仍启用的优先;否则回退第一个启用实例(禁用的不参与,对齐 Swift)"""
        with self._lock:
            per = self._settings["per_instance"]
            enabled = [
                it["id"]
                for it in self._settings["instances"]
                if per.get(it["id"], {}).get("enabled")
            ]
            active = self._settings["globals"].get("active_instance_id")
            if active in enabled:
                return active
            return enabled[0] if enabled else None

    def set_active_instance_id(self, instance_id: Optional[str]) -> None:
        with self._lock:
            self._settings["globals"]["active_instance_id"] = instance_id
            self._save_settings()

    # ------------------------------------------------------------ 任务栏数字面板(overlay)

    def overlay_enabled(self) -> bool:
        """任务栏数字面板开关,默认开(它是 Windows 上替代 Mac 菜单栏的主显示)"""
        with self._lock:
            return bool(self._settings["globals"].get("overlay_enabled", True))

    def set_overlay_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._settings["globals"]["overlay_enabled"] = enabled
            self._save_settings()

    def overlay_pos(self) -> Optional[tuple[int, int]]:
        """面板位置(用户拖拽后记忆);None = 用默认位置"""
        with self._lock:
            pos = self._settings["globals"].get("overlay_pos")
            if isinstance(pos, list) and len(pos) == 2:
                return int(pos[0]), int(pos[1])
            return None

    def set_overlay_pos(self, pos: tuple[int, int]) -> None:
        with self._lock:
            self._settings["globals"]["overlay_pos"] = [int(pos[0]), int(pos[1])]
            self._save_settings()
