"""自动更新模块 - 从 GitHub Releases 或局域网共享检查并下载新版本。"""
import os
import sys
import shutil
import requests
from packaging.version import Version

VERSION = "1.0.0"
GITHUB_REPO = "mxtrace/push-clp-tool"
ASSET_NAME = "PushCLP.exe"
API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# 局域网备用源：miaoyua 机器上的共享目录
LAN_SHARE_DIR = r"\\REM-HALUTNLTBII\PushCLP"
LAN_VERSION_FILE = os.path.join(LAN_SHARE_DIR, "version.txt")
LAN_EXE_FILE = os.path.join(LAN_SHARE_DIR, ASSET_NAME)


def get_exe_path():
    """获取当前 exe 路径。"""
    if getattr(sys, 'frozen', False):
        return sys.executable
    return None


def check_update_github(timeout=10):
    """从 GitHub 检查新版本。返回 (new_version, download_url) 或 None。"""
    try:
        resp = requests.get(API_URL, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        tag = data.get("tag_name", "").lstrip("v")
        if not tag:
            return None
        if Version(tag) <= Version(VERSION):
            return None
        for asset in data.get("assets", []):
            if asset["name"] == ASSET_NAME:
                return (tag, asset["browser_download_url"])
        return None
    except Exception as e:
        print(f"  [更新] GitHub 检查失败: {e}")
        return None


def check_update_lan():
    """从局域网共享检查新版本。返回 (new_version, lan_exe_path) 或 None。"""
    try:
        if not os.path.exists(LAN_VERSION_FILE):
            return None
        with open(LAN_VERSION_FILE, "r", encoding="utf-8") as f:
            tag = f.read().strip().lstrip("v")
        if not tag:
            return None
        if Version(tag) <= Version(VERSION):
            return None
        if not os.path.exists(LAN_EXE_FILE):
            return None
        return (tag, LAN_EXE_FILE)
    except Exception as e:
        print(f"  [更新] 局域网检查失败: {e}")
        return None


def download_from_github(download_url, exe_path, new_version):
    """从 GitHub 下载新版本。"""
    tmp_path = exe_path + ".new"
    print(f"  [更新] 从 GitHub 下载 v{new_version}...")
    resp = requests.get(download_url, stream=True, timeout=300)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    with open(tmp_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                pct = downloaded * 100 // total
                print("  [更新] 下载中... " + str(pct) + "%", end="\r", flush=True)
    print()
    return tmp_path


def copy_from_lan(lan_exe_path, exe_path, new_version):
    """从局域网共享复制新版本。"""
    tmp_path = exe_path + ".new"
    print(f"  [更新] 从局域网复制 v{new_version}...")
    shutil.copy2(lan_exe_path, tmp_path)
    print("  [更新] 复制完成")
    return tmp_path


def replace_exe(tmp_path, exe_path, new_version):
    """用新文件替换当前 exe。"""
    old_path = exe_path + ".old"
    if os.path.exists(old_path):
        os.remove(old_path)
    os.rename(exe_path, old_path)
    shutil.move(tmp_path, exe_path)
    print(f"  [更新] 已更新到 v{new_version}，下次运行生效")
    return True


def download_and_replace(source, new_version):
    """下载/复制新版本并替换当前 exe。"""
    exe_path = get_exe_path()
    if not exe_path:
        print("  [更新] 非 exe 模式，跳过更新")
        return False

    tmp_path = exe_path + ".new"
    old_path = exe_path + ".old"

    try:
        if source.startswith("http"):
            tmp_path = download_from_github(source, exe_path, new_version)
        else:
            tmp_path = copy_from_lan(source, exe_path, new_version)
        return replace_exe(tmp_path, exe_path, new_version)
    except Exception as e:
        print(f"  [更新] 更新失败: {e}")
        if os.path.exists(old_path) and not os.path.exists(exe_path):
            os.rename(old_path, exe_path)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def cleanup_old():
    """清理上次更新留下的 _old 文件。"""
    exe_path = get_exe_path()
    if not exe_path:
        return
    old_path = exe_path + ".old"
    if os.path.exists(old_path):
        try:
            os.remove(old_path)
            print("  [更新] 已清理旧版本")
        except OSError:
            pass


def auto_update():
    """主入口: 清理旧文件 -> 检查更新(GitHub优先, LAN备用) -> 替换。"""
    cleanup_old()
    print("  [更新] 检查新版本...")

    # 优先 GitHub
    result = check_update_github()
    if result:
        new_version, download_url = result
        print(f"  [更新] 发现新版本 v{new_version} (当前 v{VERSION}) [GitHub]")
        return download_and_replace(download_url, new_version)

    # 备用：局域网共享
    result = check_update_lan()
    if result:
        new_version, lan_path = result
        print(f"  [更新] 发现新版本 v{new_version} (当前 v{VERSION}) [LAN]")
        return download_and_replace(lan_path, new_version)

    print(f"  [更新] 当前已是最新版 v{VERSION}")
    return False


# Alias for main.py
check_and_update = auto_update
