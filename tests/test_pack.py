"""release 打包测试：源码 zip 与成品 zip 必须严格分开且内容干净。

覆盖：
- 源码 zip：git archive 路径 + 无 .git 时的回退扫描路径
- 成品 zip：zip 内不含多余顶层目录（解压即见 exe）
- release/ 目录每次产出的文件列表确定，重跑会清掉过期 zip

为什么重要：v0.2.1 的源码 zip 曾被本地缓构建残留污染，
用户下载后解压出来一层 __pycache__。这些测试把"干净"钉死。
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_exe as be

EXPECTED_RELEASE = sorted(["SmartDrawingBoard-Windows.zip",
                           "SmartDrawingBoard-Source.zip"])
#: 成品 zip 固定 1 个 —— 四个 exe 打进同一个 zip，
#: 因为解压后必须同目录才能互相拉起
N_BINARY_ZIPS = 1


class PackFixture(unittest.TestCase):
    """构造一个带 git 仓库的假源码树，内含各种不该打包的噪音。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="packtest_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"

        for d in ("core", "edit", "functions", "__pycache__",
                  "build/pyi-work", "build/pyi-spec"):
            (self.src / d).mkdir(parents=True, exist_ok=True)

        (self.src / "launcher.py").write_text("# launcher\n", encoding="utf-8")
        (self.src / "core" / "server.py").write_text("# server\n", encoding="utf-8")
        (self.src / "edit" / "page.py").write_text("# page\n", encoding="utf-8")
        (self.src / "functions" / "wave_func.py").write_text("# fn\n", encoding="utf-8")

        # 这些一律不进源码 zip
        (self.src / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"junk")
        (self.src / "build" / "pyi-work" / "warn.txt").write_text("j", encoding="utf-8")
        (self.src / "nul").write_bytes(b"junk")
        (self.src / "TODO.md").write_text("local", encoding="utf-8")
        (self.src / "kernel.log").write_text("log", encoding="utf-8")

    def _git_commit_only_tracked(self):
        """只提交应该进版本库的那几个文件。"""
        for cmd in (["git", "init", "-q"],
                    ["git", "config", "user.email", "t@t"],
                    ["git", "config", "user.name", "t"]):
            subprocess.run(cmd, cwd=str(self.src), check=True)
        for f in ("launcher.py", "core/server.py",
                  "edit/page.py", "functions/wave_func.py"):
            subprocess.run(["git", "add", f], cwd=str(self.src), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "x"],
                       cwd=str(self.src), check=True)

    def _point_module_at_fake_tree(self):
        """把 build_exe 的目录常量指向假树（不碰真实仓库）。"""
        self.saved = (be._HERE, be._DIST, be._RELEASE)
        self.addCleanup(self._restore)
        be._HERE = self.src
        be._DIST = self.src / "dist"
        be._RELEASE = self.src / "release"

    def _restore(self):
        be._HERE, be._DIST, be._RELEASE = self.saved

    @staticmethod
    def _names(zip_path: Path):
        return sorted(n for n in zipfile.ZipFile(zip_path).namelist() if n)

    @staticmethod
    def _has_no_junk(names):
        return (
            not any("__pycache__" in n or n.endswith(".pyc") for n in names)
            and not any("pyi-work" in n or "pyi-spec" in n for n in names)
            and not any(n.endswith("/nul") or n == "nul" for n in names)
            and not any("TODO.md" in n or "kernel.log" in n for n in names)
        )


class TestSourceZipViaGitArchive(PackFixture):
    """有 .git 时，源码 zip 应只含被跟踪的文件。"""

    def test_clean_and_prefixed(self):
        self._git_commit_only_tracked()
        self._point_module_at_fake_tree()
        be._prepare_release_dir()

        zip_path = be._make_source_zip()
        names = self._names(zip_path)

        self.assertTrue(names, "源码 zip 不能为空")
        self.assertTrue(
            all(n.startswith("Smart-Drawing-Board-Source/") for n in names),
            "所有条目应位于统一顶层目录下",
        )
        for expect in ("Smart-Drawing-Board-Source/launcher.py",
                       "Smart-Drawing-Board-Source/core/server.py",
                       "Smart-Drawing-Board-Source/edit/page.py",
                       "Smart-Drawing-Board-Source/functions/wave_func.py"):
            self.assertIn(expect, names)
        self.assertTrue(self._has_no_junk(names), f"源码 zip 含噪音: {names}")


