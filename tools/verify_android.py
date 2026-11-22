#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""呆猫漫画安卓端：构建 / 安装启动 / 端口发现 / 接口验证 / 截图 / 隐私审计。

一条命令可以串联多个动作，按下面的固定顺序执行：
    sync -> build -> install(+launch) -> launch -> port -> api -> shot
         -> tap/swipe/ui/tap-text -> audit -> logcat

常用示例（在 呆猫漫画-安卓 根目录执行，用 Python 3.10）：
    python tools/verify_android.py --help
    python tools/verify_android.py --sync                 # 只同步 v3 -> 安卓工程
    python tools/verify_android.py --build                # 同步 + gradle assembleRelease + 复制到 dist/
    python tools/verify_android.py --install              # 装最新 dist APK（保留数据）并启动等待就绪
    python tools/verify_android.py --install --fresh      # 先卸载再装（清数据）
    python tools/verify_android.py --port                 # 找端口 + adb forward tcp:18080 + 健康检查
    python tools/verify_android.py --api /api/health      # 自动带 token 调本地服务
    python tools/verify_android.py --api /api/shelf
    python tools/verify_android.py --shot old-app         # 截图到 .scratch/shots/old-app.png
    python tools/verify_android.py --ui                   # uiautomator dump（打印可点节点 + 存 .scratch/ui.xml）
    python tools/verify_android.py --tap 540 200          # 点屏幕
    python tools/verify_android.py --swipe 540 1800 540 600
    python tools/verify_android.py --tap-text 书架        # 按文本/描述找到节点并点它的中心
    python tools/verify_android.py --audit                # 扫 dist/*.apk 隐私 + 包信息/签名
    python tools/verify_android.py --logcat 100           # 打最近 100 行应用相关日志
    # 完整一轮（不构建）：
    python tools/verify_android.py --install --port --api /api/health --shot old-app

约定：
* 端口/令牌会缓存到 .scratch/state.json，后续单条命令不必再找一遍；
* 服务只监听 127.0.0.1，宿主机经 adb forward tcp:18080 访问；
* token 从 logcat 的 `server ready: <端口>|<令牌>` 抓（MainActivity 打的那行），
  也可以用 `x-dumbcat-token` 请求头或 `?token=` 查询参数；
* 任何外部命令失败都会打印完整命令行与退出码，不静默吞掉。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

# ---------------------------------------------------------------- 固定路径

ROOT = Path(__file__).resolve().parent.parent          # 呆猫漫画-安卓
V3 = ROOT.parent / "呆猫漫画v3"                        # 源码真相（只读）
DIST = ROOT / "dist"
SCRATCH = ROOT / ".scratch"
SHOTS = SCRATCH / "shots"
STATE_FILE = SCRATCH / "state.json"

PY310 = Path("C:/Users/linghhhh/AppData/Local/Programs/Python/Python310/python.exe")
GRADLE = (Path.home() / ".gradle" / "wrapper" / "dists" / "gradle-8.12.1-bin"
          / "eumc4uhoysa37zql93vfjkxy0" / "gradle-8.12.1" / "bin" / "gradle.bat")
JAVA_HOME = Path("E:/java/JDK21")
ANDROID_SDK = Path("C:/Users/linghhhh/AppData/Local/Android/Sdk")
ADB = ANDROID_SDK / "platform-tools" / "adb.exe"
APK_OUT = ROOT / "app" / "build" / "outputs" / "apk" / "release"

PKG = "com.dumbcat.manga"
ACTIVITY = f"{PKG}/.MainActivity"
LOCAL_PORT = 18080                                     # 宿主机转发端口
SERIAL = "emulator-5554"                               # 可被 --serial 覆盖

# 隐私审计：硬断言（命中即失败）
SECRETS_HARD = ("linghhhhq", "E9qf5ueI")
# 隐私审计：警示词（只报告，不判失败；例如字段名 jwttoken 属正常代码）
SECRETS_WARN = ("jwttoken",)


class StepError(RuntimeError):
    """某一步失败；message 里已经带完整命令和退出码。"""


# ---------------------------------------------------------------- 输出/命令


def fmt_cmd(cmd: list) -> str:
    parts = []
    for item in cmd:
        text = str(item)
        parts.append(f'"{text}"' if (" " in text or "\t" in text) else text)
    return " ".join(parts)


def run(cmd, cwd=None, env=None, capture=True, timeout=None, check=True,
        quiet=False, label=None):
    """跑外部命令。失败时抛异常，message 带完整命令 + 退出码 + 输出尾部。"""
    cmd = [str(c) for c in cmd]
    if not quiet:
        print(("  $ " if label else "$ ") + fmt_cmd(cmd), flush=True)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=capture,
            text=capture,
            encoding="utf-8" if capture else None,
            errors="replace" if capture else None,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise StepError(f"命令不存在：{cmd[0]}\n  系统错误：{exc}") from exc
    except PermissionError as exc:
        raise StepError(f"没有执行权限：{cmd[0]}\n  系统错误：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise StepError(
            f"命令超时（{timeout} 秒）：\n  {fmt_cmd(cmd)}\n"
            f"  （命令被强制结束，退出码未知；可用 --timeout 加大等待）"
        ) from exc
    if proc.returncode != 0:
        out = (getattr(proc, "stdout", "") or "").strip()
        err = (getattr(proc, "stderr", "") or "").strip()
        tail = ""
        if out:
            tail += "\n  --- stdout（末 3000 字符）---\n" + out[-3000:]
        if err:
            tail += "\n  --- stderr（末 3000 字符）---\n" + err[-3000:]
        message = (f"命令失败（退出码 {proc.returncode}）：\n  {fmt_cmd(cmd)}" + tail)
        if check:
            raise StepError(message)
        print("！" + message, flush=True)
    return proc


