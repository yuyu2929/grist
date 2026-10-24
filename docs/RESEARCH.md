# Grist 调研报告：矢量→栅格翻译的现有方案与优缺点

> 版本：1.0 · 2026-10-20 · 项目：Grist（积分式矢量-栅格翻译器）
>
> 本文档记录对新对象的调研过程、现有方案的优缺点矩阵，以及 Grist 的差异化论证。
> 所有结论均标注来源；对与 Grist 构思接近的先例工作如实披露，不占为己有。

---

## 1. 问题定义

**矢量→栅格翻译**（rasterization / vector-to-raster conversion）：把点、线、面三类矢量要素，
按给定网格（原点、格距、宽高）转换为栅格格值。三类要素的栅格语义各不相同：

| 要素 | 常见语义 | 数学本质 |
|---|---|---|
| 点 | 格内计数 / 核密度场 | 计数测度 / 卷积 |
| 线 | 中心线点亮 / 长度密度 / 缓冲区掩膜 | 1-测度 / 线积分 |
| 面 | 类别灼烧（burn）/ 面积占比 | 2-测度（面积）在格上的积分 |

栅格格值 c 的诚实定义是**格积分**（cell integral）：c = (1/A_c) ∫_c f(x,y) dxdy，
即要素覆盖函数 f 在格内的平均值。现有绝大多数实现并不计算这个积分，
而是计算它的**采样**（格心是否在面内）或**灼烧**（是否触碰），二者都是积分的
有偏估计。Grist 的立场：**栅格应该是积分出来的，不是采样出来的**。

---

## 2. 现有方案调研

### 2.1 GDAL / rasterio（gdal_rasterize, RasterizeLayer）

