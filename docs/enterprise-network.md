# 企业网络部署与恢复

Status: current

企业网络部署属于首次安装路径。VAWS 在缺少 Fork 或依赖阶段时发现已有网络配置，
复用系统证书，检查 GitHub、PyPI 索引与一个锁定制品，然后保存本地传输配置。
完成初始化的工作区和已就绪环境不会因为普通任务重新做网络探测。
显式修复使用下列入口；它们只需要现有 Python 3.10+ 和标准库，不先下载 VAWS 依赖。

```text
python .agents/scripts/vaws_network.py status
python .agents/scripts/vaws_network.py certificates
python .agents/scripts/vaws_network.py check --apply --native
```

`status` 只读本地配置与上次结果。`certificates` 将系统已信任的根证书整理为
工作区证书包。`check` 比较已有路线，`--apply` 保存选择，`--native` 再分别验证
真实 Git 传输和 gh 账号认证。失败返回非零退出码，不把 HTTP 成功当成完整部署成功。
恢复初始化仍用 `vaws_init.py apply`，已完成阶段和用户选择保留。

## 代理与目标路由

发现来源包括大小写 HTTP(S)/ALL_PROXY 环境变量、Git 的 GitHub URL 匹配配置、
Windows WinINET 和 WinHTTP。最多比较六条已有路线，包含继承配置和直连。
不会扫描内网端口或猜测代理地址。代理凭据在执行时读取，不写入报告、Git URL 或配置快照。
PAC/WPAD 配置会被标记为需要解析；当前版本不会执行 PAC 或猜测其返回地址。
只有 PAC 的环境需要提供已解析的显式代理，或使用已配置且受支持的系统代理。

GitHub、PyPI 制品与模型服务分别选择路线；速度差不足 25% 时保留可用的继承路线。
成功的直连目标加入本地 NO_PROXY，同时保留用户已有绕过规则及 loopback。
GitHub 的显式选择还通过子进程 `GIT_CONFIG_COUNT` 配置 URL 级 `http.proxy`，
避免 Git 自己的代理设置覆盖已经验证的选择。不更改全局 Git、注册表或用户环境变量。
保存的是来源引用；来源消失时要求重新检查，不继续使用陈旧凭据。

网络变化后再执行 `check --apply`。所有路线失败时保留旧配置；部分成功会更新相应目标，
报告仍为 partial。小样本只说明当次可达性与速度，不证明全部 CDN、LFS 或制品主机都可用。
模型检查目前覆盖元数据，实际模型下载与完整性仍以知识包的 setup 结果为准。

## 证书的来源、补齐和轮换

先复用信任，再考虑取得缺失材料。Windows 浏览器、gh 的原生 TLS、Python、requests、
Git/OpenSSL、uv 可能使用不同证书库。系统已经信任企业代理 CA 时，开发工具报错通常
需要连接这些信任来源，无需用户重新取得 CA，也无需管理员权限。

独立分发的 Python 还可能保留构建机器的 OpenSSL 路径。默认上下文没有加载根证书时，
VAWS 会读取操作系统维护的标准 CA bundle 路径，再生成工作区证书包。

`certificates` 从系统有效信任上下文导出公开 CA，记录组合包 SHA256；后续子进程使用
SSL_CERT_FILE、REQUESTS_CA_BUNDLE、CURL_CA_BUNDLE、GIT_SSL_CAINFO，并为 uv 开启系统证书。
显式客户端覆盖值保留。文件缺失、无法解析或保存后的证书包被修改会明确失败。
证书包没有私钥，保存在未跟踪目录；不会修改 certifi、Git 安装目录或操作系统根证书库。
证书轮换后重新运行该命令，再检查真实端点。过期、尚未生效、缺少签发者与主机名不匹配
保留为不同 TLS 验证原因；系统里存在某张旧证书不等于当前连接使用了它。

如果系统也没有企业 CA，可复用用户已经持有、来源已核实的 PEM CA 文件：

```text
python .agents/scripts/vaws_network.py certificates --ca-bundle /path/to/approved-enterprise-ca.pem
python .agents/scripts/vaws_network.py check --apply --native
```

输入可以来自另一台已受管设备的信任库导出、企业证书门户、安装包的受信发布渠道，
或此前工具生成且能核实来源的 CA bundle。调用者显式选择该文件作为信任输入；
先解析为 CA、拒绝私钥，再与系统根证书合并。解析成功仅说明文件可用，端点检查才验证
实际 TLS 链。不能自行生成一个根 CA 来验证现有企业代理，也不能从失败的 TLS 连接
抓取证书后自动提升为可信根。浏览器临时绕过、`sslVerify=false` 和 `--insecure`
都不是补齐信任的办法。

