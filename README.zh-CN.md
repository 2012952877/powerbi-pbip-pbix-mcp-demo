# PBIP ↔ PBIX：Desktop 转换与受控试点任务入口

本项目把**完整 PBIP 项目 ZIP、项目文件夹或 PBIX**交给同一持久队列，由专用 Windows 普通用户交互会话运行 Power BI Desktop，执行 **File → Save As**，关闭后再用一个新 Desktop 进程复开结果。网页与 MCP 按同一已认证用户查询任务和下载产物，不另建转换后端。旧 `submit_project` 和 `--zip` 正向入口保持兼容。

微软明确说明目前没有官方 PBIP/PBIX 程序化转换 API，支持的转换操作是 Desktop 的 Save As。本项目自动操作这个桌面流程，**不是微软官方无头转换 API**，也不使用 pbi-tools、PBIT、重命名 ZIP 或在线报表替代输出。[1]

公开源码基于 rc7 / **0.2.4**。本文侧重实现、接口和运行参数；
完整阅读入口为[中文文档首页](docs/README.md)，当前合成双向结果见[示例索引](examples/README.md)。
原部署流水、内部任务 ID、进程编号、会话路径和历史发布包不随公开仓库提供。
原文的早期正向限制不能覆盖当前已实现的双向/公网功能；以这里的模式与认证契约为准。

## 受控多人试点与反向转换

网页地址默认为 `http://127.0.0.1:8765/`，同源 HTTP MCP 为 `/mcp`。每人使用独立随机访问凭证，网页登录交换成一小时 HttpOnly、SameSite=Strict Cookie，不把凭证写 URL 或 localStorage；Cookie 变更请求检查 `X-CSRF-Token` 和 Origin。所有任务读取、列表、取消、下载及 MCP resource 都执行服务端 owner 检查，管理员可按 ID 查看他人任务和队列汇总。客户端自报 `owner_id`、`X-User` 不具有身份效力。普通用户不获得全局任务列表或他人文件名。

这是**受信人员、同一安全域内的受控试点**，不是企业 SSO，也不是恶意 M/连接器的沙箱。共享 Windows 账户不会形成跨客户操作系统隔离；多客户或不可信安全域必须独立 worker/部署。默认仅监听 loopback，不提供匿名任务访问、不创建 Entra 应用。显式启用的公网 HTTPS 网关也不改变这条身份与执行安全边界。未来 OIDC 应在 `auth.py` 的身份提供器边界验证 issuer、audience、签名和租户，再建立同样的 `Principal`；不能信任反向代理随意传入的用户头。

安装后，使用实际运行 portal 的专用账户执行以下配置。`setup.ps1` 创建的 `pilot` 目录只允许该账户、SYSTEM 和本机管理员访问；管理员命令只输出用户信息，不输出凭证。通过管理员批准的私密渠道分发各自文件，交付后删除个人凭证文件或继续保存在受 ACL 保护的位置。服务端 `users.json` 仅保留 SHA-256 摘要。

```powershell
$Python = 'C:\PBIPMCP\app\.venv\Scripts\python.exe'
& $Python -m pbip_mcp.admin add-user --auth-config C:\PBIPMCP\pilot\users.json `
  --credential-file C:\PBIPMCP\pilot\alice.token --user-id alice --display-name Alice
& $Python -m pbip_mcp.admin add-user --auth-config C:\PBIPMCP\pilot\users.json `
  --credential-file C:\PBIPMCP\pilot\bob.token --user-id bob --display-name Bob
C:\PBIPMCP\app\scripts\start-portal.ps1 -Root C:\PBIPMCP -AuthConfig C:\PBIPMCP\pilot\users.json
```

这条命令运行真实前台 portal；在该终端按 Ctrl+C 只停止 portal，不终止 worker 或删除任务。状态入口为网页、`pbip_mcp.admin status --data-dir C:\PBIPMCP\data`，或带个人凭证的 HTTP MCP。worker 仍按第 4 节独立启动，必须保持交互桌面解锁。凭证文件/用户配置不会进入部署 ZIP。需要管理员身份时，创建用户显式加 `--role admin`；普通用户不能选择自己的角色。变更用户配置后需重启 portal。紧急撤销：先停止 portal，执行 `pbip_mcp.admin remove-user --auth-config C:\PBIPMCP\pilot\users.json --user-id <用户ID>`，再启动；已移除用户的 Cookie 与 Bearer 都失效，重新添加身份应使用新 ID。不要向普通用户提供 stdio/SSH 运维身份或工作目录读写权限。

| 转换 | 输入与产物 | 数据语义 |
|---|---|---|
| PBIP→PBIX | 完整 ZIP/文件夹；`kind=pbix` | 非内置项目必须在所引用模型目录自带非空 `.pbi/cache.abf`，否则在创建任务、入队和保留文件前返回 `DATA_CACHE_REQUIRED`；不自动刷新任意查询 |
| PBIX→PBIP，默认 definitions | `kind=pbip`，文件为 `report.pbip.zip` | 保留定义/资源，剔除缓存与本机设置；`contains_data=false`、`requires_data_reload=true`，可能需要人工加载数据 |
| PBIX→PBIP，portable | 同样返回 PBIP ZIP | 仅保留本任务 Desktop 导出的非空缓存，缺缓存失败；新进程打开导出的缓存副本，无自动刷新 |

部署命名适配版本后，新任务的文件上传保留原文件名，工程文件夹上传显示原文件夹名；下载采用同一名称主体，例如 `客户销售.zip` → `客户销售.pbix`，`客户销售.pbix` → `客户销售.pbip.zip`，转换记录为 `客户销售.verification.json`。文件夹名称逐字保留，包括其中的点号；没有共同外层文件夹的 API 上传使用 PBIP 入口名。回传 `客户销售.pbip.zip` 时会整体替换 `.pbip.zip` 后缀，不逐次堆叠扩展名。旧任务保留原显示名及 `report.pbix` / `report.pbip.zip` / `verification.json`，不猜测原名或改写历史。

