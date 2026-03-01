"""
C 盘可清理文件扫描器（与 disk-cleaner 技能逻辑一致）
扫描临时文件、缓存、日志等类别，返回按类别和大小排序的文件列表。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Iterator

# 保护的后缀，绝不删除
PROTECTED_EXTENSIONS = {
    ".exe", ".dll", ".sys", ".drv", ".bat", ".cmd", ".ps1",
    ".sh", ".bash", ".zsh", ".app", ".dmg", ".pkg", ".deb",
    ".rpm", ".msi", ".iso", ".vhd", ".vhdx",
}


@dataclass
class FileItem:
    path: str
    size: int
    category: str

    def to_dict(self) -> dict:
        return {"path": self.path, "size": self.size, "category": self.category}


def _size_mb(size: int) -> float:
    return round(size / (1024 * 1024), 2)


def _get_c_drive_root() -> Path:
    return Path("C:\\")


def _get_user_local() -> Path:
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        return Path(local)
    return Path(os.path.expanduser("~")) / "AppData" / "Local"


def _get_user_temp() -> Path:
    t = os.environ.get("TEMP") or os.environ.get("TMP")
    if t:
        return Path(t)
    return _get_user_local() / "Temp"


def _get_windows_temp() -> Path:
    return _get_c_drive_root() / "Windows" / "Temp"


def _is_safe_to_suggest(path: Path) -> bool:
    """仅建议可安全删除的文件（非可执行、非系统关键）。"""
    suf = path.suffix.lower()
    if suf in PROTECTED_EXTENSIONS:
        return False
    return True


def _scan_dir(
    root: Path,
    category: str,
    max_files: int = 5000,
    max_total_mb: float = 2000.0,
) -> Iterator[FileItem]:
    """递归扫描目录下的文件，返回 FileItem。"""
    count = 0
    total_mb = 0.0
    try:
        for entry in os.scandir(root):
            if count >= max_files or total_mb >= max_total_mb:
                break
            try:
                if entry.is_file():
                    if not _is_safe_to_suggest(Path(entry.path)):
                        continue
                    size = entry.stat().st_size
                    total_mb += _size_mb(size)
                    count += 1
                    yield FileItem(path=entry.path, size=size, category=category)
                elif entry.is_dir():
                    yield from _scan_dir(
                        Path(entry.path), category, max_files - count, max_total_mb - total_mb
                    )
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError):
        pass


def _scan_flat_dir(
    root: Path,
    category: str,
    max_files: int = 5000,
) -> list[FileItem]:
    """只扫描一层目录（用于 Temp 等）。"""
    items: list[FileItem] = []
    try:
        for entry in os.scandir(root):
            if len(items) >= max_files:
                break
            try:
                if entry.is_file() and _is_safe_to_suggest(Path(entry.path)):
                    size = entry.stat().st_size
                    items.append(FileItem(path=entry.path, size=size, category=category))
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError):
        pass
    return items


def get_scan_roots_windows() -> list[tuple[Path, str]]:
    """Windows C 盘下建议扫描的根目录及对应类别。"""
    local = _get_user_local()
    roots = [
        (_get_user_temp(), "temp"),
        (_get_windows_temp(), "temp"),
        (local / "Microsoft" / "Windows" / "INetCache", "cache"),
        (local / "Google" / "Chrome" / "User Data" / "Default" / "Cache", "cache"),
        (local / "Microsoft" / "Edge" / "User Data" / "Default" / "Cache", "cache"),
        (local / "Temp", "temp"),
    ]
    logs_dirs = [
        local / "Microsoft" / "Windows" / "WER" / "ReportQueue",
        local / "Microsoft" / "Windows" / "WER" / "ReportArchive",
    ]
    for d in logs_dirs:
        roots.append((d, "logs"))
    return [(r, c) for r, c in roots if r.exists()]


def scan_c_drive() -> dict:
    """
    扫描 C 盘可清理文件，按类别聚合，每类内按大小降序。
    返回格式: { "categories": { "temp": [...], "cache": [...], "logs": [...], "remnants": [...], "large": [...] }, "summary": {...} }
    """
    by_category: dict[str, list[FileItem]] = {"temp": [], "cache": [], "logs": [], "remnants": [], "large": [], "wechat_files": [], "wechat_cache": []}
    roots = get_scan_roots_windows()

    for root_path, category in roots:
        if category == "temp":
            items = _scan_flat_dir(root_path, category, max_files=3000)
        else:
            items = list(_scan_dir(root_path, category, max_files=2000, max_total_mb=500.0))
        by_category.setdefault(category, []).extend(items)

    try:
        from backend.remnants import scan_remnants
        for item in scan_remnants():
            by_category["remnants"].append(
                FileItem(path=item["path"], size=item["size"], category="remnants")
            )
    except Exception:
        pass

    try:
        from backend.large_files import scan_large_files
        for item in scan_large_files(min_mb=50.0, max_count=300):
            by_category["large"].append(
                FileItem(path=item["path"], size=item["size"], category="large")
            )
    except Exception:
        pass

    try:
        from backend.wechat_scan import scan_wechat
        for item in scan_wechat():
            cat = item["category"]
            by_category.setdefault(cat, []).append(
                FileItem(path=item["path"], size=item["size"], category=cat)
            )
    except Exception:
        pass

    seen = set()
    for cat in by_category:
        unique = []
        for it in by_category[cat]:
            if it.path not in seen:
                seen.add(it.path)
                unique.append(it)
        unique.sort(key=lambda x: -x.size)
        by_category[cat] = unique

    total_size = sum(f.size for items in by_category.values() for f in items)
    total_count = sum(len(items) for items in by_category.values())

    return {
        "categories": {
            cat: [it.to_dict() for it in items]
            for cat, items in by_category.items()
            if items
        },
        "summary": {
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "total_count": total_count,
        },
    }


if __name__ == "__main__":
    import json
    out = scan_c_drive()
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
