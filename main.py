"""短期活動排班系統 — 程式進入點。

`--selftest`：用暫存資料庫啟動後自動關閉（供打包後驗證 exe 是否完整）。
"""
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from app.db import DatabaseTooNewError, default_db_path
from app.repo import Repository
from app.ui.main_window import MainWindow


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    selftest = "--selftest" in argv
    app = QApplication(argv)
    app.setApplicationName("短期活動排班系統")
    app.setFont(QFont("Microsoft JhengHei UI", 10))

    db_path = Path(tempfile.mkdtemp()) / "selftest.db" if selftest else default_db_path()
    try:
        repo = Repository.open(db_path)
    except DatabaseTooNewError as exc:
        QMessageBox.critical(None, "無法開啟資料", str(exc))
        return 1

    window = MainWindow(repo, db_path)
    window.resize(1320, 820)
    window.show()
    if selftest:
        QTimer.singleShot(800, window.close)
    code = app.exec()
    repo.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