反向不是改扩展名：先校验有本地二进制模型的 PBIX 容器，真实 Desktop 打开、另存完整 PBIP、白名单打包、独立副本新 PID 复开，再交付。未知/加密/损坏容器受控拒绝；PBIP 不支持敏感度标签，遇 Desktop 限制失败，绝不移除标签或自动确认登录/法律/安全提示。`SecurityBindings` 本身不是标签证据，普通未标签 PBIX 也可能含此条目。

rc7 只为 definitions 的复开关闭阶段允许最多两个不同的、已识别且属于当前任务进程的保存决定窗口；每个窗口仅响应一次已启用的 `Don't save` / `Do not save` / `Discard changes` 控件，仍受 25 秒关闭期限与整个转换期限约束。普通关闭路径仍最多一次，第三个或未知提示明确失败；这不是任意弹窗自动点击，也不会刷新或给 definitions 声称离线数据成功。

白名单只允许当前 pointer 引用的单个 report/model 定义、指定静态资源及可选 `cache.abf`、`.gitignore`，不交付 localSettings、CLIXML、运行身份、私钥、令牌或其他任务目录。**不含数据缓存并不等于脱敏：M 查询、TMDL 等定义自身仍可能包含内嵌数据、敏感文本或连接信息，均不会自动移除**；内置 Sales 样例也在定义中包含数据。portable 缓存是数据，应按原始 PBIX 的敏感级别管理。每个任务独立目录与 Desktop 进程，临时 work/export/reopen 副本在正常完成/失败后清理；中断遗留由受控留存维护处理。旧失败不会被改为成功，重试必须新建任务。

`contains_data` 表示是否携带缓存/二进制模型，不等于“业务结果完全等价”或“必有非空数据行”。验证 JSON 的 `data_values_verified` 仅在可信内置样例观察到指定数值时为 true；任意业务模型仍需调用者检查数据与外部连接。转换器不会对所有 DAX/逐行数据作无依据的等价承诺。

```powershell
# 受信本机/SSH stdio 运维入口；旧 --zip 行为不变
& $Python -m pbip_mcp.client --data-dir C:\PBIPMCP\data submit `
  --pbix C:\Input\trusted.pbix --export-mode portable
# 当前用户 HTTP MCP；凭证不出现在命令参数正文中
& $Python -m pbip_mcp.client --url http://127.0.0.1:8765/mcp `
  --token-file C:\PBIPMCP\pilot\alice.token submit --pbix C:\Input\trusted.pbix
& $Python -m pbip_mcp.client --url http://127.0.0.1:8765/mcp `
  --token-file C:\PBIPMCP\pilot\alice.token list
& $Python -m pbip_mcp.client --data-dir C:\PBIPMCP\data download `
  --job-id <任务ID> --kind pbip --output C:\Output\new-project.pbip.zip
```

`convert` 也支持 `--pbix`、`--export-mode` 与显式 `--direction`，输出自动选相应 artifact kind；`--output`/`--verification` 必须不同且不存在。远端 `run-remote-demo.py` 支持同样参数，batch 每项使用二选一的 `zip` 或 `pbix`，可混排 `direction`、`export_mode`；先入同一队列后顺序取回，最多 3 个任务，不把后续提交失败伪装成已回滚前面的任务。

### 可选 Azure 公网 HTTPS 测试入口

`deploy\public-gateway` 是无第三方运行依赖的 Node.js 22+ 流式反向代理。Linux App Service 使用默认托管 HTTPS 域名、HTTPS-only 和 VNet integration；网关通过专用子网访问 Windows VM 私网 portal，不向 VM 公网开放 8765 或 RDP。管理员先建好资源与 VNet，再使用 `deploy-existing-app.ps1` 部署独立网关 ZIP；脚本不会创建资源。配置仅含 `PUBLIC_ORIGIN=https://<app>.azurewebsites.net`、`BACKEND_HOST`/`BACKEND_PORT`（或 `BACKEND_URL`），不能填个人凭证。`PORT` 由 App Service 提供。

VM 用 `scripts\start-public-portal.ps1 -PublicOrigin https://<app>.azurewebsites.net -BindAddress <VM_PRIVATE_IP>` 启动。它显式传入 `--host <VM_PRIVATE_IP> --public-origin ... --allow-public-https`，默认本机启动行为不变。Cookie 的 Secure 属性取决于管理员配置的 HTTPS origin，而不是请求头；HttpOnly、SameSite=Strict 和 CSRF 保留。Uvicorn 禁用 proxy-header 信任，应用仍精确校验 Host/Origin；网关固定上游 Host 和转发协议、保留真实客户端 Origin、不替用户提供身份。`set-public-portal-firewall.ps1 -Action Apply -BindAddress <VM_PRIVATE_IP> -GatewaySubnet <GATEWAY_SUBNET_CIDR>` 只加具名规则，允许 `<GATEWAY_SUBNET_CIDR>` 到 `<VM_PRIVATE_IP>:8765`；部署管理员必须同时确认没有宽泛的既有放行规则，且 NSG 没有公网 8765 放行。

网关以流的背压传递上传和下载，不缓存完整请求体；实际计数上限 24 MiB 用于容纳原 16 MiB 输入的 multipart/base64 封装，后端输入、展开、用户配额不变。Set-Cookie、Content-Disposition、状态和既有 CSP 透传，逐跳头剔除；客户端断开会关闭上游下载流。网关 `/healthz` 真实探测后端匿名身份端点，但只返回健康布尔值，不保证 Desktop ready，转换就绪仍看已登录的 `/api/status`。默认不记录请求头、正文、凭证或访问日志。公网只给受信试用者分发独立凭证，不能把公网可达误称为企业 SSO 或多客户沙箱。

CLI 访问公网 MCP 同样需要显式 `--allow-public-https`：`pbip-client --url https://<app>.azurewebsites.net/mcp --allow-public-https --token-file <本机受ACL凭证路径> status`。默认继续拒绝公网目标；公网选项只接受 HTTPS DNS `/mcp` 地址，不接受明文 HTTP、IP、URL 内凭证、查询参数或重定向。提交、转换、查询和下载共用这条身份传输。

停机使用具名 `stop-public-portal.ps1`，只停止自有 portal，不杀 Desktop/worker；停止前仍应等待任务、上传和下载结束。网关/portal 升级要静止安装并核对 release/app/installed hash，不能热混用模块。不得覆盖旧候选包或回执；公网验收应新建真实任务，不能复用先前私网的成功标签。

