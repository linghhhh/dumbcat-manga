"""禁漫切条还原自测（一致性 + 性能）。

运行：
    C:/Users/linghhhh/AppData/Local/Programs/Python/Python310/python.exe tools/test_jm_scramble.py
    ... tools/test_jm_scramble.py <样本目录>
    ... tools/test_jm_scramble.py <样本目录> --reference .scratch/jm_scramble_numpy.py

样本：禁漫原图（**未还原**的 webp/jpg/png）。本工具不联网、不需要账号；样本可用
``.scratch/fetch_jm_samples.py`` 登录后抓取（账号密码只走命令行参数），或从别处拷贝。

一致性基准：旧 numpy 实现。按 ``--reference`` → ``DUMBCAT_JM_REFERENCE`` →
``.scratch/jm_scramble_numpy.py`` 的顺序查找。找到时逐张比较：detect 的条数、灰度
字节级的 band 偏移、还原像素、unscramble 输出；找不到时退化为自洽检查（往返还原、
looks_scrambled、检测稳定性），并提示如何补基准。

性能：全部样本计时；另外用真实样本合成一张 800x1200 的乱序图，测完整还原链路
（检测 + 换条 + 重编码），默认要求 ≤300ms（``--max-ms`` 可调）。

退出码：0 全过；1 有失败；2 没有样本（友好提示，不抛栈）。
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from PIL import Image, ImageChops

HERE = Path(__file__).resolve().parent
ANDROID_ROOT = HERE.parent
DEFAULT_V3 = ANDROID_ROOT.parent / "呆猫漫画v3"
DEFAULT_SAMPLES = ANDROID_ROOT / ".scratch" / "jm_samples"
DEFAULT_REFERENCE = ANDROID_ROOT / ".scratch" / "jm_scramble_numpy.py"

SAMPLE_EXTS = {".webp", ".jpg", ".jpeg", ".png", ".bmp"}

#: 在子进程里屏蔽 numpy，验证 jm_scramble 不依赖它（安卓 Chaquopy 环境就是这样）。
_NO_NUMPY_CODE = r"""
import importlib.abc
import sys


