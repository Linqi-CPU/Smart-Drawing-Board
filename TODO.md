# Smart Drawing Board - 待办清单

## 当前状态

- 四项进阶算法已实现、已测试、已接入 UI：`tests/test_band_advanced.py`（63 项）
  + `tests/test_band_page_ui.py`（12 项）
- **全量测试 514 项通过**（26 项 skip，均为无显示环境下的 GPU 一致性测试）
- UI「进阶算法」下拉项可用，默认参数全部关闭，三种经典模式零回归
- 本轮实测并修复两个真缺陷：自适应分段第二阶段结构性失效、分位数回归
  极端分位点收敛不完整。详见 [CHANGELOG.md](CHANGELOG.md) 的「未发布」条目
- **未提交**：decimal + deps、GPU 一致性、server/client/UI 接入、Legendre 基、
  四个算法模块、两组新测试，均未 git commit

---

## 高优先级 TODO

### 1. 提交并验证打包
- [ ] `git add -A && git commit`（本轮全部改动，尚未提交）
- [ ] 确认 PyInstaller 真的把 `band_advanced` / `gpu_backend` / `deps` 打进去
      （`build_exe.py` 的 hidden imports 需核对，漏一个则 exe 里 `import` 即崩）
- [ ] 重打 dist 二进制并真机启动一次，确认「进阶算法」项在 exe 里可用
- [ ] release/ 目前只有源码包，需补 Windows 成品

### 2. 文档同步
- [x] CHANGELOG.md 补「未发布」条目
- [x] README.md 补进阶算法说明
- [x] ARCHITECTURE.md 补新模块与路由
- [x] TODO.md 更新本清单

---

## 中优先级 TODO

### 3. 算法进阶
- [x] 分位数回归（`quantile_fit`，IRLS）
- [x] Bootstrap 置信带（`bootstrap_band`）
- [x] 自适应分段（`adaptive_segments`，密度 + 离散度）
- [x] AIC/BIC 自动选阶（`select_degree`）
- [ ] **10 阶拟合**：当前架构做不到，`scale^10 > 2^53` 硬墙。
      要做需放弃 x 空间系数表示，改用分段 + 每个子区间独立低阶拟合，
      或用 `decimal` 全程高精度（已引入但拖慢明显）。这是算法重设计，非调参
- [ ] Legendre 基已修好 t 空间；x 空间 `_denormalize` 的 `/scale^j`
      仍是唯一剩余瓶颈，可考虑改为按项递推缩放

### 4. UI 细节优化
- [ ] 算法选择下拉框添加图例说明（颜色对应）
- [ ] 对比模式下的 R² 对比面板美化
- [ ] 增加"仅画中心线"选项（不显示上下界）
- [ ] 进阶参数的输入校验（Bootstrap 次数上限、τ 范围、α 范围）目前只在内核侧

---

## 低优先级 TODO

### 5. UI 自动化测试补强
- [ ] 进阶参数的输入 → 内核参数 → 结果摘要，全链路参数穿越测试
      （目前只测了默认值穿越，未测用户改过参数后的穿越）

### 6. 性能
- [ ] 压力测试：1000+ 点的拟合性能（进阶算法尤其是 Bootstrap 开销大）
- [ ] GPU 后端只在 RTX 3050 Laptop 上验证过，需确认其它显存档位不 OOM

---

## 已知问题

1. **10 阶拟合精度失控**：`scale=50` 时 `scale^10 = 9.77e16 > 2^53`，
   double 尾数耗尽。实测 x 空间误差 8 阶 5.5e-04、10 阶 1.567。
   非实现问题，是 double 物理极限（CHANGELOG「已知限制」有完整论证）
2. **埃尔米特插值不解决高阶问题**：实测 10 阶下解析 1.567 / 埃尔米特 1.632 /
   切比雪夫 1.742，同量级，最好情况反而略差。范德蒙系统条件数约 1e60
3. `build_exe.py` 中文输出在 Actions 中可能编码错误（早前已修，未在新版 CI 复核）
4. 对比模式仅支持经典 vs 改进，暂不支持更多算法

---

## 下一步行动

1. `git add -A && git commit -m "..."`，把本轮全部改动入库
2. 核对 `build_exe.py` 的 hidden imports 是否含 `band_advanced` / `gpu_backend` / `deps`
3. 重打 dist，真机启动验证「进阶算法」项
4. 若 3 通过，push tag 触发 Actions 构建，确认 Release 产物
