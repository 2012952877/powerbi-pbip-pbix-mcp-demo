# Power BI PBIP ↔ PBIX MCP Demo

把已有 **PBIX 成品导成可编辑 PBIP 工程**，也把完整 **PBIP 工程保存为 PBIX**。网页与 MCP 接入同一持久队列，由受控 Windows 桌面上的 Power BI Desktop 实际执行 **Save As**，再用新进程重新打开产物。

微软[没有提供公开的 PBIP/PBIX 程序化转换 API](https://learn.microsoft.com/en-us/power-bi/developer/projects/projects-overview#frequently-asked-questions)。这是受控 Desktop 自动化 Demo，**不是官方无头转换服务**，不靠重命名 ZIP、PBIT 或在线报表代替成品。

## 为什么需要 / 能做什么

PBIP 的文本定义适合编辑、代码审阅与版本管理；PBIX 适合 Desktop 文件交付。本项目连接这两种工作方式，转换本身不调用大模型。

| 能力 | 范围 |
|---|---|
| 双向转换 | 完整 PBIP 文件夹 / ZIP → PBIX；带本地模型的 PBIX → PBIP ZIP |
| definitions / portable | 默认仅定义、不带缓存；可选携带本任务缓存以离线打开或回转 |
| 网页与 MCP | 个人登录、上传、我的任务、排队取消、成品及 verification 下载 |
| 多用户 owner 授权 | 网页与 HTTP MCP 共用身份、队列和产物，服务端逐次检查归属 |
| 公网网关 | Azure App Service HTTPS → VNet → Windows 私网 portal |
| 合成示例 | 销售合计 60；多页、中文、关系与加权合计 200 |

## 架构

```text
Browser / HTTP MCP
        |
  App Service HTTPS gateway
        | private VNet
  Authenticated portal + MCP ---- SQLite / per-owner job files
                                         |
                              one interactive Windows worker
                                         |
                              Power BI Desktop Save As
                                         |
                              fresh-process reopen
                                         |
                              PBIX / PBIP ZIP + verification
```

管理员另用 Bastion / SSH 维护环境；普通用户不领取 SSH 密钥。portal 与 worker 分别启停：网页可达不等于桌面就绪。

## 在线演示

**入口：<https://pbip-mcp-bidir-2d79425f.azurewebsites.net>**

受控试用需要维护者单独分配个人访问凭证，**仓库不提供登录密码或 token**。它不是微软账号登录，也未接入企业 SSO。可用性取决于演示窗口与交互桌面状态；操作步骤见 [线上版本使用说明](docs/04-线上版本使用说明.md)。

## 快速开始

开发环境：Windows、Python 3.12 x64；网关测试另需 Node.js 22+。真实转换还需标准版 Power BI Desktop、Visual C++ x64 运行库、英文 Desktop UI，以及保持连接且解锁的专用普通用户桌面。先由有权人员处理首次启动、格式选项和法律 / 安全提示。

在仓库根目录 PowerShell 安装并运行本地测试（不会触发真实转换）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -q
npm --prefix .\deploy\public-gateway test
```

部署不要直接从开发目录启动公网服务。按 [环境与安装指南](docs/01-完整方案与实操指南.md) 将代码部署到专用 Windows 节点；分别准备 [worker 管理](scripts/manage-worker.ps1)、[portal 管理](scripts/manage-public-portal.ps1) 与 [App Service 网关](deploy/public-gateway/README.md)。公网脚本要求显式提供本机私网地址，不能照抄别人的部署参数。转换输入使用 `sample\Synthetic.zip` / `sample\Rich.zip`，**不是整个仓库 ZIP**。

## 文档与样例

| 入口 | 内容 |
|---|---|
| [中文文档首页](docs/README.md) | 完整阅读路线 |
| [完整方案与实操指南](docs/01-完整方案与实操指南.md) | 概念、机制、安装、MCP、恢复、替代路线与成本边界 |
| [日常复跑与运维](docs/02-日常复跑与运维.md) | 用户操作、管理员恢复、个人身份、留存 |
| [验证结果与边界](docs/03-验证结果与边界.md) | 场景观察与不适用条件 |
| [实现与运行参考](README.zh-CN.md) | 接口、配额、进程和开发细节 |
| [合成示例成品](examples/README.md) | 公网双向 / Sales / Rich 产物、公开版 verification、文件摘要 |

## 安全边界

只接受获准的受信任工程。worker 必须在 **Interactive + Limited、已连接且解锁**的普通用户桌面中串行工作，不能用 SYSTEM / Session 0 代替。共享 Windows 账户不是恶意 M 查询沙箱；不可信客户或不同安全域应使用独立 worker、存储、网络和操作系统隔离。

**definitions 不等于脱敏**：M、TMDL、参数及连接定义仍可能有内嵌数据或敏感内容；portable 还携带模型缓存。非内置工程不自动刷新，缺缓存明确失败。未知登录、安全、法律或保护提示不自动绕过。凭据、用户库、队列、运行诊断及内部部署回执不进入仓库。

## 状态与许可证

源码基于固定 rc6 / **0.2.3**，公开副本将私有部署参数改为显式输入；保留既有合成结果，不把本地单元测试冒充新的云端转换。样例观察不代表任意模型语义等价或生产级无人值守承诺。

**未附加许可证，使用前请联系维护者。** Public 可见不等于已授予开源许可证。
