"""
已卸载软件残余扫描（与 disk-cleaner 思路一致：按类别扫描可清理项）
通过注册表已安装程序列表与 AppData/ProgramData 目录对比，找出可能残留的文件夹。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from dataclasses import dataclass

# 仅 Windows
if sys.platform != "win32":
    def scan_remnants() -> list[dict]:
        return []

else:
    import winreg

    @dataclass
    class RemnantItem:
        path: str
        size: int
        category: str = "remnants"

        def to_dict(self) -> dict:
            return {"path": self.path, "size": self.size, "category": self.category}

    def _normalize_name(s: str) -> str:
        """用于匹配的名称：小写、去空格和常见符号。"""
        if not s:
            return ""
        s = re.sub(r"[\s\-_\.]+", "", s.lower())
        return s[:50]

    def _get_installed_names() -> set[str]:
        """从注册表读取已安装程序名称/路径，生成可匹配的字符串集合。"""
        names = set()
        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hkey, subkey in keys:
            try:
                key_handle = winreg.OpenKey(hkey, subkey)
            except OSError:
                continue
            try:
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key_handle, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        sub = winreg.OpenKey(key_handle, subkey_name)
                        for name in ("DisplayName", "DisplayIcon", "InstallLocation", "InstallDir"):
                            try:
                                v, _ = winreg.QueryValueEx(sub, name)
                                if v and isinstance(v, str):
                                    v = v.strip()
                                    if v:
                                        names.add(_normalize_name(v))
                                        if name in ("InstallLocation", "InstallDir") and os.sep in v:
                                            names.add(_normalize_name(Path(v).name))
                            except OSError:
                                pass
                        if subkey_name and not subkey_name.startswith("{"):
                            names.add(_normalize_name(subkey_name))
                        winreg.CloseKey(sub)
                    except OSError:
                        continue
                winreg.CloseKey(key_handle)
            except Exception:
                try:
                    winreg.CloseKey(key_handle)
                except Exception:
                    pass
        return names

    def _dir_size(path: Path, limit_mb: float = 500.0) -> int:
        """递归计算目录大小（MB 上限避免过久）。"""
        total = 0
        limit_bytes = int(limit_mb * 1024 * 1024)
        try:
            for entry in os.scandir(path):
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                    elif entry.is_dir():
                        total += _dir_size(Path(entry.path), 0)
                    if total >= limit_bytes:
                        break
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            pass
        return total

    _SKIP_NAMES = {
        _normalize_name(x)
        for x in (
            "Microsoft", "Windows", "Google", "Mozilla", "Adobe", "Apple",
            "Package Management", "WindowsApps", "Programs", "Temp",
            "Application Data", "Desktop", "Documents", "Favorites",
            "Default", "Public", "Local", "Roaming", "LocalLow",
            "Microsoft Edge", "Chrome", "Firefox", "Edge",
            "Intel", "AMD", "NVIDIA", "Realtek", "Dell", "HP", "Lenovo",
        )
    }

    def _get_appdata_roots() -> list[Path]:
        roots = []
        local = os.environ.get("LOCALAPPDATA")
        roaming = os.environ.get("APPDATA")
        program_data = os.environ.get("PROGRAMDATA", "C:\\ProgramData")
        if local:
            roots.append(Path(local))
        if roaming:
            roots.append(Path(roaming))
        if program_data:
            roots.append(Path(program_data))
        return [r for r in roots if r.exists()]

    def _candidates_in(root: Path, depth: int) -> list[tuple[Path, str]]:
        out = []
        try:
            for entry in os.scandir(root):
                if not entry.is_dir():
                    continue
                p = Path(entry.path)
                name = entry.name
                if depth <= 1:
                    out.append((p, name))
                else:
                    for sub in p.iterdir():
                        if sub.is_dir():
                            out.append((sub, f"{name}{sub.name}"))
        except (OSError, PermissionError):
            pass
        return out

    def scan_remnants() -> list[dict]:
        """扫描可能为已卸载软件的残余文件夹。"""
        installed = _get_installed_names()
        installed.update(_SKIP_NAMES)
        results: list[RemnantItem] = []
        seen_paths: set[str] = set()

        for root in _get_appdata_roots():
            for folder_path, match_name in _candidates_in(root, 1):
                path_str = str(folder_path.resolve())
                if path_str in seen_paths:
                    continue
                norm = _normalize_name(match_name)
                if not norm or norm in installed:
                    continue
                if any(norm in inst or inst in norm for inst in installed):
                    continue
                try:
                    size = _dir_size(folder_path)
                    if size == 0:
                        continue
                    seen_paths.add(path_str)
                    results.append(RemnantItem(path=path_str, size=size, category="remnants"))
                except (OSError, PermissionError):
                    continue

        results.sort(key=lambda x: -x.size)
        return [r.to_dict() for r in results]
