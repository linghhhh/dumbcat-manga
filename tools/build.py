"""一键打包：可选同步源码 → 可选改版本号 → 构建 release APK → 放到 dist/。

用法：
    python tools/build.py                    # 用当前版本号打一次
    python tools/build.py --version 0.1.1    # 升版本号（versionCode 自动 +1）再打包

依赖：JDK 17+（设 JAVA_HOME，或 PATH 里有 java）、Android SDK（local.properties
里的 sdk.dir）、Python 3.10（Chaquopy 编译 Python 用）。

签名：口令从 local.properties / 环境变量读取，见 app/build.gradle。没有 keystore
也能构建，只是产出的 APK 未签名。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_GRADLE = ROOT / "app" / "build.gradle"
APK = ROOT / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
DIST = ROOT / "dist"
#: 仓库自带的 Gradle Wrapper；Windows 上要用 .bat
GRADLEW = ROOT / ("gradlew.bat" if os.name == "nt" else "gradlew")
#: 桌面版源码目录（可选）。开源仓库里 app/src/main/python 已经是独立可构建的
#: 源码副本，没有这个目录时跳过同步，直接用工程里的代码构建。
DESKTOP_SRC = ROOT.parent / "呆猫漫画v3"


def gradle_command() -> list[str]:
    if GRADLEW.exists():
        return [str(GRADLEW)]
    gradle = shutil.which("gradle")
    if gradle:
        return [gradle]
    sys.exit("找不到 Gradle：请用仓库自带的 ./gradlew，或把 gradle 加进 PATH")


def build_env() -> dict[str, str]:
    env = dict(os.environ)
    if not env.get("JAVA_HOME"):
        java = shutil.which("java")
        if java:
            # <jdk>/bin/java(.exe) → <jdk>
            candidate = Path(java).resolve().parent.parent
            if (candidate / "bin").is_dir():
                env["JAVA_HOME"] = str(candidate)
    return env


def bump_version(new_version: str) -> None:
    text = BUILD_GRADLE.read_text(encoding="utf-8")
    m_code = re.search(r"versionCode\s+(\d+)", text)
    m_name = re.search(r'versionName\s+"([^"]+)"', text)
    if not m_code or not m_name:
        sys.exit("app/build.gradle 里找不到 versionCode / versionName")
    text = text.replace(m_code.group(0), f"versionCode {int(m_code.group(1)) + 1}", 1)
    text = text.replace(m_name.group(0), f'versionName "{new_version}"', 1)
    BUILD_GRADLE.write_text(text, encoding="utf-8")
    print(f"版本号：{m_name.group(1)} -> {new_version}（versionCode {m_code.group(1)} -> "
          f"{int(m_code.group(1)) + 1}）")


def current_version() -> str:
    m = re.search(r'versionName\s+"([^"]+)"', BUILD_GRADLE.read_text(encoding="utf-8"))
    return m.group(1) if m else "unknown"


def run(cmd: list[str], cwd: Path) -> None:
    print("$", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(cwd), env=build_env())
    if result.returncode != 0:
        sys.exit(f"命令失败（{result.returncode}）：{' '.join(cmd)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="新的 versionName，例如 0.1.1")
    args = parser.parse_args()

    if args.version:
        bump_version(args.version)

    print("== 1/3 同步桌面版源码（可选）==")
    if DESKTOP_SRC.is_dir():
        run([sys.executable, str(ROOT / "tools" / "sync_app.py")], ROOT)
    else:
        print(f"没有 {DESKTOP_SRC}，跳过同步，直接用工程内现有源码构建")

    print("== 2/3 构建 release APK ==")
    # Chaquopy 不会因为 Python 源码变化就重打包 app.imy，必须 --rerun-tasks
    run(gradle_command() + ["assembleRelease", "--console=plain", "--rerun-tasks"], ROOT)

    print("== 3/3 复制到 dist/ ==")
    if not APK.exists():
        sys.exit(f"没有生成 APK：{APK}")
    DIST.mkdir(exist_ok=True)
    target = DIST / f"呆猫漫画_{current_version()}.apk"
    shutil.copy2(APK, target)
    print(f"\n完成：{target}\n大小：{target.stat().st_size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