需要摆脱本机 SSH 生命周期时，可用 `scripts\manage-public-portal.ps1 -Action Register -PublicOrigin https://<app>.azurewebsites.net -BindAddress <VM_PRIVATE_IP>` 注册唯一 `PBIPMCP-PublicPortal`，再显式 `-Action Start`。任务运行于既有 `pbipdemo` 的 Interactive+Limited 身份、该用户登录触发、最多三次间隔一分钟的失败重启，不存密码。现有凭证/数据 ACL 的校验绑定专用账户，所以不为 SYSTEM 运行放宽 ACL。此形态仍需该账户已登录；注销会失去运行前提，不能宣称独立于 Windows 登录的无人值守服务。公网 portal 不操作 GUI，worker 保持独立且只在 Active、解锁桌面执行；portal 在线时 worker 可以真实显示 offline/queue。

`manage-public-portal.ps1 -Action Status` 只看具名任务和真实 portal 响应；`-Action Stop` 先停用具名任务以防登录/失败自动重启，再只停其自有 portal。再次启用必须管理员显式 `Enable-ScheduledTask -TaskName PBIPMCP-PublicPortal` 后 Start，不会覆盖停用意图。任务日志位于受 ACL 保护的 `pilot-auth-bidir\public-portal-runs`，不记录 HTTP 访问正文或凭证。此 portal 管理器不会配置 worker 的恢复行为。

公网管理的每次 Register/Start/Stop/Status 均需传入相同的 `-PublicOrigin` 和 `-BindAddress`。防火墙帮助脚本保持试点的 `/26` 专用网关子网契约；地址与子网都必须来自自己的批准环境，详细配置见[网关部署入口](deploy/public-gateway/README.md)。

### REST 合同（同源）

成功响应 `{ok:true,...}`，错误使用 HTTP 错误状态和 `{ok:false,error:{code,message}}`，不输出内部路径/凭据/堆栈。`GET /api/session` 返回 `user:{id,display_name,role}` 和 `csrf_token`，未登录 401；`POST /api/session` 用 Authorization Bearer 建 Cookie，`DELETE` 带 CSRF 注销。`GET /api/status` 返回真实 `worker`、`queue`、有限 `limits`、`capabilities`；普通用户只得到 `mine_pending`，全局 `pending/running` 为 null。

`GET /api/jobs` 列本人的最近 100 个任务。`POST /api/jobs` 接受单 `file`（`.pbix`/完整 `.zip`）或多个 `project_files`（filename 为含根目录的 webkitRelativePath），字段 `direction=auto|pbip_to_pbix|pbix_to_pbip`，仅反向接受 `export_mode=definitions|portable`。不能上传孤立 `.pbip` 或服务器路径。所有路径逐项验证，读取实际 body 时限制大小，不只相信 Content-Length。

`GET /api/jobs/{id}` 返回 job；`POST /api/jobs/{id}/cancel` 只取消 queued；`GET /api/jobs/{id}/artifacts/{kind}` 授权 attachment 下载。job 包括 `job_id/status/direction/export_mode/source.name/created_at/error/artifacts`，以及 `phase/contains_data/requires_data_reload/warnings/artifact_status/retention_deadline`。status 为 queued/running/succeeded/failed/cancelled；产物过期单独标为 expired，不改写历史成功。没有伪造百分比或 ETA。

网页在节点不可用时默认不允许直接提交；只有读到明确的不可用状态及有限排队期限，并由用户确认仍要排队，才保留原来的离线入队行为。节点状态未知、刷新失败或未取得有效期限时不能以此确认绕过。忙碌但 `ready=true` 的节点仍可正常排队。REST/MCP 的持久离线队列契约不变，这不是新增服务端访问控制。2026-09-14 已核对演示部署包含该更新；其他环境仍需安装对应版本，不能只凭源码推断服务端已升级。

命名适配使用新任务的可空 `artifact_basename` 元数据；迁移只新增该列，旧任务为 null 并维持既有命名。`job.source.name`、`artifacts[].filename`、MCP `get_artifact` 的 `filename` 和 HTTP 下载头遵循同一命名规则。中文文件名使用 RFC 5987 `filename*=UTF-8''...`，并保留安全 ASCII 回退。名称若无法生成合法的 Windows 下载文件名，在入队前返回 `INPUT_NAME`，不静默截断。CLI 明确指定的 `--output` 路径不被服务器文件名覆盖。

显示/下载名不参与服务端磁盘路径计算；内部仍使用任务独立目录和固定 `report.pbix` / `report.pbip.zip`，不重命名 PBIP ZIP 内的入口、模型、报表目录或引用。升级应停止自有 portal/worker 后安装；原部署可能使用不同的启动脚本参数，不能为命名适配顺带覆盖无关运维脚本或重注册任务。

`worker.last_reported_state` 与 `worker.last_reported_message` 保存最后一次心跳报告，尚无 worker 时为 null；网页、MCP 和管理员状态共用这两个字段。心跳过期后，当前状态仍为 `stale`、`ready=false`，但不再丢失最后一次“会话断连/锁定”等原因。历史报告不证明当前桌面可用，也不能用它自动启动或解锁。`QUEUE_TIMEOUT` 表示领取前超时，不证明源文件损坏；应先恢复节点，再新建任务，不改写旧失败。

### 配额、留存与升级

默认上传 16 MiB、展开 128 MiB、单文件 64 MiB、4096 项；全局待办 32、每人待办 3、每小时每人 30 次/全局 100 次提交、全局及单人留存预算 2 GiB、单任务产物 512 MiB、队列 30 分钟、Desktop 10 分钟。存储预算包含活动任务的输出预留，因此可能早于待办数量上限拒绝提交。portal 同时最多读取两个有界上传，整个 body 最长 120 秒，单次读取等待最长 30 秒。使用类似 `C:\PBIPMCP\data` 的短工作根目录；Windows 的项目路径限制会使深层会话临时目录上传失败，服务会明确返回 `WORKER_PATH_LENGTH`。

