"""
C 盘大文件扫描：查找与软件/系统无关的大文件（用户文档、下载、桌面等）。
排除 Windows、Program Files、ProgramData 等系统与程序目录。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from dataclasses import dataclass

# 仅 Windows
if sys.platform != "win32":
    def scan_large_files(min_mb: float = 50.0, max_count: int = 300) -> list[dict]:
        return []

else:
    @dataclass
    class LargeFileItem:
        path: str
        size: int
        category: str = "large"

        def to_dict(self) -> dict:
            return {"path": self.path, "size": self.size, "category": self.category}

    # C 盘下需排除的系统/程序路径前缀（小写）
    _EXCLUDE_PREFIXES = (
        "c:\\windows",
        "c:\\program files",
        "c:\\program files (x86)",
        "c:\\programdata",
        "c:\\users",  # 用户目录由 _get_user_large_roots 按文档/下载等单独扫
        "c:\\$recycle.bin",
        "c:\\system volume information",
        "c:\\recovery",
        "c:\\config.msi",
    )

    def _is_excluded(path: str) -> bool:
        p = path.lower().replace("/", "\\")
        for prefix in _EXCLUDE_PREFIXES:
            if p == prefix or p.startswith(prefix + "\\"):
                return True
        return False

    def _get_user_large_roots() -> list[Path]:
        """用户目录：文档、下载、桌面、视频、音乐、图片等（与软件/系统无关）。"""
        roots = []
        user_profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        base = Path(user_profile)
        names = ("Documents", "Downloads", "Desktop", "Videos", "Music", "Pictures", "OneDrive", "Favorites")
        for name in names:
            d = base / name
            if d.exists() and d.is_dir():
                roots.append(d)
        return roots

    def _get_c_drive_roots() -> list[Path]:
        """C 盘根下仅一层，排除系统目录。"""
        roots = []
        c = Path("C:\\")
        try:
            for entry in os.scandir(c):
                if not entry.is_dir():
                    continue
                path_abs = str(Path(entry.path).resolve())
                if _is_excluded(path_abs):
                    continue
                roots.append(Path(entry.path))
        except (OSError, PermissionError):
            pass
        return roots

    def _collect_large(
        root: Path,
        min_bytes: int,
        max_count: int,
        collected: list[tuple[str, int]],
    ) -> None:
        """递归收集 >= min_bytes 的文件，最多 max_count 个（按大小降序需后续排序）。"""
        if len(collected) >= max_count:
            return
        try:
            for entry in os.scandir(root):
                if len(collected) >= max_count:
                    return
                try:
                    path_abs = str(Path(entry.path).resolve())
                    if _is_excluded(path_abs):
                        continue
                    if entry.is_file():
                        size = entry.stat().st_size
                        if size >= min_bytes:
                            collected.append((path_abs, size))
                    elif entry.is_dir():
                        _collect_large(Path(entry.path), min_bytes, max_count, collected)
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            pass

    def scan_large_files(min_mb: float = 50.0, max_count: int = 300) -> list[dict]:
        """
        扫描 C 盘中与软件/系统无关的大文件。
        范围：用户文档/下载/桌面/视频/音乐/图片、C 盘根下非系统目录。
        仅列出 >= min_mb MB 的文件，最多 max_count 个，按大小降序。
        """
        min_bytes = int(min_mb * 1024 * 1024)
        collected: list[tuple[str, int]] = []

        for root in _get_user_large_roots():
            _collect_large(root, min_bytes, max_count, collected)
        for root in _get_c_drive_roots():
            _collect_large(root, min_bytes, max_count, collected)

        collected.sort(key=lambda x: -x[1])
        result = collected[:max_count]
        return [LargeFileItem(path=p, size=s, category="large").to_dict() for p, s in result]
