# PBIP ↔ PBIX：双向转换与网页工作台

> 公开版说明：保留原技术叙事与合成场景观察，部署定位参数已改为占位符。内部任务、身份、部署及截图来源回执不随仓库发布；公开成品与经过字段精简的 verification 见 [examples](../examples/README.md)。请先用自己的获准环境参数替换占位符。

## 项目初衷

一份已有的 Power BI 报表通常以 PBIX 文件交付，但后续批量编辑、代码审阅和版本管理更适合使用文本化的 Power BI Project（PBIP）工程。反过来，开发工具或 AI 生成的 PBIP 工程完成后，接收方又可能需要可直接打开的 PBIX。两种工作方式需要衔接：从成品回到工程，也从工程回到成品。

本项目把“打开、另存为、重新打开确认、取回文件”集中到受控 Windows 环境，由 Power BI Desktop 原生保存。普通用户在网页选择文件、查看自己的任务并下载；程序或 AI Agent 通过 MCP（统一的工具调用协议）接入同一队列。调用方无需逐一安装 Desktop，转换也不依赖大模型猜测或拼装文件格式。

## 工具已经能做什么

服务支持 **PBIP 工程 → PBIX** 和 **PBIX → PBIP 工程 ZIP**，并通过公网 HTTPS 提供网页与 MCP 入口。两个方向都由 Desktop 实际保存，再以新进程重新打开结果；网页与 HTTP MCP 使用同一用户身份、任务记录和产物。部署版本及推荐包见本页末尾。

| 能力 | 说明 |
|---|---|
| 双向文件转换 | 完整 PBIP 文件夹或 ZIP 转 PBIX；带本地模型的 PBIX 导出可编辑工程 |
| 两种工程导出模式 | 默认 definitions 保留定义、不带模型缓存；portable 保留本任务导出的缓存，供支持条件下离线回转 |
| 多人网页入口 | 个人凭证登录，上传、选择方向/模式、查看自己的任务、排队取消和下载 |
| 同身份 HTTP MCP | 与网页共用所有权检查及产物；普通用户不能读取、取消或下载其他人的任务 |
| 公网 HTTPS、私网转换节点 | App Service 提供 HTTPS 入口，经专用虚拟网络子网访问 VM；VM 的转换端口与 RDP 不向公网开放 |
| 可重复运维 | 独立管理网页服务 portal 与转换进程 worker，诊断/启停、空闲退出、有界配额及显式留存维护 |
| 代表性场景 | 销售 PBIX 往返后仍显示 60；多页、中文与关系工程往返后保留两页及 60/200；另有定义导出、错误输入和断线恢复场景 |

**不带缓存不等于脱敏。** definitions 中的查询、模型定义仍可能含内嵌数据和连接信息；portable 缓存更应按原 PBIX 的敏感级别管理。它只取当前任务自己的缓存，不借用其他用户的数据。

这条链路适合受信人员、同一安全域内的受控试用。多人可以排队，但一个 worker（执行转换的工作进程）仍在已连接、解锁的 Windows 桌面中串行工作。个人任务授权不是企业 SSO，也不是恶意工程沙箱或跨客户的 Windows 安全隔离；在线发布仍是另一条交付流程。

## 先试用，再深入

打开公网试用入口：`https://pbip-mcp-bidir-2d79425f.azurewebsites.net/`。获得管理员分配的个人访问凭证后即可登录，**不需要在自己的电脑建立 SSH 隧道**。公网可达不等于匿名可用：文件提交、自己的任务和下载仍受身份与所有权检查。

从[02 的用户操作部分](02-日常复跑与运维.md)开始：登录 → 选择 PBIX 或完整工程 → 确认目标和数据模式 → 在“我的任务”等待 → 下载。节点离线时由管理员检查 VM、已连接解锁的桌面、worker 和门户任务；用户不领取 SSH 密钥，也不操作远端 Desktop。

