# -*- coding: utf-8 -*-
"""配置与存储层:合并 macOS 版的 ConfigService / PlatformInstanceStore /
PlatformConfigStore / FileKeyStore 四个存储的职责。

- settings.json:账号注册表 + 全局配置 + 每实例非密配置
- keys.json:凭据(API key / cookie),与 macOS 版 FileKeyStore 对应
- 数据目录:%APPDATA%\\QuotaBar;测试用 QUOTABAR_HOME 环境变量重定向隔离
- 写入均为原子写(临时文件 + os.replace)
"""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import sys
import threading
import time
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


class ConfigRuleError(ValueError):
    """业务规则拒绝(不是落盘失败)。

    与「写盘失败」必须区分开:规则拒绝是正常反馈(比如勾了 3 个指标、禁用最后
    一个账号),UI 要给对应的提示;落盘失败才提示「保存失败」。
    它继承 ValueError,现有的 `except ValueError` 分支照旧能接住。
    """


class ConfigReadError(OSError):
    """配置文件存在但读不出来(被占用/权限不足)。

    继承 OSError:UI 的保存失败提示路径只需捕获 OSError 即可覆盖它。
    这类错误**绝不能**被当成「首次运行」——按默认值继续跑再允许保存,
    下一次写盘就会把磁盘上的真实账号与凭据整体覆盖掉。
    """


def _sanitize_text(value) -> str:
    """清掉编不出 UTF-8 的代理字符(剪贴板里被截断的 emoji 会带 \\ud800 这类字符)。

    这种字符一旦进了配置文件,之后**每一次**落盘都会抛 UnicodeEncodeError——
    不只是那个账号改名改不动,连改别的设置都存不下去。
    """
    if not isinstance(value, str):
        return ""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        value = value.encode("utf-8", "replace").decode("utf-8")
    # 控制字符(换行等)会让显示名在 tooltip 里伪造成多一行
    return "".join(ch for ch in value if ch == "	" or ord(ch) >= 32)


def _sanitize_per_instance(st: dict) -> dict:
    """收拾每实例配置:字段类型归位 + 字符串清毒。

    - 字符串字段必须是 str(被手改成 dict 时,is_configured 会判成"已配置",
      取数时才炸出裸 TypeError)
    - enabled_metrics 必须是字符串列表(写成数字时,读指标那一步直接 TypeError,
      整个应用起不来)
    """
    out: dict = {}
    for key in ("enabled", "pinned"):
        out[key] = bool(st.get(key, False))
    metrics = st.get("enabled_metrics")
    if isinstance(metrics, (list, tuple)):
        # 去重保序:重复 label 会让指标菜单"勾 2 个只显示 1 个",且勾掉唯一
        # 可见项后提交空列表被拒 → 该项再也改不了(只能删号重建)
        cleaned_metrics: list[str] = []
        for m in metrics:
            if isinstance(m, str):
                item = _sanitize_text(m)
                if item and item not in cleaned_metrics:
                    cleaned_metrics.append(item)
        out["enabled_metrics"] = cleaned_metrics
    else:
        out["enabled_metrics"] = []
    for key in ("api_base_url", "api_base_url_international", "auth_header", "auth_prefix", "region"):
        value = st.get(key)
        out[key] = _sanitize_text(value) if isinstance(value, str) else ""
    return out