`server`、`portal`、`worker` 可使用同一 `--limits-config <JSON文件>` 覆盖 `Limits` 的正整数键；提供 `start-controller.ps1`/`start-portal.ps1` 的 `-LimitsConfig` 包装。不能配置无限大小/超时；实际入队的留存期限会保存在任务记录，不会因后续配置改变而追溯删除旧任务。

默认新任务终态后保留 24 小时，**只实现显式管理员维护，不运行自动后台删除**：

```powershell
& $Python -m pbip_mcp.admin cleanup --data-dir C:\PBIPMCP\data
```

维护一次最多处理 100 个到期新任务，只删除其命名的输入/工作副本/证据/输出，保留数据库历史与维护审计。不会清理 running、正在下载、迁移前的 legacy 任务或 baseline/旧资料。HTTP 流用持久文件租约保护；异常退出留下的 HTTP 租约宁可阻止清理，管理员应在确认没有下载进程后人工检查，而非按时间盲目强删。MCP 跨块下载先调用 `begin_artifact_download`，每次 `get_artifact` 携带返回的 `download_id`，完成或异常后调用 `finish_artifact_download`。随机传输 ID 绑定调用者、任务与产物，每块续租 300 秒，空闲超时后可被维护释放；每位调用者最多 64 个有效传输租约。新版 CLI 自动完成这个生命周期。旧客户端不带 ID 的调用仍兼容，但使用该用户/任务/产物的保守租约，最后一块之后也保留 300 秒，避免一条旧传输结束时影响另一条旧传输。租约过期的显式传输返回 `DOWNLOAD_NOT_FOUND`，需重新开始，而不会默默沿用失效租约。

到期清理后下载明确 410/`ARTIFACT_EXPIRED`，不无声返回空文件。SQLite 迁移给旧任务绑定 `legacy`，只有受信 stdio 运维或明确管理员能读取，普通门户/HTTP 用户不可借此访问。客户端提交按服务端公开的压缩、解压、单文件、元数据、路径深度/长度等有效限额预检；旧服务端未公开的字段沿用既有默认值。配额扫描仅容忍与任务完成清理并发发生的文件消失，权限错误、链接和重解析点仍会拒绝，不将损坏存储默认为零。

安装包同时包含网页静态资源；`setup.ps1` 在安装前清理匹配的旧 build/lib 副本，再比较实际 installed/source 文件哈希（包括 HTML），避免旧构建缓存冒充新部署。不要覆盖以前的代码 ZIP/样例/回执。

## 1. 输入、输出与进程职责

| 环节 | 输入与动作 | 持久化结果 |
|---|---|---|
| MCP controller | 校验完整 ZIP、相对引用、大小及唯一入口；不操作 GUI | `queue.sqlite3`、原始 `source.zip`、`original` 目录 |
| 交互 worker | 领取一个任务，在工作副本上打开 PBIP；样例刷新内嵌数据；真实 Save As | 私有 PBIX、按阶段的进程内 UIA 诊断 |
| 新 Desktop 进程 | 重新打开输出 PBIX，确认报表、页面及模型相关证据 | `verification.json`，随后原子提交 `succeeded` |
| MCP client | 查询状态，按偏移量下载，核对长度和 SHA-256 | 指定桌面的 PBIX，独立 JSON 结果文件 |

controller 可以作为服务运行，但 worker **绝不能由 SYSTEM 或 Session 0 运行**。SQLite/WAL 和本地文件队列连接这两个进程；controller 在线不等于 Desktop 就绪。微软明确指出 WebView2 不支持 SYSTEM，Desktop 也不支持这样运行。[2]

部署只使用一个 worker 和同一个数据目录。队列排他锁、领取事务和进程 Job Object 分别防止双 worker、双领取及拥有者退出后的残留转换进程。原始 ZIP 始终保留，Desktop 只接触 `work` 副本。

## 2. 准备专用 Windows 会话

| 项目 | 要求 |
|---|---|
| 系统 | Windows Server 2022 **Desktop Experience/GUI**，不是 Server Core |
| Desktop | 当前标准 Power BI Desktop x64；不能使用 Report Server 优化版 |
| 运行时 | Python 3.12 x64；依赖锁定在 `requirements.lock` |
| 原生依赖 | Microsoft Visual C++ v14 **x64** Redistributable；否则 `win32ui` 可能因缺少 DLL 而导入失败 [9] |
| 登录 | 专用普通用户 `pbipdemo` 的真实、连接中、未锁定的交互会话 |
| 屏幕 | 建议 1600×900 或更高、100% 缩放、英文 Desktop UI |
| 网络 | 首次安装 Python 包需要访问包源；转换样例不需要访问外部数据源 |
| 端口 | 默认 stdio：零监听端口；可选 HTTP 仅 `127.0.0.1:8765/mcp` |

先人工完成 Desktop 首次启动中的产品提示，并按当前版本的 Options → Preview features 启用 PBIP/PBIR/TMDL（若仍显示为预览）。不得让脚本自动接受法律声明、安全提示、组织策略或登录请求。之后关闭人工打开的 Desktop；转换器只使用自己启动的进程，遇到版本不兼容或未知对话框会明确失败。[1][3][4]

此次环境为 Python **3.12.10**、MCP **2.1.1**、pywinauto **0.6.9**、Desktop **2.157.1354.0**；已关闭“保存到 OneDrive/SharePoint”选项并完成 Desktop 重启，以使用本地 Windows Save As 对话框。`setup.ps1` 现在会实际导入 UIA/原生控件依赖，不再只检查包是否存在。

RDP/Bastion 会话必须保持连接且未锁定；只存在 `explorer.exe`、设置自动登录或计划任务返回“已启动”，都不代表 GUI 可用。Azure Run Command 可以安装软件、解包、注册任务和读取文件，**不能直接执行 Desktop 转换**。

## 3. 安装与首个真实任务

把干净部署 ZIP 解压到 `C:\PBIPMCP\app`。安装脚本可以在管理员/SYSTEM 安装上下文运行，因为它不会启动 Desktop。以下均为虚拟机内 PowerShell 命令。

