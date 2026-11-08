"""主視窗：三個頁籤 + 備份 / 還原選單。"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMainWindow, QTabWidget

from ..backup import auto_backup
from ..db import connect
from ..repo import Repository
from .common import confirm, error, info
from .events_tab import EventsTab
from .settings_tab import SettingsTab
from .staff_tab import StaffTab


class MainWindow(QMainWindow):
    def __init__(self, repo: Repository, db_path: Path):
        super().__init__()
        self.repo = repo
        self.db_path = Path(db_path)
        self.setWindowTitle("短期活動排班系統")

        self.events_tab = EventsTab(repo)
        self.staff_tab = StaffTab(repo)
        self.settings_tab = SettingsTab(repo, self.db_path)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.events_tab, "活動專案")
        self.tabs.addTab(self.staff_tab, "人員管理")
        self.tabs.addTab(self.settings_tab, "設定")
        self.setCentralWidget(self.tabs)

        self.staff_tab.changed.connect(self.events_tab.reload_catalogs)
        self.settings_tab.changed.connect(self._on_positions_changed)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        menu = self.menuBar().addMenu("檔案")
        backup = QAction("備份資料庫…", self)
        backup.triggered.connect(lambda _c=False: self.backup_database())
        restore = QAction("還原備份…", self)
        restore.triggered.connect(lambda _c=False: self.restore_database())
        open_dir = QAction("開啟資料所在資料夾", self)
        open_dir.triggered.connect(lambda _c=False: self.open_data_folder())
        quit_action = QAction("離開", self)
        quit_action.triggered.connect(lambda _c=False: self.close())
        menu.addAction(backup)
        menu.addAction(restore)
        menu.addAction(open_dir)
        menu.addSeparator()
        menu.addAction(quit_action)

        self.statusBar().showMessage(f"資料庫：{self.db_path}")

    # ------------------------------------------------------------------
    def _on_positions_changed(self) -> None:
        self.staff_tab.reload()
        self.events_tab.reload_catalogs()

    def _on_tab_changed(self, index: int) -> None:
        # 切換回活動頁時，確保人員與崗位選項是最新的
        if self.tabs.widget(index) is self.events_tab:
            self.events_tab.reload_catalogs()

    def _reload_everything(self) -> None:
        self.settings_tab.reload()
        self.staff_tab.reload()
        self.events_tab.current_event_id = None
        self.events_tab.reload_events()
        self.events_tab.new_event()
        self.events_tab.stack.setCurrentIndex(0)

    # ------------------------------------------------------------------
    def backup_database(self) -> None:
        default = f"排班備份_{datetime.now().strftime('%Y%m%d_%H%M')}.db"
        path, _ = QFileDialog.getSaveFileName(self, "備份資料庫", default, "資料庫備份 (*.db)")
        if not path:
            return
        try:
            self.repo.backup_to(path)
        except Exception as exc:  # noqa: BLE001
            error(self, "備份失敗", str(exc))
            return
        info(self, "備份完成", f"已備份到：\n{path}")

    def restore_database(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "選擇備份檔", "", "資料庫備份 (*.db)")
        if not path:
            return
        try:
            probe = sqlite3.connect(path)
            try:
                tables = {r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                probe.close()
        except sqlite3.Error as exc:
            error(self, "無法還原", f"這不是有效的備份檔：{exc}")
            return
        if not {"staff", "positions", "events"} <= tables:
            error(self, "無法還原", "這個檔案不是本軟體的備份檔。")
            return
        if not confirm(
            self,
            "確認還原",
            "還原會用備份檔取代目前所有資料（人員、崗位、活動與班表）。\n"
            "系統會先把目前的資料另存一份，確定要還原嗎？",
        ):
            return
        try:
            safety = self.db_path.with_name(
                f"還原前自動備份_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            )
            self.repo.backup_to(safety)
            self.repo.close()
            shutil.copyfile(path, self.db_path)
            self.repo.conn = connect(self.db_path)
        except Exception as exc:  # noqa: BLE001
            # 盡量讓程式回到可用狀態
            try:
                self.repo.conn = connect(self.db_path)
            except Exception:  # noqa: BLE001
                pass
            error(self, "還原失敗", str(exc))
            return
        self._reload_everything()
        info(self, "還原完成", f"已還原。還原前的資料已另存為：\n{safety}")

    def open_data_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.db_path.parent)))

    def closeEvent(self, event) -> None:  # noqa: N802
        self.events_tab.shutdown()
        try:
            auto_backup(self.repo, self.db_path)
        except Exception:  # noqa: BLE001  備份失敗不應該影響關閉程式
            pass
        super().closeEvent(event)
