"""
C 盘空文件夹扫描：仅在可安全清理的目录下查找递归空文件夹，不影响系统和软件运行。
"""
from __future__ import annotations

import os
from pathlib import Path

# 复用 scanner 的扫描根目录（仅 Temp、缓存、日志等安全区域）
def _get_scan_roots():
    try:
        from backend.scanner import get_scan_roots_windows
        return get_scan_roots_windows()
    except Exception:
        return []


def _normalize(path: str) -> str:
    """统一小写，便于与 main 中的 _normalize_path_for_check 一致。"""
    s = path.lower()
    if os.name == "nt" and s.startswith("\\\\?\\"):
        s = s[4:]
    return s


def _collect_empty_dirs(
    root: Path,
    max_count: int,
    result: list[str],
    result_set: set[str],
) -> None:
    """后序遍历收集递归空目录（无文件、子目录均为空），结果按深度从深到浅。"""
    if len(result) >= max_count:
        return
    has_file = False
    direct_subdirs: list[Path] = []
    try:
        for entry in os.scandir(root):
            try:
                if entry.is_file() or entry.is_symlink():
                    has_file = True
                elif entry.is_dir():
                    direct_subdirs.append(Path(entry.path).resolve())
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError):
        return
    for sub in direct_subdirs:
        if len(result) >= max_count:
            return
        _collect_empty_dirs(sub, max_count, result, result_set)
    if not has_file:
        if not direct_subdirs:
            path_str = str(root.resolve())
            path_norm = _normalize(path_str)
            if path_norm not in result_set:
                result.append(path_str)
                result_set.add(path_norm)
        else:
            if all(_normalize(str(p)) in result_set for p in direct_subdirs):
                path_str = str(root.resolve())
                path_norm = _normalize(path_str)
                if path_norm not in result_set:
                    result.append(path_str)
                    result_set.add(path_norm)


def scan_empty_folders(max_total: int = 3000) -> list[str]:
    """
    在安全根目录下扫描递归空文件夹，返回路径列表（深到浅，便于删除时先删子再删父）。
    仅限 Temp、缓存、日志等目录，不影响系统和软件运行。
    """
    roots = _get_scan_roots()
    result: list[str] = []
    result_set: set[str] = set()
    per_root = max(500, max_total // max(len(roots), 1))
    for root_path, _ in roots:
        if not root_path.exists() or not root_path.is_dir():
            continue
        _collect_empty_dirs(
            root_path.resolve(),
            per_root,
            result,
            result_set,
        )
        if len(result) >= max_total:
            break
    return result[:max_total]
