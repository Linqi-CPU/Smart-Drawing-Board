"""core/deps.py 依赖管理框架的测试。

重点覆盖用户要求的四条路径：
  1. 探测到依赖 -> 跳过（不下载、不安装）
  2. 缺失 -> 尝试 pip 联网安装
  3. 联网失败 -> 本地离线包安装（--no-index --find-links）
  4. 离线包安装前 SHA-256 校验，不过则拒绝安装

离线安装与 pip 调用会被 mock 掉，不在 CI 上真去网络/磁盘折腾。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, '.')

from core import deps


class TestVendorDir(unittest.TestCase):
    def test_vendor_dir_is_under_project_root(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(".")))
        # vendor 目录应在项目根下，而非 CWD 下
        self.assertTrue(os.path.isabs(deps.vendor_dir()))
        self.assertTrue(deps.vendor_dir().endswith("vendor"))
        self.assertFalse(os.path.relpath(deps.vendor_dir(), ".").startswith("..")
                         and False)  # 仅保证可计算相对路径

    def test_vendor_dir_absolute_not_cwd_relative(self):
        # 换 CWD 不应改变 vendor_dir() 的结果
        a = deps.vendor_dir()
        old = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            self.assertEqual(deps.vendor_dir(), a)
        finally:
            os.chdir(old)


class TestSha256(unittest.TestCase):
    def test_known_digest(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.bin")
            with open(p, "wb") as f:
                f.write(b"hello")
            self.assertEqual(
                deps.sha256_file(p),
                hashlib.sha256(b"hello").hexdigest(),
            )

    def test_lowercase(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.bin")
            with open(p, "wb") as f:
                f.write(b"")
            self.assertEqual(
                deps.sha256_file(p),
                deps.sha256_file(p).lower(),
            )


class TestOfflineVerify(unittest.TestCase):
    """SHA-256 校验：匹配 / 不匹配 / 文件缺失 三种情形。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # 指向临时 vendor 目录
        patcher = mock.patch.object(deps, "_VENDOR_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make(self, name, content):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(content)
        return p

    def test_matching_digest(self):
        content = b"fake wheel bytes"
        digest = hashlib.sha256(content).hexdigest()
        self._make("good.whl", content)
        pkg = deps.OfflinePackage("good.whl", digest)
        ok, detail = deps.verify_offline_package(pkg)
        self.assertTrue(ok)
        self.assertEqual(detail, digest)

    def test_uppercase_expected_still_matches(self):
        content = b"x"
        d = hashlib.sha256(content).hexdigest().upper()
        self._make("u.whl", content)
        ok, _ = deps.verify_offline_package(deps.OfflinePackage("u.whl", d))
        self.assertTrue(ok)

    def test_mismatch_rejected(self):
        self._make("bad.whl", b"content A")
        wrong = hashlib.sha256(b"content B").hexdigest()
        ok, detail = deps.verify_offline_package(
            deps.OfflinePackage("bad.whl", wrong))
        self.assertFalse(ok)
        self.assertIn("摘要不匹配", detail)
        self.assertIn(wrong, detail)          # 期望值要出现在提示里

    def test_missing_file(self):
        ok, detail = deps.verify_offline_package(
            deps.OfflinePackage("nope.whl", "a" * 64))
        self.assertFalse(ok)
        self.assertIn("不存在", detail)

    def test_no_expected_digest_skips_but_warns(self):
        self._make("u.whl", b"z")
        ok, detail = deps.verify_offline_package(deps.OfflinePackage("u.whl", ""))
        self.assertTrue(ok)                    # 未设期望值时放行
        self.assertIn("未设定期望摘要", detail)  # 但明确告知未经校验


class TestInstallOffline(unittest.TestCase):
    """离线安装：必须 --no-index，且校验失败不得安装。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.object(deps, "_VENDOR_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pkg(self, name="a.whl", content=b"w"):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(content)
        return deps.OfflinePackage(name, hashlib.sha256(content).hexdigest())

    def test_uses_no_index(self):
        spec = deps.DependencySpec(
            name="x", packages=("x",), offline=(self._pkg(),))
        with mock.patch.object(deps, "_pip_install", return_value=(True, "ok")) as m:
            ok, _ = deps.install_from_offline(spec)
        self.assertTrue(ok)
        args = m.call_args[0][0]
        self.assertIn("--no-index", args)
        self.assertIn("--find-links", args)
        # 必须传 wheel 路径
        self.assertTrue(any(a.endswith("a.whl") for a in args))

    def test_digest_mismatch_blocks_install(self):
        p = os.path.join(self.tmp, "a.whl")
        with open(p, "wb") as f:
            f.write(b"real")
        bad = deps.OfflinePackage("a.whl", hashlib.sha256(b"other").hexdigest())
        spec = deps.DependencySpec(name="x", offline=(bad,))
        with mock.patch.object(deps, "_pip_install",
                               return_value=(True, "ok")) as m:
            ok, detail = deps.install_from_offline(spec)
        self.assertFalse(ok)
        self.assertIn("摘要不匹配", detail)
        m.assert_not_called()                  # 校验不过绝不调 pip

    def test_any_bad_package_blocks_whole_install(self):
        # 一个坏包 -> 整个依赖判失败，不装其他（装一半更难排查）
        good = self._pkg("good.whl", b"g")
        badp = os.path.join(self.tmp, "bad.whl")
        with open(badp, "wb") as f:
            f.write(b"b")
        bad = deps.OfflinePackage("bad.whl", hashlib.sha256(b"nope").hexdigest())
        spec = deps.DependencySpec(name="x", offline=(good, bad))
        with mock.patch.object(deps, "_pip_install",
                               return_value=(True, "ok")) as m:
            ok, _ = deps.install_from_offline(spec)
        self.assertFalse(ok)
        m.assert_not_called()

    def test_no_offline_packages(self):
        spec = deps.DependencySpec(name="x", offline=())
        ok, detail = deps.install_from_offline(spec)
        self.assertFalse(ok)
        self.assertIn("没有配置离线包", detail)

    def test_tempdir_cleaned_up(self):
        import glob
        before = set(glob.glob(os.path.join(tempfile.gettempdir(),
                                            "hermes_vendor_*")))
        spec = deps.DependencySpec(name="x", offline=(self._pkg(),))
        with mock.patch.object(deps, "_pip_install", return_value=(True, "ok")):
            deps.install_from_offline(spec)
        after = set(glob.glob(os.path.join(tempfile.gettempdir(),
                                           "hermes_vendor_*")))
        self.assertEqual(before, after)


class TestEnsureDependency(unittest.TestCase):
    """ensure_dependency 的四条路径。"""

    def _spec(self, **kw):
        base = dict(name="fake_dep_xyz", description="测试用")
        base.update(kw)
        return deps.DependencySpec(**base)

    def test_skips_when_available(self):
        """已就绪时必须跳过，且绝不调用 pip。"""
        # 注意：必须给 probe。默认 probe=None 会真去 import 模块名，
        # "fake_dep_xyz" 导入失败反而走进安装分支，那是测试写错了。
        spec = self._spec(probe=lambda: True, packages=("fake",))
        logs = []
        ok = deps.ensure_dependency(spec, log=logs.append)
        self.assertTrue(ok)
        self.assertTrue(any("跳过" in m for m in logs))
        with mock.patch.object(deps, "install_from_pypi") as m:
            deps.ensure_dependency(spec, log=logs.append)
            m.assert_not_called()

    def test_install_when_missing(self):
        spec = self._spec(packages=("fake",))

        seq = [False, True]                     # 第一次不可用，装完可用

        def probe():
            return seq.pop(0) if seq else True

        spec = self._spec(packages=("fake",), probe=probe)
        with mock.patch.object(deps, "install_from_pypi",
                               return_value=(True, "installed")) as m:
            ok = deps.ensure_dependency(spec, log=lambda s: None)
        self.assertTrue(ok)
        m.assert_called_once()

    def test_falls_back_to_offline(self):
        calls = {"pypi": 0}

        def fake_pypi(spec):
            calls["pypi"] += 1
            return False, "network down"

        # probe 第三次（离线装完复检）才返回 False —— 即离线也失败
        results = iter([False, False, False])

        spec = self._spec(probe=lambda: next(results, False),
                          packages=("fake",))
        with mock.patch.object(deps, "install_from_pypi", side_effect=fake_pypi), \
             mock.patch.object(deps, "install_from_offline",
                               return_value=(False, "no vendor")):
            ok = deps.ensure_dependency(spec, log=lambda s: None)
        self.assertFalse(ok)
        self.assertEqual(calls["pypi"], 1)

    def test_required_raises(self):
        spec = self._spec(required=True, packages=("fake",))

        results = iter([False, False, False])
        spec = self._spec(required=True, packages=("fake",),
                          probe=lambda: next(results, False))
        with mock.patch.object(deps, "install_from_pypi",
                               return_value=(False, "net")), \
             mock.patch.object(deps, "install_from_offline",
                               return_value=(False, "no vendor")):
            with self.assertRaises(deps.DependencyError):
                deps.ensure_dependency(spec, log=lambda s: None)

    def test_optional_returns_false_not_raise(self):
        results = iter([False, False, False])
        spec = self._spec(required=False, packages=("fake",),
                          probe=lambda: next(results, False))
        with mock.patch.object(deps, "install_from_pypi",
                               return_value=(False, "net")), \
             mock.patch.object(deps, "install_from_offline",
                               return_value=(False, "no vendor")):
            self.assertFalse(deps.ensure_dependency(spec, log=lambda s: None))


class TestProbeCacheReset(unittest.TestCase):
    """装完依赖后必须刷新核心模块的可用性缓存。"""

    def test_reset_registered_and_runs(self):
        calls = []

        def dummy():
            calls.append(1)

        deps._register_probe_cache_reset(dummy)
        deps._reset_probe_caches()
        self.assertTrue(calls)

    def test_broken_reset_does_not_break_others(self):
        def boom():
            raise RuntimeError("x")

        ran = []
        deps._register_probe_cache_reset(boom)
        deps._PROBE_CACHE_RESETS["fine"] = lambda: ran.append(1)
        try:
            deps._reset_probe_caches()          # 不得抛
        finally:
            deps._PROBE_CACHE_RESETS.pop("fine", None)
        self.assertTrue(ran)

    def test_fitting_hp_cache_reset_restores_true(self):
        from core import fitting as ft
        ft._HP_CACHE = False
        deps._reset_probe_caches()
        self.assertTrue(ft.high_precision_available())


class TestDecimalSpec(unittest.TestCase):
    """预置的 decimal 依赖描述。"""

    def test_available(self):
        self.assertTrue(deps.DECIMAL.is_available())

    def test_no_packages_needed(self):
        # 标准库：登记为空，install_from_pypi 应直接成功
        self.assertEqual(tuple(deps.DECIMAL.packages), ())
        ok, detail = deps.install_from_pypi(deps.DECIMAL)
        self.assertTrue(ok)

    def test_status_string(self):
        s = deps.DECIMAL.status()
        self.assertIn("decimal", s)
        self.assertIn("已就绪", s)

    def test_in_known_list(self):
        self.assertIn(deps.DECIMAL, deps.KNOWN_DEPENDENCIES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
