# Grist 商业设计（open-core）

## 定位

**精确矢量→栅格翻译的基础设施。** 卖点不是"又一个栅格化工具"，而是三个
现有方案都给不出的保证：

1. **逐格精确**（格积分语义，非采样近似）——质量守恒到 1e-12；
2. **层间一致**（聚合漂移 ~1e-15，COG 式独立重采样最高漂移 43.6%）；
3. **字节级确定**（同状态两次烘焙、更新 vs 全新烘焙，输出字节相同）——
   可审计、可复现、可缓存。

## 分层（与代码一一对应）

| 层 | 内容 | 许可 |
|---|---|---|
| **Core**（本仓库） | 全部数学：覆盖核、金字塔、混合格、内容寻址容器、增量更新、CLI、`.grist` 格式 | **MIT，永久免费，无功能限制** |
| **Pro** | `bake.parallel` 多进程烘焙；`audit.log` 追加式 JSONL 审计 | 付费许可（Ed25519 离线签发） |
| **Enterprise** | Pro 全部 + `container.encrypt`（AES-256-GCM 密封容器） | 付费许可 |
| **Roadmap** | `store.remote` 远程瓦片存储 —— **尚未实现，任何许可都不会解锁** | 未发售 |

> 这一节刻意写得像产品说明书而不是愿景：`TIER_FEATURES` 里只列已经实现的
> 功能，未实现的进 `PLANNED_FEATURES`，永远不随许可下发。卖不存在的东西比
> 不卖更伤。

**核心永不设限**是有意的商业策略：栅格化的正确性必须是公共品（整个生态
都在 GDAL 的偏差上付过学费），付费点放在**规模与运维**上——大数据集的
并行烘焙、静态数据的加密、合规要求的审计。

## 现在到底卖什么

- **并行烘焙**：10⁶ 要素量级的墙钟时间。结果与串行烘焙**字节级相同**,
  所以付费买的不是正确性，是时间。
- **加密容器**：把 `.grist` 落盘为 `GRISENC1 | salt | nonce | AES-256-GCM`，
  密钥由 scrypt 从口令派生。错口令或截断都会 fail closed（GCM 标签校验），
  不会解出垃圾数据。适用于地籍、测绘等敏感底数据。
- **审计日志**：`Gate(audit_path=...)` 把每次 bake/update/encrypt/decrypt
  追加成 JSONL（含时间戳、许可主体、操作、摘要），可直接喂给 SIEM。
  只追加、不覆盖，满足"谁在什么时候处理了哪份数据"的合规问询。

## 许可机制（为什么这样设计）

`grist/enterprise.py` 实现离线许可：

- **格式**：`{"payload": <规范 JSON>, "sig": <hex Ed25519 签名>}`，
  签名覆盖 payload 字节；
- **载荷**：`{sub, tier, exp, feats}`——订阅方、等级、过期时间、功能列表；
- **验证**：纯 Python RFC 8032 Ed25519（零依赖，核心库不引入任何加密库），
  发行方公钥内置于分发构建；
- **无电话回家**：验证完全是本地签名检查，适合内网/离线环境；
- **防篡改**：改 payload 任何字节（如把 pro 升级成 enterprise）签名即失效；
  防到期伪造靠签名本身（载荷里的 exp 也被签名覆盖）；
- **签发权唯一**：`grist license --issue` 必须提供发行方的 `--private-key`，
  发行二进制里不含任何可用私钥（0.1.0 曾内置一个 demo 私钥，
  一行命令就能给自己签发企业许可——那是把付费墙画在地上）。

已知限制（如实记录）：

- 离线许可可被共享（一个 license 文件多人用）；
- `License` 是普通值对象，纯 Python 库无法阻止调用者自行构造——本设计是
  **授权凭证 + 法律约束**，不是 DRM。需要强绑定的场景应升级到带设备指纹的
  在线许可，本仓库不包含该实现。

## 功能门控的落点

门控只包住**便利层**，绝不碰数学正确性：

```python
from grist.enterprise import Gate

gate = Gate.free(vendor_public_key)                    # 免费：全部核心功能
gate = Gate.from_file("license.json", vendor_public_key,
                      audit_path="audit.jsonl")        # 付费：解锁便利功能

gate.check("bake.parallel")                            # 无许可 -> LicenseError
pyr = gate.bake_parallel(feats, grid, workers=8)

gate.save_encrypted(pyr, "city.sealed", "passphrase")  # enterprise
back = gate.load_encrypted("city.sealed", "passphrase")
```

`container.encrypt` 需要可选后端：

```bash
pip install "grist-raster[enterprise]"   # 仅装 cryptography
```

## 定价思路（建议，非实现）

- **Pro**（个人/小团队）：年费，解锁并行烘焙 + 审计日志。
  锚点：在 10⁶ 要素量级，并行烘焙省下的时间 × 人力成本。
- **Enterprise**（团队/合规）：年费 + 加密容器 + 支持。
  锚点：敏感底数据的落盘合规；审计日志满足"谁在何时处理了哪份数据"。

## 与竞品的商业差异

- GDAL/exactextract：工具，无分层，无增量，无确定性保证；
- 商业 GIS 平台：按席位收费，正确性不透明；
- Grist：核心免费且可验证（测试套件 + 基准全部公开），
  为规模与合规付费——"先证明对，再为快付费"。