```powershell
C:\PBIPMCP\app\scripts\setup.ps1 `
  -Root C:\PBIPMCP `
  -WorkerUser pbipdemo `
  -PythonExe 'C:\Python312\python.exe' `
  -DesktopExe 'C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe' `
  -RegisterInteractiveTask
```

如果 Python 实际装在别处，显式修改 `-PythonExe`。脚本创建 venv、安装锁定依赖、预生成 UIA 类型库、配置专用目录 ACL，并可注册 `PBIPMCP-Demo` 任务；任务使用 **Interactive + Limited**，不保存密码，也不会将 SYSTEM 冒充成已登录用户。已存在同名任务时脚本拒绝覆盖。[5][6]

用户已登录并完成上述 Desktop 准备后，二选一启动：

```powershell
# 在 pbipdemo 的交互 PowerShell 中运行：
C:\PBIPMCP\app\scripts\start-demo.ps1 -Root C:\PBIPMCP

# 或由管理员触发已经注册的 Interactive 任务：
Start-ScheduledTask -TaskName PBIPMCP-Demo
```

`start-demo.ps1` 在同一正常用户会话启动真实 worker，然后启动官方 Python MCP SDK 客户端。客户端通过 stdio 启动 controller，依次执行状态查询、完整 ZIP 提交、任务轮询、PBIX 和验证 JSON 下载。没有模型推理调用。

每次生成独立的 `C:\PBIPMCP\runs\<UTC时间-随机后缀>`，内含 `e2e-result.json`、worker/client 日志和 `verification.json`。PBIX 直接写到该用户的 Desktop，文件名为 `Synthetic-converted-<同一后缀>.pbix`；已有文件绝不静默覆盖。主机最终交付时也须下载到主机 `C:\Work` 的新文件名。

合成样例包含一个完整 TMDL 模型、`Sales` 表三行 `A/10、B/20、C/30`、`Total Amount = SUM(Sales[Amount])` 度量值，以及 `Synthetic overview` 页上的卡片。没有 `cache.abf` 的 PBIP 初次打开只有定义、没有缓存数据，因此 worker 会刷新这个**逐文件精确匹配内置样例**的项目，预期卡片值为 **60**。上传 JSON 自称“安全样例”不会获得刷新权限。[4]

## 4. 分开运行 controller、worker 与 MCP client

需要多次演示时，在普通用户交互会话保留一个 worker：

```powershell
C:\PBIPMCP\app\scripts\start-worker.ps1 -Root C:\PBIPMCP
```

其他同机、有数据目录权限的客户端可以通过 stdio 连接各自的 controller，共用一个持久队列：

```powershell
$Python = 'C:\PBIPMCP\app\.venv\Scripts\python.exe'
& $Python -m pbip_mcp.client --data-dir C:\PBIPMCP\data status
& $Python -m pbip_mcp.client --data-dir C:\PBIPMCP\data submit `
  --zip C:\PBIPMCP\app\sample\Synthetic.zip
& $Python -m pbip_mcp.client --data-dir C:\PBIPMCP\data job --job-id <提交返回的32位ID>
```

提交动作在 worker 离线时也可以成功入队；`convert` 一站式客户端则先等待有限时间的 worker 就绪，避免把“排队成功”当成“已转换”。每个 stdio controller 的 stdout 仅用于 MCP，诊断走 stderr。

MCP host 的 stdio 配置示例：

```json
{
  "mcpServers": {
    "pbip-desktop-converter": {
      "command": "C:\\PBIPMCP\\app\\.venv\\Scripts\\python.exe",
      "args": ["-m", "pbip_mcp.server", "--data-dir", "C:\\PBIPMCP\\data"]
    }
  }
}
```

| MCP 工具 | 主要参数 | 返回 |
|---|---|---|
| `converter_status` | 无 | 交互 worker 心跳/就绪状态、大小和超时限制 |
| `submit_project` | `archive_base64`，可选 ZIP `sha256` | `job_id`、`queued` 状态、原始 ZIP 摘要 |
| `submit_pbix` | `pbix_base64`、可选 `sha256`、`export_mode` | 同一持久队列中的反向任务 |
| `list_jobs` | 无 | 当前认证用户的最近任务 |
| `get_job` | `job_id` | 状态、明确错误、成功后产物信息 |
| `cancel_job` | `job_id` | 仅取消尚未被领取的任务 |
| `get_artifact` | `job_id`、`kind`、`offset`、`length` | Base64 块、下个偏移、EOF、总长度及 SHA-256 |

`kind` 为 `pbix`、`pbip` 或 `verification`，单块最大 256 KiB；没有文件路径、任意程序、任意命令或输出目录参数。`pbip://jobs/<job_id>` 资源只返回授权任务元数据。错误同时设置 MCP `isError`，并提供结构化 `error.code` 和 `error.message`。[7]

确需 HTTP 时，必须配置前述个人凭证；不能仅凭 loopback 省略认证：

```powershell
C:\PBIPMCP\app\scripts\start-controller.ps1 `
  -Root C:\PBIPMCP -Transport streamable-http -Port 8765 -AuthConfig C:\PBIPMCP\pilot\users.json