class _BlockNumpy(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "numpy" or name.startswith("numpy."):
            raise ImportError("numpy is blocked by test_jm_scramble")


sys.meta_path.insert(0, _BlockNumpy())
sys.path.insert(0, sys.argv[1])

import dumbcat.sources.jm_scramble as m
from PIL import Image

assert "numpy" not in sys.modules, "jm_scramble 间接导入了 numpy"
img = Image.open(sys.argv[2])
img.load()
bands = m.detect_bands(img)
with open(sys.argv[2], "rb") as f:
    raw = f.read()
looks = m.looks_scrambled(raw)
print("OK", bands, looks)
"""


def _fail(message: str) -> None:
    print(message)


def _load_new(v3_root: Path):
    if not (v3_root / "dumbcat" / "sources" / "jm_scramble.py").is_file():
        raise SystemExit(f"找不到桌面版源码：{v3_root / 'dumbcat' / 'sources' / 'jm_scramble.py'}")
    sys.path.insert(0, str(v3_root))
    from dumbcat.sources import jm_scramble as module

    return module


def _load_reference(path: Path | None):
    """加载旧 numpy 实现；返回 (module 或 None, 说明)。"""
    if path is None or not path.is_file():
        return None, "未提供（只做自洽检查）"
    try:
        import numpy  # noqa: F401
    except ImportError:
        return None, f"{path.name} 需要 numpy，当前环境没有"
    spec = importlib.util.spec_from_file_location("jm_scramble_numpy_ref", path)
    if spec is None or spec.loader is None:
        return None, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, str(path)


def _iter_samples(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in SAMPLE_EXTS)


def _band_plan(height: int, num: int) -> list[tuple[int, int, int]]:
    """(src, height, dst)：与 _decode_arrays / _decode_image 同一套公式。"""
    over = height % num
    step = height // num
    plan = []
    for i in range(num):
        band_height = step + over if i == 0 else step
        src = height - step * (i + 1) - over
        dst = 0 if i == 0 else step * i + over
        plan.append((src, band_height, dst))
    return plan


def _encode_like_scrambled(image, num: int):
    """_decode_image 的逆操作：把正常图按 num 条还原成站点那种乱序图。"""
    width, height = image.size
    out = Image.new(image.mode, (width, height))
    for src, band_height, dst in _band_plan(height, num):
        out.paste(image.crop((0, dst, width, dst + band_height)), (0, src))
    return out


def _bench(fn, repeat: int = 2) -> tuple[float, float]:
    """返回 (最快 ms, 中位 ms)。"""
    samples = []
    for _ in range(max(1, repeat)):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return min(samples), statistics.median(samples)


def _same_pixels(left: Image.Image, right: Image.Image) -> bool:
    if left.size != right.size:
        return False
    left = left.convert("RGB") if left.mode not in ("RGB", "RGBA") else left
    right = right.convert("RGB") if right.mode not in ("RGB", "RGBA") else right
    return ImageChops.difference(left, right).getbbox() is None


def _check_no_numpy(v3_root: Path, sample: Path, expected_bands: int) -> tuple[bool, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _NO_NUMPY_CODE, str(v3_root), str(sample)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    output = (proc.stdout or "").strip().splitlines()
    if proc.returncode == 0 and output:
        last = output[-1]
        expected = f"OK {expected_bands} {expected_bands}"
        if last == expected:
            return True, last
        return False, f"输出不一致：{last}（期望 {expected}）"
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, detail[-1] if detail else f"退出码 {proc.returncode}"


def _sample_row(name: str, size, bands: int, band_ok, pixel_ok, new_ms: float, ref_ms) -> str:
    ref_text = f"{ref_ms:8.1f}" if ref_ms is not None else "     n/a"
    return (
        f"{name:<34} {size[0]:>4}x{size[1]:<4} {bands:>5}  "
        f"{('是' if band_ok else '否') if band_ok is not None else '?':<8} "
        f"{('是' if pixel_ok else '否') if pixel_ok is not None else '?':<8} "
        f"{new_ms:8.1f} {ref_text}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="禁漫切条还原一致性 + 性能自测")
    parser.add_argument("samples", nargs="?", default=str(DEFAULT_SAMPLES), help="样本目录（未还原的 webp/jpg/png）")
    parser.add_argument("--v3", default=os.environ.get("DUMBCAT_V3", str(DEFAULT_V3)), help="桌面版呆猫漫画目录")
    parser.add_argument(
        "--reference",
        default=os.environ.get("DUMBCAT_JM_REFERENCE", ""),
        help="旧 numpy 实现路径（默认找 .scratch/jm_scramble_numpy.py）",
    )
    parser.add_argument("--limit", type=int, default=0, help="只测前 N 张（0 = 全部）")
    parser.add_argument("--repeat", type=int, default=2, help="计时重复次数（取中位数）")
    parser.add_argument("--max-ms", type=float, default=300.0, help="800x1200 单张耗时上限（毫秒）")
    parser.add_argument("--skip-numpy-check", action="store_true", help="跳过无 numpy 子进程验证")
    args = parser.parse_args(argv)

    v3_root = Path(args.v3)
    sample_dir = Path(args.samples)
    samples = _iter_samples(sample_dir)
    if not samples:
        _fail(f"样本目录里没有可用的图片：{sample_dir}")
        _fail("请先准备真实禁漫样本（未还原的原图，webp/jpg/png）：")
        _fail("  1) 桌面版目录下运行 .scratch/fetch_jm_samples.py（账号密码只走命令行参数）抓取；")
        _fail("  2) 或把已有的 .webp 样本拷进该目录。")
        _fail(f"然后：python {Path(__file__).name} <样本目录>")
        return 2
    if args.limit and args.limit > 0:
        samples = samples[: args.limit]

    new = _load_new(v3_root)
    if args.reference:
        reference_path = Path(args.reference)
    else:
        reference_path = DEFAULT_REFERENCE
    ref, ref_note = _load_reference(reference_path)

    print(f"新实现：{v3_root / 'dumbcat' / 'sources' / 'jm_scramble.py'}")
    print(f"Pillow：{getattr(Image, '__version__', '?')}   numpy：{'有' if ref else '无/未用'}")
    print(f"一致性基准：{ref_note}")
    if ref is None:
        print("  未找到旧 numpy 基准，只做自洽检查；把旧实现放到 .scratch/jm_scramble_numpy.py 可启用逐项对照。")
    print(f"样本：{sample_dir}（{len(samples)} 张）")
    print()
    header = (
        f"{'样本':<34} {'尺寸':<10} {'bands':>5}  "
        f"{'条数一致':<8} {'像素一致':<8} {'新(ms)':>8} {'旧(ms)':>8}"
    )
    print(header)
    print("-" * 100)

    failures: list[str] = []
    rows: list[dict] = []
    band_dist: dict[int, int] = {}
    banded_sample: Path | None = None
    banded_expected = 0

    for path in samples:
        try:
            raw = path.read_bytes()
            image = Image.open(io.BytesIO(raw))
            image.load()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path.name}: 打不开（{type(exc).__name__}: {exc}）")
            continue

        new_bands = new.detect_bands(image)
        band_dist[new_bands] = band_dist.get(new_bands, 0) + 1
        if new_bands and banded_sample is None:
            banded_sample, banded_expected = path, new_bands
        band_ok = None
        pixel_ok = None
        ref_ms = None
        ref_bands = None

        if ref is not None:
            try:
                ref_bands = ref.detect_bands(image)
                band_ok = ref_bands == new_bands
                if not band_ok:
                    failures.append(f"{path.name}: 条数不一致 新={new_bands} 旧={ref_bands}")
                elif ref_bands:
                    # 灰度字节级比较 band 偏移（_prepare 后按同一套公式搬运）
                    gray, width, height = new._prepare(image)
                    import numpy as np

                    arr = np.frombuffer(gray, dtype=np.uint8).reshape(height, width)
                    ref_bytes = ref._decode_arrays(arr, ref_bands).tobytes()
                    new_bytes = new._decode_bands(gray, width, height, new_bands)
                    if ref_bytes != new_bytes:
                        band_ok = False
                        failures.append(f"{path.name}: band 偏移字节不一致")

                ref_bytes_out = ref.unscramble(raw)
                new_bytes_out = new.unscramble(raw)
                if ref_bytes_out != new_bytes_out:
                    # 不同 Pillow 版本的编码器可能产生不同字节，像素一致即可
                    left = Image.open(io.BytesIO(ref_bytes_out)).convert("RGB")
                    right = Image.open(io.BytesIO(new_bytes_out)).convert("RGB")
                    pixel_ok = _same_pixels(left, right)
                    if not pixel_ok:
                        failures.append(f"{path.name}: 还原像素不一致")
                else:
                    pixel_ok = True
                ref_ms, _ = _bench(lambda: ref.unscramble(raw), args.repeat)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{path.name}: 与旧实现比较时出错（{type(exc).__name__}: {exc}）")
                band_ok = pixel_ok = False

        # 往返自检：还原后再按同一公式打乱，必须能逐像素还原（不依赖 numpy）
        roundtrip_ok = True
        if new_bands:
            try:
                fixed = new._decode_image(image.convert("RGB"), new_bands)
                rescrambled = _encode_like_scrambled(fixed, new_bands)
                again = new._decode_image(rescrambled, new_bands)
                roundtrip_ok = _same_pixels(fixed, again)
                if not roundtrip_ok:
                    failures.append(f"{path.name}: 往返还原像素不一致")
            except Exception as exc:  # noqa: BLE001
                roundtrip_ok = False
                failures.append(f"{path.name}: 往返还原出错（{type(exc).__name__}: {exc}）")

        looks = new.looks_scrambled(raw)
        if looks != new_bands:
            failures.append(f"{path.name}: looks_scrambled={looks} 与 detect_bands={new_bands} 不一致")

        new_ms, _ = _bench(lambda: new.unscramble(raw), args.repeat)
        rows.append(
            {
                "name": path.name,
                "size": image.size,
                "bands": new_bands,
                "band_ok": band_ok,
                "pixel_ok": pixel_ok,
                "new_ms": new_ms,
                "ref_ms": ref_ms,
                "roundtrip": roundtrip_ok,
                "looks": looks,
            }
        )
        print(_sample_row(path.name, image.size, new_bands, band_ok, pixel_ok, new_ms, ref_ms))

    # ---- 性能：真实样本合成一张 800x1200 的乱序图，走完整还原链路 ----
    print()
    print("性能（合成 800x1200 乱序图，完整检测 + 换条 + 重编码）")
    perf_source = next((p for p in samples if p.suffix.lower() in SAMPLE_EXTS), samples[0])
    source_image = Image.open(perf_source)
    source_image.load()
    source_bands = new.detect_bands(source_image) or 10
    fixed = source_image.convert("RGB").resize((800, 1200), Image.LANCZOS)
    synth = _encode_like_scrambled(fixed, source_bands)
    buf = io.BytesIO()
    synth.save(buf, "WEBP", quality=90, method=4)
    synth_bytes = buf.getvalue()

    roundtrip = new._decode_image(synth, source_bands)
    synth_ok = _same_pixels(fixed, roundtrip)
    if not synth_ok:
        failures.append("800x1200 合成样本往返还原失败")
    else:
        # 编码成 WEBP 的字节只用于性能计时（有损编码，不能再逐像素比对）
        print(f"  合成样本往返还原：逐像素一致（{source_bands} 条，800x1200）")

    auto_ms, auto_med = _bench(lambda: new.unscramble(synth_bytes), max(3, args.repeat))
    forced_ms, forced_med = _bench(
        lambda: new.unscramble(synth_bytes, num=source_bands), max(3, args.repeat)
    )
    ref_synth_ms = None
    if ref is not None:
        ref_synth_ms, _ = _bench(lambda: ref.unscramble(synth_bytes, num=source_bands), args.repeat)

    print(
        f"  自动检测 {new.detect_bands(Image.open(io.BytesIO(synth_bytes)))} 条"
        f"  自动 {auto_ms:.1f}ms（中位 {auto_med:.1f}）"
        f"  指定 {source_bands} 条 {forced_ms:.1f}ms（中位 {forced_med:.1f}）"
        + (f"  旧实现 {ref_synth_ms:.1f}ms" if ref_synth_ms is not None else "")
    )
    perf_ok = max(auto_med, forced_med) <= args.max_ms
    print(
        f"  耗时上限 {args.max_ms:.0f}ms：{'PASS' if perf_ok else 'FAIL'}"
        f"（取自动/指定两种路径的中位较大者）"
    )
    if not perf_ok:
        failures.append(f"800x1200 耗时超过 {args.max_ms:.0f}ms（中位 {max(auto_med, forced_med):.1f}ms）")

    # ---- 无 numpy 环境验证 ----
    if not args.skip_numpy_check:
        print()
        print("无 numpy 验证（子进程用 MetaPathFinder 屏蔽 numpy 后 import 并检测）")
        check_sample = banded_sample or samples[0]
        ok, detail = _check_no_numpy(v3_root, check_sample, banded_expected if banded_sample else 0)
        print(f"  {'PASS' if ok else 'FAIL'}  {detail}  （{check_sample.name}）")
        if not ok:
            failures.append(f"无 numpy 环境验证失败：{detail}")

    # ---- 汇总 ----
    print()
    total = len(rows)
    bad = len(failures)
    print(f"样本 {total} 张，bands 分布 {dict(sorted(band_dist.items()))}")
    if rows:
        new_times = [r["new_ms"] for r in rows]
        print(
            f"全部样本耗时（新）min/中位/max = {min(new_times):.1f} / "
            f"{statistics.median(new_times):.1f} / {max(new_times):.1f} ms"
        )
        if ref is not None:
            ref_times = [r["ref_ms"] for r in rows if r["ref_ms"] is not None]
            if ref_times:
                print(
                    f"全部样本耗时（旧）min/中位/max = {min(ref_times):.1f} / "
                    f"{statistics.median(ref_times):.1f} / {max(ref_times):.1f} ms"
                )
    if bad:
        print(f"FAILED {bad} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    mode = "与旧实现一致" if ref is not None else "自洽"
    suffix = " + 无 numpy" if not args.skip_numpy_check else ""
    print(f"ALL PASS ({total} 张样本{mode} + 往返 + 性能{suffix})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
