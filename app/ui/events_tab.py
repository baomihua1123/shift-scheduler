"""「活動專案」頁籤：活動清單、活動設定、班表檢視與手動調整。"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QDate, Qt, QThread, QTime, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from .. import engine, excel_io
from ..repo import (
    STATUS_FINAL,
    STATUS_LABELS,
    Event,
    EventBreak,
    EventDay,
    EventPosition,
    EventSummary,
    Repository,
)
from ..schedule_view import DayView, ScheduleView
from ..timeslots import date_label
from .common import confirm, error, info, safe_filename, warn
from .dialogs import CopyEventDialog

_STYLE_LOCKED = "QComboBox { background-color: #FFF2CC; }"
_STYLE_BAD = "QComboBox { background-color: #F8CBAD; }"
_DED_CHOICES = [("自動判斷", None), ("一律專任", 1), ("一律不專任", 0)]


class SolveWorker(QThread):
    """在背景執行排班計算（只做純計算，不碰資料庫），避免畫面凍結。"""

    done = Signal(object)

    def __init__(self, problem: engine.Problem, options: engine.SolveOptions, parent=None):
        super().__init__(parent)
        self._problem = problem
        self._options = options

    def run(self) -> None:
        try:
            result = engine.solve(self._problem, self._options)
        except Exception as exc:  # noqa: BLE001
            result = engine.SolveResult(ok=False, errors=[f"排班時發生未預期的錯誤：{exc}"])
        self.done.emit(result)


class EventsTab(QWidget):
    def __init__(self, repo: Repository, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.current_event_id: Optional[int] = None
        self._status = "draft"
        self._worker: Optional[SolveWorker] = None
        self._gen_warnings: List[str] = []
        self._gen_notes: List[str] = []
        self._cells: Dict[Tuple[int, int, int], QComboBox] = {}
        self._pos_id_by_name: Dict[str, int] = {}
        self._qual_staff: Dict[int, List[Tuple[int, str]]] = {}
        self._view: Optional[ScheduleView] = None

        self._build_ui()
        self.reload_events()
        self._show_placeholder()

    # ==================================================================
    # 建立介面
    # ==================================================================
    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ---- 左側：活動清單 ----
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("活動專案（含過往紀錄）"))
        self.event_list = QListWidget()
        self.event_list.currentItemChanged.connect(self._on_event_item_changed)
        left_layout.addWidget(self.event_list, 1)
        new_btn = QPushButton("新增活動")
        new_btn.clicked.connect(lambda _c=False: self.new_event())
        copy_btn = QPushButton("複製所選活動的設定")
        copy_btn.clicked.connect(lambda _c=False: self.copy_event())
        del_btn = QPushButton("刪除所選活動")
        del_btn.clicked.connect(lambda _c=False: self.delete_event())
        imp_btn = QPushButton("匯入 Excel 班表")
        imp_btn.clicked.connect(lambda _c=False: self.import_event())
        for b in (new_btn, copy_btn, del_btn, imp_btn):
            left_layout.addWidget(b)
        left.setMinimumWidth(250)

        # ---- 右側：設定 / 班表 ----
        self.stack = QStackedWidget()
        self.placeholder = QLabel("請從左側選擇活動，或按「新增活動」開始。")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.right_tabs = QTabWidget()
        self.right_tabs.addTab(self._build_settings_page(), "活動設定")
        self.right_tabs.addTab(self._build_schedule_page(), "班表")
        self.stack.addWidget(self.placeholder)
        self.stack.addWidget(self.right_tabs)

        splitter.addWidget(left)
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 1000])

        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

    def _build_settings_page(self) -> QWidget:
        inner = QWidget()
        root = QVBoxLayout(inner)

        # 基本資料
        basic = QGroupBox("基本資料")
        form = QFormLayout(basic)
        self.name_edit = QLineEdit()
        self.note_edit = QLineEdit()
        self.status_label = QLabel("")
        self.shift_spin = QSpinBox()
        self.shift_spin.setRange(5, 720)
        self.shift_spin.setSingleStep(5)
        self.shift_spin.setValue(90)
        self.shift_spin.setSuffix(" 分鐘")
        self.avoid_check = QCheckBox("盡量避免連續上班（做 1 休 1）")
        self.avoid_check.setChecked(True)
        form.addRow("活動名稱", self.name_edit)
        form.addRow("備註", self.note_edit)
        form.addRow("每班時長", self.shift_spin)
        form.addRow("休息分佈", self.avoid_check)
        form.addRow("狀態", self.status_label)
        root.addWidget(basic)

        # 工作日
        days_box = QGroupBox("工作日期與工作時間")
        days_layout = QVBoxLayout(days_box)
        self.days_table = QTableWidget(0, 3)
        self.days_table.setHorizontalHeaderLabels(["日期", "開始時間", "結束時間"])
        self.days_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.days_table.setMaximumHeight(170)
        day_bar = QHBoxLayout()
        add_day = QPushButton("新增一天")
        add_day.clicked.connect(lambda _c=False: self._add_next_day())
        del_day = QPushButton("刪除所選的一天")
        del_day.clicked.connect(lambda _c=False: self._remove_selected_day())
        day_bar.addWidget(add_day)
        day_bar.addWidget(del_day)
        day_bar.addStretch(1)
        days_layout.addWidget(self.days_table)
        days_layout.addLayout(day_bar)
        root.addWidget(days_box)

        # 共同休息
        self.break_check = QCheckBox("有共同休息時間（例如午餐、晚餐）")
        self.break_check.toggled.connect(self._on_break_toggled)
        self.breaks_box = QGroupBox("共同休息時段（休息時間不排班，也不算連續上班）")
        breaks_layout = QVBoxLayout(self.breaks_box)
        self.breaks_table = QTableWidget(0, 4)
        self.breaks_table.setHorizontalHeaderLabels(["日期", "名稱", "開始", "結束"])
        self.breaks_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.breaks_table.setMaximumHeight(150)
        break_bar = QHBoxLayout()
        add_all = QPushButton("為每一天新增休息時段")
        add_all.clicked.connect(lambda _c=False: self._add_breaks_for_all_days())
        del_break = QPushButton("刪除所選休息")
        del_break.clicked.connect(lambda _c=False: self._remove_selected_break())
        break_bar.addWidget(add_all)
        break_bar.addWidget(del_break)
        break_bar.addStretch(1)
        breaks_layout.addWidget(self.breaks_table)
        breaks_layout.addLayout(break_bar)
        self.breaks_box.setEnabled(False)
        root.addWidget(self.break_check)
        root.addWidget(self.breaks_box)

        # 所需崗位
        pos_box = QGroupBox("本次所需崗位")
        pos_layout = QVBoxLayout(pos_box)
        self.pos_table = QTableWidget(0, 4)
        self.pos_table.setHorizontalHeaderLabels(["使用", "崗位", "每班人數", "專任設定"])
        header = self.pos_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.pos_table.setMaximumHeight(220)
        tip = QLabel(
            "專任設定：「自動判斷」會把合格人數很少的崗位（例如只有 2 人會導播）改由這些人專任，"
            "他們不再進入其他崗位的輪替。"
        )
        tip.setWordWrap(True)
        pos_layout.addWidget(self.pos_table)
        pos_layout.addWidget(tip)
        root.addWidget(pos_box)

        # 出席人員
        staff_box = QGroupBox("本次出席人員")
        staff_layout = QVBoxLayout(staff_box)
        self.staff_list = QListWidget()
        self.staff_list.setMinimumHeight(160)
        staff_bar = QHBoxLayout()
        all_btn = QPushButton("全選")
        all_btn.clicked.connect(lambda _c=False: self._set_all_staff(True))
        none_btn = QPushButton("全不選")
        none_btn.clicked.connect(lambda _c=False: self._set_all_staff(False))
        staff_bar.addWidget(all_btn)
        staff_bar.addWidget(none_btn)
        staff_bar.addStretch(1)
        staff_layout.addWidget(self.staff_list)
        staff_layout.addLayout(staff_bar)
        root.addWidget(staff_box)

        # 動作
        actions = QHBoxLayout()
        self.save_btn = QPushButton("儲存設定")
        self.save_btn.clicked.connect(lambda _c=False: self.save_settings())
        self.save_gen_btn = QPushButton("儲存並產生班表")
        self.save_gen_btn.clicked.connect(lambda _c=False: self.generate())
        actions.addStretch(1)
        actions.addWidget(self.save_btn)
        actions.addWidget(self.save_gen_btn)
        root.addLayout(actions)
        root.addStretch(1)

        self.form_widgets = [basic, days_box, self.break_check, self.breaks_box, pos_box, staff_box]

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        return scroll

    def _build_schedule_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        bar = QHBoxLayout()
        self.gen_btn = QPushButton("產生 / 重新排班")
        self.gen_btn.clicked.connect(lambda _c=False: self.generate())
        self.clear_btn = QPushButton("清除班表")
        self.clear_btn.clicked.connect(lambda _c=False: self.clear_schedule())
        self.final_btn = QPushButton("定稿")
        self.final_btn.clicked.connect(lambda _c=False: self.toggle_final())
        self.export_btn = QPushButton("匯出 Excel")
        self.export_btn.clicked.connect(lambda _c=False: self.export_excel())
        for b in (self.gen_btn, self.clear_btn, self.final_btn, self.export_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        self.sched_status = QLabel("")
        layout.addLayout(bar)
        layout.addWidget(self.sched_status)
        hint = QLabel(
            "提示：每一格可直接用下拉選單手動換人；在格子上按右鍵可「鎖定」，重新排班時鎖定的格子不會被更動。"
            "黃色＝已鎖定，紅色＝有問題。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.day_tabs = QTabWidget()
        self.stats_table = QTableWidget(0, 0)
        self.stats_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.issue_list = QListWidget()

        stats_box = QGroupBox("人員班次統計（各日與本次活動總計）")
        sb = QVBoxLayout(stats_box)
        sb.addWidget(self.stats_table)
        issue_box = QGroupBox("檢查結果")
        ib = QVBoxLayout(issue_box)
        ib.addWidget(self.issue_list)

        bottom = QSplitter(Qt.Orientation.Horizontal)
        bottom.addWidget(stats_box)
        bottom.addWidget(issue_box)
        bottom.setSizes([650, 350])

        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(self.day_tabs)
        vertical.addWidget(bottom)
        vertical.setSizes([450, 250])
        layout.addWidget(vertical, 1)
        return page

    # ==================================================================
    # 活動清單
    # ==================================================================
    @staticmethod
    def _format_summary(s: EventSummary) -> str:
        if s.first_date and s.last_date:
            rng = s.first_date if s.first_date == s.last_date else f"{s.first_date} ~ {s.last_date}"
        else:
            rng = "（尚未設定日期）"
        return f"{s.name}\n{rng}　｜　{STATUS_LABELS.get(s.status, s.status)}"

    def reload_events(self, select_id: Optional[int] = None) -> None:
        self.event_list.blockSignals(True)
        self.event_list.clear()
        target_row = -1
        for i, s in enumerate(self.repo.list_events()):
            item = QListWidgetItem(self._format_summary(s))
            item.setData(Qt.ItemDataRole.UserRole, s.id)
            self.event_list.addItem(item)
            if select_id is not None and s.id == select_id:
                target_row = i
        if target_row >= 0:
            self.event_list.setCurrentRow(target_row)
        self.event_list.blockSignals(False)

    def _on_event_item_changed(self, current, _previous) -> None:
        if current is None:
            return
        self.open_event(int(current.data(Qt.ItemDataRole.UserRole)))

    def _show_placeholder(self) -> None:
        self.stack.setCurrentIndex(0)

    def open_event(self, event_id: int) -> None:
        try:
            ev = self.repo.get_event(event_id)
        except KeyError:
            self.current_event_id = None
            self._show_placeholder()
            return
        self.current_event_id = event_id
        self._gen_warnings = []
        self._gen_notes = []
        self._load_event(ev)
        self.stack.setCurrentIndex(1)
        self.right_tabs.setTabEnabled(1, True)
        self._rebuild_schedule()
        # 已有班表就直接顯示班表，否則顯示設定
        has_schedule = ev.status != "draft"
        self.right_tabs.setCurrentIndex(1 if has_schedule else 0)

    def new_event(self) -> None:
        self.event_list.blockSignals(True)
        self.event_list.clearSelection()
        self.event_list.setCurrentRow(-1)
        self.event_list.blockSignals(False)
        self.current_event_id = None
        self._gen_warnings = []
        self._gen_notes = []
        self._load_event(None)
        self.stack.setCurrentIndex(1)
        self.right_tabs.setCurrentIndex(0)
        self.right_tabs.setTabEnabled(1, False)
        self._clear_schedule_widgets()
        self.name_edit.setFocus()

    def copy_event(self) -> None:
        item = self.event_list.currentItem()
        if item is None:
            info(self, "請先選擇", "請先在左側選擇要複製設定的活動")
            return
        src_id = int(item.data(Qt.ItemDataRole.UserRole))
        src = self.repo.get_event(src_id)
        dlg = CopyEventDialog(self, f"{src.name}（複製）")
        if not dlg.exec():
            return
        name, start_date = dlg.values()
        try:
            new_id = self.repo.duplicate_event(src_id, name, start_date)
        except ValueError as exc:
            warn(self, "無法複製", str(exc))
            return
        self.reload_events(select_id=new_id)
        self.open_event(new_id)

    def delete_event(self) -> None:
        item = self.event_list.currentItem()
        if item is None:
            info(self, "請先選擇", "請先在左側選擇要刪除的活動")
            return
        eid = int(item.data(Qt.ItemDataRole.UserRole))
        ev = self.repo.get_event(eid)
        if not confirm(self, "確認刪除", f"確定要刪除活動「{ev.name}」與它的班表嗎？此動作無法復原。"):
            return
        self.repo.delete_event(eid)
        self.current_event_id = None
        self._clear_schedule_widgets()
        self.reload_events()
        self._show_placeholder()

    def import_event(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "選擇班表 Excel", "", "Excel 檔案 (*.xlsx)")
        if not path:
            return
        try:
            imp = excel_io.read_event_workbook(path, default_name=Path(path).stem)
            new_id, warnings = self.repo.create_event_from_import(imp)
        except Exception as exc:  # noqa: BLE001
            error(self, "匯入失敗", f"無法匯入：{exc}")
            return
        self.reload_events(select_id=new_id)
        self.open_event(new_id)
        msg = "已匯入並建立新的活動專案。"
        if warnings:
            msg += "\n\n提醒：\n" + "\n".join(warnings[:15])
        info(self, "匯入完成", msg)

    # ==================================================================
    # 設定表單：載入與收集
    # ==================================================================
    def reload_catalogs(self) -> None:
        """人員或崗位在其他頁籤被修改後，更新表單中的選項（保留目前勾選）。"""
        sel_pos, sel_staff = self._capture_selection()
        self._fill_positions(sel_pos)
        self._fill_staff(sel_staff)
        if self.current_event_id is not None:
            self._rebuild_schedule()

    def _capture_selection(self) -> Tuple[Dict[int, EventPosition], set]:
        sel_pos: Dict[int, EventPosition] = {}
        for r in range(self.pos_table.rowCount()):
            item = self.pos_table.item(r, 1)
            if item is None or self.pos_table.item(r, 0).checkState() != Qt.CheckState.Checked:
                continue
            pid = int(item.data(Qt.ItemDataRole.UserRole))
            spin: QSpinBox = self.pos_table.cellWidget(r, 2)
            combo: QComboBox = self.pos_table.cellWidget(r, 3)
            sel_pos[pid] = EventPosition(pid, spin.value(), combo.currentData())
        sel_staff = set()
        for i in range(self.staff_list.count()):
            it = self.staff_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                sel_staff.add(int(it.data(Qt.ItemDataRole.UserRole)))
        return sel_pos, sel_staff

    def _fill_positions(self, selected: Dict[int, EventPosition]) -> None:
        positions = self.repo.list_positions()
        known = {p.id for p in positions}
        for p in self.repo.list_positions(include_inactive=True):
            if p.id in selected and p.id not in known:
                positions.append(p)  # 已停用但本活動仍在使用的崗位
        self.pos_table.setRowCount(len(positions))
        for r, p in enumerate(positions):
            use = QTableWidgetItem("")
            use.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            use.setCheckState(Qt.CheckState.Checked if p.id in selected else Qt.CheckState.Unchecked)
            self.pos_table.setItem(r, 0, use)
            label = p.name + ("（已停用）" if not p.is_active else "")
            name = QTableWidgetItem(label)
            name.setFlags(Qt.ItemFlag.ItemIsEnabled)
            name.setData(Qt.ItemDataRole.UserRole, p.id)
            self.pos_table.setItem(r, 1, name)
            spin = QSpinBox()
            spin.setRange(1, 20)
            spin.setValue(selected[p.id].headcount if p.id in selected else 1)
            self.pos_table.setCellWidget(r, 2, spin)
            combo = QComboBox()
            for text, value in _DED_CHOICES:
                combo.addItem(text, value)
            ded = selected[p.id].is_dedicated if p.id in selected else None
            for idx, (_t, value) in enumerate(_DED_CHOICES):
                if value == ded:
                    combo.setCurrentIndex(idx)
            self.pos_table.setCellWidget(r, 3, combo)

    def _fill_staff(self, selected: set) -> None:
        names = {p.id: p.name for p in self.repo.list_positions(include_inactive=True)}
        staff = self.repo.list_staff()
        known = {s.id for s in staff}
        for s in self.repo.list_staff(include_inactive=True):
            if s.id in selected and s.id not in known:
                staff.append(s)
        self.staff_list.clear()
        for s in staff:
            pos_text = "、".join(names.get(pid, "?") for pid in s.position_ids) or "（未設定崗位）"
            suffix = "（已停用）" if not s.is_active else ""
            item = QListWidgetItem(f"{s.name}{suffix}　［{pos_text}］")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if s.id in selected else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, s.id)
            self.staff_list.addItem(item)

    def _set_all_staff(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.staff_list.count()):
            self.staff_list.item(i).setCheckState(state)

    def _load_event(self, ev: Optional[Event]) -> None:
        if ev is None:
            self._status = "draft"
            self.name_edit.setText("")
            self.note_edit.setText("")
            self.shift_spin.setValue(90)
            self.avoid_check.setChecked(True)
            days = [EventDay(QDate.currentDate().toString("yyyy-MM-dd"), "09:00", "18:00")]
            sel_pos: Dict[int, EventPosition] = {}
            sel_staff: set = set()
            status_text = "尚未儲存"
        else:
            self._status = ev.status
            self.name_edit.setText(ev.name)
            self.note_edit.setText(ev.note)
            self.shift_spin.setValue(ev.shift_minutes)
            self.avoid_check.setChecked(ev.avoid_consecutive)
            days = ev.days
            sel_pos = {p.position_id: p for p in ev.positions}
            sel_staff = set(ev.staff_ids)
            status_text = STATUS_LABELS.get(ev.status, ev.status)
            if ev.copied_from:
                status_text += "（由其他活動複製而來）"

        self.status_label.setText(status_text)
        self.days_table.setRowCount(0)
        for d in days:
            self._add_day_row(d.work_date, d.start_time, d.end_time)
        self.breaks_table.setRowCount(0)
        has_breaks = any(d.breaks for d in days)
        self.break_check.blockSignals(True)
        self.break_check.setChecked(has_breaks)
        self.break_check.blockSignals(False)
        self.breaks_box.setEnabled(has_breaks)
        for idx, d in enumerate(days):
            for b in d.breaks:
                self._add_break_row(idx, b.label, b.start_time, b.end_time)
        self._fill_positions(sel_pos)
        self._fill_staff(sel_staff)
        self._apply_final_state()

    def _apply_final_state(self) -> None:
        final = self._status == STATUS_FINAL
        for w in self.form_widgets:
            w.setEnabled(not final)
        if not final:
            self.breaks_box.setEnabled(self.break_check.isChecked())
        self.save_btn.setEnabled(not final)
        self.save_gen_btn.setEnabled(not final)
        self.gen_btn.setEnabled(not final)
        self.clear_btn.setEnabled(not final)
        self.final_btn.setText("解除定稿" if final else "定稿")
        if final:
            self.status_label.setText("已定稿（唯讀）。如需修改請到「班表」頁按「解除定稿」。")

    def _collect_event(self) -> Event:
        name = self.name_edit.text().strip()
        if not name:
            raise ValueError("請輸入活動名稱")
        days: List[EventDay] = []
        for r in range(self.days_table.rowCount()):
            de: QDateEdit = self.days_table.cellWidget(r, 0)
            ts: QTimeEdit = self.days_table.cellWidget(r, 1)
            te: QTimeEdit = self.days_table.cellWidget(r, 2)
            days.append(
                EventDay(
                    de.date().toString("yyyy-MM-dd"),
                    ts.time().toString("HH:mm"),
                    te.time().toString("HH:mm"),
                )
            )
        if self.break_check.isChecked():
            for r in range(self.breaks_table.rowCount()):
                combo: QComboBox = self.breaks_table.cellWidget(r, 0)
                label_edit: QLineEdit = self.breaks_table.cellWidget(r, 1)
                bs: QTimeEdit = self.breaks_table.cellWidget(r, 2)
                be: QTimeEdit = self.breaks_table.cellWidget(r, 3)
                idx = combo.currentIndex()
                if 0 <= idx < len(days):
                    days[idx].breaks.append(
                        EventBreak(
                            label_edit.text().strip() or "休息",
                            bs.time().toString("HH:mm"),
                            be.time().toString("HH:mm"),
                        )
                    )
        positions: List[EventPosition] = []
        for r in range(self.pos_table.rowCount()):
            if self.pos_table.item(r, 0).checkState() != Qt.CheckState.Checked:
                continue
            pid = int(self.pos_table.item(r, 1).data(Qt.ItemDataRole.UserRole))
            spin: QSpinBox = self.pos_table.cellWidget(r, 2)
            combo2: QComboBox = self.pos_table.cellWidget(r, 3)
            positions.append(EventPosition(pid, spin.value(), combo2.currentData()))
        staff_ids: List[int] = []
        for i in range(self.staff_list.count()):
            it = self.staff_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                staff_ids.append(int(it.data(Qt.ItemDataRole.UserRole)))
        return Event(
            id=self.current_event_id,
            name=name,
            shift_minutes=self.shift_spin.value(),
            merge_threshold=0.5,
            avoid_consecutive=self.avoid_check.isChecked(),
            note=self.note_edit.text().strip(),
            status=self._status,
            days=days,
            positions=positions,
            staff_ids=staff_ids,
        )

    # ------------------------------------------------------------------
    # 工作日與休息時段的表格操作
    # ------------------------------------------------------------------
    def _add_day_row(self, iso: str, start: str = "09:00", end: str = "18:00") -> None:
        r = self.days_table.rowCount()
        self.days_table.insertRow(r)
        de = QDateEdit(QDate.fromString(iso, "yyyy-MM-dd"))
        de.setCalendarPopup(True)
        de.setDisplayFormat("yyyy-MM-dd")
        de.dateChanged.connect(lambda _d: self._refresh_break_day_choices())
        ts = QTimeEdit(QTime.fromString(start, "HH:mm"))
        ts.setDisplayFormat("HH:mm")
        te = QTimeEdit(QTime.fromString(end, "HH:mm"))
        te.setDisplayFormat("HH:mm")
        self.days_table.setCellWidget(r, 0, de)
        self.days_table.setCellWidget(r, 1, ts)
        self.days_table.setCellWidget(r, 2, te)
        self._refresh_break_day_choices()

    def _add_next_day(self) -> None:
        n = self.days_table.rowCount()
        if n:
            last: QDateEdit = self.days_table.cellWidget(n - 1, 0)
            ts: QTimeEdit = self.days_table.cellWidget(n - 1, 1)
            te: QTimeEdit = self.days_table.cellWidget(n - 1, 2)
            iso = last.date().addDays(1).toString("yyyy-MM-dd")
            self._add_day_row(iso, ts.time().toString("HH:mm"), te.time().toString("HH:mm"))
        else:
            self._add_day_row(QDate.currentDate().toString("yyyy-MM-dd"))

    def _remove_selected_day(self) -> None:
        r = self.days_table.currentRow()
        if r < 0:
            info(self, "請先選擇", "請先點選要刪除的那一天")
            return
        # 移除該天的休息，並調整其後各天的索引
        keep: List[Tuple[int, int]] = []
        for br in range(self.breaks_table.rowCount() - 1, -1, -1):
            combo: QComboBox = self.breaks_table.cellWidget(br, 0)
            idx = combo.currentIndex()
            if idx == r:
                self.breaks_table.removeRow(br)
            elif idx > r:
                keep.append((br, idx - 1))
        self.days_table.removeRow(r)
        self._refresh_break_day_choices()
        for br, new_idx in keep:
            combo = self.breaks_table.cellWidget(br, 0)
            combo.setCurrentIndex(new_idx)

    def _on_break_toggled(self, checked: bool) -> None:
        self.breaks_box.setEnabled(checked)
        if checked and self.breaks_table.rowCount() == 0:
            self._add_breaks_for_all_days()

    def _fill_day_combo(self, combo: QComboBox) -> None:
        for i in range(self.days_table.rowCount()):
            de: QDateEdit = self.days_table.cellWidget(i, 0)
            combo.addItem(f"第{i + 1}天 {de.date().toString('MM/dd')}")

    def _refresh_break_day_choices(self) -> None:
        for r in range(self.breaks_table.rowCount()):
            combo: QComboBox = self.breaks_table.cellWidget(r, 0)
            idx = combo.currentIndex()
            combo.blockSignals(True)
            combo.clear()
            self._fill_day_combo(combo)
            combo.setCurrentIndex(max(0, min(idx, combo.count() - 1)))
            combo.blockSignals(False)

    def _add_break_row(self, day_idx: int, label: str = "午餐", start: str = "12:00", end: str = "13:00") -> None:
        r = self.breaks_table.rowCount()
        self.breaks_table.insertRow(r)
        combo = QComboBox()
        self._fill_day_combo(combo)
        combo.setCurrentIndex(max(0, min(day_idx, combo.count() - 1)))
        label_edit = QLineEdit(label)
        bs = QTimeEdit(QTime.fromString(start, "HH:mm"))
        bs.setDisplayFormat("HH:mm")
        be = QTimeEdit(QTime.fromString(end, "HH:mm"))
        be.setDisplayFormat("HH:mm")
        self.breaks_table.setCellWidget(r, 0, combo)
        self.breaks_table.setCellWidget(r, 1, label_edit)
        self.breaks_table.setCellWidget(r, 2, bs)
        self.breaks_table.setCellWidget(r, 3, be)

    def _add_breaks_for_all_days(self) -> None:
        for idx in range(self.days_table.rowCount()):
            self._add_break_row(idx)

    def _remove_selected_break(self) -> None:
        r = self.breaks_table.currentRow()
        if r >= 0:
            self.breaks_table.removeRow(r)

    # ==================================================================
    # 儲存設定
    # ==================================================================
    def save_settings(self) -> bool:
        try:
            ev = self._collect_event()
        except ValueError as exc:
            warn(self, "設定有誤", str(exc))
            return False
        try:
            layout_changed, has_assign = self.repo.preview_save(ev)
            if layout_changed and has_assign:
                if not confirm(
                    self,
                    "將清除現有班表",
                    "修改日期、工作時間、班次時長或休息時間後，班次會重新切分，目前的班表會被清除。\n要繼續嗎？",
                ):
                    return False
            info_obj = self.repo.save_event(ev)
        except ValueError as exc:
            warn(self, "無法儲存", str(exc))
            return False
        self.current_event_id = info_obj.event_id
        self.reload_events(select_id=info_obj.event_id)
        ev2 = self.repo.get_event(info_obj.event_id)
        self._load_event(ev2)
        self.right_tabs.setTabEnabled(1, True)
        self._rebuild_schedule()
        if info_obj.pruned_assignments:
            info(
                self,
                "已更新",
                f"因為崗位或出席人員有調整，已移除 {info_obj.pruned_assignments} 筆不再有效的排班；"
                "請到「班表」頁確認並補齊（或按「重新排班」）。",
            )
        return True

    # ==================================================================
    # 排班
    # ==================================================================
    def generate(self) -> None:
        if self._worker is not None:
            return
        if self._status == STATUS_FINAL:
            warn(self, "已定稿", "請先「解除定稿」才能重新排班")
            return
        if not self.save_settings():
            return
        eid = self.current_event_id
        if eid is None:
            return
        try:
            ctx = self.repo.build_problem(eid)
        except Exception as exc:  # noqa: BLE001
            error(self, "無法排班", str(exc))
            return
        self._ctx = ctx
        self._set_busy(True)
        self._worker = SolveWorker(ctx.problem, engine.SolveOptions(time_limit=6.0), self)
        self._worker.done.connect(self._on_solved)
        self._worker.start()

    def _set_busy(self, busy: bool) -> None:
        for b in (self.gen_btn, self.save_gen_btn, self.save_btn, self.clear_btn, self.final_btn, self.export_btn):
            b.setEnabled(not busy)
        if busy:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self.sched_status.setText("排班計算中，請稍候…")
        else:
            QApplication.restoreOverrideCursor()
            self._apply_final_state()
            self.save_btn.setEnabled(self._status != STATUS_FINAL)
            self.save_gen_btn.setEnabled(self._status != STATUS_FINAL)
            self.export_btn.setEnabled(True)

    def _on_solved(self, result: engine.SolveResult) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.wait()
            worker.deleteLater()
        self._set_busy(False)
        eid = self.current_event_id
        if eid is None:
            return
        if not result.ok:
            self._gen_warnings = []
            self._gen_notes = []
            self.right_tabs.setCurrentIndex(1)
            self._refresh_analysis()
            error(self, "無法完成排班", "\n".join(result.errors) or "未知原因")
            return
        try:
            self.repo.apply_generation(eid, self._ctx, result)
        except ValueError as exc:
            warn(self, "無法寫入班表", str(exc))
            return
        self._gen_warnings = list(result.warnings)
        self._gen_notes = list(result.notes)
        self.reload_events(select_id=eid)
        self._status = self.repo.get_event(eid).status
        self.status_label.setText(STATUS_LABELS.get(self._status, self._status))
        self._rebuild_schedule()
        self.right_tabs.setCurrentIndex(1)

    def clear_schedule(self) -> None:
        eid = self.current_event_id
        if eid is None:
            return
        if not confirm(self, "清除班表", "要清除這個活動目前的所有排班（包含已鎖定的格子）嗎？"):
            return
        try:
            self.repo.clear_assignments(eid)
        except ValueError as exc:
            warn(self, "無法清除", str(exc))
            return
        self._gen_warnings = []
        self._gen_notes = []
        self.reload_events(select_id=eid)
        self._status = self.repo.get_event(eid).status
        self.status_label.setText(STATUS_LABELS.get(self._status, self._status))
        self._rebuild_schedule()

    def toggle_final(self) -> None:
        eid = self.current_event_id
        if eid is None:
            return
        try:
            if self._status == STATUS_FINAL:
                if not confirm(self, "解除定稿", "解除定稿後可再次修改設定與班表。確定嗎？"):
                    return
                self.repo.unfinalize_event(eid)
            else:
                if not confirm(
                    self,
                    "定稿",
                    "定稿會保存目前班表的快照（之後人員或崗位改名也不影響這份紀錄），並鎖定不可修改。\n確定要定稿嗎？",
                ):
                    return
                self.repo.finalize_event(eid)
        except ValueError as exc:
            warn(self, "無法完成", str(exc))
            return
        self.reload_events(select_id=eid)
        self.open_event(eid)
        self.right_tabs.setCurrentIndex(1)

    def export_excel(self) -> None:
        eid = self.current_event_id
        if eid is None:
            return
        ev = self.repo.get_event(eid)
        if ev.status != STATUS_FINAL:
            _ctx, an, _a = self.repo.analyze_event(eid)
            if an.errors and not confirm(
                self,
                "班表尚有問題",
                f"目前班表有 {len(an.errors)} 個問題（例如空格或資格不符），仍要匯出嗎？",
            ):
                return
        default = f"{safe_filename(ev.name)}.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "匯出班表", default, "Excel 檔案 (*.xlsx)")
        if not path:
            return
        try:
            view = self.repo.build_view(eid)
            excel_io.export_schedule(view, path)
        except PermissionError:
            error(self, "匯出失敗", "無法寫入檔案，請確認該檔案沒有在 Excel 中開啟。")
            return
        except Exception as exc:  # noqa: BLE001
            error(self, "匯出失敗", str(exc))
            return
        info(self, "匯出完成", f"已匯出到：\n{path}")

    # ==================================================================
    # 班表顯示
    # ==================================================================
    def _clear_schedule_widgets(self) -> None:
        self._cells = {}
        self.day_tabs.clear()
        self.stats_table.setRowCount(0)
        self.stats_table.setColumnCount(0)
        self.issue_list.clear()
        self.sched_status.setText("")

    def _rebuild_schedule(self) -> None:
        eid = self.current_event_id
        keep_tab = self.day_tabs.currentIndex()
        self._clear_schedule_widgets()
        if eid is None:
            return
        ev = self.repo.get_event(eid)
        self._status = ev.status
        read_only = ev.status == STATUS_FINAL

        # 崗位對照與各崗位可選人員
        self._pos_id_by_name = {}
        self._qual_staff = {}
        for ep in ev.positions:
            pos = self.repo.get_position(ep.position_id)
            if pos is not None:
                self._pos_id_by_name[pos.name] = pos.id
                self._qual_staff[pos.id] = []
        for sid in ev.staff_ids:
            st = self.repo.get_staff(sid)
            if st is None:
                continue
            for pid in st.position_ids:
                if pid in self._qual_staff:
                    self._qual_staff[pid].append((st.id, st.name))

        view = self.repo.build_view(eid)
        self._view = view
        if not view.positions:
            self.sched_status.setText("尚未選擇本次所需崗位，請先到「活動設定」。")
            return
        for day in view.days:
            self.day_tabs.addTab(self._build_day_table(view, day, read_only), date_label(day.date))
        if 0 <= keep_tab < self.day_tabs.count():
            self.day_tabs.setCurrentIndex(keep_tab)
        self._refresh_analysis()

    def _build_day_table(self, view: ScheduleView, day: DayView, read_only: bool) -> QTableWidget:
        total = sum(hc for _n, hc in view.positions)
        table = QTableWidget(len(day.rows), 1 + total)
        headers = ["時段"]
        for name, hc in view.positions:
            for k in range(hc):
                headers.append(name if hc == 1 else f"{name} {k + 1}")
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        grey = QBrush(QColor("#D9D9D9"))

        for r, row in enumerate(day.rows):
            time_item = QTableWidgetItem(row.time_text)
            time_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(r, 0, time_item)
            if row.kind == "break":
                time_item.setBackground(grey)
                label = QTableWidgetItem(row.label or "休息")
                label.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setBackground(grey)
                table.setItem(r, 1, label)
                if total > 1:
                    table.setSpan(r, 1, 1, total)
                continue
            col = 1
            for name, hc in view.positions:
                for k in range(hc):
                    names = row.cells.get(name, [""] * hc)
                    ids = row.staff_ids.get(name, [None] * hc)
                    text = names[k] if k < len(names) else ""
                    cur_id = ids[k] if k < len(ids) else None
                    if read_only or row.shift_id is None:
                        item = QTableWidgetItem(text)
                        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                        table.setItem(r, col, item)
                    else:
                        pos_id = self._pos_id_by_name.get(name)
                        combo = self._make_cell_combo(pos_id, cur_id, text)
                        key = (row.shift_id, pos_id, k)
                        combo.activated.connect(lambda _i, c=combo, kk=key: self._on_cell_changed(c, kk))
                        combo.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                        combo.customContextMenuRequested.connect(
                            lambda pt, c=combo, kk=key: self._cell_menu(c, kk, pt)
                        )
                        table.setCellWidget(r, col, combo)
                        self._cells[key] = combo
                    col += 1
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for c in range(1, 1 + total):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        table.resizeRowsToContents()
        return table

    def _make_cell_combo(self, pos_id: Optional[int], cur_id: Optional[int], cur_name: str) -> QComboBox:
        combo = QComboBox()
        combo.addItem("", None)
        cands = self._qual_staff.get(pos_id, []) if pos_id is not None else []
        ids = set()
        for sid, nm in cands:
            combo.addItem(nm, sid)
            ids.add(sid)
        if cur_id is not None and cur_id not in ids:
            combo.addItem(f"⚠ {cur_name}", cur_id)  # 不具備資格的人員（例如匯入的資料）
        if cur_id is not None:
            idx = combo.findData(cur_id)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
        else:
            combo.setCurrentIndex(0)
        return combo

    def _on_cell_changed(self, combo: QComboBox, key: Tuple[int, Optional[int], int]) -> None:
        eid = self.current_event_id
        shift_id, pos_id, k = key
        if eid is None or pos_id is None:
            return
        staff_id = combo.currentData()
        try:
            self.repo.set_assignment(eid, shift_id, pos_id, k, staff_id)
        except ValueError as exc:
            warn(self, "無法修改", str(exc))
            self._rebuild_schedule()
            return
        self._gen_warnings = []  # 手動修改後，舊的排班提示不再適用
        self.reload_events(select_id=eid)
        self._status = self.repo.get_event(eid).status
        self._refresh_analysis()

    def _cell_menu(self, combo: QComboBox, key: Tuple[int, Optional[int], int], pt) -> None:
        eid = self.current_event_id
        shift_id, pos_id, k = key
        if eid is None or pos_id is None:
            return
        assigns = self.repo.get_assignments(eid)
        rec = assigns.get((shift_id, pos_id, k))
        menu = QMenu(combo)
        if rec is None:
            act = menu.addAction("（先選擇人員才能鎖定）")
            act.setEnabled(False)
        else:
            act = menu.addAction("解除鎖定" if rec[1] else "鎖定此格（重新排班時保留）")
        chosen = menu.exec(combo.mapToGlobal(pt))
        if chosen is act and rec is not None:
            try:
                self.repo.toggle_lock(eid, shift_id, pos_id, k)
            except ValueError as exc:
                warn(self, "無法鎖定", str(exc))
                return
            self._refresh_analysis()

    # ------------------------------------------------------------------
    def _refresh_analysis(self) -> None:
        eid = self.current_event_id
        if eid is None:
            return
        ctx, an, _assign = self.repo.analyze_event(eid)
        assigns = self.repo.get_assignments(eid)
        has_any = bool(assigns)
        bad = {(ctx.shift_ids[si], pid, k) for (si, pid, k) in an.bad_keys} if has_any else set()
        for key, combo in self._cells.items():
            locked = assigns.get(key, (None, False))[1]
            if key in bad:
                combo.setStyleSheet(_STYLE_BAD)
            elif locked:
                combo.setStyleSheet(_STYLE_LOCKED)
            else:
                combo.setStyleSheet("")

        self._view = self.repo.build_view(eid)
        self._fill_stats(self._view)

        self.issue_list.clear()
        if not has_any:
            self.issue_list.addItem("尚未排班。請按「產生 / 重新排班」。")
        else:
            for text in an.errors:
                it = QListWidgetItem("✖ " + text)
                it.setForeground(QBrush(QColor("#C00000")))
                self.issue_list.addItem(it)
            for text in self._gen_warnings + an.warnings:
                it = QListWidgetItem("⚠ " + text)
                it.setForeground(QBrush(QColor("#B26B00")))
                self.issue_list.addItem(it)
            for text in self._gen_notes:
                self.issue_list.addItem("ℹ " + text)
            if not an.errors and not an.warnings and not self._gen_warnings:
                self.issue_list.insertItem(0, "✔ 所有班次與崗位都已排滿，且人員皆符合適用崗位。")

        ev = self.repo.get_event(eid)
        n_err = len(an.errors) if has_any else 0
        n_warn = len(set(self._gen_warnings + an.warnings)) if has_any else 0
        self.sched_status.setText(
            f"狀態：{STATUS_LABELS.get(ev.status, ev.status)}　｜　{n_err} 個問題、{n_warn} 個提醒"
        )

    def _fill_stats(self, view: ScheduleView) -> None:
        names, dates, table = view.stats()
        t = self.stats_table
        t.clear()
        t.setColumnCount(len(names))
        t.setRowCount(len(dates) + 1)
        t.setHorizontalHeaderLabels(names)
        t.setVerticalHeaderLabels([date_label(d) for d in dates] + ["總計"])
        bold = QFont()
        bold.setBold(True)
        for c, n in enumerate(names):
            for r, d in enumerate(dates):
                item = QTableWidgetItem(str(table[n][d]))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                t.setItem(r, c, item)
            total = QTableWidgetItem(str(sum(table[n].values())))
            total.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            total.setFont(bold)
            t.setItem(len(dates), c, total)
        t.resizeColumnsToContents()

    # ==================================================================
    def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.wait(10000)
