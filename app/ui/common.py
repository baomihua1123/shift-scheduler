"""介面共用小工具。"""
from __future__ import annotations

import re

from PySide6.QtWidgets import QMessageBox, QWidget

_YES = QMessageBox.StandardButton.Yes
_NO = QMessageBox.StandardButton.No


def info(parent: QWidget, title: str, text: str) -> None:
    QMessageBox.information(parent, title, text)


def warn(parent: QWidget, title: str, text: str) -> None:
    QMessageBox.warning(parent, title, text)


def error(parent: QWidget, title: str, text: str) -> None:
    QMessageBox.critical(parent, title, text)


def confirm(parent: QWidget, title: str, text: str) -> bool:
    return QMessageBox.question(parent, title, text, _YES | _NO, _NO) == _YES


def safe_filename(name: str, default: str = "班表") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return cleaned or default