```

默认 stdio/本机 HTTP 入口只绑定 `127.0.0.1`；公网 HTTPS 使用前述显式网关配置，保留身份与请求体限制。不得添加 `0.0.0.0`、打开匿名公网入站、或用未认证的公网转发器。本轮支持应用层多人 owner 权限，但不是跨客户 OS 沙箱或企业 SSO。

### 本机通过 SSH stdio 复跑

管理员路径见[运行手册第 6 节](docs/02-日常复跑与运维.md)。
使用自己的订阅、Bastion、VM 和受保护 SSH 身份文件，保持严格服务器指纹检查。
通道只传递管理命令与文件，不创建已解锁的交互桌面。
不要把历史会话目录、其他租户的环境凭据或共享 SSH 身份当成长期部署配置。

## 5. 什么才算成功，失败后怎么办

| 状态 | 含义 | 可执行动作 |
|---|---|---|
| `queued` | 完整原件已落盘，尚未操作 Desktop | 查询或取消；默认 30 分钟未领取则失败 |
| `running` | 排他 worker 已领取，正在操作自己的 Desktop | 等待；整个转换最多 600 秒 |
| `succeeded` | 真正 Save As、容器结构核对、新 Desktop 重新打开均通过 | 分块下载及核对 SHA-256 |
| `failed` | 校验后执行失败、超时或 worker 中断 | 查看错误代码和私有诊断，修复前提后重新提交 |
| `cancelled` | 用户在领取前取消 | 原件保留，不会出现假成功产物 |

结构核对要求 PBIX 包含非空二进制 `DataModel`、报表页和相同数量的视觉对象；只有 `DataModelSchema` 的模板或改扩展名的输入 ZIP 会被拒绝。结构本身并不能证明报表可用，所以还要求两个不同的自有 Desktop PID，以及第二次真实打开后的报表就绪证据。对于合成样例，现场最终还应确认模型/度量值和卡片 **60**，不能把空数据模型当作演示完成。

公开示例包含真实 Desktop 保存与新进程复开的既有观察，
见 [verification 与成品](examples/README.md)。每次独立保存的二进制摘要可能不同；
核对本次输入、产物与下载字节，不把两个独立保存的摘要不同当作转换失败。

实跑中，UIA `SetValue` 虽能读回完整文件名，Windows Save As 却仍按默认名称保存到 `work\Synthetic.pbix`。现有 worker 改为只对已核验 PID/HWND 的原生文件名 Edit 使用 `EM_REPLACESEL`，触发编辑变更通知，再核对原生与 UIA 值、实际目标文件和新进程复开。第 16 轮及外部新任务均直接生成 `output\report.pbix`；旧失败任务没有被改状态，也没有通过搬移默认位置产物冒充成功。Save 阶段 120 秒未完成会明确失败。

controller 不会伪造 worker 心跳。心跳超过 15 秒即显示 `stale`；如果运行期限及清理宽限期也已超出，则任务查询将其标为 `WORKER_TIMEOUT`，并拒绝旧租约提交迟到的成功。意外退出留下的其他 `running` 任务在持有排他锁的新 worker 启动时标为 `WORKER_INTERRUPTED`，不自动重试、不覆盖旧任务。交互 worker 的超时监督会终止已明确拥有的进程 Job，不按进程名称杀掉其他 Desktop。

| 现象或错误 | 排查及修复 |
|---|---|
| `WORKER_NOT_READY` / Session 0 / SYSTEM | 进入真实正常用户会话，用 Interactive 任务启动；不要通过 Run Command 运行 worker |
| 会话断开或锁定 | 重新连接并保持解锁，再提交新任务；旧失败任务仍保留 |
| Desktop 首次启动、升级、登录、安全或未知对话框 | 人工检查对应自有窗口及私有诊断；完成批准的准备步骤，不修改脚本去盲点“确定” |
| `INCOMPLETE_PROJECT` | ZIP 必须同时包含 `.pbip`、Report、SemanticModel，不能只有入口文件 |
| `AMBIGUOUS_PROJECT` | 每个 ZIP 只能有一个 `.pbip`，且该入口只引用一个报表 |
| `REMOTE_MODEL_UNSUPPORTED` | 本切片不支持仅在线模型连接、发布或容量工作流 |
| `PENDING_MODEL_CHANGES` | 先在 Desktop 应用或丢弃 Power Query 未应用更改，再打包 |
| `ZIP_BOMB` / 大小或数量限制 | 默认压缩 ZIP 16 MiB、展开 128 MiB、单文件 64 MiB、4096 项、压缩比 200 |
| `QUEUE_FULL` / `STORAGE_QUOTA` | 队列上限 32，原件及工作空间总预算 2 GiB，单产物预留 512 MiB；实际可接受任务数还受预留量限制 |
| `PBIX_PAGES_CHANGED` / `PBIX_VISUALS_CHANGED` | 不交付可疑结果，检查 Desktop 版本及对应报表定义 |
| `CLIENT_OUTPUT_EXISTS` | 使用新输出路径；不会覆盖已有交付文件 |
| `UIA_UNAVAILABLE` / `win32ui` DLL load failed | 安装微软签名的 Visual C++ v14 x64 Redistributable，再重新运行 setup 的实际导入检查 |
| TMDL `InvalidLineType` / PBIR 打开后没有页签 | 本样例的 `ref table Sales` 必须顶格；`definition.pbir` 是 `4.0`，`definition\version.json` 是 Desktop 实际生成的 `2.0.0`，两者不是同一种版本号 |
| 修改源码但 VM 行为未变化 | 本次发现 setuptools 复用了时间戳较新的 `build\lib` 旧副本；重装前只清理对应的生成文件，并比较 `src\pbip_mcp` 与 venv 已安装文件的 SHA-256，不能仅凭 pip 返回成功认定补丁已生效 |

## 6. 数据边界与保留策略

ZIP 拒绝路径穿越、绝对/UNC/盘符路径、Windows ADS/保留名称、符号链接/reparse point、大小写和 Unicode 名称冲突、加密/不支持压缩、异常展开尺寸及歧义入口。提取使用新私有目录、实际字节计数和 ZIP CRC，引用必须留在提交的项目内部。

**这些检查不是 Power Query 或自定义视觉对象的沙箱。** 只允许受信任操作者提交自包含的合成项目。普通项目可能包含 M、连接器、外部资源或缓存；本切片不声称能安全执行任意互联网 PBIP，也不会自动刷新非精确匹配样例。专用虚拟机内不放客户数据、云凭据、个人令牌或其他项目；转换核心不需要 Azure 身份或 LLM；可选部署脚本另需管理员批准的 Azure CLI 登录。

所有原件、工作副本、结果和私有错误证据留在本地专用目录，ACL 只给 worker 用户及 SYSTEM/管理员所需权限。容量预算达到上限后拒绝新任务；到期新任务由前述显式管理员维护处理，旧任务不自动清理。人工归档/删除应先停止 worker，明确定位单个任务的 ID 和目录；不递归清理桌面、用户目录或整个会话根目录。worker 和 controller 分账号运行时须由管理员显式授权同一个数据目录，不能改为 Everyone 完全控制。

## 7. 开发与打包

在项目根目录 PowerShell 中运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -q
.\.venv\Scripts\python.exe scripts\validate_sample.py
```

