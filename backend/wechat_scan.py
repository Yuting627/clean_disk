"""
微信聊天缓存与传输文件扫描（C 盘）
支持：默认文档路径、自定义路径（注册表/配置文件）、商店版、xwechat_files（4.0）
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from dataclasses import dataclass


def _dir_size(path: Path, limit_mb: float = 2000.0) -> int:
    total = 0
    limit_bytes = int(limit_mb * 1024 * 1024) if limit_mb > 0 else 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
                elif entry.is_dir():
                    total += _dir_size(Path(entry.path), 0)
                if limit_bytes and total >= limit_bytes:
                    break
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError):
        pass
    return total


def _get_wechat_base_paths() -> list[Path]:
    """收集所有可能的微信数据根目录（去重、存在且为目录）。"""
    seen: set[str] = set()
    out: list[Path] = []

    def add(p: Path) -> None:
        if p is None:
            return
        try:
            path_abs = str(p.resolve())
            if path_abs.lower() in seen:
                return
            if p.exists() and p.is_dir():
                seen.add(path_abs.lower())
                out.append(p)
        except Exception:
            pass

    user = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    base = Path(user)

    # 0) 明确指定：当前用户文档下的 xwechat_files（微信 4.0 默认路径）
    add(base / "Documents" / "xwechat_files")

    # 1) 默认：文档下 WeChat Files / xwechat_files
    for name in ("Documents", "文档", "My Documents"):
        add(base / name / "WeChat Files")
        add(base / name / "xwechat_files")

    # 2) 注册表：HKCU\Software\Tencent\WeChat -> FileSavePath
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Tencent\WeChat",
                0,
                winreg.KEY_READ
            )
            path_str, _ = winreg.QueryValueEx(key, "FileSavePath")
            winreg.CloseKey(key)
            if path_str and isinstance(path_str, str):
                path_str = path_str.strip()
                if path_str:
                    add(Path(path_str))
        except (OSError, TypeError, AttributeError):
            pass

    # 3) 配置文件：AppData\Roaming\Tencent\WeChat\config.ini 或 All Users\config\*.ini
    roaming = os.environ.get("APPDATA") or (base / "AppData" / "Roaming")
    roaming = Path(roaming)
    for config_dir in [roaming / "Tencent" / "WeChat", roaming / "Tencent" / "WeChat" / "All Users" / "config"]:
        if not config_dir.exists():
            continue
        try:
            for f in config_dir.iterdir():
                if f.suffix.lower() != ".ini":
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                    for line in text.splitlines():
                        line = line.strip()
                        if "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        v = v.strip().strip('"').strip("'").strip()
                        if not v or len(v) < 3:
                            continue
                        # 已是绝对路径
                        if os.path.isabs(v):
                            add(Path(v))
                            continue
                        # 相对路径：拼到文档目录
                        if "wechat" in v.lower() or "xwechat" in v.lower():
                            add(base / "Documents" / v)
                            add(base / "文档" / v)
                except Exception:
                    pass
        except Exception:
            pass

    # 4) xwechat 4.0 配置：AppData\Roaming\Tencent\xwechat\config\*.ini -> MyDocument
    xwechat_config = roaming / "Tencent" / "xwechat" / "config"
    if xwechat_config.exists():
        try:
            for f in xwechat_config.iterdir():
                if f.suffix.lower() != ".ini":
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                    for line in text.splitlines():
                        m = re.search(r"MyDocument\s*[=:]\s*(.+)", line, re.I)
                        if m:
                            val = m.group(1).strip().strip('"').strip("'")
                            if val and len(val) > 2:
                                # 可能是 F:\Documents 形式，WeChat 数据在 F:\Documents\xwechat_files 或 WeChat Files
                                add(Path(val) / "xwechat_files")
                                add(Path(val) / "WeChat Files")
                except Exception:
                    pass
        except Exception:
            pass

    # 5) 商店版微信：LocalCache\Roaming\Tencent\WeChatAppStore\WeChatAppStore Files
    local_app = os.environ.get("LOCALAPPDATA") or (base / "AppData" / "Local")
    local_app = Path(local_app)
    store_package = local_app / "Packages" / "TencentWeChatLimited.forWindows10_sdtnhv12zgd7a"
    if store_package.exists():
        add(store_package / "LocalCache" / "Roaming" / "Tencent" / "WeChatAppStore" / "WeChatAppStore Files")
    add(roaming / "Tencent" / "WeChatAppStore" / "WeChatAppStore Files")

    # 6) C 盘根下常见自定义位置（用户可能把 WeChat Files 移到 C:\WeChat Files 等）
    for root_name in ("WeChat Files", "xwechat_files", "WeChatAppStore Files"):
        add(Path("C:\\") / root_name)
        add(base / root_name)

    return out


@dataclass
class WeChatItem:
    path: str
    size: int
    category: str  # wechat_files | wechat_cache

    def to_dict(self) -> dict:
        return {"path": self.path, "size": self.size, "category": self.category}


def scan_wechat() -> list[dict]:
    """
    扫描微信传输文件（FileStorage / msg/file、msg/attach）与聊天缓存（Image、Video 等）。
    支持 WeChat Files 与 xwechat_files（微信 4.0）两种目录结构。
    返回 [ {"path": "...", "size": 123, "category": "wechat_files"}, ... ]
    """
    result: list[WeChatItem] = []
    cache_folder_names = ("Image", "Video", "CustomEmotion", "Data", "Sticker", "Emotion")
    # 微信 4.0 xwechat_files 使用 msg/file、msg/attach、video 等

    for base in _get_wechat_base_paths():
        try:
            account_dirs: list[Path] = []
            # 根下直接有 FileStorage 或 msg -> 根即账号目录（单账号 / xwechat）
            if (base / "FileStorage").exists() and (base / "FileStorage").is_dir():
                account_dirs.append(base)
            elif (base / "file_storage").exists() and (base / "file_storage").is_dir():
                account_dirs.append(base)
            elif (base / "msg").exists() and (base / "msg").is_dir():
                account_dirs.append(base)
            else:
                for entry in os.scandir(base):
                    if entry.is_dir():
                        account_dirs.append(Path(entry.path))

            for account_dir in account_dirs:
                # 传输文件：FileStorage 或 xwechat 的 msg/file、msg/attach
                for name in ("FileStorage", "file_storage"):
                    folder = account_dir / name
                    if folder.exists() and folder.is_dir():
                        size = _dir_size(folder)
                        if size > 0:
                            result.append(WeChatItem(path=str(folder.resolve()), size=size, category="wechat_files"))
                msg_dir = account_dir / "msg"
                if msg_dir.exists() and msg_dir.is_dir():
                    for sub in ("file", "attach"):
                        sub_dir = msg_dir / sub
                        if sub_dir.exists() and sub_dir.is_dir():
                            size = _dir_size(sub_dir)
                            if size > 0:
                                result.append(WeChatItem(path=str(sub_dir.resolve()), size=size, category="wechat_files"))
                # 聊天缓存
                for name in cache_folder_names:
                    folder = account_dir / name
                    if folder.exists() and folder.is_dir():
                        size = _dir_size(folder)
                        if size > 0:
                            result.append(WeChatItem(path=str(folder.resolve()), size=size, category="wechat_cache"))
                # xwechat 可能用小写 video
                for name in ("video",):
                    folder = account_dir / name
                    if folder.exists() and folder.is_dir():
                        size = _dir_size(folder)
                        if size > 0:
                            result.append(WeChatItem(path=str(folder.resolve()), size=size, category="wechat_cache"))
        except (OSError, PermissionError):
            continue

    result.sort(key=lambda x: -x.size)
    return [x.to_dict() for x in result]


def get_wechat_diagnostic() -> dict:
    """返回微信路径诊断：检查了哪些路径、注册表/配置、实际找到的根目录、扫描条目数。"""
    user = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    base = Path(user)
    roaming = Path(os.environ.get("APPDATA") or (base / "AppData" / "Roaming"))
    local_app = Path(os.environ.get("LOCALAPPDATA") or (base / "AppData" / "Local"))
    checked_paths: list[dict] = []
    registry_path: str | None = None
    config_paths: list[str] = []

    def check(p: Path) -> None:
        try:
            path_str = str(p.resolve())
            exists = p.exists() and p.is_dir()
            checked_paths.append({"path": path_str, "exists": exists})
        except Exception:
            checked_paths.append({"path": str(p), "exists": False})

    for name in ("Documents", "文档", "My Documents"):
        check(base / name / "WeChat Files")
        check(base / name / "xwechat_files")
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat", 0, winreg.KEY_READ)
            path_str, _ = winreg.QueryValueEx(key, "FileSavePath")
            winreg.CloseKey(key)
            if path_str and isinstance(path_str, str):
                registry_path = path_str.strip()
                if registry_path:
                    config_paths.append(registry_path)
                    check(Path(registry_path))
        except Exception as e:
            registry_path = f"(读取失败: {e})"
    for config_dir in [roaming / "Tencent" / "WeChat", roaming / "Tencent" / "WeChat" / "All Users" / "config"]:
        if not config_dir.exists():
            continue
        try:
            for f in config_dir.iterdir():
                if f.suffix.lower() != ".ini":
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                    for line in text.splitlines():
                        if "=" not in line:
                            continue
                        _, v = line.split("=", 1)
                        v = v.strip().strip('"').strip("'").strip()
                        if v and len(v) >= 3 and os.path.isabs(v):
                            config_paths.append(v)
                            check(Path(v))
                except Exception:
                    pass
        except Exception:
            pass
    xwechat_config = roaming / "Tencent" / "xwechat" / "config"
    if xwechat_config.exists():
        try:
            for f in xwechat_config.iterdir():
                if f.suffix.lower() != ".ini":
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                    for line in text.splitlines():
                        m = re.search(r"MyDocument\s*[=:]\s*(.+)", line, re.I)
                        if m:
                            val = m.group(1).strip().strip('"').strip("'")
                            if val and len(val) > 2:
                                check(Path(val) / "xwechat_files")
                                check(Path(val) / "WeChat Files")
                except Exception:
                    pass
        except Exception:
            pass
    check(local_app / "Packages" / "TencentWeChatLimited.forWindows10_sdtnhv12zgd7a" / "LocalCache" / "Roaming" / "Tencent" / "WeChatAppStore" / "WeChatAppStore Files")
    check(roaming / "Tencent" / "WeChatAppStore" / "WeChatAppStore Files")
    for root_name in ("WeChat Files", "xwechat_files", "WeChatAppStore Files"):
        check(Path("C:\\") / root_name)
        check(base / root_name)
    base_paths_found = [p["path"] for p in checked_paths if p.get("exists")]
    scan_result = scan_wechat()
    return {
        "registry_path": registry_path,
        "config_paths": list(dict.fromkeys(config_paths)),
        "checked_paths": checked_paths,
        "base_paths_found": base_paths_found,
        "scan_result_count": len(scan_result),
        "scan_result_preview": [{"path": x["path"], "size": x["size"], "category": x["category"]} for x in scan_result[:20]],
    }
