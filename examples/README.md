# 合成示例成品

这里仅收录自包含合成工程的既有 Desktop 输出。销售数据为 A=10、B=20、C=30，总金额 **60**；Rich 增加两页、中文名称、一条产品关系以及权重 2/3/4，加权合计 **200**。这些不是客户业务数据。

## 从哪一组开始

| 场景 | 成品 | 公开版检查记录 |
|---|---|---|
| 公网网页 PBIP → PBIX | [销售 PBIX](Public-HTTPS-Synthetic-forward-report.pbix) | [正向 verification](Public-HTTPS-Synthetic-forward-verification.json) |
| 同一身份将该 PBIX 经公网 MCP → portable PBIP | [完整工程 ZIP](Public-HTTPS-Synthetic-portable.pbip.zip) | [反向 verification](Public-HTTPS-Synthetic-reverse-verification.json) |
| Sales：导出定义，不携带模型缓存 | [definitions ZIP](Sales-definitions-rc4.pbip.zip) | [verification](Sales-definitions-rc4-verification.json) |
| Sales：带缓存工程再转回 PBIX | [portable ZIP](Sales-portable-c268.pbip.zip) · [往返 PBIX](Sales-roundtrip-5a09.pbix) | [导出](Sales-portable-c268-verification.json) · [回转](Sales-roundtrip-5a09-verification.json) |
| Rich：多页、关系与 60/200 往返 | [portable ZIP](Rich-portable-1b49.pbip.zip) · [往返 PBIX](Rich-roundtrip-rc4.pbix) | [导出](Rich-portable-1b49-verification.json) · [回转](Rich-roundtrip-rc4-verification.json) |

公网正向使用的历史 `Synthetic.zip` 与仓库 rc6 的 [Synthetic.zip](../sample/Synthetic.zip)
**逐成员路径和内容相同，但 ZIP 容器元数据不同，压缩包 SHA-256 不同**。
正向 verification 保留当次真实输入摘要，不能拿它直接核对重新打包后的 rc6 ZIP。
公网反向记录的 `source_sha256` 对应本目录的公网 PBIX；
Sales 与 Rich 回转记录的 `source_sha256` 分别对应表中的 portable ZIP。
Sales / Rich 最初的导出源 PBIX 不重复收录；其输入摘要保留于记录，不能误认成公网链路的那份 PBIX。

## 怎样阅读与使用

将 PBIP ZIP 完整解压到新的短目录，保留入口、Report 与 SemanticModel 的相对位置，用兼容版本 Power BI Desktop 打开。
definitions 不带 `cache.abf`；portable 带本任务导出的非空缓存。**两者都不是脱敏包**，即使合成示例没有真实业务数据，也不要据此跳过自己工程的定义与缓存审阅。

这些 verification 是原观察记录的**公开精简副本**，仅删除 `job_id`、`desktop_pid`、`reopened_pid` 三个内部运行字段，其他字段保留原值。保留 `reopened_in_fresh_desktop`、方向、模式、输入摘要、Desktop 版本和观测值，不伪造新转换、不把 PID 删除解释成没有执行新进程复开。

PBIX 与工程 ZIP 保持原字节，公开精简 JSON 的摘要则不等于原始记录摘要。
verification 不是安全审计、脱敏或任意 DAX / 逐行数据完全等价证明；既有记录采用 Desktop Save As、包结构和新进程 UIA 观察，未查询全部 DAX 与数据行。详细限制见[场景与边界](../docs/03-验证结果与边界.md)。

## 下载内容摘要

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| `Public-HTTPS-Synthetic-forward-report.pbix` | 15905 | `ab8eccc83a5c5b1242a632e4d77eac48f4aa3d791c22bca1800a3589d475458a` |
| `Public-HTTPS-Synthetic-portable.pbip.zip` | 16879 | `20b0a849a2e49a091b62d6d523a88426680a6f62d4f9344405a1524fe65dfd31` |
| `Rich-portable-1b49.pbip.zip` | 23925 | `bc72237fa58070899b3b99da109c3e02a3f73d0eef289442e82f74acc472c13c` |
| `Rich-roundtrip-rc4.pbix` | 21985 | `a873b886ab1d7c39844613b5cb550b3c2f8a3d273eeeda53edb958ca144ad633` |
| `Sales-definitions-rc4.pbip.zip` | 4906 | `d68321a2c83f30fe71a60dda89a7166fe6b8626ae2b28ef5d83e2253a0f8df8a` |
| `Sales-portable-c268.pbip.zip` | 16886 | `aa570b24360661a37d3351758d5917e7564d32748a041c1ea84b6d6bf45321e5` |
| `Sales-roundtrip-5a09.pbix` | 15909 | `92eb7808246b6b742ab15aa7c7aa64a52ffda5115cf4eec253755b9b3db1b8c0` |

可用 `Get-FileHash -Algorithm SHA256` 核对下载文件。摘要确认字节身份，不证明业务结果。
文件名中的历史标记标识已有样例，不表示仓库包含或推荐旧部署包；当前代码基线为 rc6 / 0.2.3。
