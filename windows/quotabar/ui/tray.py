# -*- coding: utf-8 -*-
"""Windows 托盘层(功能语义对齐 macOS 版 StatusBar/StatusBarController)。

按 Windows 系统习惯做的取舍:
- 常驻右下角系统托盘;一个 32x32 圆形徽章图标:底色=状态色,中间白字主指标数值
- tooltip 是重点:多行列出所有已启用账号的「显示名 + 主要指标值」
- QMenu / QMessageBox / QInputDialog 全部用 Qt 原生控件与默认外观
- 左键托盘图标:开关详情窗;右键:整体重建的菜单(aboutToShow 时重建,无需手动同步)

线程模型:QTimer 触发 → 后台 threading.Thread 跑 manager.fetch_all()
→ QObject pyqtSignal 把结果列回 GUI 线程。取数绝不阻塞 UI 线程。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, Optional

from PyQt5.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QDesktopServices, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import QApplication, QInputDialog, QMenu, QMessageBox, QSystemTrayIcon

from .. import __version__, render
from ..config import ConfigService
from ..i18n import get_i18n
from ..manager import FetchResult, PlatformManager
from ..models import (
    AVAILABLE_METRIC_LABELS,
    ErrorKind,
    PlatformError,
    PlatformType,
    PlatformUsageData,
    RefreshInterval,
)
from .detail_window import DetailWindow
from .overlay import StatusOverlay

#: 状态点/徽章底色(render.status_color 的四色 → 具体色值)
_STATUS_COLORS = {
    render.COLOR_GRAY: "#8E8E93",
    render.COLOR_RED: "#FF3B30",
    render.COLOR_YELLOW: "#FFCC00",
    render.COLOR_GREEN: "#34C759",
}
_ICON_SIZE = 32                # 托盘图标画布
_MAIN_ICON_KEY = "__main__"    # 无钉选实例时单个主图标的字典 key
_RELEASES_URL = "https://github.com/lirenjian132/quota-bar/releases"

#: 刷新间隔档位 → i18n key(顺序即菜单顺序)
_REFRESH_ITEMS: tuple[tuple[RefreshInterval, str], ...] = (
    (RefreshInterval.THIRTY_SECONDS, "menu.refresh.30s"),
    (RefreshInterval.ONE_MINUTE, "menu.refresh.1m"),
    (RefreshInterval.THREE_MINUTES, "menu.refresh.3m"),
    (RefreshInterval.FIVE_MINUTES, "menu.refresh.5m"),
    (RefreshInterval.TEN_MINUTES, "menu.refresh.10m"),
)


class _FetchBridge(QObject):
    """后台取数线程 → GUI 线程的结果桥:pyqtSignal 保证槽函数跑在 GUI 线程"""

    finished = pyqtSignal(object)  # payload = (list[FetchResult], 本次拉取的实例 id 列表)


class TrayApp(QObject):
    """托盘常驻对象:图标集合、右键菜单、刷新循环、详情窗。"""

    def __init__(self, config: ConfigService, manager: PlatformManager, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.config = config
        self.manager = manager
        #: 每实例最新一次结果(成功留 data、失败留 error 并保留旧 data 供陈旧展示)
        self._data: dict[str, PlatformUsageData] = {}
        self._errors: dict[str, PlatformError] = {}
        self._loading: set[str] = set()
        #: 每实例取数代次:新拉取(定时/手动/换 key)都会推进;旧代次的结果回来直接丢弃,
        #: 防「换 key 后,旧 key 的在途请求晚到,把旧账号数据覆写回界面」(对齐 Swift 的 task 取消)
        self._gen: dict[str, int] = {}
        #: 托盘图标集合:key → QSystemTrayIcon;钉选实例用实例 id,主图标用 _MAIN_ICON_KEY
        self._trays: dict[str, QSystemTrayIcon] = {}
        self._icon_instance: dict[str, str] = {}
        self._detail: Optional[DetailWindow] = None
        self._menu: Optional[QMenu] = None
        # 任务栏数字面板:Windows 上 Mac 菜单栏的对等物(大号数字,托盘小图标只作备用入口)
        self._overlay = StatusOverlay(self)
        self._bridge = _FetchBridge()
        self._bridge.finished.connect(self._on_fetch_done)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_timer)
        # 按持久化 locale 初始化 i18n 单例
        get_i18n().set_locale(config.locale())
        self._rebuild_trays()

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动定时刷新,并立即拉一次数据"""
        self._restart_timer()
        self._show_startup_bubble()
        if self.config.overlay_enabled():
            self._overlay.show()
        self._overlay.refresh()
        self.fetch_all_async()

    def _show_startup_bubble(self) -> None:
        """启动气泡:Windows 下新应用的托盘图标可能落进「^」折叠区,弹一次让用户发现图标。

        托盘图标关闭时无从提示(面板常驻可见),跳过。
        """
        if not self.config.tray_icon_enabled():
            return
        for tray in self._trays.values():
            tray.showMessage(get_i18n().t("app.name"), _STARTUP_MESSAGE(), msecs=5000)
            return

    def _restart_timer(self) -> None:
        self._timer.start(self.config.refresh_interval().seconds * 1000)

    # ------------------------------------------------------------ 数据访问(详情窗只读)

    def data(self, instance_id: str) -> Optional[PlatformUsageData]:
        return self._data.get(instance_id)

    def error(self, instance_id: str) -> Optional[PlatformError]:
        return self._errors.get(instance_id)

    def is_loading(self, instance_id: str) -> bool:
        return instance_id in self._loading

    # ------------------------------------------------------------ 后台取数

    def fetch_all_async(self) -> None:
        """后台拉取全部「已启用且已配置」实例(GUI 线程只负责起线程)"""
        loading_ids = [
            it.id
            for it in self.config.instances()
            if self.config.is_enabled(it.id) and self.config.is_configured(it.id)
        ]
        self._start_worker(loading_ids, self._worker_all)

    def fetch_instance_async(self, instance_id: str) -> None:
        """后台拉取单个实例(账号切换 / 保存 key 后用)"""
        self._start_worker([instance_id], self._worker_one)

    def refresh_instance(self, instance_id: str) -> None:
        """换 key 后:清该实例 service 缓存并立即后台拉取(对齐 macOS saveAPIKey)"""
        self.clear_instance_cache(instance_id)
        self.fetch_instance_async(instance_id)

    def clear_instance_cache(self, instance_id: str) -> None:
        """清单实例取数缓存;实例不存在时静默(尚未建过 service)"""
        try:
            self.manager.service_for(instance_id).clear_cache()
        except KeyError:
            pass

    def _start_worker(self, loading_ids: list[str], worker: Callable) -> None:
        gens = {}
        for iid in loading_ids:
            self._gen[iid] = self._gen.get(iid, 0) + 1
            gens[iid] = self._gen[iid]
        self._loading.update(loading_ids)
        threading.Thread(target=worker, args=(loading_ids, gens), daemon=True).start()

    def _worker_all(self, loading_ids: list[str], gens: dict[str, int]) -> None:
        try:
            results = self.manager.fetch_all()
        except Exception as e:  # 逐实例兜底已在 manager 内,这里防整体意外
            results = [FetchResult(instance_id="", error=_wrap_error(e))]
        self._bridge.finished.emit((results, loading_ids, gens))

    def _worker_one(self, loading_ids: list[str], gens: dict[str, int]) -> None:
        instance_id = loading_ids[0]
        try:
            results = [FetchResult(instance_id=instance_id, data=self.manager.fetch_usage(instance_id))]
        except PlatformError as e:
            results = [FetchResult(instance_id=instance_id, error=e)]
        except Exception as e:
            results = [FetchResult(instance_id=instance_id, error=_wrap_error(e))]
        self._bridge.finished.emit((results, loading_ids, gens))

    def _on_fetch_done(self, payload) -> None:
        """结果回到 GUI 线程:过期代次丢弃、已删账号不复活,其余更新字典并重建界面"""
        results, loading_ids, gens = payload
        for iid in loading_ids:
            if gens.get(iid) == self._gen.get(iid):
                self._loading.discard(iid)  # 只有最新一代的完成才清 loading
        for result in results:
            iid = result.instance_id
            if not iid:
                continue  # 整体失败的占位结果,无对应实例
            if gens.get(iid) != self._gen.get(iid):
                continue  # 旧代次结果:换 key/手动刷新后晚到的旧请求,丢弃
            if self.config.instance(iid) is None:
                continue  # 实例已删:不让在途结果把它复活(对齐 Swift 的 instanceStore 守卫)
            if result.data is not None:
                self._data[iid] = result.data
                self._errors.pop(iid, None)
            else:
                self._errors[iid] = result.error
                # 失败时保留旧 data:详情窗按「陈旧数据 + 提示」展示(对齐 macOS)
        self._rebuild_trays()
        self._overlay.refresh()
        self._reload_detail()

    def _on_timer(self) -> None:
        self.fetch_all_async()

    # ------------------------------------------------------------ 托盘图标

    def _rebuild_trays(self) -> None:
        """按钉选状态重建图标集合,并刷新所有图标内容。

        有钉选实例:每个实例一个图标(顺序=注册表顺序);无钉选:单个主图标显示激活实例。
        托盘图标可在配置里整体关闭(面板已能承担全部功能)。
        """
        instances = self.config.instances()
        pinned = [it for it in instances if self.config.is_pinned(it.id) and self.config.is_enabled(it.id)]
        if pinned:
            wanted = [(it.id, it.id) for it in pinned]
        else:
            # 无钉选(含零账号)时都保留单个主图标;零账号时 instance_id 为 None,
            # 图标显示灰色 "--",否则托盘里什么都看不到,连添加账号的入口都找不到
            wanted = [(_MAIN_ICON_KEY, self.config.active_instance_id())]

        if not self.config.tray_icon_enabled():
            # 托盘图标关闭:清掉已创建的图标,面板继续显示
            for key in list(self._trays.keys()):
                icon = self._trays.pop(key)
                self._icon_instance.pop(key, None)
                icon.hide()
                icon.deleteLater()
            return

        wanted_keys = {key for key, _ in wanted}
        for key in [k for k in self._trays if k not in wanted_keys]:
            icon = self._trays.pop(key)
            self._icon_instance.pop(key, None)
            icon.hide()
            icon.deleteLater()

        for key, instance_id in wanted:
            if key not in self._trays:
                tray = QSystemTrayIcon(self)
                tray.activated.connect(lambda reason, k=key: self._on_activated(reason, k))
                self._trays[key] = tray
            self._icon_instance[key] = instance_id
            self._update_icon(key)  # 先设图标再 show,避免 "No Icon set" 警告
            tray = self._trays[key]
            if not tray.isVisible():
                tray.show()

    def _update_icon(self, key: str) -> None:
        tray = self._trays.get(key)
        if tray is None:
            return
        tray.setIcon(self._make_icon(self._icon_instance.get(key)))
        tray.setToolTip(self._make_tooltip())

    def _visible_metrics(self, instance_id: Optional[str]) -> list:
        """该实例当前可见的指标(按勾选过滤,最多 2 个);无实例(主图标空态)返回空"""
        if not instance_id:
            return []
        data = self._data.get(instance_id)
        return render.filter_metrics(data.metrics if data else [], self.config.enabled_metrics(instance_id))

    def _make_icon(self, instance_id: Optional[str]) -> QIcon:
        """托盘图标:纯状态色圆点,不画数字。

        数字由任务栏数字面板承载(16px 图标里塞数字既看不清又与面板重复);
        托盘只留一个安静的状态点,tooltip 里给出各账号的数值。
        """
        data = self._data.get(instance_id) if instance_id else None
        badge_color = QColor(_STATUS_COLORS.get(render.status_color(data), _STATUS_COLORS[render.COLOR_GRAY]))

        pixmap = QPixmap(_ICON_SIZE, _ICON_SIZE)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setBrush(badge_color)
            painter.setPen(Qt.NoPen)
            # 内缩留边,缩小到 16px 后仍是个饱满的圆点
            painter.drawEllipse(3, 3, _ICON_SIZE - 6, _ICON_SIZE - 6)
        finally:
            painter.end()
        return QIcon(pixmap)

    def _make_tooltip(self) -> str:
        """多行 tooltip:所有已启用账号的「显示名 + 主要指标值」,第二指标附在括号里

        托盘图标已改为纯状态点,数值明细全部由这里承载。
        """
        i18n = get_i18n()
        mode = self.config.display_mode()
        lines: list[str] = []
        for inst in self.config.instances():
            if not self.config.is_enabled(inst.id):
                continue
            metrics = self._visible_metrics(inst.id)
            primary = render.tray_text(metrics[0], mode) if metrics else "--"
            if metrics and metrics[0].unit == "%":
                primary = f"{primary}%"
            line = f"{inst.display_name}: {primary}"
            if len(metrics) > 1:  # 第二指标放 tooltip
                second = metrics[1]
                key = f"metric.{second.label}"
                title = i18n.t(key)
                if title == key:
                    title = second.label
                line += f" ({title} {render.metric_detail_text(second, i18n.locale)})"
            lines.append(line)
        return "\n".join(lines) if lines else i18n.t("app.name")

    # ------------------------------------------------------------ 图标点击

    def _on_activated(self, reason, key: str) -> None:
        if reason == QSystemTrayIcon.Trigger:  # 左键:开关详情窗
            self._toggle_detail()
        elif reason == QSystemTrayIcon.Context:  # 右键:出菜单
            self._show_menu()

    def _toggle_detail(self) -> None:
        if self._detail is None:
            self._detail = DetailWindow(self)
        if self._detail.isVisible():
            self._detail.hide()
            return
        self._detail.reload()
        self._detail.show()
        self._detail.raise_()
        self._detail.activateWindow()
        self.fetch_all_async()

    def _reload_detail(self) -> None:
        if self._detail is not None and self._detail.isVisible():
            self._detail.reload()

    # ------------------------------------------------------------ 右键菜单

    def _show_menu(self) -> None:
        """右键出菜单;aboutToShow 时整体重建,数据/配置变化无需手动同步"""
        menu = QMenu()
        menu.aboutToShow.connect(lambda: self._populate_menu(menu))
        self._menu = menu  # 持有引用,防菜单显示期间被 GC
        menu.popup(QCursor.pos())

    def _populate_menu(self, menu: QMenu) -> None:
        i18n = get_i18n()
        cfg = self.config
        menu.clear()

        refresh_now = menu.addAction(i18n.t("menu.refreshNow"))
        refresh_now.triggered.connect(self._on_refresh_now)
        menu.addSeparator()

        # 显示设置 ▸(已用/剩余互斥)
        display_menu = menu.addMenu(i18n.t("menu.displaySettings"))
        mode = cfg.display_mode()
        for mode_key, text_key in (("used", "menu.showUsed"), ("remaining", "menu.showRemaining")):
            act = display_menu.addAction(i18n.t(text_key))
            act.setCheckable(True)
            act.setChecked(mode == mode_key)
            act.setData(mode_key)
            act.triggered.connect(
                lambda checked=False, m=mode_key, sub=display_menu: self._set_display_mode(m, sub)
            )

        # 任务栏数字面板开关(主显示,默认开)
        overlay_act = menu.addAction(i18n.t("menu.taskbarOverlay"))
        overlay_act.setCheckable(True)
        overlay_act.setChecked(cfg.overlay_enabled())
        overlay_act.triggered.connect(lambda checked=False: self._toggle_overlay(checked))

        # 托盘图标开关(默认关:面板已承载全部功能,托盘图标只算备用入口)
        tray_act = menu.addAction(i18n.t("menu.trayIcon"))
        tray_act.setCheckable(True)
        tray_act.setChecked(cfg.tray_icon_enabled())
        tray_act.triggered.connect(lambda checked=False: self._toggle_tray_icon(checked))
        menu.addSeparator()

        # 刷新间隔 ▸(互斥;改后重置定时器)
        interval_menu = menu.addMenu(i18n.t("menu.refreshInterval"))
        current = cfg.refresh_interval()
        for interval, text_key in _REFRESH_ITEMS:
            act = interval_menu.addAction(i18n.t(text_key))
            act.setCheckable(True)
            act.setChecked(interval == current)
            act.setData(interval.value)
            act.triggered.connect(
                lambda checked=False, iv=interval, sub=interval_menu: self._set_refresh_interval(iv, sub)
            )

        # 平台 ▸(每实例一个子菜单 + 添加账号)
        platform_menu = menu.addMenu(i18n.t("menu.platform"))
        self._fill_platform_menu(platform_menu)

        # 显示指标 ▸(按实例分组,最多 2 个)
        metrics_menu = menu.addMenu(i18n.t("menu.enabledMetrics"))
        self._fill_metrics_menu(metrics_menu)

        # 语言 ▸(即时生效)
        language_menu = menu.addMenu(i18n.t("menu.language"))
        locale = cfg.locale()
        for code, text_key in (("en", "menu.lang.en"), ("zh-Hans", "menu.lang.zh")):
            act = language_menu.addAction(i18n.t(text_key))
            act.setCheckable(True)
            act.setChecked(locale == code)
            act.triggered.connect(lambda checked=False, c=code, sub=language_menu: self._set_locale(c, sub))
            act.setData(code)

        about = menu.addAction(i18n.t("menu.about"))
        about.triggered.connect(self._show_about)
        menu.addSeparator()
        quit_act = menu.addAction(i18n.t("menu.quit"))
        quit_act.triggered.connect(QApplication.quit)

    def _fill_platform_menu(self, platform_menu: QMenu) -> None:
        i18n = get_i18n()
        cfg = self.config
        instances = cfg.instances()
        enabled_count = sum(1 for it in instances if cfg.is_enabled(it.id))

        for index, inst in enumerate(instances):
            sub = platform_menu.addMenu(inst.display_name)
            enabled = cfg.is_enabled(inst.id)

            enable_act = sub.addAction(i18n.t("menu.platformEnabled"))
            enable_act.setCheckable(True)
            enable_act.setChecked(enabled)
            enable_act.triggered.connect(
                lambda checked=False, iid=inst.id, act=enable_act: self._toggle_enabled(iid, act)
            )

            pin_act = sub.addAction(i18n.t("menu.pinToStatusBar"))
            pin_act.setCheckable(True)
            pin_act.setChecked(cfg.is_pinned(inst.id))
            pin_act.setEnabled(enabled)  # 未启用的实例不可钉选
            pin_act.triggered.connect(lambda checked=False, iid=inst.id: self._toggle_pinned(iid))

            rename_act = sub.addAction(i18n.t("menu.renameAccount"))
            rename_act.triggered.connect(lambda checked=False, iid=inst.id: self._rename_account(iid))

            left_act = sub.addAction(i18n.t("menu.moveLeft"))
            left_act.setEnabled(index > 0)  # 首个不可再左移
            left_act.triggered.connect(lambda checked=False, iid=inst.id: self._move_account(iid, -1))

            right_act = sub.addAction(i18n.t("menu.moveRight"))
            right_act.setEnabled(index < len(instances) - 1)  # 末个不可再右移
            right_act.triggered.connect(lambda checked=False, iid=inst.id: self._move_account(iid, 1))

            delete_act = sub.addAction(i18n.t("menu.deleteAccount"))
            # 最后一个启用实例不可删(删完没有任何账号可显示,对齐 macOS)
            delete_act.setEnabled(not (enabled and enabled_count <= 1))
            delete_act.triggered.connect(lambda checked=False, iid=inst.id: self._delete_account(iid))

        platform_menu.addSeparator()
        for platform in PlatformType:
            add_act = platform_menu.addAction(i18n.t(f"menu.addAccount.{platform.value}"))
            add_act.triggered.connect(lambda checked=False, p=platform: self._add_account(p))

    def _fill_metrics_menu(self, metrics_menu: QMenu) -> None:
        i18n = get_i18n()
        cfg = self.config
        for inst in cfg.instances():
            sub = metrics_menu.addMenu(inst.display_name)
            current = cfg.enabled_metrics(inst.id)
            for label in AVAILABLE_METRIC_LABELS[inst.platform_type]:
                act = sub.addAction(i18n.t(f"menu.metric.{label}"))
                act.setCheckable(True)
                checked = label in current
                act.setChecked(checked)
                act.setData(label)
                act.setEnabled(not (len(current) >= 2 and not checked))  # 满 2 个时第 3 个置灰
                act.triggered.connect(
                    lambda checked=False, iid=inst.id, lb=label, sub=sub: self._toggle_metric(iid, lb, sub)
                )

    # ------------------------------------------------------------ 菜单动作

    def _on_refresh_now(self) -> None:
        """立即刷新:清所有实例缓存,再立即后台拉取"""
        self.manager.clear_all_caches()
        self.fetch_all_async()

    def _toggle_overlay(self, checked: bool) -> None:
        self.config.set_overlay_enabled(checked)
        if checked:
            self._overlay.refresh()
            self._overlay.show()
        else:
            self._overlay.hide()

    def _toggle_tray_icon(self, checked: bool) -> None:
        """托盘图标开关:关闭后托盘区不再有图标,面板与详情窗照常"""
        self.config.set_tray_icon_enabled(checked)
        self._rebuild_trays()

    def _set_display_mode(self, mode: str, sub: Optional[QMenu] = None) -> None:
        self.config.set_display_mode(mode)
        if sub is not None:
            self._sync_check_group(sub, mode)
        self._rebuild_trays()
        self._reload_detail()

    def _set_refresh_interval(self, interval: RefreshInterval, sub: Optional[QMenu] = None) -> None:
        self.config.set_refresh_interval(interval)
        if sub is not None:
            self._sync_check_group(sub, interval.value)
        self._restart_timer()
        self._reload_detail()

    def _set_locale(self, locale: str, sub: Optional[QMenu] = None) -> None:
        get_i18n().set_locale(locale)
        self.config.set_locale(locale)
        if sub is not None:
            self._sync_check_group(sub, locale)
        self._rebuild_trays()
        self._reload_detail()

    @staticmethod
    def _sync_check_group(sub: QMenu, selected) -> None:
        """互斥勾选组:按 action.data() 刷新勾态(菜单开着时即时同步)"""
        for act in sub.actions():
            if act.isCheckable():
                act.setChecked(act.data() == selected)

    def _toggle_enabled(self, instance_id: str, act) -> None:
        new_state = not self.config.is_enabled(instance_id)
        try:
            self.config.set_enabled(instance_id, new_state)
        except ValueError:
            QMessageBox.warning(
                self._message_parent(),
                get_i18n().t("app.name"),
                get_i18n().t("popover.atLeastOnePlatform"),
            )
            return
        act.setChecked(self.config.is_enabled(instance_id))
        self._on_config_changed()

    def _toggle_pinned(self, instance_id: str) -> None:
        try:
            self.config.set_pinned(instance_id, not self.config.is_pinned(instance_id))
        except ValueError:
            return  # 未启用实例不可钉选(菜单项已置灰,双保险)
        self._on_config_changed()

    def _rename_account(self, instance_id: str) -> None:
        inst = self.config.instance(instance_id)
        if inst is None:
            return
        i18n = get_i18n()
        text, ok = QInputDialog.getText(
            self._message_parent(),
            i18n.t("menu.renameAccount"),
            i18n.t("menu.renameAccount.title"),
            text=inst.display_name,
        )
        if not ok:
            return
        # 留空由 config 内部回退平台默认名
        self.config.rename_instance(instance_id, text)
        self._on_config_changed()

    def _move_account(self, instance_id: str, offset: int) -> None:
        self.config.move_instance(instance_id, offset)
        self._on_config_changed()

    def _delete_account(self, instance_id: str) -> None:
        inst = self.config.instance(instance_id)
        if inst is None:
            return
        i18n = get_i18n()
        box = QMessageBox(self._message_parent())
        box.setWindowTitle(i18n.t("menu.deleteAccount"))
        box.setText(i18n.t("menu.deleteAccount.confirm", inst.display_name))
        box.setInformativeText(i18n.t("menu.deleteAccount.informative"))
        box.setIcon(QMessageBox.Warning)
        ok_button = box.addButton(i18n.t("menu.deleteAccount.confirmOk"), QMessageBox.DestructiveRole)
        box.addButton(i18n.t("common.cancel"), QMessageBox.RejectRole)
        box.exec_()
        if box.clickedButton() is not ok_button:
            return
        self.config.delete_instance(instance_id)
        self._data.pop(instance_id, None)
        self._errors.pop(instance_id, None)
        self._loading.discard(instance_id)
        self._gen.pop(instance_id, None)
        self.manager.remove_instance(instance_id)  # 释放已删账号的 service 与缓存
        self._on_config_changed()

    def _add_account(self, platform: PlatformType) -> None:
        self.config.add_instance(platform)
        self._on_config_changed()

    def _toggle_metric(self, instance_id: str, label: str, sub: QMenu) -> None:
        cfg = self.config
        current = list(cfg.enabled_metrics(instance_id))
        if label in current:
            current = [item for item in current if item != label]
        else:
            current.append(label)
        try:
            cfg.set_enabled_metrics(instance_id, current)
        except ValueError:
            self._sync_metric_checks(sub, instance_id)  # 被拒时按落盘状态回滚勾态,防菜单与配置不一致
            return  # 超过 2 个等非法输入由 config 拒绝
        # 菜单还开着:按落盘后的实际状态刷新同级项(否则看到"临时勾选")
        self._sync_metric_checks(sub, instance_id)
        self._on_config_changed()

    def _sync_metric_checks(self, sub: QMenu, instance_id: str) -> None:
        """按落盘的勾选状态刷新指标子菜单的勾态与置灰"""
        persisted = self.config.enabled_metrics(instance_id)
        for act in sub.actions():
            item = act.data()
            if item is None:
                continue
            is_current = item in persisted
            act.setChecked(is_current)
            act.setEnabled(not (len(persisted) >= 2 and not is_current))

    def _show_about(self) -> None:
        i18n = get_i18n()
        box = QMessageBox(self._message_parent())
        box.setWindowTitle(i18n.t("menu.about"))
        box.setText(i18n.t("menu.about.version", __version__, datetime.now().year))
        box.setIcon(QMessageBox.Information)
        ok_button = box.addButton(i18n.t("menu.about.ok"), QMessageBox.AcceptRole)
        releases_button = box.addButton(i18n.t("menu.about.openReleases"), QMessageBox.ActionRole)
        box.exec_()
        if box.clickedButton() is releases_button:
            QDesktopServices.openUrl(QUrl(_RELEASES_URL))

    # ------------------------------------------------------------ 配置变化联动

    def _on_config_changed(self) -> None:
        """启用/钉选/顺序/指标/语言等变化:重建图标、刷新详情窗,缺数据的补拉一次"""
        self._rebuild_trays()
        self._reload_detail()
        if self._needs_data():
            self.fetch_all_async()

    def _needs_data(self) -> bool:
        for instance_id in self._icon_instance.values():
            if instance_id and instance_id not in self._data and self.config.is_configured(instance_id):
                return True
        return False

    def _message_parent(self):
        """模态框的父窗口:详情窗可见时挂在它上面,否则无父"""
        if self._detail is not None and self._detail.isVisible():
            return self._detail
        return None


def _wrap_error(e: Exception) -> PlatformError:
    """把未预期异常包成网络错误,不拖垮整批"""
    return PlatformError(ErrorKind.NETWORK, f"{type(e).__name__}: {e}")


def _STARTUP_MESSAGE() -> str:
    """启动气泡文案。

    i18n key 写集受限(本轮只允许加 common.save),中英文各给一句字面量兜底。
    """
    if get_i18n().locale.startswith("zh"):
        return "QuotaBar 已在系统托盘运行"
    return "QuotaBar is running in the system tray"