`validate_sample.py` 只向微软公开 schema 仓库读取 JSON Schema，不上传项目；它校验八个 JSON 定义，TMDL 与实际 Desktop 支持仍由真实打开确认。单元测试不会启动本机 Desktop，也不会接触宿主 UI。协议测试使用官方 SDK 的进程内、真实 stdio 及 loopback HTTP 传输；假 PBIX 都明确标为 unit-only。

发布源码见当前仓库；不附修复前版本或历史发布包。
rc7 打包器固定 ZIP 时间戳、成员顺序与文件属性，相同输入可重复生成相同字节；同时生成逐成员 SHA-256 manifest，逐文件匹配与卡片值要求不变。

需要构建新发布版本时选择尚不存在的输出路径，不覆盖已固定的发布包：

```powershell
.\scripts\build-package.ps1 -OutputZip ('C:\Work\powerbi-pbip-pbix-mcp-demo-' + (Get-Date -Format yyyyMMdd-HHmmss) + '.zip')
```

打包器按白名单收集源码、脚本、测试、依赖锁、说明及完整合成 PBIP/ZIP；不包含 venv、缓存、运行数据、令牌、SSH 密钥或私有诊断。包或 manifest 路径已存在均报错，不覆盖；`-Manifest` 可显式指定 manifest 路径。打包和源文件都不自动上传 GitHub。

## 8. 可重复运行与扩展验证

扩展场景、恢复条件及公开证据见[场景与边界](docs/03-验证结果与边界.md)。

### 8.1 管理同一个正常用户交互 worker

在 **VM 内 PowerShell**，用同一入口管理 `PBIPMCP-Worker`：

```powershell
$Manage = 'C:\PBIPMCP\app\scripts\manage-worker.ps1'
& $Manage -Action Doctor
& $Manage -Action Start
& $Manage -Action Status
```

`Start` 在任务不存在时创建 **Interactive + Limited** 任务，存在时核对专用账号 SID、执行程序、脚本、工作目录和有界参数；不覆盖不匹配的同名任务。已经运行且心跳正常时返回 `already_running=true`，不会重复启动 worker。`Stop` 只请求这个 worker 在当前转换结束后退出，队列原件保留；30 秒还未退出会明确报告“停止仍在等待”，不是强杀当前转换。需要等待完整转换结束时可将 `WaitSeconds` 增大到 700。已停止的任务再次 Stop 返回 `already_stopped=true`。

`Doctor` 检查 Python、Desktop、UIA/原生控件依赖、磁盘和 worker 心跳，不启动 Desktop。**Doctor 的环境检查通过，不等于 worker 已就绪。** 从 SSH 调用时 `caller_session` 显示 Session 0 是正常诊断结果；转换可用性要看独立的 `worker.ready`。本机外部控制与 worker 仍然分处不同会话。

新注册任务仍默认空闲 **30 分钟**退出，有限空闲模式的计划任务最长 **2 小时**。现有演示部署已显式配置 `IdleTimeout=0`，对应计划任务 `ExecutionTimeLimit=PT0S`，不再受这两项退出期限影响；单次转换仍有 600 秒上限。无限空闲本身不是会话恢复或可用性承诺，RDP 仍需 Active 且未锁定。`Manual` 模式断连退出后须先恢复桌面，再显式 Start；`SessionAware` 的等待与恢复行为见下节。旧失败任务都不会自动变成成功。

已注册任务保留 `ReviewSeconds=20`：每个已验证的富样例页面停留 20 秒，仍受总超时监督。正常新注册任务默认 0；Start/Status 保留并显示既有 Review/Idle 参数，不偷偷修改。显式修改配置须先安全 Stop，确认任务 Ready、没有运行中的队列任务且 worker 不为 ready，再执行 `Configure -IdleTimeout 0`；不传 `ReviewSeconds` 就保留原值。日常 Start 不需要重复 Configure；停机流程见[运行手册](docs/02-日常复跑与运维.md)。

### 可选会话恢复模式

2026-09-14 已核对演示部署启用了 `SessionAware`，并实际观察到 `waiting_for_session` 状态。这里的“可选”表示新安装仍默认 `Manual`，不是线上尚未启用。额外的云端会话控制器仍未启用；本节只描述已部署的 Windows Worker 恢复机制。

`worker --wait-for-session --idle-timeout 0` 增加本进程内的恢复等待，默认仍采用原有退出行为。仅当已处于非零交互会话、探测结果为 `RDP_SESSION_INACTIVE`、`DESKTOP_LOCKED` 或 `SESSION_PROBE_FAILED` 时等待；SYSTEM、Session 0、非 Windows、未知错误和安装路径错误仍受控退出。等待时每秒重新探测并发布 `ready=false`、`state=waiting_for_session`，不启动 Desktop、不领取任务，不把“进程还活着”当作“转换可用”。原始原因继续包含在最后心跳中，进入等待和恢复会写私有运行日志；没有外发邮件或 Teams 告警。

桌面恢复 Active、未锁定且运行前提有效后，本进程继续领取新任务。等待期间排队期限仍被维护，超期任务仍为 `QUEUE_TIMEOUT`；转换途中断连的任务仍由原有限监督失败并清理自有进程，恢复后不重放。显式 `worker.stop` 在任何任务恢复和领取前检查；保持该标记时不会把旧 running 记录改为失败或启动新转换。已运行的转换仍按原 Stop 契约先结束，再退出，不因停止请求强杀。

Windows 包装脚本的 `-RecoveryMode SessionAware` 将该参数接入现有具名任务，并配置同一普通用户的 AtLogOn 触发、三次失败重启（间隔一分钟）及无限任务执行期限；必须同时使用 `IdleTimeout=0`。`Manual` 为兼容默认，不因为装了新源码就偷偷修改现有任务。恢复不新增账号、服务平台或桌面会话；注销、VM 重启后仍需要获准的用户登录，锁屏仍需要正常解锁。通过 Task Scheduler 显式禁用的任务不会被 Start 擅自启用。

部署此模式会改变 worker 的 action/settings/triggers，必须在单独批准的维护窗口备份后进行；不能沿用仅替换 runtime 两处文件时“任务定义完全未改动”的验收结论。原部署的 portal 脚本参数契约仍需保留。具体配置步骤与实际断连验收见运行手册；单元测试和脚本边界测试不等于无人值守可用性证据。

