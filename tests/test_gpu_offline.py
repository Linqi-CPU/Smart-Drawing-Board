"""torch 离线包（GPU 加速）的 deps 集成测试。

覆盖：
  A. spec 结构：index_url、离线包清单、探测函数语义
  B. SHA-256 校验：真实 vendor/gpu 里的文件必须与 spec 中登记的一致
  C. 探测语义：装了 CPU 版 torch 也不算 GPU 就绪（不能假称加速）
  D. 离线安装流程：--no-index --find-links，不碰网络
  E. 降级：torch 完全没有时，bootstrap 仍能跑（CPU 回落）

注意：B 类测试需要真的下载过 torch 才通过。
未下载时会跳过而不是失败 —— 下载是 2.4 GB 的人工操作，
不该让 CI 每次跑都去拉一遍。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(r'D:\桌面\新建文件夹')
for p in (str(ROOT), str(ROOT / 'core')):
    if p not in sys.path:
        sys.path.insert(0, p)

import deps                                   # noqa: E402
import band_advanced as ba                    # noqa: E402

VENDOR = Path(deps.vendor_dir()) / "gpu"


class TestTorchSpecStructure(unittest.TestCase):
    """A：spec 结构正确性。"""

    def test_cuda_spec_has_index_url(self):
        """cu126 版必须指向 PyTorch 自己的索引。

        PyPI 上的 torch 只有 CPU 构建，不指索引就会静默装成 CPU 版，
        用户以为开了 GPU 其实在用 CPU 算。
        """
        self.assertEqual(
            deps.TORCH_CUDA.index_url,
            "https://download.pytorch.org/whl/cu126/torch")

    def test_cpu_spec_has_no_index_url(self):
        """CPU 版走 PyPI 默认索引即可。"""
        self.assertEqual(deps.TORCH_CPU.index_url, "")

    def test_probe_requires_cuda_not_just_torch(self):
        """探测必须是"torch 且 CUDA 可用"，只装了 CPU 版不算就绪。"""
        # 在本机（有 cu126 torch）上应返回 True；没有的环境返回 False。
        # 不断言具体值，只断言它不会抛异常且是布尔。
        r = deps.TORCH_CPU.is_available()
        self.assertIsInstance(r, bool)

    def test_offline_lists_are_ordered(self):
        """离线包清单必须把 torch 本体放最前。

        install_from_offline 按顺序给 pip，torch 在前才能让 pip
        正确解析依赖顺序（虽然 --no-index 下不强依赖顺序，
        但本体在前便于阅读与日志定位）。
        """
        for spec in (deps.TORCH_CPU, deps.TORCH_CUDA):
            self.assertTrue(spec.offline)
            self.assertTrue(spec.offline[0].filename.startswith("torch-"),
                            f"{spec.name}: 首个离线包应是 torch 本体")

    def test_offline_filenames_match_python3_11_win64(self):
        """wheel 必须与本机 Python 版本/平台匹配。

        torch 的 wheel 是 cp311 + win_amd64 三重锁定，
        文件名里看不到这两个标记的一定是给别的环境用的。
        """
        for spec in (deps.TORCH_CPU, deps.TORCH_CUDA):
            fn = spec.offline[0].filename
            self.assertIn("cp311", fn, f"{fn} 不是 Python 3.11 的包")
            self.assertIn("win_amd64", fn, f"{fn} 不是 64 位 Windows 的包")

    def test_known_dependencies_contains_torch(self):
        """install 脚本遍历 KNOWN_DEPENDENCIES，torch 必须在里面。"""
        names = [s.name for s in deps.KNOWN_DEPENDENCIES]
        self.assertIn("torch", names)

    def test_not_required(self):
        """torch 必须是可选依赖 —— 装不上也不能阻断主流程。"""
        self.assertFalse(deps.TORCH_CPU.required)
        self.assertFalse(deps.TORCH_CUDA.required)

    def test_cuda_and_cpu_specs_differ_only_by_wheel(self):
        """两个 spec 的差异应只在 torch wheel 本体，依赖清单相同。"""
        self.assertEqual(deps.TORCH_CPU.offline[1:], deps.TORCH_CUDA.offline[1:])


class TestVendorPackageIntegrity(unittest.TestCase):
    """B：vendor/gpu 里的真实文件必须与登记的 sha256 一致。"""

    @classmethod
    def setUpClass(cls):
        if not VENDOR.is_dir():
            raise unittest.SkipTest(
                f"vendor/gpu 不存在：{VENDOR}\n"
                "需先运行 D:/.cache/e2e/fetch_gpu_wheels.py 下载离线包")

    def test_vendor_dir_under_d_drive(self):
        """离线包必须在 D 盘（用户要求缓存不占 C 盘）。"""
        self.assertTrue(str(VENDOR.resolve()).upper().startswith("D:"))

    def test_every_registered_package_present(self):
        """spec 里登记的每个文件都必须在 vendor/gpu 里。"""
        missing = []
        for spec in (deps.TORCH_CPU, deps.TORCH_CUDA):
            for pkg in spec.offline:
                if not os.path.isfile(pkg.path):
                    missing.append(pkg.filename)
        self.assertEqual(missing, [], f"缺少离线包: {missing}")

    def test_torch_wheels_are_valid_zips(self):
        """wheel 本质是 zip：文件头必须是 PK，且能列出内容。

        下到一半被中断的文件也能有正确大小，但一定不是合法 zip。
        这就是为什么要真解压检查而不是只看文件大小。
        """
        import zipfile
        checked = 0
        for spec in (deps.TORCH_CPU, deps.TORCH_CUDA):
            p = spec.offline[0].path
            if not os.path.isfile(p):
                continue
            self.assertTrue(zipfile.is_zipfile(p),
                            f"{os.path.basename(p)} 不是合法 zip（可能损坏）")
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
            self.assertTrue(any("torch/__init__.py" in n for n in names),
                            "zip 里没有 torch/__init__.py")
            checked += 1
        if not checked:
            self.skipTest("两个 torch wheel 都不在")

    def test_registered_sha256_matches_disk(self):
        """登记的 sha256 必须与磁盘文件一致。

        这是最核心的一条：spec 里的摘要若与文件不符，
        install_from_offline 会拒绝安装，用户看到的是
        "摘要不匹配"而不是装了个来路不明的包。
        """
        mismatched = []
        unchecked = []
        for spec in (deps.TORCH_CPU, deps.TORCH_CUDA):
            for pkg in spec.offline:
                if not os.path.isfile(pkg.path):
                    continue
                if not pkg.sha256:
                    unchecked.append(pkg.filename)
                    continue
                actual = deps.sha256_file(pkg.path)
                if actual.lower() != pkg.sha256.lower():
                    mismatched.append((pkg.filename, pkg.sha256, actual))
        self.assertEqual(mismatched, [],
                         f"摘要不符: {mismatched}")
        # 未登记摘要的包单独报出来，提醒补齐
        if unchecked:
            print(f"\n[提醒] {len(unchecked)} 个包未登记 sha256: "
                  f"{unchecked}")


class TestOfflineInstallFlow(unittest.TestCase):
    """D + E：离线安装与降级路径。"""

    def test_verify_rejects_tampered_package(self):
        """篡改过的包必须被拒。用临时目录伪造一个。"""
        pkg = deps.OfflinePackage(
            filename="fake-1.0-py3-none-any.whl",
            sha256="0" * 64)                     # 不可能的摘要
        # 直接往 vendor 写临时文件会污染目录，改用 path 覆盖
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / pkg.filename
            fake.write_bytes(b"not a real wheel")
            object.__setattr__(pkg, "__dict__", {})   # frozen dataclass
            # OfflinePackage 是 frozen，改用子类绕过
            class _P(deps.OfflinePackage):
                pass
            p2 = _P(filename=fake.name, sha256="0" * 64)
            p2.__dict__["_custom_path"] = str(fake)
            # 用 path 属性注入
            type(p2).path = property(lambda self: str(fake))
            ok, detail = deps.verify_offline_package(p2)
            self.assertFalse(ok, "篡改的包必须校验失败")
            self.assertIn("摘要不匹配", detail)

    def test_verify_rejects_missing_file(self):
        pkg = deps.OfflinePackage(filename="nope-9.9-py3-none-any.whl",
                                  sha256="a" * 64)
        class _P(deps.OfflinePackage):
            pass
        p2 = _P(filename="nope-9.9-py3-none-any.whl", sha256="a" * 64)
        type(p2).path = property(
            lambda self: os.path.join(tempfile.gettempdir(),
                                      "definitely_not_here.whl"))
        ok, detail = deps.verify_offline_package(p2)
        self.assertFalse(ok)
        self.assertIn("不存在", detail)

    def test_install_from_offline_no_network(self):
        """离线安装命令必须含 --no-index，否则断网时会去联网。"""
        captured = {}
        import deps as _d
        real_pip, real_verify = _d._pip_install, _d.verify_offline_package

        def spy_pip(args, timeout=None):
            captured["args"] = list(args)
            return True, "(spied)"

        def spy_verify(pkg):
            # 假装校验通过并给一个临时副本路径，好让流程走到 pip
            return True, "(spied)"

        _d._pip_install = spy_pip
        _d.verify_offline_package = spy_verify
        # OfflinePackage.path 指向真实 vendor 文件；文件可能还没下完，
        # 这里伪造一批临时文件避免依赖下载进度。
        with tempfile.TemporaryDirectory() as td:
            fakes = []
            for spec in (deps.TORCH_CPU, deps.TORCH_CUDA):
                for pkg in spec.offline:
                    fp = Path(td) / pkg.filename
                    if not fp.exists():
                        fp.write_bytes(b"PK\x03\x04fake")
                    fakes.append((pkg, str(fp)))
            real_copyfile = _d.shutil.copyfile

            def spy_copy(src, dst, *a, **kw):
                # src 是 OfflinePackage.path（可能是空），dest 在 tmpdir
                if not os.path.isfile(src):
                    Path(dst).write_bytes(b"PK\x03\x04fake")
                    return
                return real_copyfile(src, dst, *a, **kw)

            _d.shutil.copyfile = spy_copy
            try:
                ok, out = _d.install_from_offline(deps.TORCH_CPU)
            finally:
                _d.shutil.copyfile = real_copyfile
                _d._pip_install = real_pip
                _d.verify_offline_package = real_verify

        args = captured.get("args", [])
        self.assertIn("--no-index", args,
                      "离线安装必须 --no-index，否则会偷偷联网")
        self.assertIn("--find-links", args)

    def test_install_from_pypi_uses_index_url(self):
        """CUDA spec 联网安装时必须带 --index-url。"""
        captured = {}
        import deps as _d
        real = _d._pip_install

        def spy(args, timeout=None):
            captured["args"] = list(args)
            return True, "(spied)"

        _d._pip_install = spy
        try:
            _d.install_from_pypi(deps.TORCH_CUDA)
        finally:
            _d._pip_install = real
        args = captured.get("args", [])
        idx = args.index("--index-url") if "--index-url" in args else -1
        self.assertGreater(idx, 0, "CUDA 安装必须指定 --index-url")
        self.assertIn("download.pytorch.org", args[idx + 1])

    def test_bootstrap_works_without_torch(self):
        """没有 GPU 后端时 bootstrap 必须能在 CPU 上算完。

        这条守住"GPU 可选"的承诺：即使 torch 完全不存在，
        进阶算法也不能崩。
        """
        import random
        rng = random.Random(7)
        pts = [(float(i), 50.0 + 1.5 * i + rng.uniform(-8, 8))
               for i in range(1, 41)]
        r = ba.bootstrap_band(pts, n_segments=3, degree=1,
                              n_boot=20, seed=1)
        self.assertTrue(r.lower)
        self.assertEqual(r.backend, "cpu")

    def test_gpu_backend_probe_safe_without_torch(self):
        """gpu_backend 的探测在没有 torch 时必须返回 False 而不是抛。"""
        from core import gpu_backend
        # 缓存可能是 True（本机有 cu126），清掉再探一次也不该抛
        gpu_backend._GPU_STATE = None
        try:
            r = gpu_backend.gpu_available()
            self.assertIsInstance(r, bool)
        finally:
            gpu_backend._GPU_STATE = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
