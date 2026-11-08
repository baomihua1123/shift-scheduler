"""自動備份：每天最多一份，只保留最近幾天。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from .repo import Repository

PREFIX = "auto_"


def auto_backup(repo: Repository, db_path, keep: int = 7, now: Optional[datetime] = None) -> Optional[Path]:
    """備份到資料庫旁的 backups 資料夾；同一天重複備份會覆蓋當天那一份。回傳備份檔路徑。"""
    now = now or datetime.now()
    folder = Path(db_path).resolve().parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{PREFIX}{now.strftime('%Y%m%d')}.db"
    repo.backup_to(target)
    olds = sorted(folder.glob(f"{PREFIX}*.db"), reverse=True)
    for old in olds[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
    return target
