"""「人員管理」頁籤。"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import excel_io
from ..repo import Repository
from .common import confirm, error, info, warn
from .dialogs import StaffDialog


class StaffTab(QWidget):
    changed = Signal()

    def __init__(self, repo: Repository, parent=None):
        super().__init__(parent)
        self.repo = repo

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["姓名", "適用崗位", "備註", "狀態"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemDoubleClicked.connect(lambda _item: self.edit_staff())

        self.show_inactive = QCheckBox("顯示已停用的人員")
        self.show_inactive.toggled.connect(lambda _v: self.reload())

        add_btn = QPushButton("新增人員")
        add_btn.clicked.connect(self.add_staff)
        edit_btn = QPushButton("編輯")
        edit_btn.clicked.connect(self.edit_staff)
        del_btn = QPushButton("刪除")
        del_btn.clicked.connect(self.delete_staff)
        restore_btn = QPushButton("恢復停用的人員")
        restore_btn.clicked.connect(self.restore_staff)
        import_btn = QPushButton("從 Excel 匯入名單")
        import_btn.clicked.connect(self.import_excel)
        export_btn = QPushButton("匯出名單")
        export_btn.clicked.connect(self.export_excel)
        template_btn = QPushButton("下載匯入範本")
        template_btn.clicked.connect(self.export_template)

        top = QHBoxLayout()
        for b in (add_btn, edit_btn, del_btn, restore_btn):
            top.addWidget(b)
        top.addStretch(1)
        for b in (import_btn, export_btn, template_btn):
            top.addWidget(b)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.show_inactive)
        layout.addWidget(self.table)
        self.reload()

    # ------------------------------------------------------------------
    def reload(self) -> None:
        positions = {p.id: p.name for p in self.repo.list_positions(include_inactive=True)}
        staff = self.repo.list_staff(include_inactive=self.show_inactive.isChecked())
        self.table.setRowCount(len(staff))
        for r, s in enumerate(staff):
            pos_text = "、".join(positions.get(pid, "?") for pid in s.position_ids)
            values = [s.name, pos_text, s.note, "使用中" if s.is_active else "已停用"]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                if not s.is_active:
                    item.setForeground(QBrush(QColor("gray")))
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, s.id)
                self.table.setItem(r, c, item)

    def _selected_id(self) -> Optional[int]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return int(item.data(Qt.ItemDataRole.UserRole)) if item else None

    # ------------------------------------------------------------------
    def add_staff(self) -> None:
        dlg = StaffDialog(self, self.repo.list_positions())
        if not dlg.exec():
            return
        name, pids, note = dlg.values()
        try:
            self.repo.add_staff(name, pids, note)
        except ValueError as exc:
            warn(self, "無法新增", str(exc))
            return
        self.reload()
        self.changed.emit()

    def edit_staff(self) -> None:
        sid = self._selected_id()
        if sid is None:
            info(self, "請先選擇", "請先在表格中選擇要編輯的人員")
            return
        staff = self.repo.get_staff(sid)
        if staff is None:
            return
        positions = self.repo.list_positions()
        # 已停用的崗位若仍在此人員的適用崗位中，也要顯示出來，避免編輯時被悄悄移除
        known = {p.id for p in positions}
        for p in self.repo.list_positions(include_inactive=True):
            if p.id in staff.position_ids and p.id not in known:
                positions.append(p)
        dlg = StaffDialog(self, positions, staff)
        if not dlg.exec():
            return
        name, pids, note = dlg.values()
        try:
            self.repo.update_staff(sid, name, pids, note)
        except ValueError as exc:
            warn(self, "無法儲存", str(exc))
            return
        self.reload()
        self.changed.emit()

    def delete_staff(self) -> None:
        sid = self._selected_id()
        if sid is None:
            info(self, "請先選擇", "請先在表格中選擇要刪除的人員")
            return
        staff = self.repo.get_staff(sid)
        if staff is None:
            return
        if not confirm(
            self,
            "確認刪除",
            f"確定要刪除「{staff.name}」嗎？\n（若此人員曾參與活動，會改為「停用」以保留過往班表。）",
        ):
            return
        result = self.repo.delete_staff(sid)
        if result == "deactivated":
            info(self, "已停用", f"「{staff.name}」曾參與活動，已改為停用；過往班表不受影響。")
        self.reload()
        self.changed.emit()

    def restore_staff(self) -> None:
        sid = self._selected_id()
        if sid is None:
            info(self, "請先選擇", "請先勾選「顯示已停用的人員」，再選擇要恢復的人員")
            return
        self.repo.set_staff_active(sid, True)
        self.reload()
        self.changed.emit()

    # ------------------------------------------------------------------
    def _rows_for_export(self):
        names = {p.id: p.name for p in self.repo.list_positions(include_inactive=True)}
        return [
            (s.name, [names.get(pid, "?") for pid in s.position_ids], s.note)
            for s in self.repo.list_staff()
        ]

    def export_excel(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "匯出人員名單", "人員名單.xlsx", "Excel 檔案 (*.xlsx)")
        if not path:
            return
        try:
            excel_io.export_staff(self._rows_for_export(), path)
        except OSError as exc:
            error(self, "匯出失敗", f"無法寫入檔案：{exc}")
            return
        info(self, "匯出完成", f"已匯出到：\n{path}")

    def export_template(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "下載匯入範本", "人員名單範本.xlsx", "Excel 檔案 (*.xlsx)"
        )
        if not path:
            return
        positions = [p.name for p in self.repo.list_positions()][:2] or ["導播"]
        try:
            excel_io.export_staff([("王小明（範例，請刪除）", positions, "")], path)
        except OSError as exc:
            error(self, "匯出失敗", f"無法寫入檔案：{exc}")
            return
        info(self, "完成", f"範本已存到：\n{path}")

    def import_excel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "選擇人員名單", "", "Excel 檔案 (*.xlsx)")
        if not path:
            return
        try:
            rows = excel_io.read_staff_workbook(path)
        except Exception as exc:  # noqa: BLE001
            error(self, "匯入失敗", f"無法讀取檔案：{exc}")
            return
        if not rows:
            info(self, "沒有資料", "檔案中沒有可匯入的人員。")
            return
        if not confirm(self, "確認匯入", f"共 {len(rows)} 位人員。同名者會更新其適用崗位，確定匯入嗎？"):
            return
        try:
            rep = self.repo.import_staff_rows(rows)
        except Exception as exc:  # noqa: BLE001
            error(self, "匯入失敗", str(exc))
            return
        msg = f"新增 {rep.added} 位、更新 {rep.updated} 位。"
        if rep.warnings:
            msg += "\n\n" + "\n".join(rep.warnings[:15])
        info(self, "匯入完成", msg)
        self.reload()
        self.changed.emit()
