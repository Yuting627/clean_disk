"""
C 盘清理应用后端：扫描、预览、打开路径、删除选中文件。
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.scanner import scan_c_drive, get_scan_roots_windows, PROTECTED_EXTENSIONS
_last_remnant_paths: set[str] = set()
# 上次扫描得到的「大文件」路径（用于允许删除）
_last_large_file_paths: set[str] = set()
# 上次扫描得到的「微信」目录路径（传输文件/聊天缓存，用于允许删除整目录）
_last_wechat_paths: set[str] = set()
# 上次扫描得到的空文件夹路径（仅允许删除这些）
_last_empty_folder_paths: set[str] = set()

app = FastAPI(title="C 盘清理", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 前端静态文件（开发时从项目根运行）
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/assets", StaticFiles(directory=_frontend_dir), name="assets")


@app.get("/")
def index():
    idx = _frontend_dir / "index.html"
    if idx.exists():
        return FileResponse(
            idx,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    return {"message": "C 盘清理 API", "docs": "/docs"}


class DeleteRequest(BaseModel):
    paths: list[str]


class OpenPathRequest(BaseModel):
    path: str


def _get_allowed_roots() -> set[str]:
    roots = set()
    for p, _ in get_scan_roots_windows():
        roots.add(_normalize_path_for_check(str(p.resolve())))
    return roots


def _normalize_path_for_check(path: str) -> str:
    """统一为小写，并去掉 Windows 长路径前缀便于比较。"""
    s = path.lower()
    if os.name == "nt" and s.startswith("\\\\?\\"):
        s = s[4:]
    return s


def _get_forbidden_delete_prefixes() -> set[str]:
    """不允许删除的路径前缀（桌面、收藏夹、壁纸/主题等），避免影响桌面显示与壁纸。"""
    if os.name != "nt":
        return set()
    prefixes = set()
    user_profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    if user_profile:
        base = _normalize_path_for_check(str(Path(user_profile).resolve()))
        for name in ("desktop", "favorites", "桌面"):
            prefixes.add(base + os.sep + name)
    # Windows 壁纸与主题目录（TranscodedWallpaper 等），删除会导致壁纸黑屏
    for env_name, subpath in (
        ("APPDATA", os.path.join("microsoft", "windows", "themes")),
        ("LOCALAPPDATA", os.path.join("microsoft", "windows", "themes")),
    ):
        val = os.environ.get(env_name)
        if val:
            p = _normalize_path_for_check(str((Path(val) / subpath).resolve()))
            prefixes.add(p)
    return prefixes


def _is_under_forbidden(path_norm: str) -> bool:
    """路径是否在禁止删除的目录下（桌面、收藏夹等）。"""
    for prefix in _get_forbidden_delete_prefixes():
        if path_norm == prefix or path_norm.startswith(prefix + os.sep):
            return True
    return False


def _is_path_allowed(path: str) -> bool:
    path_norm = _normalize_path_for_check(str(Path(path).resolve()))
    for root in _get_allowed_roots():
        if path_norm == root or path_norm.startswith(root + os.sep):
            return True
    if path_norm in _last_remnant_paths:
        return True
    if path_norm in _last_large_file_paths:
        return True
    if path_norm in _last_wechat_paths:
        return True
    if path_norm in _last_empty_folder_paths:
        return True
    return False


def _is_protected(path: str) -> bool:
    suf = Path(path).suffix.lower()
    return suf in PROTECTED_EXTENSIONS


@app.get("/api/scan")
def api_scan():
    global _last_remnant_paths, _last_large_file_paths, _last_wechat_paths
    try:
        data = scan_c_drive()
        cats = data.get("categories") or {}
        remnant_list = cats.get("remnants") or []
        _last_remnant_paths = {_normalize_path_for_check(item["path"]) for item in remnant_list}
        large_list = cats.get("large") or []
        _last_large_file_paths = {_normalize_path_for_check(item["path"]) for item in large_list}
        wechat_items = (cats.get("wechat_files") or []) + (cats.get("wechat_cache") or [])
        _last_wechat_paths = {_normalize_path_for_check(item["path"]) for item in wechat_items}
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/open-path")
def api_open_path(body: OpenPathRequest):
    path = body.path
    if not path or not path.strip():
        raise HTTPException(status_code=400, detail="路径不能为空")
    p = Path(path.strip())
    if not p.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    path_abs = str(p.resolve())
    if not _is_path_allowed(path_abs):
        raise HTTPException(status_code=403, detail="仅允许打开扫描范围内的路径")
    try:
        if sys.platform == "win32":
            subprocess.run(["explorer", "/select," + path_abs], check=False, shell=False)
        else:
            subprocess.run(["xdg-open", path_abs], check=False)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/delete")
def api_delete(body: DeleteRequest):
    if not body.paths:
        return {"deleted": [], "skipped": [], "errors": []}
    allowed_roots = _get_allowed_roots()
    deleted = []
    skipped = []
    errors = []
    for raw in body.paths:
        path = raw.strip()
        if not path:
            continue
        try:
            p = Path(path)
            if not p.exists():
                errors.append({"path": path, "reason": "文件或目录不存在"})
                continue
            path_abs = str(p.resolve())
            path_norm = _normalize_path_for_check(path_abs)
            # 禁止删除桌面、收藏夹下的内容，避免影响桌面显示
            if _is_under_forbidden(path_norm):
                skipped.append({"path": path, "reason": "不允许删除桌面或收藏夹中的文件"})
                continue
            # 允许：在 temp/cache/logs 下的文件、本次扫描的残余目录、本次扫描的大文件、或本次扫描的微信目录
            in_roots = any(path_norm == r or path_norm.startswith(r + os.sep) for r in allowed_roots)
            is_remnant_dir = path_norm in _last_remnant_paths and p.is_dir()
            is_large_file = path_norm in _last_large_file_paths and p.is_file()
            is_wechat_dir = path_norm in _last_wechat_paths and p.is_dir()
            if not in_roots and not is_remnant_dir and not is_large_file and not is_wechat_dir:
                skipped.append({"path": path, "reason": "不在允许的扫描范围内"})
                continue
            if p.is_file():
                if _is_protected(path):
                    skipped.append({"path": path, "reason": "受保护的文件类型"})
                    continue
                p.unlink()
                deleted.append(path)
            elif p.is_dir() and (is_remnant_dir or is_wechat_dir):
                shutil.rmtree(p)
                deleted.append(path)
            else:
                skipped.append({"path": path, "reason": "仅支持删除文件或已扫描的残余目录"})
        except PermissionError as e:
            errors.append({"path": path, "reason": f"权限不足（文件可能被占用）: {e}"})
        except OSError as e:
            errors.append({"path": path, "reason": str(e)})
        except Exception as e:
            errors.append({"path": path, "reason": str(e)})
    return {"deleted": deleted, "skipped": skipped, "errors": errors}


def _run_delete_as_admin_windows(paths: list[str], allowed_roots: set[str]) -> tuple[list[str], list[dict], list[str]]:
    """在 Windows 下以管理员权限执行删除；支持文件和残余目录。返回 (已删除, 错误, 本次尝试的路径)。"""
    import ctypes
    from ctypes import wintypes

    to_delete: list[tuple[str, bool]] = []  # (path_abs, is_dir)
    for raw in paths:
        path = raw.strip()
        if not path:
            continue
        p = Path(path)
        if not p.exists():
            continue
        path_abs = str(p.resolve())
        path_norm = _normalize_path_for_check(path_abs)
        if _is_under_forbidden(path_norm):
            continue
        if p.is_file():
            if not any(path_norm == r or path_norm.startswith(r + os.sep) for r in allowed_roots):
                if path_norm not in _last_large_file_paths:
                    continue
            if _is_protected(path):
                continue
            to_delete.append((path_abs, False))
        elif p.is_dir() and path_norm in _last_remnant_paths:
            to_delete.append((path_abs, True))
        elif p.is_dir() and path_norm in _last_wechat_paths:
            to_delete.append((path_abs, True))

    if not to_delete:
        return [], [], []

    lines = ["@echo off"]
    for path_abs, is_dir in to_delete:
        safe = path_abs.replace('"', '"'"'"'"')
        if is_dir:
            lines.append(f'rd /s /q "{safe}"')
        else:
            lines.append(f'del /f /q "{safe}"')
    lines.append("exit /b 0")
    content = "\r\n".join(lines)

    fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="clean_disk_", text=True)
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        bat_path_abs = str(Path(bat_path).resolve())

        SEE_MASK_NOCLOSEPROCESS = 0x00000100
        SW_HIDE = 0

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("fMask", wintypes.DWORD),
                ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR),
                ("hKeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD),
                ("hIconOrMonitor", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE),
            ]

        sei = SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
        sei.fMask = SEE_MASK_NOCLOSEPROCESS
        sei.hwnd = None
        sei.lpVerb = "runas"
        sei.lpFile = "cmd.exe"
        sei.lpParameters = f'/c "{bat_path_abs}"'
        sei.lpDirectory = None
        sei.nShow = SW_HIDE
        sei.hProcess = None

        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        path_list = [p for p, _ in to_delete]
        if not shell32.ShellExecuteExW(ctypes.byref(sei)):
            return [], [{"path": p, "reason": "无法请求管理员权限（请在弹出的 UAC 中确认）"} for p in path_list], path_list

        if sei.hProcess:
            kernel32.WaitForSingleObject(sei.hProcess, 0xFFFFFFFF)
            kernel32.CloseHandle(sei.hProcess)
    finally:
        try:
            os.unlink(bat_path)
        except OSError:
            pass

    path_list = [p for p, _ in to_delete]
    deleted = [p for p in path_list if not Path(p).exists()]
    errors = [{"path": p, "reason": "可能仍被占用或删除被拒绝"} for p in path_list if Path(p).exists()]
    return deleted, errors, path_list


@app.post("/api/delete-admin")
def api_delete_admin(body: DeleteRequest):
    """以管理员权限删除选中文件（会弹出 UAC）。仅 Windows。"""
    if sys.platform != "win32":
        raise HTTPException(status_code=501, detail="仅支持 Windows")
    if not body.paths:
        return {"deleted": [], "skipped": [], "errors": []}
    allowed_roots = _get_allowed_roots()
    deleted, errors, attempted = _run_delete_as_admin_windows(body.paths, allowed_roots)
    attempted_set = set(attempted)
    skipped = []
    for raw in body.paths:
        path = raw.strip()
        if not path:
            continue
        p = Path(path)
        if not p.exists():
            skipped.append({"path": path, "reason": "文件不存在"})
            continue
        path_abs = str(p.resolve())
        if path_abs in attempted_set:
            continue
        path_norm = _normalize_path_for_check(path_abs)
        if not any(path_norm == r or path_norm.startswith(r + os.sep) for r in allowed_roots):
            skipped.append({"path": path, "reason": "不在允许的扫描范围内"})
        elif _is_protected(path):
            skipped.append({"path": path, "reason": "受保护的文件类型"})
        else:
            skipped.append({"path": path, "reason": "未包含在本次删除中"})
    return {"deleted": deleted, "skipped": skipped, "errors": errors}


@app.get("/api/wechat-diagnostic")
def api_wechat_diagnostic():
    """返回微信存储路径诊断，用于确认为何未列出微信文件。"""
    try:
        from backend.wechat_scan import get_wechat_diagnostic
        return get_wechat_diagnostic()
    except Exception as e:
        return {"error": str(e), "checked_paths": [], "base_paths_found": [], "scan_result_count": 0}


@app.get("/api/scan-empty-folders")
def api_scan_empty_folders():
    """扫描可安全清理的空文件夹（仅 Temp、缓存、日志等目录）。"""
    global _last_empty_folder_paths
    try:
        from backend.empty_folders import scan_empty_folders
        paths = scan_empty_folders(max_total=3000)
        _last_empty_folder_paths = {_normalize_path_for_check(p) for p in paths}
        return {"paths": paths, "count": len(paths)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/delete-empty-folders")
def api_delete_empty_folders(body: DeleteRequest):
    """删除选中的空文件夹（仅允许删除本次扫描结果中的、且仍为空的目录）。"""
    if not body.paths:
        return {"deleted": [], "skipped": [], "errors": []}
    deleted = []
    skipped = []
    errors = []
    for raw in body.paths:
        path = raw.strip()
        if not path:
            continue
        try:
            p = Path(path)
            if not p.exists():
                errors.append({"path": path, "reason": "路径不存在"})
                continue
            if not p.is_dir():
                skipped.append({"path": path, "reason": "不是目录"})
                continue
            path_norm = _normalize_path_for_check(str(p.resolve()))
            if path_norm not in _last_empty_folder_paths:
                skipped.append({"path": path, "reason": "不在本次扫描的空文件夹列表中"})
                continue
            try:
                p.rmdir()
                deleted.append(path)
            except OSError as e:
                errors.append({"path": path, "reason": f"删除失败（可能非空或权限不足）: {e}"})
        except Exception as e:
            errors.append({"path": path, "reason": str(e)})
    return {"deleted": deleted, "skipped": skipped, "errors": errors}


@app.get("/api/health")
def health():
    return {"status": "ok"}
