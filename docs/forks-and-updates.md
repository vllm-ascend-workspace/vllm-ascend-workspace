# 个人 Fork 与主仓自动更新

Status: current

首次使用确认个人 GitHub 身份并配置客户端。之后，新任务在独立编辑目录中
采用本轮主仓版本和配套组件，开始工作后保持固定。入口不依赖用户调用 Skill，
也不要求安装个人修改版客户端。遵循[九条设计原则](design-principles.md)。

## 首次使用

根 `AGENTS.md` 提供首次身份提示和新任务入口；初始化通过
`vaws_client_setup.py --client all --apply` 配置已安装的五种客户端。
原生客户端负责项目与 hook 信任。Git clone 本身不执行代码，写出配置文件
也不等于客户端已经加载配置；真实验收见[本轮验收](unified-session-validation-2026-09-13.md)。

首次确认个人 GitHub 用户名。`gh` 登录是候选，不能静默代替用户选择。
确认结果位于未跟踪的 `.vaws-local/github.json`，不包含凭据。
coordinator 自动将它用于 native session 的用户归属，SSH 仍使用 root；见
[用户与协调](identity-and-agent-coordination.md)。身份待确认时，独立本地查询
和 Review 可继续，不重复追问或添加任务检查清单。

## 个人 Fork

```text
uv run --no-project python .agents/scripts/workspace_forks.py
uv run --no-project python .agents/scripts/workspace_forks.py --github-user USER --apply
```

默认只读计划；用户接受后 apply。默认覆盖 workspace、vLLM、vLLM-Ascend，
`--repo workspace` 可只配置主仓。入口为纯标准库，不依赖 Skill 或 VAWS runtime。
工具核对认证 User、实际仓库名、个人所有者及 canonical fork network，拒绝组织
Fork、其他所有者的 redirect 和无关同名仓库。`origin` 是个人 Fork，`upstream`
是官方来源；GitHub 分配不同 Fork 名时保存并复用实际地址。
`.gitmodules` 保留社区 URL；先初始化子模块再配置其 remotes。

重复执行复用正确 Fork，保留额外 remote、脏内容和 Git HEAD。复杂 fetch/push
配置报告具体差异，显式替换时保存备份。组件只在需要贡献时 fork，安装使用
工作区锁定的版本。公开知识贡献由 knowledge package 管理；配置代码 Fork
不会启用知识发布。所有开发 Fork 都应属于个人 GitHub User。

工作区入口校验上述三个开发仓库；外部组件的贡献入口由其 owner 负责。
这不是拦截任意终端 Git 命令的权限系统。

## 每个新任务准备一次

| 场景 | 行为 |
|---|---|
| 原生客户端已经创建独立 worktree，并由 setup 选定环境 | 直接使用已有目录和环境 |
| 普通新会话尚未准备 | 项目短指引使 Agent 首个仓库动作运行 `vaws_start.py --client CLIENT` |
| 同一任务重复准备 | 返回已完成的选择，不再 fetch、安装或创建目录 |
| 恢复会话 | 沿用原任务、目录和环境，无准备或更新步骤 |

`vaws_start.py` 在母仓外的同级位置创建独立工作树，返回 `workspace`、`head`、
`environment` 和 `context_file`，并绑定任务的默认 sources。用户继续使用原客户端，
无需启动 VAWS launcher。客户端 UI/default cwd 可以保持原目录；后续 shell
以返回目录为 cwd，文件、搜索和补丁使用该目录下的绝对路径。
官方 Kimi 从已有 hook 取得 context，并将其传给启动命令及三个 VAWS MCP provider。
各客户端短指引和边界见[编辑隔离合同](native-workspace-isolation.md)。

准备检查 canonical 默认分支一次（当前为 main），不依赖 tag 或 Release。
独立准备目录固定此次取得的精确 SHA；`vaws_deps.py sync --locked` 复用或创建
该版本的不可变环境，并复用配套 monitor wheel。准备成功后，个人 Fork 默认分支
仅 fast-forward 到该提交。同一提交已准备完成就复用，不重复下载。
沿用 `.vaws-local/updates/releases/<SHA>` 缓存名不代表要求发布 Release。