- **算法**：扫描线（scanline）+ 边表（edge table）跨格推进；面要素默认"格心规则"
  （pixel centre in polygon），`-at`（ALL_TOUCHED）改为"触碰即烧"；
  线要素用 Bresenham 类算法；`MERGE_ALG=REPLACE|ADD`；`OPTIM=RASTER|VECTOR`
  两种扫描组织方式。([gdal.org](http://gdal.org/gdal_rasterize.html))
- **优点**：快、格式全、生态标准、`-add` 支持热力图式累加。
- **缺点**：
  1. **无覆盖占比语义**——25% 覆盖的格与 100% 覆盖的格同值，类别灼烧丢失面积信息；
  2. **结果依赖要素顺序**（ALL_TOUCHED 模式下后写覆盖先写，官方文档明示）；
  3. **多级金字塔独立重采样**（gdaladdo），粗层不是细层的函数，层间无一致性保证；
  4. 连续场用 average、类别场用 nearest 是业界"常识性陷阱"——
     对类别影像做均值重采样会造出 6.5 类这种不存在的值
     （[data-compression.net](https://www.data-compression.net/geospatial-storage-fundamentals/format-comparison/cloud-optimized-geotiff-for-raster-workloads/overview-pyramids-and-internal-tiling-for-cog)）；
  5. 无内容寻址、无增量、无误差界。

### 2.2 exactextract / exactextractr（iSciences, C++）

- **算法**：每条环边遍历一次，记录进出栅格格；被边触碰的格，
  在格内识别所有逆时针有界区域（`left_hand_area()` 链式追踪）求精确覆盖面积；
  未被触碰的格用点在面内测试（PIP）+ 洪水填充判定 0/1。
  ([isciences.github.io/exactextract](https://isciences.github.io/exactextract/background.html))
- **优点**：**精确的逐格覆盖率**（解析面积，非采样）；已被瑞士国家级作物制图
  （SwissCrop25）采用为生产标准——对 140 个源类别逐格计算覆盖占比、
  聚合后取多数类，论文明确"面积加权分配比格心规则更准，且与传感器积分物理一致"
  （[arXiv:2608.09497](https://arxiv.org/html/2608.09497)）。
- **缺点**：
  1. 定位是**分区统计**（zonal statistics），栅格化用例仍需物化稠密矩阵；
  2. **单分辨率**——每个层级独立计算，无金字塔一致性；
  3. 无内容寻址/去重/增量；
  4. C++ 依赖（GEOS/GDAL 编译链），Python 侧是绑定而非纯库。

### 2.3 controlledburn（R, 2024–2025，与 Grist 构思最接近的先例，如实披露）

- **算法**：边界格求精确覆盖率 + 内部格用环绕数（winding number）分类，
  输出为**游程编码的内部条带 + 逐格权重的边界格**两张小表，永不分配稠密矩阵。
  大陆尺度 32000×16000 网格输出约 50MB 而非 2GB。
  （[hypertidy.github.io/controlledburn](https://hypertidy.github.io/controlledburn/articles/controlledburn-journey.html)）
- **优点**：内存 O(周长) 而非 O(面积)；稀疏即输出；精确覆盖率。
- **缺点**（与 exactextract 相同层级）：单分辨率；按多边形逐个输出
  （cell, fraction, polygon_id）三元组，**无统一格结构、无多格重叠混合表示**；
  无金字塔、无内容寻址、无增量。R 语言生态。

> **诚实声明**：controlledburn 的"精确边界 + 缠绕数内部 + 稀疏输出"与 Grist 的
> 覆盖核心理念有实质交集。Grist 的差异化**不依赖**覆盖核子本身的技巧性
> （那部分属于该问题空间的收敛最优解），而依赖下文 §4 的三个系统级构造。

### 2.4 其他

| 方案 | 算法 | 主要缺点 |
|---|---|---|
| PostGIS `ST_AsRaster` | 包装 GDAL（`touched` 参数即 ALL_TOUCHED） | 同 GDAL；`ST_ExtractToRaster`（MapAlgebra 回调）灵活但慢 |
| rasterix（Python） | dask 包装三引擎（rasterio / rusterize / exactextract） | 引擎即前述，无新语义 |
| rusterize（Rust） | 无 GDAL 依赖的快速燃烧 | 仍为类别灼烧语义 |
| 超采样法（rasterio 10× supersample） | 统计逼近 | 无误差界，O(factor²) 成本，噪声不可控 |
| QGIS / R raster / velox / rasterstats | 格心规则（±解聚） | 小地块误差可达十分之一量级且非随机（[python-remote-sensing.com](https://www.python-remote-sensing.com/core-raster-fundamentals-stac-mapping/zonal-statistics-and-vector-raster-integration/computing-weighted-zonal-statistics-for-partial-pixels)） |
| 线栅格化：Bresenham / Wu / DDA | 中心线点亮 / 抗锯齿强度 / 保守遍历 | **无一计算格内线的真实长度积分**；Bresenham 非保守（角切格漏记），Wu 强度≠长度 |
| GPU 瓦片化光栅化（tile-based, edge function） | 角点符号测试判定全内/全外瓦片 | 目标是可见性判定；不做解析面积、无多级、无持久化 |
| 点云→DEM（PDAL/LASTools） | TIN 密化 / IDW / Shepard | 解决的是散点插值连续场，不是格积分翻译；无矢量语义保留 |

### 2.5 内容寻址先例

内容寻址存储（CAS）本身是成熟领域（git、IPFS、Docker layer）；
地理空间侧 DVC 用文件级/瓦片对齐段级哈希做二进制去重与版本指针
（[geospatial-data-versioning.com](https://www.geospatial-data-versioning.com/geospatial-data-versioning-fundamentals-architecture/pointer-synchronization-for-raster-datasets)）。
**没有**任何现有栅格结构把内容寻址做进栅格内部单元（瓦片级哈希 +
聚合传播 + 增量失效）。这正是 Grist 的结构级创新空间。

---

## 3. 优点综合与缺点消除映射

| # | 现有方案的优点（来源） | Grist 如何吸收 |
|---|---|---|
| A1 | 精确逐格覆盖率（exactextract） | 覆盖核输出精确面积占比，而非格心采样 |
| A2 | 稀疏输出、内存 O(周长)（controlledburn） | 格结构本身稀疏：只有混合格存储，纯内/纯外格用区间压缩 |
| A3 | 生态与工具链（GDAL/COG） | 兼容 GeoJSON 输入；导出稠密数组/PGM/CSV，不与生态对立 |
| A4 | 面积加权制图的生产验证（SwissCrop25） | 混合格天然支持"先聚合后取多数类" |
| A5 | CAS 去重与完整性（DVC/git 思想） | 瓦片级 blake2b 内容寻址内建于结构 |
| A6 | 超采样的可调精度思想 | 点/线场模式提供显式核与显式误差说明（近似模式如实标注） |

| # | 现有方案的缺点 | Grist 如何消除 |
|---|---|---|
| D1 | 无覆盖占比（GDAL 灼烧） | 格积分语义：面=精确面积占比、线=精确长度、点=精确计数 |
| D2 | 顺序依赖、重叠丢失（REPLACE/ADD） | 格内**混合表示** (fid, area) 列表，重叠不丢；"谁赢"是读取策略而非烘焙损失 |
| D3 | 金字塔独立重采样、层间漂移、类别均值静默缺陷 | **聚合金字塔**：粗格 = 细格的精确面积加权聚合（见 THEORY.md 一致性定理） |
| D4 | 编辑即全量重算 | 脏瓦片增量 + 向上聚合传播，未变瓦片哈希不变 |
| D5 | 无确定性/无完整性 | 全确定性计算 + blake2b 内容哈希，同输入→同字节 |
| D6 | 线栅格化无长度语义 | 线段-矩形参数化裁剪求精确格内长度 |
| D7 | C++ 重依赖 | 纯 Python 零依赖核心（shapely 仅用于基准测试的 ground-truth oracle） |
| D8 | 无误差界/近似不可控 | 面覆盖为解析精确；点/线场模式的近似有显式子采样密度参数与误差说明 |

---

## 4. 新颖性论证（与先例的逐条对照）

**已核查并排除的接近先例**：exactextract（精确但单分辨率、无聚合）、
controlledburn（精确+稀疏但单分辨率、按要素输出、无统一格结构）、
DVC（文件级 CAS，非栅格结构内部瓦片级）、GPU tile rasterization
（可见性判定，非持久化数据结构）。未发现任何工作同时具备
Grist 的以下三项系统级构造：

1. **聚合金字塔（Aggregated Pyramid）**：粗层格值定义为细层格测度的精确和
   （面积可加性定理的直接推论）。现有金字塔全部是独立重采样
   （nearest/average/gauss），层间无函数关系；Grist 中层 ℓ 的格值是
   层 0 格值的**确定函数**，从根本上消除重采样漂移与类别均值缺陷。
2. **混合格表示（Coverage Mixture Cell）**：格内存储 (fid, 面积) 稀疏列表，
   重叠要素各自保留份额；烘焙不决策、读取时策略化（多数类/最大面积/分层优先）。
   现有结构每格单值（灼烧）或按要素分散输出（exactextract/controlledburn）。
3. **内容寻址瓦片 + 增量传导（Content-Addressed Tiles + Incremental Propagation）**：
   瓦片按 blake2b 内容哈希寻址去重；编辑沿金字塔向上只重算受影响聚合，
   未变瓦片保持同一哈希——栅格获得 git 式的 diff/去重/完整性。

覆盖核子（条带认证-裁剪积分，见 THEORY.md §3）与 exactextract 的
"环遍历 + 格内面积链接"在**目的上部分重合**（都求精确覆盖率），但机制不同：
exactextract 逐格链接 traversal 端点成闭合多边形；Grist 按行条带认证
（整条无穿越则整行同质，一次 PIP 定值）后仅对跨界条带做环-条带裁剪 +
奇偶区间积分。该差异是**实现级**的；新颖性主张主要落在 §4 的三项系统构造上，
本文档对此不做夸大。

---

## 5. 结论

现有生态的最优解（exactextract + controlledburn）已经解决了"精确、稀疏"，
但全部停留在**单分辨率、一次性、无寻址**的翻译器形态。Grist 的定位不是
再造一个更快的 rasterize，而是把"翻译"升级为**可寻址、可增量、多级自洽的
栅格资产**：栅格从一次性计算的产物，变成与矢量数据同级的、可版本化的
一等数据。商业模式沿用三层 open-core（个人免费 / 团队付费 / 企业付费），
详见 `docs/BUSINESS.md`。
