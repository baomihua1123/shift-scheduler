"""新增 / 編輯用的對話框。"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from ..repo import Position, Staff
from .common import warn


class StaffDialog(QDialog):
    def __init__(self, parent, positions: Sequence[Position], staff: Optional[Staff] = None):
        super().__init__(parent)
        self.setWindowTitle("編輯人員" if staff else "新增人員")
        self.setMinimumWidth(360)
        form = QFormLayout()
        self.name_edit = QLineEdit(staff.name if staff else "")
        form.addRow("姓名", self.name_edit)

        self.pos_list = QListWidget()
        selected = set(staff.position_ids) if staff else set()
        for p in positions:
            item = QListWidgetItem(p.name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if p.id in selected else Qt.CheckState.Unchecked
            )
            item.setData(Qt.ItemDataRole.UserRole, p.id)
            self.pos_list.addItem(item)
        form.addRow("適用崗位（可複選）", self.pos_list)
        self.note_edit = QLineEdit(staff.note if staff else "")
        form.addRow("備註", self.note_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        if not positions:
            layout.addWidget(QLabel("（目前沒有崗位，請先到「設定」頁籤新增崗位）"))
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        if not self.name_edit.text().strip():
            warn(self, "資料不完整", "請輸入姓名")
            return
        self.accept()

    def values(self) -> Tuple[str, List[int], str]:
        ids = []
        for i in range(self.pos_list.count()):
            it = self.pos_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                ids.append(int(it.data(Qt.ItemDataRole.UserRole)))
        return self.name_edit.text().strip(), ids, self.note_edit.text().strip()


class PositionDialog(QDialog):
    def __init__(self, parent, position: Optional[Position] = None):
        super().__init__(parent)
        self.setWindowTitle("編輯崗位" if position else "新增崗位")
        self.setMinimumWidth(320)
        form = QFormLayout()
        self.name_edit = QLineEdit(position.name if position else "")
        self.note_edit = QLineEdit(position.note if position else "")
        form.addRow("崗位名稱", self.name_edit)
        form.addRow("備註", self.note_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        if not self.name_edit.text().strip():
            warn(self, "資料不完整", "請輸入崗位名稱")
            return
        self.accept()

    def values(self) -> Tuple[str, str]:
        return self.name_edit.text().strip(), self.note_edit.text().strip()


class CopyEventDialog(QDialog):
    def __init__(self, parent, default_name: str):
        super().__init__(parent)
        self.setWindowTitle("複製活動設定")
        self.setMinimumWidth(360)
        form = QFormLayout()
        self.name_edit = QLineEdit(default_name)
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        form.addRow("新活動名稱", self.name_edit)
        form.addRow("第一天日期", self.date_edit)
        tip = QLabel("會複製：班次時長、工作時間、共同休息、所需崗位、出席人員。\n不會複製班表；多天活動的日期會整體平移。")
        tip.setWordWrap(True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(tip)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        if not self.name_edit.text().strip():
            warn(self, "資料不完整", "請輸入活動名稱")
            return
        self.accept()

    def values(self) -> Tuple[str, str]:
        return self.name_edit.text().strip(), self.date_edit.date().toString("yyyy-MM-dd")
