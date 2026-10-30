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
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QPoint, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import (
    QColor,
    QCursor,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QPainter,
    QPixmap,
)
from PyQt5.QtWidgets import (
    QApplication,
    QInputDialog,
    QMenu,
    QMessageBox,
    QStyle,
    QSystemTrayIcon,
)

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
#: 托盘 tooltip 字符上限(Windows 通知区域约 127 字符,超出被静默截断)
_TOOLTIP_MAX_CHARS = 127

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


class _WriteState:
    """一次配置写入的结果:ok=False 表示落盘失败(内存已回滚)"""

    ok = True


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
        #: 新建但还没配凭据的账号:配置前取消要回收(对齐 macOS)
        self._pending_instance_id: Optional[str] = None
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
        self._ensure_entry_point()
        self._restart_timer()
        self._show_startup_bubble()
        if self.config.overlay_enabled():
            self._overlay.show()
            self._overlay.attach()   # 嵌入任务栏(在 show 之后,否则会被 Qt 重置)
        self._overlay.refresh()
        self.fetch_all_async()

    def _ensure_entry_point(self) -> None:
        """启动自愈:面板与托盘图标不能同时关着。

        两个入口都关掉时进程还活着(setQuitOnLastWindowClosed 为 False)、
        定时器还在打网络,但用户既打不开详情窗也点不出菜单,只能去任务管理器
        杀进程。正常路径不会写出这种配置,但历史遗留/手改可能——启动时兜住。
        """
        if self.config.overlay_enabled() or self.config.tray_icon_enabled():
            return
        try:
            self.config.set_entry_visibility(overlay=True, tray=False)
        except (OSError, TypeError, UnicodeEncodeError, KeyError):
            pass  # 落盘失败:至少先把界面开出来
        self._overlay.show()
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
        except KeyError:
            return  # 实例已被删:不必报错,界面侧会跳过
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
                # 第二指标与主指标同一口径(都按显示模式):否则一行里会出现
                # 「已用 20%」和「剩余 59%」这种自相矛盾的两个数字
                value = render.metric_value_text(second, mode, i18n.locale)
                line += f" ({title} {value})"
            lines.append(line)
        if not lines:
            return i18n.t("app.name")
        # Windows 托盘 tooltip 上限约 127 字符,超出被系统静默截断(后面的账号
        # 直接看不见)。这里自己截,并补一行"共 N 个账号"说明还有多少。
        if len("\n".join(lines)) <= _TOOLTIP_MAX_CHARS:
            return "\n".join(lines)
        kept: list[str] = []
        used = 0
        for line in lines:
            need = len(line) + (1 if kept else 0)
            if kept and used + need > _TOOLTIP_MAX_CHARS - 24:
                break
            clipped = line[:_TOOLTIP_MAX_CHARS - 24]
            kept.append(clipped)
            used += len(clipped) + (1 if len(kept) > 1 else 0)
        more = i18n.t("menu.trayTooltipMore", len(lines))
        return "\n".join(kept + [more])

    # ------------------------------------------------------------ 图标点击

    def _on_activated(self, reason, key: str) -> None:
        if reason == QSystemTrayIcon.Trigger:  # 左键:开关详情窗
            self._toggle_detail()
        elif reason == QSystemTrayIcon.Context:  # 右键:出菜单
            self._show_menu()

    def _toggle_detail(self) -> None:
        if self._detail is not None and self._detail.isVisible():
            # 配置面板被程序化隐藏(点面板 chip、托盘左键):等同「取消」——
            # 否则新建的空壳账号不会被回收,详情窗下次打开还停在配置面板
            if self._detail.is_configuring():
                self._detail._cancel_config()
            self._detail.hide()
            return
        self._ensure_detail_visible()
        self._detail.activateWindow()
        self.fetch_all_async()

    def open_detail_for(self, instance_id: Optional[str] = None) -> None:
        """点任务栏账号块/菜单「查看详情」:只显示这个账号。

        详情窗已开着时直接切换账号,不把它关掉再开;窗口不可见时再显示。
        """
        if instance_id and self.config.instance(instance_id) is None:
            return
        # 点账号块开详情时,悬停卡片立刻收起,不能盖在详情窗上
        self._overlay.hide_tooltip()
        if instance_id:
            with self._config_write():
                self.config.set_active_instance_id(instance_id)
        if self._detail is None:
            self._detail = DetailWindow(self)
        if instance_id:
            self._detail.show_account(instance_id)
        else:
            self._ensure_detail_visible()
        self._detail.activateWindow()
        self._detail.raise_()
        if (
            instance_id
            and self.config.is_configured(instance_id)
            and self.data(instance_id) is None
        ):
            self.fetch_instance_async(instance_id)
        self.fetch_all_async()

    def _open_config_for(self, instance_id: str) -> None:
        """菜单「配置…」:打开该账号的配置面板"""
        if self.config.instance(instance_id) is None:
            return
        self.open_detail_for(instance_id)
        if self._detail is not None:
            self._detail._open_config(instance_id)

    def _relogin_account(self, instance_id: str) -> None:
        """菜单「重新登录…」:直接进配置面板并开浏览器登录(cookie 型平台)"""
        inst = self.config.instance(instance_id)
        if inst is None or not inst.platform_type.cookie_based():
            return
        self._open_config_for(instance_id)
        if self._detail is not None:
            self._detail._open_login(inst.platform_type)

    def _reload_detail(self) -> None:
        if self._detail is not None and self._detail.isVisible():
            self._detail.reload()

    # ------------------------------------------------------------ 右键菜单

    def _show_menu(self) -> None:
        """右键出菜单:先整体建好再 popup。

        旧写法在 aboutToShow 里 clear + 重建,菜单已经显示出来又被清空重画,
        视觉上就是一闪。现在新建的菜单一次性建好,并夹到任务栏上方。
        """
        menu = QMenu()
        self._populate_menu(menu)
        menu.adjustSize()
        self._menu = menu  # 持有引用,防菜单显示期间被 GC
        menu.popup(self._clamped_menu_pos(menu))

    @staticmethod
    def _clamped_menu_pos(menu: QMenu) -> QPoint:
        """菜单定位:跟随鼠标,但整体夹在任务栏上方的可用区域里"""
        pos = QCursor.pos()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        if screen is None:
            return pos
        geo = screen.availableGeometry()
        size = menu.sizeHint()
        x = max(geo.left() + 4, min(pos.x(), geo.right() - size.width() - 4))
        y = pos.y()
        if y + size.height() > geo.bottom() - 4:
            y = max(geo.top() + 4, geo.bottom() - size.height() - 4)
        return QPoint(x, y)

    def _populate_menu(self, menu: QMenu) -> None:
        i18n = get_i18n()
        cfg = self.config
        style = QApplication.style()
        menu.clear()

        refresh_now = menu.addAction(
            style.standardIcon(QStyle.SP_BrowserReload), i18n.t("menu.refreshNow")
        )
        refresh_now.triggered.connect(self._on_refresh_now)
        menu.addSeparator()

        # 显示设置 ▸(已用/剩余互斥)
        display_menu = menu.addMenu(
            style.standardIcon(QStyle.SP_FileDialogListView),
            i18n.t("menu.displaySettings"),
        )
        mode = cfg.display_mode()
        for mode_key, text_key in (("used", "menu.showUsed"), ("remaining", "menu.showRemaining")):
            act = display_menu.addAction(i18n.t(text_key))
            act.setCheckable(True)
            act.setChecked(mode == mode_key)
            act.setData(mode_key)
            act.triggered.connect(
                lambda checked=False, m=mode_key, sub=display_menu: self._set_display_mode(m, sub)
            )

        # 任务栏数字面板 + 位置 + 百分号
        overlay_act = menu.addAction(
            style.standardIcon(QStyle.SP_DesktopIcon), i18n.t("menu.taskbarOverlay")
        )
        overlay_act.setCheckable(True)
        overlay_act.setChecked(cfg.overlay_enabled())
        overlay_act.triggered.connect(lambda checked=False: self._toggle_overlay(checked))

        anchor_menu = menu.addMenu(i18n.t("menu.overlayPosition"))
        anchor = cfg.overlay_anchor()
        for key, text_key in (("left", "menu.overlayPosition.left"), ("right", "menu.overlayPosition.right")):
            act = anchor_menu.addAction(i18n.t(text_key))
            act.setCheckable(True)
            act.setChecked(anchor == key)
            act.triggered.connect(
                lambda checked=False, a=key, sub=anchor_menu: self._set_overlay_anchor(a, sub)
            )

        pct_act = menu.addAction(i18n.t("menu.showPercent"))
        pct_act.setCheckable(True)
        pct_act.setChecked(cfg.show_percent())
        pct_act.triggered.connect(lambda checked=False: self._toggle_show_percent(checked))
        menu.addSeparator()

        # 账号 ▸:查看详情 / 配置 / 重新登录 / 启用 / 改名 / 排序 / 删除 / 添加
        accounts_menu = menu.addMenu(
            style.standardIcon(QStyle.SP_FileDialogDetailedView),
            i18n.t("menu.accounts"),
        )
        self._fill_accounts_menu(accounts_menu)

        # 显示指标 ▸(按实例分组,最多 2 个)
        metrics_menu = menu.addMenu(i18n.t("menu.enabledMetrics"))
        self._fill_metrics_menu(metrics_menu)
        menu.addSeparator()

        # 设置 ▸
        settings_menu = menu.addMenu(
            style.standardIcon(QStyle.SP_ComputerIcon), i18n.t("menu.settings")
        )
        interval_menu = settings_menu.addMenu(i18n.t("menu.refreshInterval"))
        current = cfg.refresh_interval()
        for interval, text_key in _REFRESH_ITEMS:
            act = interval_menu.addAction(i18n.t(text_key))
            act.setCheckable(True)
            act.setChecked(interval == current)
            act.setData(interval.value)
            act.triggered.connect(
                lambda checked=False, iv=interval, sub=interval_menu: self._set_refresh_interval(iv, sub)
            )

        language_menu = settings_menu.addMenu(i18n.t("menu.language"))
        locale = cfg.locale()
        for code, text_key in (("en", "menu.lang.en"), ("zh-Hans", "menu.lang.zh")):
            act = language_menu.addAction(i18n.t(text_key))
            act.setCheckable(True)
            act.setChecked(locale == code)
            act.triggered.connect(lambda checked=False, c=code, sub=language_menu: self._set_locale(c, sub))
            act.setData(code)

        tray_act = settings_menu.addAction(i18n.t("menu.trayIcon"))
        tray_act.setCheckable(True)
        tray_act.setChecked(cfg.tray_icon_enabled())
        tray_act.triggered.connect(lambda checked=False: self._toggle_tray_icon(checked))

        settings_menu.addSeparator()
        about = settings_menu.addAction(
            style.standardIcon(QStyle.SP_MessageBoxInformation), i18n.t("menu.about")
        )
        about.triggered.connect(self._show_about)

        menu.addSeparator()
        quit_act = menu.addAction(
            style.standardIcon(QStyle.SP_DialogCloseButton), i18n.t("menu.quit")
        )
        quit_act.triggered.connect(QApplication.quit)

    def _account_color(self, inst) -> str:
        data = self.data(inst.id)
        if data is None and self.error(inst.id) is not None:
            return _STATUS_COLORS[render.COLOR_RED]
        return _STATUS_COLORS.get(render.status_color(data), _STATUS_COLORS[render.COLOR_GRAY])

    def _fill_accounts_menu(self, accounts_menu: QMenu) -> None:
        i18n = get_i18n()
        cfg = self.config
        style = QApplication.style()
        instances = cfg.instances()
        enabled_count = sum(1 for it in instances if cfg.is_enabled(it.id))

        for index, inst in enumerate(instances):
            sub = accounts_menu.addMenu(_dot_icon(self._account_color(inst)), inst.display_name)
            enabled = cfg.is_enabled(inst.id)

            details = sub.addAction(
                style.standardIcon(QStyle.SP_FileDialogDetailedView), i18n.t("menu.details")
            )
            details.triggered.connect(
                lambda checked=False, iid=inst.id: self.open_detail_for(iid)
            )

            configure = sub.addAction(
                style.standardIcon(QStyle.SP_DialogSaveButton), i18n.t("menu.configurePlatform")
            )
            configure.triggered.connect(
                lambda checked=False, iid=inst.id: self._open_config_for(iid)
            )

            if inst.platform_type.cookie_based():
                relogin = sub.addAction(i18n.t("menu.renewSession"))
                relogin.triggered.connect(
                    lambda checked=False, iid=inst.id: self._relogin_account(iid)
                )
            sub.addSeparator()

            pin_act = sub.addAction(i18n.t("menu.pinToStatusBar"))
            pin_act.setCheckable(True)
            pin_act.setChecked(cfg.is_pinned(inst.id))
            pin_act.setEnabled(enabled)
            pin_act.triggered.connect(
                lambda checked=False, iid=inst.id: self._toggle_pinned(iid)
            )

            enable_act = sub.addAction(i18n.t("menu.platformEnabled"))
            enable_act.setCheckable(True)
            enable_act.setChecked(enabled)
            enable_act.triggered.connect(
                lambda checked=False, iid=inst.id, act=enable_act: self._toggle_enabled(iid, act)
            )

            rename_act = sub.addAction(i18n.t("menu.renameAccount"))
            rename_act.triggered.connect(lambda checked=False, iid=inst.id: self._rename_account(iid))

            left_act = sub.addAction(
                style.standardIcon(QStyle.SP_ArrowLeft), i18n.t("menu.moveLeft")
            )
            left_act.setEnabled(index > 0)
            left_act.triggered.connect(lambda checked=False, iid=inst.id: self._move_account(iid, -1))

            right_act = sub.addAction(
                style.standardIcon(QStyle.SP_ArrowRight), i18n.t("menu.moveRight")
            )
            right_act.setEnabled(index < len(instances) - 1)
            right_act.triggered.connect(lambda checked=False, iid=inst.id: self._move_account(iid, 1))

            delete_act = sub.addAction(
                style.standardIcon(QStyle.SP_TrashIcon), i18n.t("menu.deleteAccount")
            )
            delete_act.setEnabled(not (enabled and enabled_count <= 1))
            delete_act.triggered.connect(lambda checked=False, iid=inst.id: self._delete_account(iid))

        accounts_menu.addSeparator()
        add_menu = accounts_menu.addMenu(
            style.standardIcon(QStyle.SP_FileDialogNewFolder), i18n.t("menu.platform")
        )
        for platform in PlatformType:
            add_act = add_menu.addAction(i18n.t(f"menu.addAccount.{platform.value}"))
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
        """面板开关;关闭时若托盘图标也是关的,自动打开托盘图标。

        不变式:面板与托盘图标至少留一个入口。否则进程还在(setQuitOnLastWindowClosed
        为 False,不会自杀)、定时器还在打网络,但用户既打不开详情窗也点不出菜单,
        只能去任务管理器杀进程——等于把应用锁死。

        两个开关一起改时用**一次落盘**(set_entry_visibility):分两次写的话,
        第二步失败会留下「面板关 + 托盘关」的落盘状态,重启后彻底没有入口。
        """
        with self._config_write() as st:
            if not checked and not self.config.tray_icon_enabled():
                self.config.set_entry_visibility(overlay=False, tray=True)
            else:
                self.config.set_overlay_enabled(checked)
        if not st.ok:
            return  # 落盘失败:配置已回滚,界面保持原状
        if checked:
            self._overlay.show()
            if self.config.overlay_embedded():
                # 嵌入模式重新打开:要补一次嵌入(explorer 重启过的话旧句柄已失效)
                self._overlay.ensure_embedded_state()
        else:
            # 关闭必须真的把面板收起来(只改配置不 hide,面板会一直挂在任务栏上)
            self._overlay.hide()
            self._rebuild_trays()
        self._overlay.refresh()

    def _toggle_show_percent(self, checked: bool) -> None:
        """面板数值是否带 % 号"""
        with self._config_write() as st:
            self.config.set_show_percent(checked)
        if not st.ok:
            return
        self._overlay.refresh()

    def _toggle_tray_icon(self, checked: bool) -> None:
        """托盘图标开关:关闭后托盘区不再有图标,面板与详情窗照常。

        不变式:托盘图标关闭时面板必须开着(否则没有任何入口)。
        两个开关一起改时同样用一次落盘。
        """
        with self._config_write() as st:
            if not checked and not self.config.overlay_enabled():
                self.config.set_entry_visibility(overlay=True, tray=False)
            else:
                self.config.set_tray_icon_enabled(checked)
        if not st.ok:
            return
        if not checked and self.config.overlay_enabled():
            self._overlay.show()
            if self.config.overlay_embedded():
                self._overlay.attach()  # 嵌入模式要把面板送回任务栏,不能只当浮层
        self._rebuild_trays()

    def _set_overlay_anchor(self, anchor: str, sub: Optional[QMenu] = None) -> None:
        """面板锚点:靠任务栏左端 / 靠通知区"""
        with self._config_write() as st:
            self.config.set_overlay_anchor(anchor)
        if not st.ok:
            return
        if sub is not None:
            self._sync_check_group(sub, anchor)
        self._overlay.refresh()

    def _toggle_overlay_embedded(self, checked: bool) -> None:
        """切换「嵌入任务栏」与「浮层」两种形态"""
        with self._config_write() as st:
            self.config.set_overlay_embedded(checked)
        if not st.ok:
            return
        if checked:
            self._overlay.attach()
        else:
            self._overlay.detach()
        self._overlay.refresh()

    def _set_display_mode(self, mode: str, sub: Optional[QMenu] = None) -> None:
        with self._config_write() as st:
            self.config.set_display_mode(mode)
        if not st.ok:
            return
        if sub is not None:
            self._sync_check_group(sub, mode)
        self._rebuild_trays()
        self._reload_detail()

    def _set_refresh_interval(self, interval: RefreshInterval, sub: Optional[QMenu] = None) -> None:
        with self._config_write() as st:
            self.config.set_refresh_interval(interval)
        if not st.ok:
            return
        if sub is not None:
            self._sync_check_group(sub, interval.value)
        self._restart_timer()
        self._reload_detail()

    def _set_locale(self, locale: str, sub: Optional[QMenu] = None) -> None:
        get_i18n().set_locale(locale)
        with self._config_write() as st:
            self.config.set_locale(locale)
        if not st.ok:
            get_i18n().set_locale(self.config.locale())  # 回滚语言
            return
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
            with self._config_write():
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
            with self._config_write():
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
        with self._config_write():
            try:
                self.config.rename_instance(instance_id, text)
            except KeyError:
                return  # 实例已被删(菜单开着的时候被别的路径删了):静默忽略
        self._on_config_changed()

    def _move_account(self, instance_id: str, offset: int) -> None:
        with self._config_write():
            try:
                self.config.move_instance(instance_id, offset)
            except (KeyError, ValueError):
                return  # 实例已被删:静默忽略(对齐 macOS 的 guard)
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
        with self._config_write() as st:
            try:
                self.config.delete_instance(instance_id)
            except KeyError:
                st.ok = False  # 确认框开着时账号已被别的路径删了
        if not st.ok:
            return
        self._data.pop(instance_id, None)
        self._errors.pop(instance_id, None)
        self._loading.discard(instance_id)
        self._gen.pop(instance_id, None)
        self.manager.remove_instance(instance_id)  # 释放已删账号的 service 与缓存
        self._on_config_changed()

    def _add_account(self, platform: PlatformType) -> None:
        """新增账号:直接进配置态。

        对齐 macOS 版——新增后立刻弹配置框让用户填凭据;配置前取消就把
        空壳账号回收掉,不在菜单里堆积没配置的"幽灵账号"。
        """
        with self._config_write() as st:
            inst = self.config.add_instance(platform)
        if not st.ok:
            return
        self._pending_instance_id = inst.id
        self._on_config_changed()
        self._ensure_detail_visible()
        self._detail._open_config(inst.id)

    def _on_config_cancelled(self, instance_id: str) -> None:
        """配置面板被取消:新建的空壳账号回收(对齐 macOS)"""
        if self._pending_instance_id != instance_id:
            return
        self._pending_instance_id = None
        if self.config.is_configured(instance_id):
            return  # 已经配好了,不是空壳
        with self._config_write() as st:
            try:
                self.config.delete_instance(instance_id)
            except KeyError:
                st.ok = False
        if not st.ok:
            return
        self._data.pop(instance_id, None)
        self._errors.pop(instance_id, None)
        self._loading.discard(instance_id)
        self._gen.pop(instance_id, None)
        self.manager.remove_instance(instance_id)
        self._on_config_changed()

    def _on_config_saved(self, instance_id: str) -> None:
        """配置面板保存成功:不再是待回收的空壳账号"""
        if self._pending_instance_id == instance_id:
            self._pending_instance_id = None

    def _ensure_detail_visible(self) -> None:
        """确保详情窗已打开(不切换显隐)"""
        if self._detail is None:
            self._detail = DetailWindow(self)
        if not self._detail.isVisible():
            self._detail.reload()
            self._detail.show()
            self._detail.raise_()

    def _toggle_metric(self, instance_id: str, label: str, sub: QMenu) -> None:
        cfg = self.config
        current = list(cfg.enabled_metrics(instance_id))
        if label in current:
            current = [item for item in current if item != label]
        else:
            current.append(label)
        try:
            with self._config_write():
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
        """有没有「已启用已配置但还没数据」的账号(需要补拉一次)。

        必须遍历全部账号而不是只遍历托盘图标集合:托盘图标默认关闭时图标集合
        恒空,新加的账号会一直显示 "--" 直到下一个刷新周期(最长 10 分钟)。
        """
        for inst in self.config.instances():
            if inst.id in self._data:
                continue
            if self.config.is_enabled(inst.id) and self.config.is_configured(inst.id):
                return True
        return False

    def _message_parent(self):
        """模态框的父窗口:详情窗可见时挂在它上面,否则无父"""
        if self._detail is not None and self._detail.isVisible():
            return self._detail
        return None

    @contextmanager
    def _config_write(self):
        """配置落盘的统一出口:写盘失败时提示用户并回滚。

        不提示的话,界面会显示"已保存",重启后设置却悄悄消失——用户完全
        无从察觉。ConfigService 侧已回滚内存状态,这里负责告知,并把
        写入结果通过 st.ok 交给调用方:失败时调用方要跳过界面联动,
        否则界面显示的状态与落盘的不一致。
        """
        st = _WriteState()
        try:
            yield st
        except (OSError, TypeError, UnicodeEncodeError, KeyError, RecursionError) as e:
            # OSError:磁盘满/无权限;UnicodeEncodeError:内容里有代理字符,编不出;
            # TypeError:数据结构不对;KeyError:实例已被别的路径删掉;
            # RecursionError:配置嵌套过深。都算「没保存成功」,统一提示 + 回滚。
            # **不捕 ValueError**:那是业务规则拒绝(ConfigRuleError),要交给
            # 调用方自己的 except ValueError 分支给对应提示,不能混成「保存失败」。
            self._report_save_error(e)
            st.ok = False

    def _report_save_error(self, e: Exception) -> None:
        """落盘失败提示"""
        QMessageBox.warning(
            self._message_parent(),
            get_i18n().t("app.name"),
            get_i18n().t("error.saveFailed", str(e)),
        )


def _wrap_error(e: Exception) -> PlatformError:
    """把未预期异常包成网络错误,不拖垮整批"""
    return PlatformError(ErrorKind.NETWORK, f"{type(e).__name__}: {e}")


def _STARTUP_MESSAGE() -> str:
    """启动气泡文案。"""
    if get_i18n().locale.startswith("zh"):
        return "QuotaBar 已在系统托盘运行"
    return "QuotaBar is running in the system tray"


def _dot_icon(color: str, size: int = 10) -> QIcon:
    """菜单里的账号状态点"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(1, 1, size - 2, size - 2)
    finally:
        painter.end()
    return QIcon(pixmap)