class TestSourceZipFallback(PackFixture):
    """从 Release 下载的源码没有 .git，必须能回退扫描并保持干净。"""

    def test_fallback_scan_is_clean(self):
        # Windows 下 .git/objects 是只读的，rmtree 会 PermissionError；
        # git archive 只判断"是不是 git 仓库"，重命名即可触发回退分支。
        self._git_commit_only_tracked()
        (self.src / ".git").rename(self.src / ".git-disabled")

        self._point_module_at_fake_tree()
        be._prepare_release_dir()

        try:
            zip_path = be._make_source_zip()
        finally:
            (self.src / ".git-disabled").rename(self.src / ".git")

        names = self._names(zip_path)
        self.assertTrue(names, "回退路径也不该产出空包")
        self.assertIn("Smart-Drawing-Board-Source/launcher.py", names)
        self.assertTrue(self._has_no_junk(names), f"回退路径含噪音: {names}")
        self.assertFalse(
            any(".git" in n for n in names),
            "git 元数据不得进源码 zip",
        )


def _make_fake_bundle() -> Path:
    """造一个合并后的 bundle 目录（构建产物应有的形态）。

    模块级函数而非测试类方法，因为 TestBinaryZips 与
    TestReleaseDirContents 都要用。故意不造四个平级目录 ——
    那是旧布局，Launcher 用它找 Page 会报「找不到入口」，
    用户已经实际踩过这个坑。
    """
    d = be._DIST / be.BUNDLE_NAME
    (d / "core").mkdir(parents=True, exist_ok=True)
    (d / "_internal").mkdir(parents=True, exist_ok=True)
    for name, _script in be.APP_ENTRIES:
        (d / f"{name}.exe").write_bytes(b"MZ fake")
    (d / "_internal" / "python311.dll").write_bytes(b"MZ dll")
    (d / "core" / "server.py").write_text("# srv", encoding="utf-8")
    return d


class TestBinaryZips(PackFixture):
    """成品 zip 必须解压即见 exe，且四个 exe 同目录。"""

    def test_no_extra_top_dir(self):
        self._point_module_at_fake_tree()
        be._prepare_release_dir()
        _make_fake_bundle()

        zips = be._make_binary_zip()
        self.assertEqual(len(zips), N_BINARY_ZIPS)

        names = self._names(zips[0])
        exes = [n for n in names if n.endswith(".exe")]
        # 四个 exe 必须都在 zip 根，不带目录前缀
        self.assertEqual(len(exes), len(be.APP_ENTRIES),
                         f"zip 里的 exe 数量不对: {exes}")
        for name, _script in be.APP_ENTRIES:
            self.assertIn(f"{name}.exe", names,
                          f"zip 里缺少 {name}.exe")
        self.assertFalse(
            any("/" in e for e in exes),
            f"exe 不应带目录前缀，否则解压后还要再进一层: {exes}",
        )

    def test_bundle_has_shared_internal(self):
        """bundle 里必须有一份共享的 _internal/。

        四个 exe 共用同一份 Python 运行时（同代码同排除名单，内容一致），
        分开带四份会让体积翻四倍且毫无收益。
        """
        self._point_module_at_fake_tree()
        be._prepare_release_dir()
        _make_fake_bundle()
        zips = be._make_binary_zip()
        names = self._names(zips[0])
        self.assertIn("_internal/python311.dll", names)
        # 只应有一个 _internal/ 顶层目录（zip 里它自己也会作为一条
        # "_internal/" 目录条目出现，所以判顶层条目而非 startswith）
        tops = {n.split("/")[0] for n in names}
        internal_tops = [t for t in tops if t == "_internal"]
        self.assertEqual(len(internal_tops), 1,
                         f"_internal/ 应只有一份: {names}")

    def test_missing_bundle_raises(self):
        self._point_module_at_fake_tree()
        be._prepare_release_dir()
        # 不造 bundle，应当立刻报错而不是产出空 zip
        with self.assertRaises(SystemExit):
            be._make_binary_zip()