Codex 本地环境 setup、Cursor setup-worktree，以及客户端已有的原生创建回调
仍可提前完成准备；它们只处理客户端刚创建的目录，不通过 hook 修改父进程 cwd。
已有用户 setup 命令保留，VAWS 准备置于其前。已有环境选择的目录再次 setup
只复用和修复接线。原生机制见
[Codex 本地环境](https://learn.chatgpt.com/docs/environments/local-environment) 和
[Cursor worktrees](https://cursor.com/docs/configuration/worktrees)。

原生回调可采用主仓更新的基线包括：新目录精确复制母仓当前 HEAD，或 Codex 的
detached 新目录精确匹配可识别的本地默认 tip。母仓处于 feature 分支或有未完成
改动，本身不阻止这个干净的新目录采用主仓。不同于这两类基线的显式 HEAD、
fork 来源、已选环境及新目录中的编辑继续保留。客户端没有提供用户选择 ref
的完整标记，显式选择恰好同一基线时无法再区分；结果记录这个边界。回调不初始化
尚未拉取的子模块，也不覆盖准备期间发生的编辑。普通 fallback 在另一个目录
准备主仓，不改母仓的业务分支。

更新不可用时可使用可用的本地版本，并返回未更新原因。若本地依赖或接线也无法
准备，则返回实际失败及证据，不报告已就绪。不自动 stash、reset、rebase 或强推。

## 组件和知识

主仓提交通过 `pyproject.toml`、`uv.lock`、vaws-top wheel pin 和 submodule gitlink
确定配套版本。维护者更新并验证这组输入；新任务取得主仓所选组合，不各自追逐
所有组件仓库的分支头。Release 可作为里程碑，不是更新触发条件。

稳定的 MCP gateway 根据明确的 native context 路由到该任务固定的环境，启动
其中的 coordinator、remote-dev 和 knowledge 后端。已有客户端 MCP 连接也可为
新任务选择新后端；旧任务继续使用原环境，不被新任务升级。目录和环境选择记录在
任务的 `start.json`，原生已准备目录也可从其已保存环境选择中复用。
Gateway 不从最近任务或 cwd 猜身份，亦不将“最新环境”当旧任务的默认值。

同一工作区家族共享知识配置、项目知识快照、候选内容和包维护的模型/index 缓存。
共享知识内容更新独立于任务代码版本；普通任务无需复制知识库、重建相同索引或
单独维护模型。用户自定义知识根和发布选择保留，默认不开启公开贡献。
准备 pending 不阻止独立工具。详见[依赖合同](dependency-plane.md)。

coordinator daemon 的 idle 升级和 monitor 的实例管理仍由各包负责；忙碌实例的
状态不由 workspace 强制改写。没有每五分钟轮询、常驻代码 watcher 或工作中换版本。

## 显式维护与证据

```text
uv run --no-project python .agents/scripts/workspace_update.py check
uv run --no-project python .agents/scripts/workspace_update.py prepare
uv run --no-project python .agents/scripts/workspace_update.py apply
```

这些是主动维护入口。`check` 查版本，`prepare` 准备精确提交，`apply` 只更新
干净、无 merge/rebase 的默认分支；已初始化子模块须无业务改动，且只采用记录的
gitlink。普通任务无需运行它们。`vaws_client.py` 仍是可选终端便利入口，恢复已有
目录需给原 `--workspace PATH`；日常新任务使用项目指引即可。

`.vaws-local/updates/state.json` 保留检测提交、当前步骤、结果和未完成原因，
失败命令证据保留在 logs 子目录。更新目录 `config.json` 的 `enabled: false`
暂停新任务自动更新；显式命令仍可用于维护。同一 Git 公共目录使用 OS 锁串行更新。
状态记录事实，不承担任务或设备权属。

Windows 挂载工作区的原生 setup 使用 Windows owner，不从 WSL 的 `/mnt` 路径
运行该回调，也不承诺混合系统 linked-worktree 行为。原生 owner、回调和安装边界
见[平台合同](platform-contract.md)。
