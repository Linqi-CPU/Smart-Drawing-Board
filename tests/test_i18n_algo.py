"""算法下拉框汉化测试。

覆盖：中文显示名 ↔ 内部 key 双向映射、脏值回落、Combobox 实际渲染。

为什么值得单测：`_run_calc_worker` 按内部 key 分支，
而 `StringVar` 在 UI 未初始化时会返回默认值。
如果让 Combobox 直接存中文值，历史配置恢复时会静默落进
else 分支按经典算法跑，用户毫无察觉 —— 这是必须钉死的回归。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edit import page


class TestAlgoI18n(unittest.TestCase):
    """显示名 ↔ 内部 key 的映射规则。"""

    def test_labels_map_to_distinct_keys(self):
        """中文显示名必须互不重复（不能有两个 key 共用同一显示名）。"""
        keys = list(page.ALGO_LABELS)
        self.assertEqual(len(keys), len(set(keys)))
        labels = list(page.ALGO_LABELS.values())
        self.assertEqual(len(labels), len(set(labels)))

    def test_display_list_matches_labels(self):
        """下拉框显示列表必须与映射表同源同序。

        不断言具体数量：新增算法（如「进阶算法」）时这个测试应继续成立。
        真正的不变量是「ALGO_DISPLAY 里的每一项都来自 ALGO_LABELS，
        且顺序一致」。
        """
        self.assertEqual(
            page.ALGO_DISPLAY,
            [page.ALGO_LABELS[k] for k in page.ALGO_LABELS],
        )
        self.assertEqual(len(set(page.ALGO_DISPLAY)), len(page.ALGO_LABELS))

    def test_chinese_name_resolves_to_key(self):
        for key, label in page.ALGO_LABELS.items():
            self.assertEqual(page._algo_key(label), key)

    def test_english_key_still_works(self):
        """历史配置/测试直接往 StringVar 里塞英文 key 也要认。"""
        for key in ("classic", "improved", "compare"):
            self.assertEqual(page._algo_key(key), key)

    def test_unknown_value_falls_back_to_default(self):
        """脏值不能崩，也不能静默跑到别的算法上。"""
        self.assertEqual(page._algo_key("垃圾值"), page.DEFAULT_ALGO)
        self.assertEqual(page._algo_key("classic2"), page.DEFAULT_ALGO)
        self.assertEqual(page._algo_key(""), page.DEFAULT_ALGO)
        self.assertIsNone(page._algo_key(None) and None)
        self.assertEqual(page._algo_key(None), page.DEFAULT_ALGO)

    def test_whitespace_tolerated(self):
        """复制粘贴带空格的情况。"""
        self.assertEqual(page._algo_key("  改进算法  "), "improved")

    def test_default_algo_is_valid(self):
        self.assertIn(page.DEFAULT_ALGO, page.ALGO_LABELS)


class TestComboRenders(unittest.TestCase):
    """Combobox 实际渲染出的中文与读回结果。

    只在有图形环境时运行，CI（无头）会 skip 而不是失败。
    """

    def setUp(self):
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception:
            self.skipTest("tkinter 不可用")
        # 无显示环境（CI）下 Tk() 会抛 TclError
        try:
            self.root = tk.Tk()
        except Exception:
            self.skipTest("无图形环境")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_combobox_shows_chinese_and_reads_back_key(self):
        import tkinter as tk
        from tkinter import ttk

        var = tk.StringVar(value=page.DEFAULT_ALGO)
        cb = ttk.Combobox(self.root, textvariable=var,
                          values=page.ALGO_DISPLAY,
                          state="readonly", width=8)
        cb.set(page.ALGO_LABELS[page.DEFAULT_ALGO])

        self.assertEqual(list(cb["values"]), page.ALGO_DISPLAY)
        for label, expected in [("改进算法", "improved"),
                                ("对比模式", "compare"),
                                ("经典算法", "classic")]:
            cb.set(label)
            self.root.update_idletasks()
            self.assertEqual(cb.get(), label)
            self.assertEqual(page._algo_key(cb.get()), expected)


if __name__ == "__main__":
    unittest.main()