公网链路已完成一个连续示例：普通用户在网页提交销售工程并下载 PBIX，再以**同一身份、该确切 PBIX**通过公网 MCP 导出 portable 工程。两次保存后的新进程都观察到 60，工程又能从网页和 MCP 取得相同文件。公开[成品与检查记录](../examples/README.md)保留这条合成链路的输入摘要、模式和复开观察；部署方法见[网关入口](../deploy/public-gateway/README.md)。

公网入口没有改变桌面执行条件。portal 使用专用用户的持久计划任务，仍依赖该用户登录；worker 还需 Active、解锁的交互桌面。企业组织登录、跨客户隔离及生产级无人值守仍需另外设计。

## 从哪里开始

| 你的目标 | 推荐入口 |
|---|---|
| 第一次使用线上版本，读懂密码、任务详情和 verification | [04-线上版本使用说明.md](04-线上版本使用说明.md)：公网登录、最短操作、数据模式、结果字段和平台责任 |
| 理解文件、数据与整套机制 | [01-完整方案与实操指南.md](01-完整方案与实操指南.md)：从三行数据理解双向格式、身份与队列，再看完整操作、恢复、替代路线和部署决策 |
| 网页试用，或恢复已有服务 | [02-日常复跑与运维.md](02-日常复跑与运维.md)：前半是普通用户流程，后半是管理员恢复、身份和留存维护 |
| 判断自己的工程是否适合接入 | [03-验证结果与边界.md](03-验证结果与边界.md)：从往返、定义模式、多人任务及错误场景推导适用条件 |
| 查阅可下载成品与摘要 | [examples](../examples/README.md)：合成 PBIX/PBIP、公开版 verification 与 SHA-256 |

本页负责找到入口；普通用户先读 04，学习原理从 01 开始，重复操作与管理员运维用 02，选型结合 03。只需了解产物，可以先比较下方 definitions 与 portable 工程，再打开往返 PBIX 对照 60 和 200。

## 材料放在哪里

| 目录 | 内容 |
|---|---|
| [src](../src) · [scripts](../scripts) · [tests](../tests) | rc6 / 0.2.3 实现、运维入口与单元测试 |
| [sample](../sample) | 完整 Synthetic / Rich 输入及 ZIP |
| [deploy/public-gateway](../deploy/public-gateway/README.md) | 独立 App Service HTTPS 网关 |
| [examples](../examples/README.md) | 公网双向、Sales 与 Rich 往返成品和公开版 verification |
| [assets](assets) · [bidirectional/assets](bidirectional/assets) | 公开图像；架构图可编辑源在 assets/figures/src |

## 示例成品

先用 [公网销售 PBIX](../examples/Public-HTTPS-Synthetic-forward-report.pbix) 与
[由它导出的 portable 工程](../examples/Public-HTTPS-Synthetic-portable.pbip.zip) 理解 60。
再用 [Rich 往返成品](../examples/Rich-roundtrip-rc4.pbix) 与
[portable 工程](../examples/Rich-portable-1b49.pbip.zip) 对照两页和 60/200。
definitions 的无缓存工程与各文件摘要见 [示例索引](../examples/README.md)。

## 部署代码与转换输入必须分开

公开源码基于固定 rc6 / 0.2.3，历史候选包不包含在本仓库。
从[仓库首页](../README.md)安装开发依赖，按 01 准备专用 Windows 节点，
并分别配置[网关](../deploy/public-gateway/README.md)、portal 与 worker。
转换输入为 `sample\Synthetic.zip`、完整工程文件夹或受信 PBIX，不是下载的整个仓库 ZIP。

本仓库不包含旧发布 ZIP、原始运行回执、身份库、队列、会话、密钥或凭证。
公开 verification 删除了内部任务 ID 与进程编号，保留实际方向、模式、输入摘要及复开观察；
截图剔除内部运维画面，公网工作台图遮盖个人显示名与任务 ID。
这些公开材料不代替你自己工程的业务验收，也不是脱敏证书。
