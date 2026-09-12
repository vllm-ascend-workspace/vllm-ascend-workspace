# 原生客户端与编辑隔离

Status: current

官方 Codex、Cursor、Claude Code、Grok 和 Kimi Code 共用项目启动指引。
首次初始化配置客户端 hooks、MCP providers 和项目指引；用户照常创建会话，
无需调用 Skill、选择额外 VAWS 启动器或安装个人客户端补丁。原生 worktree
setup 是可复用的优化，不是所有客户端都必须具备的前置能力。

## 新建与恢复

新任务有两条准备路径：

- 客户端已创建独立 worktree，且 setup 已提供选定环境：直接使用这个目录。
- 普通项目目录中的新会话：项目指引让 Agent 的第一个仓库操作运行一次
  `uv run --no-project python .agents/scripts/vaws_start.py --client CLIENT`。
  官方 Kimi 加上原生 hook 已提供的 `--context-file PATH`。

`CLIENT` 是 codex、cursor、claude、grok 或 kimi。命令复用当前原生 task，
在母仓外的同级位置准备独立编辑目录，选择 canonical main 的精确提交及其锁定
组件环境，显式绑定 sources，并返回 `workspace`、`head`、`environment` 和
`context_file`。同一 task 重复调用复用已保存结果，不再次 fetch、安装或换目录。
初始化状态由命令在本地读取；只有缺少首次配置时才返回 setup 提示，不让 Agent
每次检查身份文件。多个会话同时准备时，命令显示并等待仓库更新锁，超时才返回
等待时长和锁文件证据。
它不是通用客户端 launcher，也不解析客户端的 resume 参数。

后续 shell 使用返回目录作为 cwd，或者在命令中使用 `cd W && ...`；文件、搜索
和 patch 使用该目录中的绝对路径。客户端 UI 和默认 cwd 可以保留原项目。
这能完成隔离编辑，不声称子进程 `cd` 可以改变父客户端或所有工具的默认根目录。
Grok 默认会过滤主仓内被忽略的目录，因此新编辑目录放在母仓外，不要求关闭
Git ignore 或放宽全局权限。

resume 沿用既有 task、编辑目录、sources 和环境，不调用新的准备流程。
已经接纳的执行继续使用自己的固定输入。新建时失败返回阶段、原因和证据；
不能把未准备成功的目录标为 ready，也不自动 stash、reset 或 rebase 用户工作。

## 客户端接线

`vaws_client_setup.py --client all --apply` 一次配置实际已安装的客户端，保留
用户自定义 provider、hook 和指引。结果保存在主工作树的
`.vaws-local/client-initialization.json`；它不是每个任务的检查清单。
原生信任和审批仍由客户端处理，配置生成不等于真实会话验收。

| 客户端 | 普通新会话指引与 context | 可复用的原生能力 |
|---|---|---|
| Codex | AGENTS.md；MCP 的真实 thread metadata 或 hook 关联当前 task | App local environment setup 准备客户端创建的 worktree；固定用户 hooks 避免每个目录重复安装 |
| Cursor | AGENTS.md 和 alwaysApply 项目规则；sessionStart / preToolUse 自动关联与注入 context | worktrees.json setup 准备新目录；用户级 providers 接收原生 workspaceFolder |
| Claude Code | CLAUDE.md 导入 AGENTS.md；SessionStart 导出 context，PreToolUse 注入 | WorktreeCreate 可准备客户端选用的原生 worktree；薄入口保留真实调用者 |
| Grok | 原生读取 AGENTS.md；Bash 提供 GROK_SESSION_ID，PreToolUse 注入 context | Git worktree 创建回调及原生 /new、/fork 偏好；普通入口不依赖个人修复版 |
| Kimi Code | 原生读取 AGENTS.md；UserPromptSubmit 提供 context_file；准备命令和三个 VAWS provider 的调用显式携带它 | 显式启用的 SessionSetup 扩展可提前选目录，官方配置只使用受支持事件 |

Grok 的 SessionStart stdout 不会成为模型提示，因此启动入口放在客户端真实
读取的项目指引中。Kimi 官方 Bash 有 cwd 参数，Read/Write/Edit 支持绝对路径；
无需假定存在会话中途改根 API。客户端仍按自己的审批机制处理文件访问。

