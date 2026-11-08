"""「設定」頁籤：崗位管理。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..repo import Repository
from .common import confirm, info, warn
from .dialogs import PositionDialog


class SettingsTab(QWidget):
    changed = Signal()

    def __init__(self, repo: Repository, db_path: Path, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.db_path = db_path

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["崗位名稱", "備註", "狀態"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemDoubleClicked.connect(lambda _i: self.edit_position())

        self.show_inactive = QCheckBox("顯示已停用的崗位")
        self.show_inactive.toggled.connect(lambda _v: self.reload())

        add_btn = QPushButton("新增崗位")
        add_btn.clicked.connect(self.add_position)
        edit_btn = QPushButton("編輯")
        edit_btn.clicked.connect(self.edit_position)
        del_btn = QPushButton("刪除")
        del_btn.clicked.connect(self.delete_position)
        up_btn = QPushButton("上移")
        up_btn.clicked.connect(lambda: self.move(-1))
        down_btn = QPushButton("下移")
        down_btn.clicked.connect(lambda: self.move(1))
        restore_btn = QPushButton("恢復停用的崗位")
        restore_btn.clicked.connect(self.restore_position)

        bar = QHBoxLayout()
        for b in (add_btn, edit_btn, del_btn, up_btn, down_btn, restore_btn):
            bar.addWidget(b)
        bar.addStretch(1)

        hint = QLabel(
            "崗位資訊為所有活動共用；順序即為班表由左到右的順序。\n"
            "崗位若曾被活動使用，刪除時會改為「停用」，過往班表不受影響。"
        )
        hint.setWordWrap(True)

        box = QGroupBox("資料位置")
        box_layout = QVBoxLayout(box)
        path_label = QLabel(f"資料庫檔案：{self.db_path}")
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path_label.setWordWrap(True)
        box_layout.addWidget(path_label)
        box_layout.addWidget(QLabel("備份與還原請使用上方選單「檔案」。"))

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addLayout(bar)
        layout.addWidget(self.show_inactive)
        layout.addWidget(self.table, 1)
        layout.addWidget(box)
        self.reload()

    def reload(self) -> None:
        items = self.repo.list_positions(include_inactive=self.show_inactive.isChecked())
        self.table.setRowCount(len(items))
        for r, p in enumerate(items):
            for c, text in enumerate([p.name, p.note, "使用中" if p.is_active else "已停用"]):
                item = QTableWidgetItem(text)
                if not p.is_active:
                    item.setForeground(QBrush(QColor("gray")))
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, p.id)
                self.table.setItem(r, c, item)

    def _selected_id(self) -> Optional[int]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return int(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _select_by_id(self, position_id: int) -> None:
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and int(item.data(Qt.ItemDataRole.UserRole)) == position_id:
                self.table.selectRow(r)
                return

    def add_position(self) -> None:
        dlg = PositionDialog(self)
        if not dlg.exec():
            return
        name, note = dlg.values()
        try:
            self.repo.add_position(name, note)
        except ValueError as exc:
            warn(self, "無法新增", str(exc))
            return
        self.reload()
        self.changed.emit()

    def edit_position(self) -> None:
        pid = self._selected_id()
        if pid is None:
            info(self, "請先選擇", "請先在表格中選擇要編輯的崗位")
            return
        pos = self.repo.get_position(pid)
        if pos is None:
            return
        dlg = PositionDialog(self, pos)
        if not dlg.exec():
            return
        name, note = dlg.values()
        try:
            self.repo.update_position(pid, name, note)
        except ValueError as exc:
            warn(self, "無法儲存", str(exc))
            return
        self.reload()
        self._select_by_id(pid)
        self.changed.emit()

    def delete_position(self) -> None:
        pid = self._selected_id()
        if pid is None:
            info(self, "請先選擇", "請先在表格中選擇要刪除的崗位")
            return
        pos = self.repo.get_position(pid)
        if pos is None:
            return
        if not confirm(self, "確認刪除", f"確定要刪除崗位「{pos.name}」嗎？"):
            return
        result = self.repo.delete_position(pid)
        if result == "deactivated":
            info(self, "已停用", f"「{pos.name}」曾被活動使用，已改為停用；過往班表不受影響。")
        self.reload()
        self.changed.emit()

    def restore_position(self) -> None:
        pid = self._selected_id()
        if pid is None:
            info(self, "請先選擇", "請先勾選「顯示已停用的崗位」，再選擇要恢復的崗位")
            return
        self.repo.set_position_active(pid, True)
        self.reload()
        self.changed.emit()

    def move(self, delta: int) -> None:
        pid = self._selected_id()
        if pid is None:
            return
        self.repo.move_position(pid, delta)
        self.reload()
        self._select_by_id(pid)
        self.changed.emit()