class TestReleaseDirContents(PackFixture):
    """release/ 的文件列表必须确定，且重跑会清掉过期产物。"""

    def test_exact_contents_and_stale_cleanup(self):
        self._git_commit_only_tracked()
        self._point_module_at_fake_tree()
        be._prepare_release_dir()
        be._make_source_zip()
        _make_fake_bundle()
        be._make_binary_zip()

        self.assertEqual(sorted(p.name for p in be._RELEASE.iterdir()),
                         EXPECTED_RELEASE)

        # 残留 zip 必须被清掉，否则 upload 会把过期文件一起传进 Release
        (be._RELEASE / "Obsolete-Old.zip").write_bytes(b"stale")
        be._prepare_release_dir()
        self.assertFalse((be._RELEASE / "Obsolete-Old.zip").exists())
        self.assertEqual(list(be._RELEASE.iterdir()), [],
                         "_prepare_release_dir 应清空整个 release/")


    def test_all_spawn_targets_have_entries(self):
        """core/spawn.py 声明要拉起的 exe，必须都在 APP_ENTRIES 里。

        这是用户实际踩到的坑的回归守护：exe 环境下 sys.executable 是
        exe 自己而非解释器，Launcher 只能"一个 exe 起另一个 exe"。
        漏打一个入口，源码模式完全正常、CI 构建成功，
        只有用户下载解压后点对应功能才报「找不到入口」。
        """
        core_dir = Path(be.__file__).resolve().parent / "core"
        sys.path.insert(0, str(core_dir))
        try:
            import spawn as spawn_mod
            targets = {s["exe"] for s in spawn_mod.CHILD_TARGETS.values()}
        finally:
            if sys.path and sys.path[0] == str(core_dir):
                sys.path.pop(0)
        entries = {name for name, _ in be.APP_ENTRIES}
        self.assertEqual(
            targets - entries, set(),
            "APP_ENTRIES 缺少这些 exe，对应功能在 exe 里会找不到入口")

    def test_kernel_entry_exists(self):
        """内核必须是独立入口 —— 早前它不是，导致 exe 里内核起不来。"""
        names = {name for name, _ in be.APP_ENTRIES}
        self.assertIn("SmartDrawingBoard-Kernel", names)
        scripts = {name: s for name, s in be.APP_ENTRIES}
        self.assertEqual(scripts["SmartDrawingBoard-Kernel"].name, "server.py")


class TestRepoIgnoresBuildDirs(unittest.TestCase):
    """真实仓库的 build_exe 常数不能被上面的测试污染。"""

    def test_module_points_at_repo_after_tests(self):
        repo = Path(be.__file__).resolve().parent
        self.assertEqual(Path(be._HERE).resolve(), repo)
        self.assertEqual(Path(be._DIST).resolve(), repo / "dist")
        self.assertEqual(Path(be._RELEASE).resolve(), repo / "release")

    def test_no_non_ascii_outside_reconfigure_guard(self):
        """中文提示只能出现在 reconfigure 之后。

        CI 的 windows runner 不设 PYTHONIOENCODING，stdout 是 cp1252，
        任何中文 print 都会让打包挂在最后一步（Run #10 实测）。
        这里保证 reconfigure 调用位于所有中文输出之前。
        """
        src = Path(be.__file__).read_text(encoding="utf-8")
        self.assertIn("reconfigure", src,
                      "build_exe.py 必须重配 stdout 编码才能安全输出中文")

        # reconfigure 必须出现在模块顶层 import 之后、任何 print 之前
        import ast
        tree = ast.parse(src)
        first_print_line = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print":
                if first_print_line is None or node.lineno < first_print_line:
                    first_print_line = node.lineno
        reconf_line = src.splitlines().index(
            [ln for ln in src.splitlines() if "reconfigure" in ln][0]
        ) + 1
        if first_print_line is not None:
            self.assertLess(
                reconf_line, first_print_line,
                "reconfigure 必须早于第一个 print，否则中文仍会崩",
            )


if __name__ == "__main__":
    unittest.main()