#: 模板文件缺失时的兜底取值。
#: 打包(PyInstaller/zip)漏带 Resources 目录时,模板读不到 → api_base_url 为空 →
#: 账号永远停在「未配置」,用户粘了 key 也没反应,且没有任何诊断信息。
#: 这里内置一份同样的默认值,保证取数端点永远拿得到(有单测钉住两份取值一致)。
_TEMPLATE_FALLBACK: dict[str, dict] = {
    "minimax": {
        "api_base_url": "https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains",
        "api_base_url_international": "https://api.minimax.io/v1/api/openplatform/coding_plan/remains",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "region": "domestic",
    },
    "glm": {
        "api_base_url": "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
        "api_base_url_international": "https://api.z.ai/api/monitor/usage/quota/limit",
        "auth_header": "Authorization",
        "auth_prefix": "",
        "region": "domestic",
    },
    "stepfun": {
        "api_base_url": "https://platform.stepfun.com/api/step.openapi.devcenter.Dashboard",
        "auth_header": "Cookie",
        "auth_prefix": "",
        "region": "domestic",
    },
    "tokenrhythm": {
        "api_base_url": "https://tokenrhythm.studio/api/wallet/summary",
        "auth_header": "Cookie",
        "auth_prefix": "tr_session=",
        "region": "domestic",
    },
}


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
        self._read_failed: Optional[ConfigReadError] = None
        try:
            self._settings = self._load_settings()
            self._keys: dict[str, str] = self._load_keys()
        except ConfigReadError as e:
            # 读不出来:内存先按空配置跑(界面能起来),但**所有保存被拒绝**——
            # 绝不能用"从未成功读到的默认值"覆盖磁盘上的真实数据。原文件原地
            # 不动,占用解除后重启即可自愈(旧方案把文件改名隔离,重启仍是空配置,
            # 下次保存就把默认值固化到磁盘,用户只能手工找回)。
            self._read_failed = e
            self._settings = self._default_settings()
            self._keys = {}
        self._normalize()
        # 上一次落盘成功的快照:写盘失败时回滚到它,保证「内存 = 磁盘」不被拆开
        self._settings_snapshot = copy.deepcopy(self._settings)
        self._keys_snapshot = dict(self._keys)

    def read_failed(self) -> Optional[ConfigReadError]:
        """启动时配置文件读不出来则返回该错误;调用方应给用户可见提示"""
        return self._read_failed

    def _normalize(self) -> None:
        """兼容旧数据 + 结构自愈(__init__ 与 reload 共用)。

        字段缺失补齐;容器类型不对的回落默认;坏掉的账号条目剔除(其余账号不受影响);
        每实例配置缺失/类型不对的**补一份默认值**——少了它,账号会变成
        「在列表里但取不到配置」的僵尸,is_enabled 之类取值直接抛 KeyError,
        整个应用启动即崩。
        结构坏到连 dict 都不是时整体回落默认——否则启动即崩,用户无法自愈。
        """
        if not isinstance(self._settings, dict):
            self._settings = self._default_settings()
            return
        dropped = False
        # 根键白名单:未知根键(可能是手改/恶意配置里的深层嵌套值)必须丢弃。
        # 只清洗 globals 是不够的——根层的 junk 值一样能让后面的 deepcopy 抛
        # RecursionError,应用启动即崩。
        known_root_keys = ("instances", "tombstones", "globals", "per_instance")
        if set(self._settings) - set(known_root_keys):
            dropped = True
        self._settings = {key: self._settings.get(key) for key in known_root_keys}
        if not isinstance(self._settings.get("instances"), list):
            self._settings["instances"] = []
        if not isinstance(self._settings.get("tombstones"), list):
            self._settings["tombstones"] = []
        if not isinstance(self._settings.get("globals"), dict):
            self._settings["globals"] = {}
        if self._normalize_globals():
            dropped = True
        if not isinstance(self._settings.get("per_instance"), dict):
            self._settings["per_instance"] = {}
        # 账号条目逐个校验:坏的剔除、好的保留(不让一个坏条目带走全部账号)
        valid: list[dict] = []
        seen_ids: set[str] = set()
        for it in self._settings["instances"]:
            if not isinstance(it, dict):
                dropped = True
                continue
            iid = _sanitize_text(it.get("id"))
            ptype = it.get("platform_type")
            if not iid or not isinstance(ptype, str):
                dropped = True
                continue
            try:
                platform = PlatformType(ptype)
            except ValueError:
                dropped = True
                continue  # 未知平台(版本回退/手改):剔除,别拖垮启动
            if iid in seen_ids:
                dropped = True
                continue  # 重复 id(手改坏了):保留第一条,重复条目会共用同一份配置
            seen_ids.add(iid)
            name = _sanitize_text(it.get("display_name"))
            valid.append(
                {
                    "id": iid,
                    "platform_type": ptype,
                    "display_name": name or platform.display_name(),
                }
            )
        self._settings["instances"] = valid
        self._settings["tombstones"] = [
            t for t in (_sanitize_text(x) for x in self._settings["tombstones"] if isinstance(x, str)) if t
        ]
        # 每实例配置必须是 dict,否则 is_enabled 等取值时会崩
        per = self._settings["per_instance"]
        cleaned: dict[str, dict] = {}
        for k, v in per.items():
            if not isinstance(k, str) or not isinstance(v, dict):
                dropped = True
                continue
            key = _sanitize_text(k)
            if not key:
                dropped = True
                continue
            cleaned[key] = _sanitize_per_instance(v)
        if len(cleaned) != len(per):
            dropped = True  # 有条配置坏了被替换掉 → 算数据改动,留备份
        # 缺配置的账号补一份默认值,保证「账号 ↔ 配置」一一对应。
        # 补默认不算丢数据(原来就没有),不触发备份——否则每次启动都多一份 .corrupt。
        for inst in valid:
            if inst["id"] not in cleaned:
                cleaned[inst["id"]] = self._default_per_instance(PlatformType(inst["platform_type"]))
        self._settings["per_instance"] = cleaned
        if dropped:
            # 剔掉了东西就留个备份(原文件挪走,后续保存会写一份干净的)
            self._backup_corrupt(self._settings_path)

    def _normalize_globals(self) -> bool:
        """globals 白名单化:只保留已知键并校正类型,未知/深层嵌套垃圾直接丢弃。

        手改或恶意配置里的深层嵌套值(如 500 层 list)能通过 json.loads,
        但随后的 copy.deepcopy 会抛 RecursionError —— 白名单是根因修复
        (异常兜底只能挡住一处,挡不住下一次 deepcopy)。返回是否有改动。
        """
        raw = self._settings.get("globals")
        if not isinstance(raw, dict):
            self._settings["globals"] = {}
            return True
        clean: dict = {}
        if isinstance(raw.get("display_mode"), str):
            clean["display_mode"] = raw["display_mode"]
        # refresh_interval 的合法值是枚举字符串(如 "fiveMinutes")或旧版整数;
        # 嵌套对象/列表一律丢弃(深层垃圾会让后续 deepcopy 抛 RecursionError)
        refresh = raw.get("refresh_interval")
        if isinstance(refresh, (int, str)) and not isinstance(refresh, bool):
            clean["refresh_interval"] = refresh
        if isinstance(raw.get("locale"), str):
            clean["locale"] = raw["locale"]
        active = raw.get("active_instance_id")
        if active is None or isinstance(active, str):
            clean["active_instance_id"] = active
        pos = raw.get("overlay_pos")
        if (
            isinstance(pos, (list, tuple))
            and len(pos) == 2
            and all(
                isinstance(v, (int, float))
                and not isinstance(v, bool)
                and math.isfinite(float(v))
                for v in pos
            )
        ):
            clean["overlay_pos"] = [int(pos[0]), int(pos[1])]
        for key in ("overlay_embedded", "overlay_enabled", "tray_icon_enabled", "show_percent"):
            if key in raw:
                clean[key] = bool(raw[key])
        anchor = raw.get("overlay_anchor")
        if isinstance(anchor, str):
            clean["overlay_anchor"] = anchor
        # 只有"丢掉了原有键"才算数据改动(需要留备份);给缺失键补默认值不算丢数据,
        # 否则每次加载旧配置都会多一份 .corrupt
        changed = bool(set(raw) - set(clean))
        self._settings["globals"] = clean
        return changed

    def _default_per_instance(self, platform: PlatformType) -> dict:
        """某平台的默认每实例配置(模板填充 + 默认勾选指标)"""
        tpl = self._load_template(platform)
        return {
            "enabled": True,
            "pinned": False,
            "enabled_metrics": list(DEFAULT_ENABLED_METRICS[platform]),
            "api_base_url": tpl.get("api_base_url", ""),
            "api_base_url_international": tpl.get("api_base_url_international", ""),
            "auth_header": tpl.get("auth_header", ""),
            "auth_prefix": tpl.get("auth_prefix", ""),
            "region": tpl.get("region", "domestic"),
        }

    # ------------------------------------------------------------ 基础读写

    def _load_settings(self) -> dict:
        """读 settings.json;根不是 dict 视为损坏(备份后回落默认)"""
        data = self._load_json(self._settings_path, protect=True)
        if data is None:
            return self._default_settings()
        if not isinstance(data, dict):
            self._backup_corrupt(self._settings_path)
            return self._default_settings()
        return data

    def _load_keys(self) -> dict:
        """读 keys.json;根不是 dict 视为损坏(备份后回落空)"""
        data = self._load_json(self._keys_path, protect=True)
        if data is None:
            return {}
        if not isinstance(data, dict):
            self._backup_corrupt(self._keys_path)
            return {}
        # 凭据值也要清毒:粘贴来的 key/cookie 里可能有截断的 emoji(代理字符),
        # 一个坏字符会让这个文件的**所有**写盘都失败(连改别的账号都不行)
        return {
            _sanitize_text(k): _sanitize_text(v)
            for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str)
        }

    @staticmethod
    def _backup_corrupt(path: str) -> None:
        """把异常配置复制一份 .corrupt 备份(留证据,原文件留在原位)。

        用复制而不是移动:原文件还在(下次保存会覆盖它),证据留在 .corrupt 里——
        移动会把磁盘上的配置直接弄没,应用下次启动就成空白了。
        """
        backup = path + ".corrupt"
        try:
            shutil.copy2(path, backup)
            print(f"[QuotaBar] 配置文件异常,已备份到 {backup}", file=sys.stderr)
        except OSError:
            pass

    @staticmethod
    def _load_json(path: str, protect: bool = False):
        """读 JSON;文件不存在返回 None;内容损坏先备份 .corrupt 再返回 None。

        损坏时若直接静默回落默认值,后续任何一次保存都会把残缺状态覆盖回磁盘,
        原配置永久丢失——所以必须先留备份。

        读不出来(OSError)与「文件不存在」严格区分:同步客户端占位/杀软扫描/
        权限抖动造成的读失败不能当「首次运行」处理。先重试(瞬时锁一般几十
        毫秒内释放);仍失败且 protect=True 时抛 ConfigReadError,由 ConfigService
        拒绝一切保存并把原文件留在原地——用户关掉占用程序后重启即可自愈。
        """
        last_err: Optional[OSError] = None
        for _ in range(5):
            try:
                # utf-8-sig:带 BOM 的文件(记事本/部分同步客户端产物)也能读
                with open(path, encoding="utf-8-sig") as f:
                    return json.load(f)
            except FileNotFoundError:
                return None
            except OSError as e:
                last_err = e  # 瞬时锁:退避后重试(总等待约 0.75s)
                time.sleep(0.05 * (1 + _))
            except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
                # UnicodeDecodeError:文件被非 UTF-8 的工具/同步客户端转过码,
                # 与 JSON 损坏同等处理(备份 + 回落默认),不能直接崩在启动阶段
                ConfigService._backup_corrupt(path)
                return None
        if protect:
            # 尽力留一份证据(读都读不了时复制多半也失败,失败就算了)
            ConfigService._backup_corrupt(path)
            raise ConfigReadError(f"配置文件读取失败: {path} ({last_err})")
        return None

    @staticmethod
    def _atomic_write(path: str, data) -> None:
        """原子写:临时文件名带 pid+线程 id,防多进程/多线程同时写时互相截断。

        失败时清掉临时文件(不留垃圾),并把异常抛给调用方——调用方要能
        告诉用户「没保存成功」,不能默默吞掉。
        """
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _save_settings(self) -> None:
        try:
            # 必须在 try 内:读失败禁写抛错时要走下面同一套快照回滚,
            # 否则"拒绝保存"了但内存已被调用方改掉(幽灵账号/凭据会参与取数)
            self._ensure_writable()
            self._atomic_write(self._settings_path, self._settings)
        except Exception:
            # 回滚到上次落盘成功的状态:内存新/磁盘旧会让界面显示"已保存",
            # 重启后改动却消失
            self._settings = copy.deepcopy(self._settings_snapshot)
            raise
        self._settings_snapshot = copy.deepcopy(self._settings)

    def _save_keys(self) -> None:
        try:
            # 同 _save_settings:禁写异常也要走快照回滚
            self._ensure_writable()
            self._atomic_write(self._keys_path, self._keys)
        except Exception:
            self._keys = dict(self._keys_snapshot)
            raise
        self._keys_snapshot = dict(self._keys)

    def _ensure_writable(self) -> None:
        """启动读失败期间拒绝一切保存:内存是默认值,写下去就是数据丢失"""
        if self._read_failed is not None:
            raise ConfigReadError(str(self._read_failed))

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
        """重新从磁盘加载(测试用);成功即解除"读失败"的保存封锁"""
        with self._lock:
            try:
                settings = self._load_settings()
                keys = self._load_keys()
            except ConfigReadError as e:
                self._read_failed = e
                return
            self._read_failed = None
            self._settings = settings
            self._keys = keys
            self._normalize()
            self._settings_snapshot = copy.deepcopy(self._settings)
            self._keys_snapshot = dict(self._keys)

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

    def registration_order(self) -> list[PlatformInstance]:
        """按**注册顺序**(实例 id 的数字后缀)列出账号。

        账号编号(M1/M2…)跟账号走,不随显示位置变:主力永远 M1、备用永远 M2,
        调整顺序时编号不变——与 macOS 版一致。
        """
        import re

        def key(inst: PlatformInstance):
            m = re.search(r"-(\d+)$", inst.id)
            return int(m.group(1)) if m else 0

        return sorted(self.instances(), key=key)

    def instance(self, instance_id: str) -> Optional[PlatformInstance]:
        for it in self.instances():
            if it.id == instance_id:
                return it
        return None

    def _next_id(self, platform: PlatformType) -> str:
        """生成不与现存实例、墓碑及**残留凭据**冲突的 id(已删 id 永不复用,防凭据撞 key)。

        keys.json 里可能留有孤儿凭据(settings 丢失/删除半途崩溃):id 一旦与
        孤儿 key 撞上,新账号会静默继承旧账号的凭据并以旧账号身份取数。
        """
        used = set()
        for it in self._settings["instances"]:
            used.add(it["id"])
        used.update(self._settings["tombstones"])
        used.update(self._keys.keys())
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
        """读平台模板;缺失字段用内置兜底补齐(打包漏带 Resources 的保险)。

        只把模板当整体兜底是不够的:模板是**部分字典**(缺 api_base_url)时,
        账号会永远停在「未配置」。以内置默认值为底、模板值覆盖,缺字段自动补。
        """
        name = self._template_name(platform)
        path = os.path.join(_resources_dir(), "ConfigTemplates", f"{name}.template.json")
        data = self._load_json(path)
        merged = dict(_TEMPLATE_FALLBACK.get(name, {}))
        if isinstance(data, dict):
            merged.update(data)
        return merged

    def add_instance(self, platform: PlatformType, display_name: Optional[str] = None) -> PlatformInstance:
        """添加账号实例:自动启用、按模板填充取数配置、默认勾选指标"""
        with self._lock:
            inst = PlatformInstance(
                id=self._next_id(platform),
                platform_type=platform,
                # 与 rename/_normalize 同一消毒口径:名字来自用户输入(剪贴板粘贴),
                # 带代理字符会让之后每一次落盘都失败,带换行会在 tooltip 里伪造多行
                display_name=_sanitize_text((display_name or "").strip()) or platform.display_name(),
            )
            self._settings["instances"].append(
                {"id": inst.id, "platform_type": platform.value, "display_name": inst.display_name}
            )
            self._settings["per_instance"][inst.id] = self._default_per_instance(platform)
            self._save_settings()
            return inst

    def rename_instance(self, instance_id: str, new_name: str) -> None:
        """重命名;留空回退平台默认名"""
        with self._lock:
            for it in self._settings["instances"]:
                if it["id"] == instance_id:
                    name = _sanitize_text(new_name.strip())
                    it["display_name"] = name or PlatformType(it["platform_type"]).display_name()
                    self._save_settings()
                    return
        raise KeyError(f"实例不存在: {instance_id}")

    def delete_instance(self, instance_id: str) -> None:
        """删除实例:清配置与凭据,写墓碑防 id 复用。

        两步落盘(settings 与 keys)必须**要么都成、要么都不成**:
        只删了一半的话,账号会变成「有账号没凭据」或「凭据残留在文件里」,
        前后不一致。所以第二步失败时把第一步也回滚。
        """
        with self._lock:
            before = len(self._settings["instances"])
            snapshot = copy.deepcopy(self._settings)
            self._settings["instances"] = [it for it in self._settings["instances"] if it["id"] != instance_id]
            if len(self._settings["instances"]) == before:
                raise KeyError(f"实例不存在: {instance_id}")
            self._settings["per_instance"].pop(instance_id, None)
            if instance_id not in self._settings["tombstones"]:
                self._settings["tombstones"].append(instance_id)
            if self._settings["globals"].get("active_instance_id") == instance_id:
                remain = self._settings["instances"]
                self._settings["globals"]["active_instance_id"] = remain[0]["id"] if remain else None
            deleted = copy.deepcopy(self._settings)  # 删除后的状态(恢复失败时内存跟磁盘走)
            # 先落盘账号注册表(失败时 _save_settings 自己回滚,凭据还没动)
            self._save_settings()
            # 再删凭据:失败就把账号恢复回去,不留「删了一半」的错位状态
            self._keys.pop(instance_id, None)
            try:
                self._save_keys()
            except Exception:
                # 恢复要绕开 _save_settings:它的失败分支会用**快照**覆盖内存,
                # 而快照已经是「删除后」的状态——等于把刚恢复的账号又抹掉。
                self._settings = snapshot
                self._settings_snapshot = copy.deepcopy(snapshot)
                try:
                    self._atomic_write(self._settings_path, self._settings)
                except Exception as e:
                    # 连恢复都写不进去(磁盘满):磁盘上是「已删除」,内存必须跟着磁盘走,
                    # 否则内存与磁盘分叉,重启后账号凭空消失。
                    self._settings = deleted
                    self._settings_snapshot = copy.deepcopy(deleted)
                    # 账号真没了,凭据也别留在内存里:下次任意一次 keys 落盘会清掉它。
                    # 快照同样要摘:否则后续某次 keys 写失败回滚时,已删凭据会从
                    # 快照"复活"并再被写回磁盘(违反「删除即删除」)
                    self._keys.pop(instance_id, None)
                    self._keys_snapshot.pop(instance_id, None)
                    print(
                        f"[QuotaBar] 账号 {instance_id} 删除后恢复失败(磁盘可能是半删状态):{e}",
                        file=sys.stderr,
                    )
                raise

    def reorder_instances(self, ordered_ids: list[str]) -> None:
        """按给定顺序重排账号(拖拽排序用)。未列出的账号保持在后面。"""
        with self._lock:
            by_id = {it["id"]: it for it in self._settings["instances"]}
            # 传入序列可能带重复 id(UI 层传错):去重,否则同一实例会被排进列表两次
            ordered_unique: list[str] = []
            seen: set[str] = set()
            for iid in ordered_ids:
                if iid in by_id and iid not in seen:
                    seen.add(iid)
                    ordered_unique.append(iid)
            rest = [it for it in self._settings["instances"] if it["id"] not in seen]
            self._settings["instances"] = [by_id[i] for i in ordered_unique] + rest
            self._save_settings()

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


    def instance_by_id(self, instance_id: str) -> Optional[str]:
        """按 id 取账号显示名"""
        inst = self.instance(instance_id)
        return inst.display_name if inst else None

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
            st = self._settings["per_instance"].get(instance_id)
            return bool(isinstance(st, dict) and st.get("enabled", False))

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
                    raise ConfigRuleError("至少需要启用一个平台")
                st["pinned"] = False
            st["enabled"] = enabled
            self._save_settings()

    def is_pinned(self, instance_id: str) -> bool:
        with self._lock:
            st = self._settings["per_instance"].get(instance_id)
            return bool(isinstance(st, dict) and st.get("pinned", False))

    def set_pinned(self, instance_id: str, pinned: bool) -> None:
        with self._lock:
            st = self._state(instance_id)
            if pinned and not st.get("enabled"):
                raise ConfigRuleError("未启用的实例不可钉选")
            st["pinned"] = pinned
            self._save_settings()

    def enabled_metrics(self, instance_id: str) -> list[str]:
        """勾选的指标:死 label 过滤;全被过滤掉时回退平台默认勾选

        回退与 macOS 版一致(落盘值全空/全死 → 用平台默认),否则会出现
        「面板显示前 2 个指标、菜单里一个都没勾」的错位状态。
        """
        with self._lock:
            inst = self._find_instance(instance_id)
            platform = PlatformType(inst["platform_type"])
            allowed = AVAILABLE_METRIC_LABELS[platform]
            raw = self._state(instance_id).get("enabled_metrics")
            if not isinstance(raw, (list, tuple)):
                return list(DEFAULT_ENABLED_METRICS[platform])  # 落盘值不是列表:当作没勾
            # 死 label 过滤:老配置里已下架的 label 不占勾选名额
            picked = [m for m in raw if m in allowed]
            # 超过 2 个(手改坏了)也要回落默认:菜单会显示 3 个全勾、面板只显示前 2 个
            if not picked or len(picked) > 2:
                return list(DEFAULT_ENABLED_METRICS[platform])
            return picked

    def set_enabled_metrics(self, instance_id: str, labels: list[str]) -> None:
        """勾选指标:必须 1~2 个、都在该平台清单内

        顺序与 macOS 版一致:**先按原始名单查数**(3 个一律拒),再过滤死 label。
        反过来做的话,「3 个里混 1 个死 label」会被悄悄缩成 2 个而通过。
        重复项计入原始数量(与 Swift 同口径),落盘前去重保序。
        """
        with self._lock:
            inst = self._find_instance(instance_id)
            allowed = AVAILABLE_METRIC_LABELS[PlatformType(inst["platform_type"])]
            if len(labels) > 2:
                raise ConfigRuleError("每账号最多勾选 2 个指标")
            if not labels:
                raise ConfigRuleError("每账号至少勾选 1 个指标")
            cleaned: list[str] = []
            for label in labels:
                # 去重保序:重复勾选会被菜单显示成勾了 2 个、面板只显示 1 个
                if label in allowed and label not in cleaned:
                    cleaned.append(label)
            if not cleaned:
                raise ConfigRuleError("勾选的指标已全部下架")
            self._state(instance_id)["enabled_metrics"] = cleaned
            self._save_settings()

    # ------------------------------------------------------------ 凭据与取数配置

    def api_key(self, instance_id: str) -> str:
        with self._lock:
            return self._keys.get(instance_id, "")

    def set_api_key(self, instance_id: str, key: str) -> None:
        """写入凭据;空串等于清除。

        写盘失败时由 _save_keys 回滚内存值(旧值恢复),异常继续抛给调用方——
        界面必须能告诉用户「没保存成功」,否则重启后凭据凭空消失。

        实例必须存在才写:删除账号与保存凭据并发时(详情窗先查存在、随后账号
        被删),不校验的话凭据会写进 keys.json 留成孤儿。校验在锁内做,与
        delete_instance 互斥,竞态窗口闭合:要么这里抛 KeyError,要么删除随后
        会把这把 key 一并清掉。
        """
        with self._lock:
            self._find_instance(instance_id)
            clean = _sanitize_text(key)
            if clean:
                self._keys[instance_id] = clean
            else:
                self._keys.pop(instance_id, None)
            self._save_keys()

    def compare_and_set_api_key(self, instance_id: str, expected: str, new: str) -> bool:
        """原子 CAS(自动续期写回专用):仅当当前凭据仍等于 expected 时写入。

        比较与写入必须在同一把锁内:先 `api_key()` 再 `set_api_key()` 是两次
        独立加锁,中间用户保存了新凭据时,旧凭据刷出的串会把用户的新值覆盖掉
        (「改完 key 自己变回去」)。
        """
        with self._lock:
            if self._keys.get(instance_id, "") != expected:
                return False
            self._find_instance(instance_id)
            clean = _sanitize_text(new)
            if clean:
                self._keys[instance_id] = clean
            else:
                self._keys.pop(instance_id, None)
            self._save_keys()
            return True

    def is_configured(self, instance_id: str) -> bool:
        """是否已配置好可取数:有凭据 + 有取数端点。

        端点由模板(或内置兜底)填充,正常情况下恒非空;真为空说明平台模板缺失,
        此时判未配置比发一个必然失败的请求更安全。
        """
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
        """界面语言:只认 en / zh-Hans,其它值回落默认。

        类型也要校验:locale 被写成 list/dict 时,拿它当字典键查 i18n 表会抛
        TypeError(不可哈希),第一次出菜单就崩——整个界面没有可用语言。
        """
        with self._lock:
            value = self._settings["globals"].get("locale", "zh-Hans")
            return value if isinstance(value, str) and value in ("en", "zh-Hans") else "zh-Hans"

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

    # ------------------------------------------------------------ 托盘图标

    def tray_icon_enabled(self) -> bool:
        """托盘图标开关,默认关。

        任务栏面板已承载全部功能(显示/左键详情/右键菜单/退出),
        托盘图标只是备用入口,常驻反而占地方(Windows 常把它挤进「^」折叠区,
        还会留下被杀进程清理不掉的残影)。
        """
        with self._lock:
            return bool(self._settings["globals"].get("tray_icon_enabled", False))

    def set_tray_icon_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._settings["globals"]["tray_icon_enabled"] = enabled
            self._save_settings()

    def set_entry_visibility(self, overlay: bool, tray: bool) -> None:
        """一次性设置「任务栏面板」与「托盘图标」两个入口开关(单次落盘)。

        两个开关必须**同时**改、一次写盘:分两次写的话,第二步失败会留下
        「面板关 + 托盘关」的落盘状态——重启后进程还在,却没有任何 UI 入口,
        用户只能去任务管理器杀进程(这正是这个不变式要防的事)。
        """
        with self._lock:
            self._settings["globals"]["overlay_enabled"] = overlay
            self._settings["globals"]["tray_icon_enabled"] = tray
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
                try:
                    x, y = int(pos[0]), int(pos[1])
                except (TypeError, ValueError, OverflowError):
                    return None  # 手改成字符串/NaN/inf:当作没记过位置
                return x, y
            return None

    def set_overlay_pos(self, pos: tuple[int, int]) -> None:
        with self._lock:
            self._settings["globals"]["overlay_pos"] = [int(pos[0]), int(pos[1])]
            self._save_settings()

    def overlay_embedded(self) -> bool:
        """是否嵌入任务栏(真·任务栏子窗口)。

        默认关:Qt 窗口被 SetParent 到 explorer 任务栏后不稳定(会崩溃、
        鼠标事件收不到)。默认走「浮层 + 任务栏同色底 + 无边框 + 贴任务栏内居中」,
        视觉上与嵌入完全一致,交互稳定,还能拖拽换位置。
        """
        with self._lock:
            return bool(self._settings["globals"].get("overlay_embedded", False))

    def set_overlay_embedded(self, embedded: bool) -> None:
        with self._lock:
            self._settings["globals"]["overlay_embedded"] = embedded
            self._save_settings()

    def show_percent(self) -> bool:
        """是否在面板数值后显示 % 号(对标 TrafficMonitor 的「不显示百分号」)"""
        with self._lock:
            return bool(self._settings["globals"].get("show_percent", True))

    def set_show_percent(self, show: bool) -> None:
        with self._lock:
            self._settings["globals"]["show_percent"] = show
            self._save_settings()

    def overlay_anchor(self) -> str:
        """面板锚点:嵌入任务栏时靠哪一端("right" 靠通知区 / "left" 靠开始按钮)。"""
        with self._lock:
            anchor = self._settings["globals"].get("overlay_anchor", "right")
            return anchor if anchor in ("left", "right") else "right"

    def set_overlay_anchor(self, anchor: str) -> None:
        with self._lock:
            self._settings["globals"]["overlay_anchor"] = anchor
            self._save_settings()
