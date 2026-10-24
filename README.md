# Grist — 精确的矢量→栅格翻译器

[![CI](https://github.com/yuyu2929/grist/actions/workflows/ci.yml/badge.svg)](https://github.com/yuyu2929/grist/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![deps](https://img.shields.io/badge/runtime%20deps-none-34d399)](pyproject.toml)

Grist 把点、线、面矢量数据**精确地**翻译成栅格：每个格子的值不是采样估计，
而是要素覆盖函数在该格上的**平均值**（格积分语义）。点被精确计数，线贡献精确的
裁剪后长度，面贡献精确的裁剪后面积占比——没有中心规则的系统性偏差，也没有超采样
"收敛但永不精确"的统计逼近。

零依赖（仅标准库；测试与基准额外用到 numpy/shapely 作对照）。

```python
import grist

pyr = grist.bake_geojson(open("city.geojson").read(), cell_count=512)
m = grist.dense_matrix(pyr, kind="polygon")      # 每格的面覆盖占比
owner = grist.dominant_matrix(pyr)               # 每格的主导要素 fid
pyr.update(added=[new_feature])                  # 增量更新，只重算受影响的瓦片
grist.codec.save(pyr, "city.grist")              # 内容寻址的 .grist 容器
```

## 为什么不是 GDAL / exactextract / COG？

| | GDAL 栅格化 | exactextract | COG 金字塔 | **Grist** |
|---|---|---|---|---|
| 语义 | 中心规则 / ALL_TOUCHED（有偏） | 精确单层 | 精确单层 + 独立重采样粗层 | **精确 + 层间一致** |
| 多分辨率 | 重新栅格化（层间漂移） | 无 | 独立计算（"6.5 类"静默缺陷） | **精确面积加权聚合（漂移 ~1e-15）** |
| 重叠要素 | 后写覆盖（顺序依赖） | 单值 | 单值 | **混合格：谁赢是读取策略** |
| 增量更新 | 全量重算 | 全量 | 全量 | **只重算受影响瓦片（最高 75×）** |
| 存储 | 稠密数组 | 稠密数组 | 稠密数组 × 层数 | **内容寻址瓦片 + 去重** |

完整调研见 [docs/RESEARCH.md](docs/RESEARCH.md)，理论与正确性证明见
[docs/THEORY.md](docs/THEORY.md)。

## 核心概念

**格积分语义**：格值 = 覆盖函数在格上的平均。这不是近似——`sum(所有格的值) =
要素总测度`精确成立（基准中质量误差 ~1e-12）。

**半开规则**：x 方向 `[x0+ic, x0+(i+1)c)`，y 方向行区间闭在下端。边界归属唯一，
测度可加——这是"各格之和精确等于整体"的前提。

**SCCI 覆盖核**：自底向上活动边扫掠 → 认证条带（一次奇偶测试整行填充）→
未认证条带精确裁剪。解析精确（模糊测试对 shapely 误差 ≤ 1e-13），
且比朴素逐格相交快 28–42×。

**聚合金字塔**：粗格 = 细格精确面积加权聚合（定理 2）。层间无漂移——
这是 COG 式独立重采样做不到的。

**混合格**：格内是 `(fid, 测度)` 稀疏列表，重叠不丢失。"谁赢"（求和/最大/顶层）
是读取策略，不是存储假设。

**内容寻址 + 增量传导**：blake2b 瓦片哈希、跨位置去重、增量只重算受影响瓦片并
向上传播。更新后的金字塔与同状态新烘焙**字节级相同**（测试钉死）。

## 安装与使用

```bash
pip install .          # 零依赖，装完即有 grist 命令
# 或者不安装直接跑：
python3 -c "import sys; sys.path.insert(0,'.'); import grist; print(grist.__version__)"
```

CLI：

```bash
python3 -m grist.cli bake city.geojson -o city.grist --cell-count 512
python3 -m grist.cli stats city.grist
python3 -m grist.cli query city.grist 12.5 47.3
python3 -m grist.cli export city.grist out.pgm --format pgm
python3 -m grist.cli update city.grist -o city2.grist --edits edits.geojson
python3 -m grist.cli diff city.grist city2.grist
python3 -m grist.cli verify city.grist
```

## 基准（实测，Python 3.11 单核）

| 数据集 | Grist | 中心规则 | 超采样16 | 朴素精确 |
|---|---|---|---|---|
| 40 多边形(带洞) 100² | **44 ms** | 527 ms | 31.4 s | 1.84 s |
| 20 圆 200² | **146 ms** | 3.31 s | 122.9 s | 4.12 s |
| 15 细条(采样天敌) | **14 ms** | 145 ms | 28.5 s | 0.76 s |

精度：Grist 与 shapely oracle 逐格误差 ≤ 1e-12；中心规则最大格误差 1.99 格、
质量误差 74–292；粗层独立栅格化漂移最高 **43.6%**（Grist 聚合漂移 ≤5e-14）。
增量更新：稀疏编辑场景比全量重烘焙快 **73×**，且结果字节级一致。

## 测试

```bash
python3 tests/run_all.py     # 9 个文件；零第三方依赖即可跑（shapely 缺失时自动跳过 oracle 测试）
pip install -e ".[test]"     # 装上 shapely/numpy 后重跑，覆盖核会逐格与 shapely 对照
python3 bench/bench.py --quick   # 快速基准；不带 --quick 是完整对照（约 3 分钟）
```

域拟合默认带 1e-9 相对自动 padding：半开域宽 W 在数学上不可能严格包含
跨度 W 的闭数据，自动余量保证守恒精确成立（`grist verify` 默认路径通过）；
`--pad 0` 可显式退出，让域与数据 bbox 重合（贴边几何落在域外，见
[docs/DEVLOG.md](docs/DEVLOG.md) BUG 15/18）。

## 许可与商业

核心库 **MIT**，全部数学与全部命令行功能永久免费、无功能限制。

付费只包住**规模与运维**，且只卖已经实现的东西：

| 层 | 付费点 |
|---|---|
| Pro | `bake.parallel` 多进程烘焙（与串行结果字节级相同）、`audit.log` 追加式 JSONL 审计 |
| Enterprise | 以上 + `container.encrypt`（AES-256-GCM 密封容器，`pip install "grist-raster[enterprise]"`） |

`store.remote`（远程瓦片存储）**尚未实现**，列在 `PLANNED_FEATURES`，
任何许可都不会解锁它。

许可为 Ed25519 离线签名（无电话回家），签发需发行方私钥，
验证只需公钥。机制、定价与已知限制见 [docs/BUSINESS.md](docs/BUSINESS.md)，
变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## 文档

- [docs/THEORY.md](docs/THEORY.md) — 语义定义、SCCI 算法、正确性证明
- [docs/RESEARCH.md](docs/RESEARCH.md) — 现有项目调研与优缺点映射
- [docs/DEVLOG.md](docs/DEVLOG.md) — 开发日志（含 20 个真实 bug 的发现与修复）
- [docs/BUSINESS.md](docs/BUSINESS.md) — open-core 商业模式与许可设计