def adb(args, capture=True, timeout=180, check=True, quiet=False):
    cmd = [ADB]
    if SERIAL:
        cmd += ["-s", SERIAL]
    cmd += [str(a) for a in args]
    return run(cmd, capture=capture, timeout=timeout, check=check, quiet=quiet)


def java_env() -> dict:
    env = dict(os.environ)
    env["JAVA_HOME"] = str(JAVA_HOME)
    env["PATH"] = str(JAVA_HOME / "bin") + os.pathsep + env.get("PATH", "")
    return env


def gradle_version_name() -> str:
    path = ROOT / "app" / "build.gradle"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'versionName\s+"([^"]+)"', text)
    return match.group(1) if match else "unknown"


def gradle_version_code() -> str:
    path = ROOT / "app" / "build.gradle"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r"versionCode\s+(\d+)", text)
    return match.group(1) if match else "unknown"


def human_size(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB（{size} 字节）"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


# ---------------------------------------------------------------- 状态缓存


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(port, token, extra: dict | None = None) -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    data = {
        "serial": SERIAL,
        "port": int(port),
        "token": token or "",
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if extra:
        data.update(extra)
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def clear_state() -> None:
    """丢掉缓存的端口/令牌（模拟器重启后旧端口会失效）。"""
    try:
        STATE_FILE.unlink()
    except OSError:
        pass


def app_running() -> bool:
    try:
        proc = adb(["shell", "pidof", PKG], capture=True, quiet=True,
                   timeout=30, check=False)
    except StepError:
        return False
    text = (proc.stdout or "").strip()
    return bool(text) and "not found" not in text


# ---------------------------------------------------------------- 设备


def device_rows() -> list[tuple[str, str]]:
    proc = run([ADB, "devices"], capture=True, quiet=True)
    rows = []
    for line in (proc.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            rows.append((parts[0], parts[1]))
    return rows


def ensure_device() -> None:
    global SERIAL
    rows = device_rows()
    states = dict(rows)
    if not rows:
        raise StepError(
            "没有连接任何设备/模拟器。\n"
            "  启动模拟器：\n"
            f'    "{ANDROID_SDK / "emulator" / "emulator.exe"}" -avd Small_Phone -no-snapshot-load\n'
            f"  可用 AVD：{_list_avds() or '（无）'}"
        )
    if SERIAL and states.get(SERIAL) == "device":
        return
    online = [serial for serial, state in rows if state == "device"]
    if len(online) == 1:
        if SERIAL and SERIAL != online[0]:
            print(f"！设备 {SERIAL} 不在线，改用唯一在线设备 {online[0]}")
        SERIAL = online[0]
        return
    detail = "\n".join(f"  {serial}\t{state}" for serial, state in rows) or "  （空）"
    raise StepError(
        f"设备 {SERIAL!r} 不在线。当前 adb devices：\n{detail}\n"
        f"  指定设备：--serial <序列号>；"
        f"可用 AVD：{_list_avds() or '（无）'}"
    )


def _list_avds() -> str:
    emulator = ANDROID_SDK / "emulator" / "emulator.exe"
    if not emulator.exists():
        return ""
    try:
        proc = subprocess.run([str(emulator), "-list-avds"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=30)
        return ", ".join(line.strip() for line in proc.stdout.splitlines() if line.strip())
    except (OSError, subprocess.TimeoutExpired):
        return ""


def device_brief() -> str:
    try:
        abi = (adb(["shell", "getprop", "ro.product.cpu.abi"], quiet=True,
                   timeout=30).stdout or "").strip()
        sdk = (adb(["shell", "getprop", "ro.build.version.sdk"], quiet=True,
                   timeout=30).stdout or "").strip()
        model = (adb(["shell", "getprop", "ro.product.model"], quiet=True,
                     timeout=30).stdout or "").strip()
    except StepError:
        return SERIAL
    return f"{SERIAL}  ABI={abi or '?'}  SDK={sdk or '?'}  型号={model or '?'}"


def package_info() -> str:
    try:
        proc = adb(["shell", "dumpsys", "package", PKG], capture=True, quiet=True,
                   timeout=60, check=False)
    except StepError:
        return ""
    text = proc.stdout or ""
    if "Unable to find package" in text or "Unable to find" in text:
        return ""
    wants = ("versionCode=", "versionName=", "firstInstallTime=", "lastUpdateTime=")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(wants) and len(lines) < 6:
            lines.append("  " + stripped)
    return "\n".join(dict.fromkeys(lines))


# ---------------------------------------------------------------- HTTP / 端口


def http_get(path: str, token: str = "", timeout: float = 8.0):
    """经 127.0.0.1:LOCAL_PORT 请求；返回 (状态码, 响应字节, 用时秒, URL)。"""
    if not path.startswith("/"):
        path = "/" + path
    query = ""
    if token and "token=" not in path:
        query = ("&" if "?" in path else "?") + "token=" + urllib.parse.quote(token)
    url = f"http://127.0.0.1:{LOCAL_PORT}{path}{query}"
    headers = {"x-dumbcat-token": token} if token else {}
    request = urllib.request.Request(url, headers=headers)
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), time.time() - started, url
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), time.time() - started, url
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise StepError(
            f"连不上本地服务：{url}\n  原因：{reason}\n"
            f"  检查：1) 应用是否在前台运行（--launch）；2) 转发是否存在"
            f"（adb forward --list）；3) 模拟器刚重启时缓存端口已失效，"
            f"重跑 --launch 会重新发现端口"
        ) from exc


def reset_forward() -> None:
    """撤掉旧的 tcp:18080 转发。没有旧转发是正常情况，不该报错刷屏。"""
    cmd = [str(ADB)]
    if SERIAL:
        cmd += ["-s", SERIAL]
    cmd += ["forward", "--remove", f"tcp:{LOCAL_PORT}"]
    try:
        subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass


def set_forward(port: int) -> None:
    reset_forward()
    run([ADB, "forward", f"tcp:{LOCAL_PORT}", f"tcp:{port}"],
        capture=True, timeout=60)


def health_ok(token: str = "", timeout: float = 3.0) -> bool:
    try:
        status, _, _, _ = http_get("/api/health", token=token, timeout=timeout)
    except StepError:
        return False
    return status == 200


RE_SERVER_READY = re.compile(r"server ready:\s*(\d{2,5})\s*\|\s*([A-Za-z0-9_\-]+)")
RE_LOADING = re.compile(r"http://127\.0\.0\.1:(\d{2,5})/\?token=([A-Za-z0-9_\-]+)")
RE_PY_READY = re.compile(r"服务就绪[:：]\s*127\.0\.0\.1:(\d{2,5})")
CRASH_MARKERS = (
    "Python 启动失败",
    "FATAL EXCEPTION",
    "启动失败：",
    "服务没有返回地址",
    "启动超时，请退出后重试",
)


def app_logcat_raw() -> str:
    try:
        proc = adb(
            ["logcat", "-d", "-v", "time",
             "-s", "DumbCat:I", "python.stdout:I", "python.stderr:I",
             "AndroidRuntime:E", "libc:F"],
            capture=True, quiet=True, timeout=90, check=False,
        )
    except StepError:
        return ""
    return proc.stdout or ""


def parse_logcat(dump: str) -> tuple[int, str] | None:
    """从 logcat 里按时间顺序取最后出现的 (端口, 令牌)。"""
    found = None
    for line in dump.splitlines():
        match = RE_SERVER_READY.search(line)
        if match:
            found = (int(match.group(1)), match.group(2))
            continue
        match = RE_LOADING.search(line)
        if match:
            found = (int(match.group(1)), match.group(2))
            continue
        match = RE_PY_READY.search(line)
        if match:
            found = (int(match.group(1)), found[1] if found else "")
    return found


def proc_listen_ports() -> list[int]:
    """兜底：直接读 /proc/net/tcp{,6} 找 LISTEN 的本地端口。"""
    proc = adb(["shell", "cat", "/proc/net/tcp", "/proc/net/tcp6"],
               capture=True, quiet=True, timeout=60, check=False)
    ports: list[int] = []
    loopback = {
        "0100007F",                                          # 127.0.0.1
        "00000000",                                          # 0.0.0.0
        "00000000000000000000000001000000",                  # ::1
        "0000000000000000FFFF00000100007F",                  # ::ffff:127.0.0.1
    }
    skip = {LOCAL_PORT, 5037, 5554, 5555, 5556}
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] == "sl":
            continue
        local, state = parts[1], parts[3]
        if state.upper() != "0A":        # 0A = TCP_LISTEN
            continue
        address, _, port_hex = local.partition(":")
        if address.upper() not in loopback:
            continue
        try:
            port = int(port_hex, 16)
        except ValueError:
            continue
        if port < 1024 or port in skip:
            continue
        ports.append(port)
    return sorted(set(ports))


def logcat_tail(lines: int = 100) -> None:
    dump = app_logcat_raw()
    rows = dump.splitlines()
    print(f"== 应用相关 logcat（末 {min(lines, len(rows))} 行 / 共 {len(rows)} 行）==")
    for line in rows[-lines:]:
        print(line)
    if not rows:
        print("（空：可能刚清过日志，或应用还没启动）")


def discover_port(verbose: bool = True) -> tuple[int, str]:
    """找端口 + 建立转发 + 健康检查。返回 (端口, 令牌)。"""
    ensure_device()
    state = load_state()
    port = int(state.get("port") or 0)
    token = str(state.get("token") or "")

    dump = app_logcat_raw()
    from_logcat = parse_logcat(dump)
    if from_logcat:
        lport, ltoken = from_logcat
        if lport != port or ltoken != token:
            if verbose:
                print(f"logcat 发现服务：端口 {lport}（缓存：{port or '无'}）")
        port, token = lport, ltoken
    if not port:
        candidates = proc_listen_ports()
        if candidates and verbose:
            print(f"/proc/net/tcp 候选端口：{candidates}")
        for candidate in candidates:
            set_forward(candidate)
            if health_ok():
                port = candidate
                break
    if not port:
        raise StepError(
            "找不到内嵌 HTTP 服务端口。\n"
            "  logcat 里没有 `server ready: <端口>|<令牌>`，也没有可用的 LISTEN 端口。\n"
            "  先启动应用并等待就绪：python tools/verify_android.py --launch\n"
            "  再不行就看日志：python tools/verify_android.py --logcat 200"
        )

    set_forward(port)
    try:
        status, body, elapsed, _ = http_get("/api/health", token=token, timeout=8)
    except StepError:
        # 模拟器重启 / 应用被杀之后，缓存端口就没用了：当健康检查失败处理
        status, body, elapsed = 0, b"", 0.0
    if verbose:
        print(f"设备      : {device_brief()}")
        print(f"服务端口  : {port}")
        print(f"令牌      : {token or '（未知，仅 /api/health 可用）'}")
        print(f"转发      : tcp:{LOCAL_PORT} -> tcp:{port}")
        print(f"健康检查  : HTTP {status}（{elapsed * 1000:.0f} ms）")
        if status != 200:
            text = body.decode("utf-8", "replace")[:400]
            if text:
                print(f"  响应：{text}")
            print("！健康检查没通过：应用可能没在运行（模拟器重启后很常见）。")
            print("  先启动并等待就绪：python tools/verify_android.py --launch")
            if not app_running():
                print("  当前进程状态：未运行")
            else:
                print("  当前进程状态：在运行，但服务端口可能变了，试试 --launch 重新发现")
            clear_state()
            return port, token
    save_state(port, token)
    return port, token


# ---------------------------------------------------------------- 构建


def do_sync() -> None:
    print("== 同步 v3 源码到安卓工程（tools/sync_app.py）==")
    python = PY310 if PY310.exists() else Path(sys.executable)
    if not python.exists():
        raise StepError(f"找不到 Python 3.10：{PY310}")
    run([python, ROOT / "tools" / "sync_app.py"], cwd=ROOT, capture=False)


def do_build() -> None:
    if not GRADLE.exists():
        raise StepError(f"找不到 Gradle：{GRADLE}")
    if not JAVA_HOME.exists():
        raise StepError(f"找不到 JAVA_HOME：{JAVA_HOME}")
    print(f"环境：GRADLE={GRADLE}")
    print(f"      JAVA_HOME={JAVA_HOME}")
    do_sync()

    print("== 构建 release APK（assembleRelease --rerun-tasks）==")
    print("提示：Chaquopy 不会因为 Python 源码变化就重打包 app.imy，所以必须 --rerun-tasks。")
    started = time.time()
    run([GRADLE, "assembleRelease", "--console=plain", "--rerun-tasks"],
        cwd=ROOT, env=java_env(), capture=False)
    elapsed = time.time() - started

    candidates = sorted(APK_OUT.glob("*.apk"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise StepError(f"构建结束但没找到 APK：{APK_OUT}/*.apk")
    built = next((p for p in candidates if p.name == "app-release.apk"), candidates[-1])
    version = gradle_version_name()
    DIST.mkdir(parents=True, exist_ok=True)
    target = DIST / f"呆猫漫画_{version}.apk"
    shutil.copy2(built, target)
    print(f"\n构建耗时：{human_elapsed(elapsed)}")
    print(f"产物：{built}")
    print(f"复制：{target}")
    print(f"大小：{human_size(target.stat().st_size)}")
    print(f"SHA256：{sha256_file(target)}")


# ---------------------------------------------------------------- 安装/启动


def resolve_apk(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = (ROOT / path) if (ROOT / path).exists() else Path(explicit)
        if not path.exists():
            raise StepError(f"指定 APK 不存在：{path}")
        return path.resolve()
    if DIST.exists():
        dist_apks = sorted(DIST.glob("*.apk"), key=lambda p: p.stat().st_mtime)
        if dist_apks:
            return dist_apks[-1].resolve()
    built = APK_OUT / "app-release.apk"
    if built.exists():
        return built.resolve()
    raise StepError(
        f"找不到 APK：dist/ 和 {APK_OUT} 都是空的。\n"
        "  先构建：python tools/verify_android.py --build"
    )


def install(apk: Path, fresh: bool = False) -> None:
    ensure_device()
    print(f"== 安装 APK ==")
    print(f"设备：{device_brief()}")
    print(f"APK ：{apk}")
    print(f"      {human_size(apk.stat().st_size)}  SHA256={sha256_file(apk)[:16]}…")
    before = package_info()
    if fresh:
        print("--fresh：先卸载（清数据）")
        proc = run([ADB, "-s", SERIAL, "uninstall", PKG],
                   capture=True, check=False, timeout=180)
        text = ((proc.stdout or "") + (proc.stderr or "")).strip()
        print(f"  uninstall：{text or '（无输出）'}")
    try:
        args = [ADB, "-s", SERIAL, "install"]
        args += ["-r"] if not fresh else []
        args.append(str(apk))
        proc = run(args, capture=True, timeout=900)
    except StepError as exc:
        message = str(exc)
        if "NO_MATCHING_ABIS" in message or "INSTALL_FAILED_NO_MATCHING_ABIS" in message:
            message += (
                "\n  ABI 不匹配。这台模拟器是 "
                f"{(adb(['shell', 'getprop', 'ro.product.cpu.abi'], quiet=True).stdout or '').strip()}，"
                "APK 只带 arm64-v8a/x86_64。\n"
                "  起一台 x86_64 AVD 再试：\n"
                f'    "{ANDROID_SDK / "emulator" / "emulator.exe"}" -avd Small_Phone -no-snapshot-load\n'
                f"    python tools/verify_android.py --serial emulator-5556 --install --fresh"
            )
        raise StepError(message) from exc
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    print(f"  install：{output}")
    after = package_info()
    if after:
        if before and before != after:
            print("安装前包信息：")
            print(before)
            print("安装后包信息：")
            print(after)
        else:
            print("安装后包信息：")
            print(after)
    if "Success" not in output:
        raise StepError(f"adb install 没有返回 Success：\n{output}")


def launch_and_wait(timeout: int = 150) -> tuple[int, str]:
    ensure_device()
    print("== 启动应用并等待内嵌服务就绪 ==")
    print(f"设备：{device_brief()}")
    run([ADB, "-s", SERIAL, "logcat", "-c"], capture=True, quiet=True,
        check=False, timeout=60)
    adb(["shell", "am", "force-stop", PKG], timeout=60, check=False, quiet=True)
    proc = adb(["shell", "am", "start", "-W", "-n", ACTIVITY], timeout=120, check=False)
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    print(output or "（am start 无输出）")
    if "Error" in output or "Exception" in output:
        raise StepError(f"启动失败：\n{output}")

    deadline = time.time() + timeout
    last_note = 0.0
    while time.time() < deadline:
        dump = app_logcat_raw()
        info = parse_logcat(dump)
        if info:
            port, token = info
            set_forward(port)
            if health_ok(token):
                save_state(port, token)
                print(f"服务已就绪：端口 {port}，令牌 {token or '（未知）'}")
                print(f"转发：tcp:{LOCAL_PORT} -> tcp:{port}")
                return port, token
        crash = next((marker for marker in CRASH_MARKERS if marker in dump), None)
        if crash:
            print(f"\n！日志出现失败标记：{crash}")
            logcat_tail(120)
            raise StepError(
                "应用启动过程中报错（见上面的 logcat）。\n"
                "  若提示 Chaquopy 解压失败，用 --install --fresh 清数据重装；"
                "若是 ABI/OS 问题看 --audit 的 minSdk。"
            )
        if time.time() - last_note > 15:
            waited = int(time.time() - (deadline - timeout))
            print(f"  …等待中（{waited}s / {timeout}s），首次启动要解压 Chaquopy 标准库，慢一些正常")
            last_note = time.time()
        time.sleep(2)

    print(f"\n！{timeout} 秒内服务没有就绪，下面是最近的日志：")
    logcat_tail(150)
    raise StepError(
        f"等待内嵌服务超时（{timeout} 秒）。\n"
        f"  logcat：{ADB} -s {SERIAL} logcat -d | tail -100\n"
        "  重试：python tools/verify_android.py --launch --timeout 240"
    )


# ---------------------------------------------------------------- 接口/截图/交互


def normalize_api_path(path: str) -> str:
    """把 --api 的参数规范化成以 / 开头的路径。

    额外兜底 Git Bash / MSYS 的路径转换：它会把 `/api/health` 改写成
    `C:/Users/.../api/health`，这里切回 `/api/...`。用 cmd/PowerShell 时无影响。
    """
    text = path.strip()
    if not text:
        raise StepError("--api 需要路径，例如 --api /api/health（Git Bash 里写 api/health 也行）")
    if "://" in text:
        parsed = urllib.parse.urlsplit(text)
        text = parsed.path + (("?" + parsed.query) if parsed.query else "")
    if not text.startswith(("/api/", "/img/", "/static/")):
        match = re.search(r"/(api|img|static)/.*$", text)
        if match:
            text = match.group(0)
    if not text.startswith("/"):
        text = "/" + text
    return text


def redact(url: str) -> str:
    return re.sub(r"(token=)[^&]+", r"\1***", url)


def api(path: str) -> None:
    path = normalize_api_path(path)
    state = load_state()
    if not state.get("port"):
        discover_port()
        state = load_state()
    token = str(state.get("token") or "")
    ensure_device()
    # 每次调用都重新指向正确端口（便宜，且避免应用重启换端口后还转到旧口）
    set_forward(int(state["port"]))
    try:
        status, body, elapsed, url = http_get(path, token=token, timeout=30)
    except StepError:
        print("！连不上（端口/令牌可能已过期），重新发现一次…")
        discover_port(verbose=False)
        state = load_state()
        token = str(state.get("token") or "")
        status, body, elapsed, url = http_get(path, token=token, timeout=30)
    text = body.decode("utf-8", "replace")
    print(f"GET {redact(url)}")
    print(f"HTTP {status}  用时 {elapsed * 1000:.0f} ms  正文 {len(body)} 字节"
          + ("（已带 token）" if token else "（无令牌，只能调 /api/health）"))
    pretty = text
    try:
        pretty = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, ValueError):
        pass
    shown = pretty[:2000]
    print("--- 响应（前 2000 字符）---")
    print(shown)
    if len(pretty) > 2000:
        print(f"…（还有 {len(pretty) - 2000} 字符未显示）")
    if status >= 400:
        raise StepError(f"接口返回错误状态码 {status}：{path}")


def shot(name: str) -> Path:
    ensure_device()
    SHOTS.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", name).strip("_") or "shot"
    if safe.lower().endswith(".png"):
        safe = safe[:-4]
    path = (SHOTS / f"{safe}.png").resolve()
    cmd = [ADB]
    if SERIAL:
        cmd += ["-s", SERIAL]
    cmd += ["exec-out", "screencap", "-p"]
    print("$ " + fmt_cmd(cmd) + f" > {path}")
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    data = proc.stdout or b""
    if proc.returncode != 0 or not data:
        err = (proc.stderr or b"").decode("utf-8", "replace")
        raise StepError(
            f"截图失败（退出码 {proc.returncode}）：{fmt_cmd(cmd)}\n{err[-1500:]}"
        )
    if not data.startswith(b"\x89PNG"):
        raise StepError(
            f"截图返回的不是 PNG（前 16 字节：{data[:16]!r}）。\n"
            "  部分模拟器需要先关掉硬件加速：Settings > 系统 > 开发者选项，"
            "或用 --ui 确认屏幕是否黑屏。"
        )
    path.write_bytes(data)
    width, height = struct.unpack(">II", data[16:24])
    print(f"截图已保存：{path}")
    print(f"尺寸：{width}x{height}  大小：{human_size(len(data))}")
    return path


def tap(x: int, y: int) -> None:
    ensure_device()
    adb(["shell", "input", "tap", str(x), str(y)], timeout=60)
    print(f"已点击 ({x}, {y})")


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
    ensure_device()
    adb(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2),
         str(duration_ms)], timeout=60)
    print(f"已滑动 ({x1},{y1}) -> ({x2},{y2})，{duration_ms} ms")


def dump_ui(quiet: bool = False) -> str:
    ensure_device()
    remote = "/sdcard/window_dump.xml"
    adb(["shell", "uiautomator", "dump", remote], timeout=120)
    proc = adb(["exec-out", "cat", remote], timeout=60)
    xml = proc.stdout or ""
    if "<hierarchy" not in xml:
        raise StepError(f"uiautomator dump 没有返回有效 XML：\n{xml[:1000]}")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    path = (SCRATCH / "ui.xml").resolve()
    path.write_text(xml, encoding="utf-8")
    if not quiet:
        print(f"界面 XML 已保存：{path}（{len(xml)} 字符）")
        print("--- 可交互节点（文本/描述 | 资源 id | 中心坐标）---")
        for node in ui_nodes(xml)[:60]:
            label = node["text"] or node["desc"] or ""
            if not (label or node["resource_id"]):
                continue
            label = (label or "")[:40]
            print(f"  {label:<40} | {node['resource_id'][:34]:<34} | "
                  f"中心 ({node['cx']}, {node['cy']})")
        print("--- XML 前 4000 字符 ---")
        print(xml[:4000])
    return xml


def ui_nodes(xml: str) -> list[dict]:
    nodes = []
    for match in re.finditer(r"<node\b[^>]*>", xml):
        attrs = dict(re.findall(r'([\w\-:]+)="([^"]*)"', match.group(0)))
        bounds = attrs.get("bounds", "")
        numbers = [int(value) for value in re.findall(r"-?\d+", bounds)]
        if len(numbers) < 4:
            continue
        x1, y1, x2, y2 = numbers[:4]
        nodes.append({
            "text": attrs.get("text", ""),
            "desc": attrs.get("content-desc", ""),
            "resource_id": attrs.get("resource-id", ""),
            "class": attrs.get("class", ""),
            "clickable": attrs.get("clickable", "false") == "true",
            "cx": (x1 + x2) // 2,
            "cy": (y1 + y2) // 2,
        })
    return nodes


def tap_text(needle: str) -> None:
    xml = dump_ui(quiet=True)
    nodes = ui_nodes(xml)
    for node in nodes:
        haystack = f'{node["text"]} {node["desc"]} {node["resource_id"]}'
        if needle in haystack:
            tap(node["cx"], node["cy"])
            label = node["text"] or node["desc"] or node["resource_id"]
            print(f"命中节点：{label} -> 点击中心 ({node['cx']}, {node['cy']})")
            return
    names = [f'{n["text"] or n["desc"]}' for n in nodes if (n["text"] or n["desc"])]
    raise StepError(
        f"界面里找不到包含 {needle!r} 的节点。\n"
        f"  当前可见文本（前 30 个）：{', '.join(names[:30]) or '（空）'}"
    )


# ---------------------------------------------------------------- 隐私审计


def find_build_tool(executable: str) -> Path | None:
    build_tools = ANDROID_SDK / "build-tools"
    if not build_tools.exists():
        return None

    def version_key(path: Path):
        return [int(part) for part in re.findall(r"\d+", path.name)] or [0]

    for directory in sorted(build_tools.iterdir(), key=version_key, reverse=True):
        candidate = directory / executable
        if candidate.exists():
            return candidate
    return None


def scan_bytes(data: bytes, label: str, hits: dict, warns: dict,
               stats: dict) -> None:
    """在一段字节里找硬断言/警示词，记录容器、偏移和上下文。

    hits/warns 结构：{密钥: {"count": 命中数, "hits": ["容器 @ 偏移: 上下文", ...]}}
    """
    stats["scanned"] += 1
    stats["bytes"] += len(data)
    lowered = data.lower()
    for secret in SECRETS_HARD + SECRETS_WARN:
        key = secret.lower().encode("utf-8", "replace")
        offset = lowered.find(key)
        if offset < 0:
            continue
        bucket = hits if secret in SECRETS_HARD else warns
        entry = bucket.setdefault(secret, {"count": 0, "hits": []})
        entry["count"] += 1
        if len(entry["hits"]) >= 6:
            continue
        start = max(0, offset - 40)
        raw = data[start:offset + len(secret) + 40]
        context = raw.decode("utf-8", "replace")
        context = re.sub(r"[^\x20-\x7E\u4e00-\u9fff]+", "·", context).strip("·")
        entry["hits"].append(f"{label} @ {offset}: …{context}…")


def scan_apk(apk: Path, hits: dict, warns: dict, stats: dict) -> None:
    with zipfile.ZipFile(apk) as archive:
        for info in archive.infolist():
            name = info.filename
            if info.is_dir():
                continue
            interesting = (
                name.startswith("assets/")
                or re.fullmatch(r"classes\d*\.dex", name) is not None
                or name.startswith("res/raw/")
            )
            if not interesting or info.file_size > 120 * 1024 * 1024:
                continue
            stats["entries"] += 1
            data = archive.read(name)
            scan_bytes(data, name, hits, warns, stats)
            # Chaquopy 把 Python 源码打进 assets/chaquopy/app.imy（本身是 zip），要进去扫
            if name.endswith(".imy") and "app.imy" in name:
                try:
                    inner = zipfile.ZipFile(io.BytesIO(data))
                except zipfile.BadZipFile:
                    continue
                with inner:
                    for item in inner.infolist():
                        if item.is_dir() or item.file_size > 20 * 1024 * 1024:
                            continue
                        scan_bytes(inner.read(item), f"{name}!{item.filename}",
                                   hits, warns, stats)


def scan_python_sources(hits: dict, warns: dict, stats: dict) -> int:
    py_dir = ROOT / "app" / "src" / "main" / "python"
    count = 0
    if not py_dir.exists():
        return 0
    for path in sorted(py_dir.rglob("*.py")):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        count += 1
        scan_bytes(data, path.relative_to(ROOT).as_posix(), hits, warns, stats)
    return count


def apk_meta(apk: Path) -> dict:
    meta = {"package": "", "versionName": "", "versionCode": "",
            "minSdk": "", "targetSdk": "", "signer": "", "signature": "",
            "schemes": "", "note": ""}
    aapt2 = find_build_tool("aapt2.exe")
    if aapt2:
        proc = run([aapt2, "dump", "badging", apk], capture=True, quiet=True,
                   check=False, timeout=120)
        text = proc.stdout or ""
        match = re.search(
            r"package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'",
            text,
        )
        if match:
            meta["package"], meta["versionCode"], meta["versionName"] = match.groups()
        match = re.search(r"sdkVersion:'(\d+)'", text)
        if match:
            meta["minSdk"] = match.group(1)
        match = re.search(r"targetSdkVersion:'(\d+)'", text)
        if match:
            meta["targetSdk"] = match.group(1)
    else:
        meta["note"] += "没找到 aapt2，包信息回退到 app/build.gradle；"
    if not meta["package"]:
        meta["package"] = PKG
        meta["versionName"] = meta["versionName"] or gradle_version_name()
        meta["versionCode"] = meta["versionCode"] or gradle_version_code()
        meta["note"] += "版本号取自 app/build.gradle；"

    apksigner = find_build_tool("apksigner.bat")
    if apksigner:
        proc = run([apksigner, "verify", "--print-certs", apk], capture=True,
                   env=java_env(), quiet=True, check=False, timeout=180)
        text = (proc.stdout or "") + (proc.stderr or "")
        match = re.search(r"Signer #1 certificate DN: (.+)", text)
        if match:
            meta["signer"] = match.group(1).strip()
        match = re.search(r"Signer #1 certificate SHA-256 digest: (\w+)", text)
        if match:
            meta["signature"] = match.group(1)
        schemes = re.findall(r"Verified using (v\d) scheme .*: (true|false)", text)
        meta["schemes"] = ", ".join(f"{name}={'✓' if ok == 'true' else '✗'}"
                                   for name, ok in schemes)
        if proc.returncode != 0 and not meta["signer"]:
            meta["note"] += f"apksigner 校验未通过（退出码 {proc.returncode}）；"
    else:
        meta["note"] += "没找到 apksigner，签名信息不可用；"
    return meta


def audit(apk_paths: list[Path]) -> int:
    print(f"== 隐私审计（{len(apk_paths)} 个 APK）==")
    print(f"断言：不含 {' / '.join(SECRETS_HARD)}")
    print(f"警示：出现 {' / '.join(SECRETS_WARN)} 会报告（不判失败，字段名正常）")
    failed = False
    for apk in apk_paths:
        hits: dict = {}
        warns: dict = {}
        stats = {"entries": 0, "scanned": 0, "bytes": 0}
        print(f"\n--- {apk.name} ---")
        print(f"路径  : {apk}")
        print(f"大小  : {human_size(apk.stat().st_size)}")
        print(f"SHA256: {sha256_file(apk)}")

        meta = apk_meta(apk)
        print(f"包名  : {meta['package']}")
        print(f"版本  : {meta['versionName']}（versionCode {meta['versionCode']}）"
              f"  minSdk={meta['minSdk'] or '?'}  targetSdk={meta['targetSdk'] or '?'}")
        if meta["signer"]:
            print(f"签名  : {meta['signer']}")
            print(f"        SHA-256 {meta['signature']}  {meta['schemes']}")
        else:
            print("签名  : 未取得（apksigner 缺失或校验失败）")
        if meta["note"]:
            print(f"备注  : {meta['note'].rstrip('；')}")

        scan_apk(apk, hits, warns, stats)
        py_count = scan_python_sources(hits, warns, stats)
        print(f"扫描  : {stats['entries']} 个 APK 条目 / {py_count} 个 .py 源码，"
              f"共 {stats['bytes'] / 1024 / 1024:.1f} MB")

        if hits:
            failed = True
            print("结果  : 失败 ✗ 命中账号信息")
            for secret, entry in hits.items():
                print(f"  命中 {secret}（共 {entry['count']} 处，显示前 {len(entry['hits'])} 处）：")
                for line in entry["hits"]:
                    print(f"    {line}")
        else:
            print("结果  : 通过 ✓ 没有 linghhhhq / E9qf5ueI")
        if warns:
            for secret, entry in warns.items():
                print(f"  警示：出现 {secret} 字样 {entry['count']} 处"
                      "（多半是字段名/普通文本，需人工看一眼）")
                for line in entry["hits"][:3]:
                    print(f"    {line}")
    print("\n审计结论：" + ("失败 ✗" if failed else "通过 ✓"))
    return 1 if failed else 0


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_android.py",
        description="呆猫漫画安卓端构建 / 安装 / 端口 / 接口 / 截图 / 隐私审计。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用组合：\n"
            "  python tools/verify_android.py --build\n"
            "  python tools/verify_android.py --install --port --api /api/health --shot old-app\n"
            "  python tools/verify_android.py --install --fresh --launch --timeout 240\n"
            "  python tools/verify_android.py --api /api/shelf\n"
            "  python tools/verify_android.py --ui\n"
            "  python tools/verify_android.py --tap-text 书架\n"
            "  python tools/verify_android.py --audit\n"
            "  python tools/verify_android.py --logcat 150\n"
        ),
    )
    parser.add_argument("--sync", action="store_true",
                        help="只跑 tools/sync_app.py（v3 -> 安卓工程）")
    parser.add_argument("--build", action="store_true",
                        help="sync + gradle assembleRelease --rerun-tasks，产物复制到 dist/")
    parser.add_argument("--install", action="store_true",
                        help="安装 APK（默认最新 dist/*.apk）并启动等待服务就绪")
    parser.add_argument("--fresh", action="store_true",
                        help="配合 --install：先 adb uninstall 清数据再装")
    parser.add_argument("--launch", action="store_true",
                        help="启动 com.dumbcat.manga 并等待内嵌服务就绪")

    parser.add_argument("--port", action="store_true",
                        help="发现服务端口 + adb forward tcp:18080 + 健康检查")
    parser.add_argument("--api", metavar="PATH",
                        help="请求 http://127.0.0.1:18080<PATH>，自动带 token")
    parser.add_argument("--shot", metavar="NAME",
                        help="截图到 .scratch/shots/<NAME>.png")
    parser.add_argument("--tap", nargs=2, type=int, metavar=("X", "Y"),
                        help="adb shell input tap X Y")
    parser.add_argument("--swipe", nargs=4, type=int,
                        metavar=("X1", "Y1", "X2", "Y2"),
                        help="adb shell input swipe x1 y1 x2 y2")
    parser.add_argument("--swipe-ms", type=int, default=300,
                        help="滑动时长毫秒（默认 300）")
    parser.add_argument("--ui", action="store_true",
                        help="uiautomator dump，打印可点节点并存 .scratch/ui.xml")
    parser.add_argument("--tap-text", metavar="TEXT",
                        help="在界面里找包含 TEXT 的节点并点它（抽屉/底部 tab 用）")

    parser.add_argument("--audit", action="store_true",
                        help="扫 dist/*.apk（或 --apk）隐私信息 + 包信息/签名")
    parser.add_argument("--apk", metavar="PATH",
                        help="指定 APK（默认 dist/ 里最新的；--audit 时只审这个）")
    parser.add_argument("--serial", default=SERIAL,
                        help=f"adb 设备序列号（默认 {SERIAL}）")
    parser.add_argument("--timeout", type=int, default=150,
                        help="等待服务就绪的秒数（默认 150，首次启动可给 240）")
    parser.add_argument("--logcat", type=int, metavar="N",
                        help="打印最近 N 行应用相关 logcat 后退出")
    return parser