原生机制分别见 [Codex local environment](https://learn.chatgpt.com/docs/environments/local-environment)、
[Cursor worktrees](https://cursor.com/docs/configuration/worktrees)、
[Claude WorktreeCreate](https://code.claude.com/docs/en/hooks#worktreecreate)
和 [Kimi hooks](https://moonshotai.github.io/kimi-code/en/customization/hooks)。
具体已测版本记录在 dated 验收文档，不能从配置规划测试推断所有新客户端均已通过。

初始化不再因个人二进制缺失而阻塞，也不为保护补丁关闭客户端自动升级。
Kimi 普通配置会替换本仓精确拥有的旧 SessionSetup 回调，保留用户项；
`--kimi-session-setup` 仅用于明确选用的扩展。Grok 的兼容导入去重只处理已确认由
本仓生成、且存在有效替代的三个 Cursor provider 名称，不影响其他用户配置。

## Context in MCP and shell

原生会话身份、编辑目录和组件进程是三个不同对象。目录或最近会话不能证明
任务身份。原生 hooks 建立 attachment；显式 context 或客户端提供的 native
metadata 选择这个 attachment。工具收到的 context 与原生调用者冲突时返回事实。

MCP 使用稳定的 `vaws_native_mcp.py` gateway。它按当前 task 的固定选择启动或
复用 task、remote-dev 和 knowledge 后端，不实现这些包的业务功能。已有长活
MCP 连接可以服务另一个新 task 的新环境；恢复旧 task 仍调用它原来的环境。
因此更新不要求 Agent 每次手工重连，也不在旧任务工作中热换包版本。

Codex 使用真实 thread metadata；Claude、Cursor、Grok 的 PreToolUse 可以补入
context。Cursor 的已观测 MCP:toolName 形式也用于固定的 knowledge 工具和
remote 工具。官方 Kimi 没有同等的透明 native metadata，调用三个 VAWS provider
时带已有 `context_file`。其他客户端若工具报告缺少 context，复用已有值即可，
不用事先检查每次是否注入。gateway 会在转发非 task 工具前去掉这个路由字段。
Kimi 的工具 schema 将此字段声明为必填，避免先失败一次才补入上下文。

shell 与 MCP 的身份传递各自独立。shell 优先读取 VAWS_CONTEXT_FILE 或客户端
提供的原生 ID；官方 Kimi 使用 hook 返回的显式 context。Bash 的一次 cd 不被
当作身份、sources 或 MCP 版本变更。

## 固定环境与共享知识

工作区提交及锁文件决定组件组合，准备完成后环境不可变。新任务的
`.vaws-local/tasks/<task-id>/start.json` 保存选定结果；原生已准备的 worktree
也可通过自己的环境选择被复用。gateway 按这个 task 选择后端，不从“最新环境”
推断旧 task 的版本。后端 stderr 保留在主工作树的 `.vaws-local/mcp/providers/`，
并记录实际解释器、环境键、目录和 receipt。

同一 Git 工作区的各会话共享知识 service 配置、项目知识快照、candidate 和
索引；模型下载复用知识包缓存。新工作树不另建一套知识库。共享知识内容可由
知识包维护更新，但 task 使用的组件版本保持固定。查询和捕获按需使用，不要求
额外维护命令、轮询或重复总结。用户选择的知识挂载和发布设置保留。
原生回复事件自动保存已有最终文本。Kimi Stop 和 Cursor 命令行 SessionEnd
不直接携带这段文本时，适配器只读取该事件明确对应的会话记录尾部，提取已完成
的最终回复；不扫描其它会话，也不要求 Agent 再总结。

来源优先级为本次 run 显式 sources、task 显式默认值、attachment 自动来源。
更新 attachment 的实际 cwd 不覆盖 task 的显式工作树，也不改变已接纳的执行。
SessionStart 只建立本地关联，不因此分配容器、NPU 或端口。

## 平台与验收

Windows 和 WSL 沿用已有 owner 边界。共享 Windows 挂载目录的原生 worktree
setup 由 Windows owner 执行；从 WSL 的 /mnt 路径调用不代表已支持跨系统
linked-worktree。路径与配置测试不替代 Windows 实机验收。完整边界见
[platform-contract.md](platform-contract.md)。

[2026-09-12 原生验收](native-client-validation-2026-09-12.md)记录旧方案的已测范围；
[本轮统一启动验收](unified-session-validation-2026-09-13.md)单独记录新入口、
组件路由、知识共享和 resume 的完成情况。

`vaws_client.py` 仍是可选终端便利入口，用于在启动一个新的客户端进程前选目录；
正常新会话使用上述项目指引。其存在不改变客户端内部 /new 或 resume 的原生语义。