Windows 的 Go/gh 使用系统信任库，不保证接受 SSL_CERT_FILE；若其系统库缺少 CA，
需按组织的受管方式配置 Windows 信任库。其他 VAWS 客户端可先使用工作区 CA 包。
WSL、远端容器的信任库独立；本机成功不证明远端已配置。不要把代理凭据与 CA 包一起复制。

## 超时、下载、认证和恢复

| 节点 | 行为与边界 |
| --- | --- |
| DNS、连接、TLS、代理认证、HTTP | 每个探针 7 秒总预算，覆盖 DNS 和持续慢速返回；最多六个并发子进程，超时杀死并回收。报告仅保留来源名、类别、状态码和计时。407、401、403、429 分开归因。 |
| PyPI 镜像 | 只检查显式 VAWS_PYPI_MIRROR；索引读取和制品采样都有进程总超时。镜像至少快 25% 或原源失败才选择；临时转换锁文件的传输 URL，原始版本、制品哈希、环境键不变。离线与缓存命中不测速。 |
| 锁定依赖安装 | 每 15 秒输出阶段和已用时间；VAWS_INSTALL_TIMEOUT 默认 900 秒。uv 连接/读取超时默认 60 秒、下载并发默认 4；用户显式值优先。超时清理本次安装树，下一次复用缓存，未完成环境不发布为 ready。 |
| Git 和 gh 验收 | 各 30 秒总预算，分别验证传输与认证。普通 VAWS 更新的 Git 操作保留其原有 120 秒上限，另有低速检测。 |
| gh 引导安装 | Python 路径的元数据读取 30 秒，制品下载 300 秒；下载进度持续可见，SHA256 校验后才安装，部分下载在下一次调用按 Range 续传，服务端忽略 Range 则重启文件。PowerShell 无 Python 路径使用有总时限的子任务和发布校验和，失败后重下，不承诺续传。 |
| 账号与 Git 凭据 | 环境 token 只覆盖当前 Git 命令。移除本工具旧版本留下的精确 helper 覆盖，保留自定义 helper，使之后的 gh/keyring 登录正常生效。网络成功不代表 token 有 Fork、Star 或 push 权限。 |
| Fork、Star、PR 等写操作 | 不因超时自动重放写请求；先读回目标状态。初始化保存完成阶段，恢复时复用，避免重复创建。 |
| 原生依赖与服务 | DLL 崩溃、数据库锁、进程占用独立于网络错误。保留退出码与阶段；不因 HTTP 通过而把模型、索引或后台服务标为 ready。 |

需要让临时诊断命令也使用保存的路线与总超时，可运行：

```text
python .agents/scripts/vaws_network.py run --target github --timeout 120 -- git ls-remote upstream HEAD
python .agents/scripts/vaws_network.py run --target pypi --timeout 900 -- uv --version
```

命令不使用 shell 拼接；超时只清理新建的进程树。Windows 使用 Job Object，POSIX 使用
独立进程组。子进程等待不继承 MCP 输入流。完整初始化/索引耗时仍由相应包负责，
不能用单个 HTTP 超时替代服务健康检查或进程所有权判断。

## 此次部署暴露的缺口与责任边界

本次修复覆盖工作区部署入口、网络配置消费、安装器、凭据 helper 和 Windows 知识服务
入口诊断。实际企业环境验证了外网需代理、镜像需直连，以及系统 CA 复用后 Git/gh
可用；测试使用本地 TLS、自签 CA、407、慢速响应、失败下载与进程树证明故障边界。
原生知识查询曾因 Git 继承 MCP stdin 卡住，现已隔离 stdin 并设置 5 秒超时；
Windows ONNX 运行库选择有独立的本地配置和导入验收。

以下仍属于可见的能力边界，不能据此宣称任意企业网络均可自动完成安装：

- PAC 自动解析、企业集成代理认证、证书门户登录和系统 CA 下发需要对应平台能力；
  当前只消费已有支持的配置及显式 CA 输入。
- vaws-knowledge 的 OpenViking 冷启动预算和 Windows 进程命令读取超时需要在所属包修复。
  这次出现过启动超时后旧进程留下数据库锁；工作区不会通过扩大杀进程范围掩盖所有权问题。
- 模型完整下载的 CDN 路由、断点续传和健康检查以所属包的证据为准；本工具的元数据探测
  不替代模型校验或完整的服务启动验收。

本地 `network.json` 保存来源引用、NO_PROXY 和证书摘要；`network-check.json` 保存
脱敏观测。诊断输出不保存请求头、响应内容、原始错误字符串或代理地址。
前者包含机器配置，不能作为公共问题附件；分享后者仍应遵循社区诊断导出流程。