def main() -> int:
    global SERIAL
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args()
    SERIAL = args.serial

    if not ADB.exists():
        print(f"！找不到 adb：{ADB}", flush=True)

    did = False
    try:
        if args.fresh and not args.install:
            raise StepError("--fresh 只能和 --install 一起用")

        if args.sync:
            do_sync()
            did = True
        if args.build:
            do_build()
            did = True
        if args.install:
            apk = resolve_apk(args.apk)
            install(apk, fresh=args.fresh)
            launch_and_wait(args.timeout)
            did = True
        if args.launch and not args.install:
            launch_and_wait(args.timeout)
            did = True
        if args.port:
            discover_port()
            did = True
        if args.api:
            api(args.api)
            did = True
        if args.shot:
            shot(args.shot)
            did = True
        if args.tap:
            tap(*args.tap)
            did = True
        if args.swipe:
            swipe(*args.swipe, args.swipe_ms)
            did = True
        if args.ui:
            dump_ui()
            did = True
        if args.tap_text:
            tap_text(args.tap_text)
            did = True
        if args.audit:
            if args.apk:
                paths = [resolve_apk(args.apk)]
            else:
                paths = sorted(DIST.glob("*.apk"))
            if not paths:
                raise StepError("dist/ 里没有 APK；先用 --build 构建，或 --apk 指定")
            code = audit(paths)
            did = True
            if code:
                return code
        if args.logcat:
            logcat_tail(args.logcat)
            did = True

        if not did:
            parser.print_help()
        return 0
    except StepError as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