在 **本机 PowerShell**，先按[运行手册第 6 节](docs/02-日常复跑与运维.md)建立隧道，再通过窄化 SSH 入口运行相同操作。下面示例统一使用 **50027**；改用其他空闲端口时同时更新隧道与客户端：

```powershell
Set-Location 'C:\Work\powerbi-pbip-pbix-mcp-demo'
$Identity = Read-Host 'Approved SSH private-key file path'
$KnownHosts = Read-Host 'Approved pinned host-key file path'
$Connection = @{
  Port = 50027
  Identity = $Identity
  KnownHosts = $KnownHosts
  HostKeyAlias = 'approved-worker-host'
}
.\scripts\remote-worker.ps1 @Connection -Action Doctor
.\scripts\remote-worker.ps1 @Connection -Action Start
.\scripts\remote-worker.ps1 @Connection -Action Status
```

该外部管理脚本只允许 Register/Configure/Start/Stop/Status/Doctor，使用固定的 `C:\PBIPMCP\app` 入口，不提供远端任意命令选项。SSH 仍是专用密钥认证、严格主机指纹检查和 loopback 端口。当前密钥没有搬迁、轮换或进入源码包；持续部署应由管理员在 ACL 限定的用户配置/凭据位置提供新身份，并把路径传给脚本，而不是把会话临时身份包装成长期共享凭据。

### 8.2 富样例与小批量提交

`Synthetic.zip` 保留原来的 60 总额样例。新增 `Rich.zip` 是精确白名单内的第二个完整项目：中文、空格及 Ω 项目文件名，两页中文显示名，5 个视觉对象（card、clusteredColumnChart、tableEx），`Sales` 与“产品 维度”的关系，以及“加权 金额”度量值。独立预期为 `10×2 + 20×3 + 30×4 = 200`；新进程在两个页面分别读到 200 才能通过扩展验收。修改任一文件不会继承自动刷新授权。

重新生成时在**项目根目录**选择不存在的目标目录/ZIP：

```powershell
.\.venv\Scripts\python.exe -m pbip_mcp.synthetic `
  --variant rich --directory .\sample\Rich-new --zip .\sample\Rich-new.zip
```

`run-remote-demo.py` 仍兼容原来的单次 convert 参数，现在也有 `--action status|submit|job|cancel|download`；默认 action 是 convert。批量输入是含 `zip`、`output`、`verification` 的 JSON 数组，路径建议写绝对路径。示例在本机生成两个独立输出后提交：

```powershell
$Prefix = 'C:\Work\PBIP-MCP-Batch-' + (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
$Batch = @(
  @{
    zip = (Resolve-Path .\sample\Synthetic.zip).Path
    output = "$Prefix-A.pbix"
    verification = "$Prefix-A.verification.json"
  },
  @{
    zip = (Resolve-Path .\sample\Rich.zip).Path
    output = "$Prefix-B.pbix"
    verification = "$Prefix-B.verification.json"
  }
)
$Batch | ConvertTo-Json -Depth 4 | Set-Content "$Prefix.batch.json" -Encoding utf8
.\.venv\Scripts\python.exe .\scripts\run-remote-demo.py `
  --port 50027 --identity $Identity `
  --known-hosts $KnownHosts --host-key-alias approved-worker-host `
  --batch "$Prefix.batch.json" --result-json "$Prefix.e2e.json" --timeout 900
```

批量演示限定 **1～3 个任务**，先提交再逐个查询和下载，worker 仍串行运行。输出文件重复或已有文件时，提交前拒绝整批；已经成功提交的任务如果之后遇到队列/输入错误，其 ID 会保留在失败结果中，需要显式查询/取消，不自动丢弃。部分成功时 `.e2e.json` 保留每项结果并返回 `ok=false`，不能把“下载了前两个文件”解读为整批成功。

### 8.3 场景范围

Sales 的合计为 60；Rich 保留两页、5 个标准视觉对象、中文/空格/Ω 和关系计算，
两个页面的加权金额观察为 200。既有双向和公网样例见 [examples](examples/README.md)。
坏输入、取消和 owner 检查由协议与单元测试覆盖；真实交互断线的历史任务失败并保留失败状态。
恢复后应新提交任务，而不是修改旧失败记录。

仍不承诺任意外部连接器、凭据提示、中文 Desktop 菜单、复杂模型或客户 PBIP 全语义等价。
详细限制和各类观察的证据身份见[结果与边界](docs/03-验证结果与边界.md)。

## 参考资料

[1] 微软 Power BI Desktop projects 概览、入口结构、预览开关及转换 FAQ：`https://learn.microsoft.com/en-us/power-bi/developer/projects/projects-overview`

[2] 微软 Desktop 安装、系统/显示要求及不能以 SYSTEM 运行的限制：`https://learn.microsoft.com/en-us/power-bi/fundamentals/desktop-get-the-desktop`

[3] 微软 PBIR 报表定义、相对模型引用及公开 Schema：`https://learn.microsoft.com/en-us/power-bi/developer/projects/projects-report`

[4] 微软语义模型文件、TMDL、无 cache.abf 时无数据的行为：`https://learn.microsoft.com/en-us/power-bi/developer/projects/projects-dataset`；TMDL 语法：`https://learn.microsoft.com/en-us/analysis-services/tmdl/tmdl-overview`

[5] 微软计划任务主体的 Interactive 登录和 Limited 权限：`https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal`

[6] 微软 icacls ACL 参数：`https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/icacls`

[7] 官方 Python MCP SDK 及 Client 用法：`https://github.com/modelcontextprotocol/python-sdk`；`https://py.sdk.modelcontextprotocol.io/client/`

[8] 微软公开 PBIP/PBIR/模型 JSON Schema：`https://github.com/microsoft/json-schemas/tree/main/fabric`

[9] 微软 Visual C++ Redistributable 官方下载及系统要求：`https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist`

[10] Azure Bastion 原生客户端隧道及先决条件：`https://learn.microsoft.com/en-us/azure/bastion/connect-vm-native-client-windows`
