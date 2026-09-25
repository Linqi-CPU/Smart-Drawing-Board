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

EXPECTED_RELEASE = [
    "SmartDrawingBoard-Launcher.zip",
    "SmartDrawingBoard-Main.zip",
    "SmartDrawingBoard-Page.zip",
    "SmartDrawingBoard-Source.zip",
]


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


class TestBinaryZips(PackFixture):
    """成品 zip 必须解压即见 exe，不能多套一层目录。"""

    def _make_fake_dists(self):
        for name, _script in be.APP_ENTRIES:
            d = be._DIST / name
            (d / "core").mkdir(parents=True, exist_ok=True)
            (d / f"{name}.exe").write_bytes(b"MZ fake")
            (d / "core" / "server.py").write_text("# srv", encoding="utf-8")

    def test_no_extra_top_dir(self):
        self._point_module_at_fake_tree()
        be._prepare_release_dir()
        self._make_fake_dists()

        zips = be._make_binary_zips()
        self.assertEqual(len(zips), 3)
        expected = sorted(f"{name}.zip" for name, _ in be.APP_ENTRIES)
        self.assertEqual(sorted(p.name for p in zips), expected)

        names = self._names(zips[0])
        self.assertTrue(any(n.endswith(".exe") for n in names), "zip 内应有 exe")
        self.assertFalse(
            any(n.startswith("SmartDrawingBoard-Launcher/") for n in names),
            f"zip 内多了顶层目录，解压后还要再进一层: {names}",
        )

    def test_missing_dist_raises(self):
        self._point_module_at_fake_tree()
        be._prepare_release_dir()
        # 不造 dist，应当立刻报错而不是产出空 zip
        with self.assertRaises(SystemExit):
            be._make_binary_zips()


class TestReleaseDirContents(PackFixture):
    """release/ 的文件列表必须确定，且重跑会清掉过期产物。"""

    def test_exact_contents_and_stale_cleanup(self):
        self._git_commit_only_tracked()
        self._point_module_at_fake_tree()
        be._prepare_release_dir()
        be._make_source_zip()

        for name, _script in be.APP_ENTRIES:
            d = be._DIST / name
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{name}.exe").write_bytes(b"MZ")
        be._make_binary_zips()

        self.assertEqual(sorted(p.name for p in be._RELEASE.iterdir()),
                         EXPECTED_RELEASE)

        # 残留 zip 必须被清掉，否则 upload 会把过期文件一起传进 Release
        (be._RELEASE / "Obsolete-Old.zip").write_bytes(b"stale")
        be._prepare_release_dir()
        self.assertFalse((be._RELEASE / "Obsolete-Old.zip").exists())
        self.assertEqual(list(be._RELEASE.iterdir()), [],
                         "_prepare_release_dir 应清空整个 release/")


class TestRepoIgnoresBuildDirs(unittest.TestCase):
    """真实仓库的 build_exe 常数不能被上面的测试污染。"""

    def test_module_points_at_repo_after_tests(self):
        repo = Path(be.__file__).resolve().parent
        self.assertEqual(Path(be._HERE).resolve(), repo)
        self.assertEqual(Path(be._DIST).resolve(), repo / "dist")
        self.assertEqual(Path(be._RELEASE).resolve(), repo / "release")


if __name__ == "__main__":
    unittest.main()
