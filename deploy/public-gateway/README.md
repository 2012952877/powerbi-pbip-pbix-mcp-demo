# App Service HTTPS 网关

这是 rc6 源码中的独立 Node.js 网关组件（`package.json` 版本 0.2.2），不运行 Power BI Desktop，也不持有用户凭证。请求通过 App Service 的 HTTPS 入口与 VNet integration 到 Windows 私网 portal，后端继续执行个人身份、Origin / CSRF 和 owner 授权。

## 前置条件与配置

部署前由管理员批准并准备已有 App Service、Linux Plan、专用集成子网与 Windows VM。配置 NSG 和 Windows Firewall，仅允许网关子网访问后端 8765；VM 的转换端口和 RDP 不向公网开放。设置 HTTPS-only、关闭 FTP/SCM 基本发布凭据，使用获准的 Azure 身份部署。这里的部署脚本不创建资源，也不替你完成 VNet / NSG 配置。

| 参数 | 取值要求 |
|---|---|
| `PUBLIC_ORIGIN` | 自己的 `https://<APP_NAME>.azurewebsites.net`，无路径、端口或尾斜杠 |
| `BACKEND_HOST` / `BACKEND_PORT` | 自己的 VM RFC1918 私网 IPv4 地址 / 8765 |
| `BACKEND_URL` | 可选，替代 host/port；`http://<VM_PRIVATE_IP>:8765`，不含凭据、路径或查询参数 |
| `PORT` | 由 App Service 提供 |

这些变量只用于地址配置，**不得填入 token、Cookie 或密码**。Node.js 22+ 可直接运行 `npm test`；没有第三方运行依赖。

## Windows 后端入口

先按[安装指南](../../docs/01-完整方案与实操指南.md)准备 `C:\PBIPMCP\app`、Python 与专用普通用户 `pbipdemo`。
用该用户建立受 ACL 保护的 `C:\PBIPMCP\pilot-auth-bidir\users.json`，个人凭证经私密渠道分配，不提交 Git。

在 VM 的管理员 PowerShell 中，先替换下列占位符：

```powershell
$PublicOrigin = 'https://<APP_NAME>.azurewebsites.net'
$BindAddress = '<VM_PRIVATE_IP>'
$GatewaySubnet = '<GATEWAY_SUBNET_CIDR>'
$Manage = 'C:\PBIPMCP\app\scripts\manage-public-portal.ps1'
& $Manage -Action Register -PublicOrigin $PublicOrigin -BindAddress $BindAddress
& $Manage -Action Start -PublicOrigin $PublicOrigin -BindAddress $BindAddress
& $Manage -Action Status -PublicOrigin $PublicOrigin -BindAddress $BindAddress
C:\PBIPMCP\app\scripts\set-public-portal-firewall.ps1 `
  -Action Apply -BindAddress $BindAddress -GatewaySubnet $GatewaySubnet
```

防火墙帮助脚本保留本 Demo 的专用 **IPv4 `/26`** 子网契约；不同子网设计需单独审阅，不使用宽泛来源绕过检查。脚本核对具名既有规则，不覆盖不匹配配置；同时检查是否存在其他宽泛放行规则。

`Register` 仅用于首次安装。现有任务应先 `Status`；任务主体、脚本、origin、地址或参数不匹配时明确失败，不自动重写运行节点。公开版新增必填 `-BindAddress`，不能直接覆盖原部署中的已注册任务；迁移须在批准维护窗口内由管理员停用并审查原任务，再按新参数注册。每次管理调用都传相同地址与 origin。

portal 任务是 **AtLogon / Interactive / Limited**，需要 `pbipdemo` 已登录，不保存 Windows 密码，也不自动登录。worker 另用 `manage-worker.ps1` 管理，还要求 Active、未锁定的交互桌面。公网网页可达不等于 worker ready。

## 部署已有 App Service

在仓库根目录 PowerShell 中，将网关文件打包到一个不存在的新路径：

```powershell
$GatewayZip = Join-Path ([Environment]::GetFolderPath('Desktop')) 'pbip-public-gateway.zip'
if (Test-Path -LiteralPath $GatewayZip) { throw 'Choose a new ZIP filename.' }
Compress-Archive -LiteralPath .\deploy\public-gateway\server.js, `
  .\deploy\public-gateway\package.json -DestinationPath $GatewayZip
.\deploy\public-gateway\deploy-existing-app.ps1 `
  -Subscription '<SUBSCRIPTION_ID>' -ResourceGroup '<GATEWAY_RESOURCE_GROUP>' `
  -AppName '<APP_NAME>' -GatewayZip $GatewayZip -BackendHost '<VM_PRIVATE_IP>'
```

仅在自己的批准环境执行。`deploy-existing-app.ps1` 使用 Azure CLI，更新现有 Web App 的 HTTPS、启动、应用设置及部署文件；会重启该应用，不应在活动上传、转换或下载期间执行。它不修改 Windows VM、VNet 或 NSG。

`/healthz` 检查后端匿名身份端点是否返回预期的认证拒绝，不证明 Desktop 已就绪。网关真实流式转发上传/下载，限制实际请求字节，保持用户身份与响应状态；不记录凭据或请求正文。调试不得关闭 owner、Origin 或 CSRF 检查。

公开演示使用固定维护者域名；**新部署使用自己的域名与地址**，不要把公开 Demo 当作自己后端。完整用户流程见[线上说明](../../docs/04-线上版本使用说明.md)。
