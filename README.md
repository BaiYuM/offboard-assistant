# Offboard Assistant

本工具用于记录指定日期之后的安装、登录凭据元数据、环境变量和敏感数据位置，帮助离职时清理个人隐私和 API 密钥残留。

它是一个本地优先的 Windows 入职/离职清理助手，包含 CLI、Tkinter GUI、安装行为监听、加密导入导出和 WebDAV 同步。

## 项目结构

```text
offboard_assistant.py       CLI 和核心扫描/差异/清理建议逻辑
offboard_gui.py             可视化窗口和后台任务入口
sync_bundle.py              加密导入导出与 WebDAV 同步
test_offboard_assistant.py  单元测试
build_exe.ps1               Windows EXE 打包脚本
```

安全边界：

- 不读取或保存明文密码。
- 不解密浏览器密码库。
- 不记录 Cookie、聊天正文、API key/token 值。
- 只保存路径、域名、脱敏用户名、时间戳和清理建议。
- 对“入职前已存在但入职后修改过”的项目只标记人工确认，不自动删除。

## 快速开始

如果使用已打包版本：

```powershell
.\dist\OffboardAssistant\OffboardAssistant.exe
```

如果从源码运行：

```powershell
python .\offboard_assistant.py init --since 2026-07-06 --scan-root E:\job
python .\offboard_gui.py
```

## 使用

建立入职基线：

```powershell
python .\offboard_assistant.py init --since 2026-07-06
```

生成当前快照：

```powershell
python .\offboard_assistant.py scan
```

生成离职清理报告：

```powershell
python .\offboard_assistant.py report --rescan --csv .\.offboard-assistant\offboarding-report.csv
```

生成清理动作建议：

```powershell
python .\offboard_assistant.py actions --rescan --output .\.offboard-assistant\cleanup-actions.md
```

启动可视化窗口：

```powershell
python .\offboard_gui.py
```

初始化安装行为监听状态：

```powershell
python .\offboard_assistant.py watch-install --once
```

前台持续监听安装相关变化：

```powershell
python .\offboard_assistant.py watch-install --interval 60
```

默认状态目录：

```text
%APPDATA%\OffboardAssistant\.offboard-assistant\
```

旧版本曾默认使用当前目录下的 `.\.offboard-assistant\`。新版本首次启动时会尝试把旧状态文件迁移到 `%APPDATA%\OffboardAssistant\.offboard-assistant\`，之后双击 EXE 即可使用。

你也可以限制敏感文件扫描范围，避免扫描整个用户目录：

```powershell
python .\offboard_assistant.py init --since 2026-07-06 --scan-root E:\job
python .\offboard_assistant.py report --rescan --scan-root E:\job
```

## 当前扫描范围

- Windows 注册表中的已安装程序。
- 用户和系统环境变量名称。
- Chrome、Edge、Brave、Firefox 的保存登录项元数据。
- `.env`、`.npmrc`、`.pypirc`、SSH key、`*token*`、`*secret*` 等敏感文件位置。
- 常见 API key/token 内容特征，包含 OpenAI、Anthropic、GitHub、AWS、Google、Slack 和通用 `api_key`/`token`/`secret` 赋值。
- 微信、企业微信、钉钉、飞书、Slack、Teams、Telegram、Discord 的常见数据目录位置。

## API 密钥识别

工具会对用户指定目录和常见 AI/开发工具配置目录做本地扫描，例如：

- `.env`
- `.npmrc`
- `.pypirc`
- `.claude`
- `.codex`
- `.cursor`
- `.config`
- `%APPDATA%\cc-switch`
- `%APPDATA%\ccswitch`
- `%APPDATA%\Claude`
- `%APPDATA%\Cursor`

识别结果只保存：

- 密钥类型
- 所在文件路径
- 行号
- 脱敏片段
- hash 指纹

不会保存完整 API key、token 或密码值。

对于 CC SWITCH 或类似工具，如果它把大量 API 密钥放在配置文件里，通常可以识别出大部分常见格式。准确性取决于密钥格式和存储位置。建议把对应配置目录加入 `--scan-root`，例如：

```powershell
python .\offboard_assistant.py scan --scan-root "$env:APPDATA\cc-switch"
python .\offboard_assistant.py scan --scan-root "$env:APPDATA\ccswitch"
```

删除策略：

- 先到 OpenAI、Anthropic、GitHub、云厂商等平台撤销或轮换密钥。
- 再根据清理动作建议删除或重写本地配置文件。
- 默认不自动删除密钥文件，避免误删个人账号或仍在使用的配置。

## 安装行为监听

`watch-install` 不做驱动级或进程注入级监控，而是用低成本指纹对比发现安装行为：

- Windows 卸载注册表新增或删除的软件。
- 用户和系统环境变量新增或删除。
- `PATH`、`JAVA_HOME`、`MAVEN_HOME`、`NODE_HOME` 等路径型环境变量的路径项变化。
- `Program Files`、`Program Files (x86)`、`AppData`、开始菜单程序目录的顶层新增或修改。

检测到变化后会写入：

```text
.\.offboard-assistant\install-events.jsonl
```

报告命令会自动合并这些安装事件。

性能策略：

- 默认只看常见安装目录的顶层条目，不递归扫描。
- 默认每 60 秒比较一次，适合放到 Windows 计划任务里运行。
- 如果需要更精确，可以用多个 `--watch-dir` 指定开发工具目录或软件下载目录。
- 绿色软件或解压到未监听目录的软件无法自动发现，需要把目录加入 `--watch-dir`。

## 可视化窗口

`offboard_gui.py` 提供一个本地桌面窗口：

- 查看候选清理项：浏览器登录元数据、聊天数据目录、安装行为、环境变量、敏感文件位置。
- 在窗口中设置基线日期并建立/覆盖基线，不需要回到 CLI。
- 点击列表表头按分类、推荐等级、类型、时间等排序。
- 双击候选项进行勾选。
- 导出选中清理动作清单，包含风险等级、人工步骤和可复制命令。
- 导出 AI 审核包，内容只包含脱敏元数据，不包含密钥值、密码或聊天正文。
- 接入 OpenAI-compatible API 进行 AI 审核、自动推荐勾选和总结。
- 隔离选中推荐项，把明确属于临时/缓存类的文件或目录移动到本地隔离区。
- 标记选中项已处理。
- 生成完整离职清理报告。
- 配置 WebDAV 地址、用户名和远程文件名。
- 导出/导入本地加密包。
- 上传/下载 WebDAV 加密包。
- 创建或删除 Windows 后台计划任务。

安全限制：

- 不展示明文密码。
- 不展示聊天正文。
- 不上传明文状态文件。
- 不保存 WebDAV 密码或加密口令。
- 勾选后默认导出清理清单或标记已处理，不做不可逆自动删除。

### 导出清单、AI 审核和隔离

`导出选中清理清单` 的含义：

- 只生成 Markdown 清理建议。
- 不删除文件。
- 不修改浏览器密码、聊天数据或环境变量。

`导出 AI 审核包` 的含义：

- 生成 JSON 元数据，方便交给 AI 辅助归类和排序。
- 不包含明文 API key、token、密码、Cookie 或聊天正文。
- AI 只能辅助判断，最终删除仍应由用户确认。

`AI 审核` 的含义：

- 在 GUI 的“AI 审核”页填写 Base URL、模型和 API Key。
- 默认 Base URL 是 `https://api.openai.com/v1`，也可以填兼容 OpenAI Chat Completions 的第三方或自建服务。
- 点击“获取模型列表”会请求 `{Base URL}/models`，自动填充模型下拉框；如果服务不支持，可手动输入模型名。
- 勾选策略可选：保守模式、离职模式、激进模式。
- 点击“审核全部候选项并自动勾选”后，AI 会返回摘要、推荐勾选 ID 和理由。
- AI 返回推荐 ID 后，GUI 会自动勾选这些候选项。
- API Key 只在内存中使用，不保存到配置文件。
- 发送给 AI 的内容包含路径、分类、密钥类型、脱敏摘要、时间等元数据。
- 不发送明文 API key、token、密码、Cookie 或聊天正文。
- AI 勾选后，你仍需要人工确认，再点击“隔离选中推荐项”或导出清理清单。

AI 勾选策略：

- 保守模式：只勾选 AI 返回的 `select`。
- 离职模式：勾选 `select` 和 `review`，适合离职时统一处理残留。
- 激进模式：勾选 `select`、`review` 和低风险 `keep`，仍不会自动删除。

聊天数据处理：

- 个人账号：建议先加密备份到个人云端，再清理公司电脑本地残留。
- 公司账号：不备份内容到个人云端，按公司交接/归档流程处理后退出账号和清缓存。
- 不确定：先人工确认账号归属，不能直接上传或删除。

`隔离选中推荐项` 的含义：

- 只处理 `recommend_cleanup` 类型，例如 Codex 临时插件缓存、临时目录。
- 把文件/目录移动到 `%APPDATA%\OffboardAssistant\.offboard-assistant\quarantine\...`。
- 不是永久删除，隔离区里会生成 `manifest.json` 记录原路径和新路径。
- API key 文件、聊天目录、浏览器账号不会被这个按钮自动处理。

### 隔离历史与还原

`更多操作 ▾` → `查看隔离历史` 打开一个时间倒序列出所有隔离批次。
每条记录来自 `<state-dir>/quarantine/<timestamp>/manifest.json`，可执行：

- **还原**：反向 `shutil.move` 把文件移回原路径。如果原路径已有同名文件，**跳过**而不是覆盖。
- **永久删除**：递归 `rmtree` 整个批次（不可恢复）。

CLI 等价物：

```powershell
python .\offboard_assistant.py restore-quarantine --quarantine-dir .\.offboard-assistant\quarantine\20260708-153012
```

### 首次运行向导 + 本地配置

首次启动（既无基线也无 `wizard.done`）会弹出三步向导：
基线日期 → 公司/个人邮箱域名 → 扫描目录。完成后写入
`<state-dir>/config.json` + `wizard.done` 标记。`config.json` **只在本地**，不会进入云同步包。

CLI 可用 `--config <path>` 覆盖配置位置，或在配置里预填
`company_email_domains` / `personal_email_domains` 让
`account_owner_hint` 推断更准。

### IDE 最近项目维度

扫描 JetBrains 系列 IDE（IntelliJ / PyCharm / GoLand 等）的
`%APPDATA%\JetBrains\<IDE>\options\recentProjects.xml`，
把最近打开过的项目加进清理清单。**不读** `.vscdb`、**不读**
`workspaceStorage/<hash>/workspace.json` 的 `settings` 子树、**不读**
`argv.json`——只取路径 + 项目名 + 最后打开时间。

### 账户归属建议

每条候选会带 `account_owner_hint`（`company_account` /
`personal_account` / `unknown`），GUI 在行首显示 `[公司]/[个人]/[未知]`
前缀，详情面板单独列出。规则来源：路径关键字 + 内置 SaaS 域名种子
+ 用户在 `config.json` 里填的公司/个人邮箱域名。详见
`offboard_assistant.py` 的 `KNOWN_SAAS_DOMAINS` 与
`infer_account_owner_hint()`。

### 状态条分级

窗口底部状态条按严重度显示不同颜色圆点 + 文字背景：
`info` 绿、 `warn` 黄、 `error` 红、 `busy` 灰。`messagebox` 弹窗
保持原样（必须保留模态确认）。

## 更新记录

完整改动历史见 [CHANGELOG.md](CHANGELOG.md)。

## 打包 EXE

安装打包依赖并生成窗口版 EXE：

```powershell
.\build_exe.ps1
```

输出位置：

```text
dist\OffboardAssistant\OffboardAssistant.exe
```

如果只想手动执行：

```powershell
python -m pip install -r requirements-packaging.txt
python -m PyInstaller --noconfirm --windowed --name OffboardAssistant --add-data "README.md;." offboard_gui.py
```

`requirements-packaging.txt` 中的 `cryptography` 用于加密导出/导入和云同步包；没有它时 GUI 仍可查看清单，但加密同步不可用。

EXE 也支持后台参数，供 Windows 计划任务调用：

```powershell
.\dist\OffboardAssistant\OffboardAssistant.exe --background-watch-install --interval 60 --iterations 720
.\dist\OffboardAssistant\OffboardAssistant.exe --background-scan
```

也可以在 GUI 的“后台任务”页创建：

- `OffboardAssistantInstallWatch`: 登录后启动安装行为监听。
- `OffboardAssistantDailyScan`: 每日生成一次最新快照。

## 云同步和跨设备导入

推荐流程：

1. 公司电脑上在 GUI 中输入加密口令，导出 `.enc` 加密包。
2. 配置坚果云 WebDAV 地址、用户名、应用密码和远程文件名。
3. 点击上传到 WebDAV。
4. 家用电脑下载同一个 EXE，点击从 WebDAV 下载，输入同一个加密口令后导入。

注意：

- 坚果云只保存 `.enc` 密文文件。
- 加密口令丢失后无法恢复同步包。
- 不建议把 `.offboard-assistant` 目录中的 JSON/CSV/Markdown 明文文件直接同步到云端。

## GitHub 发布

建议上传源码和配置文件，不上传本地状态或构建产物。

可以上传：

- `offboard_assistant.py`
- `offboard_gui.py`
- `sync_bundle.py`
- `test_offboard_assistant.py`
- `README.md`
- `LICENSE`
- `SECURITY.md`
- `CONTRIBUTING.md`
- `RELEASE.md`
- `.github/workflows/`
- `requirements-*.txt`
- `build_exe.ps1`

不要上传：

- `.offboard-assistant/`
- `build/`
- `dist/`
- `*.enc`
- `.env`
- `*.key`
- `*.pem`

CI 会在 Windows 上运行单元测试和编译检查。手动触发 `build-windows` workflow 可生成 Windows 构建 artifact。

## 清理策略

报告中的 `cleanup_confidence` 含义：

- `high_new_after_since`: 基线中不存在，且时间晚于指定日期，通常可优先清理。
- `medium_new_but_time_unknown`: 基线中不存在，但无法确认时间，需要人工判断。
- `needs_review_modified_after_since`: 基线中已存在但指定日期后修改过，不能自动删除。
- `low`: 低置信度，仅作线索。

建议流程：

1. 先删除或撤销明确属于公司的 API token、SSH key、OAuth 授权。
2. 再清理公司域名相关浏览器保存密码和站点数据。
3. 对个人账号、入职前已有账号、银行/社交/购物等账号一律保留或人工确认。
4. 聊天软件只根据目录位置清理缓存或退出账号，不读取聊天正文。

## 自定义规则

工具使用三组规则来识别"该清理什么"：

- **path rules**：7 类内置规则（codex 临时缓存、AI 工具配置、SSH 配置等），决定如何分类敏感文件位置
- **SaaS domains**：10 个内置种子（github.com / gitlab.com / slack.com / notion.so 等），用来推断账户归属
- **secret patterns**：8 个内置正则（OpenAI、Anthropic、GitHub、AWS、Google、Slack、Generic bearer、Generic assignment），决定从 .env / .npmrc / .pypirc 等文件里识别什么

### 加性合并

不动源码也能扩展：在 `<state_dir>/rules/overrides.yaml` 写你自己的规则：

- **saas_domains**（加性 union）：你列的域名会**追加**到内置集。内置集永不被裁剪。
- **path_rules**（按 category 加性）：同 category 追加 needles；新 category 追加在末尾。
- **secret_patterns**（加性）：你的正则追加在内置之后。

合并后所有调用方（`infer_account_owner_hint` / `categorize_path` / `detect_secret_references`）都自动看到新规则。

### 例子：公司内网域名

公司内网用 `jira.acme.internal` 协作，写：

```yaml
# %APPDATA%\OffboardAssistant\.offboard-assistant\rules\overrides.yaml
# （macOS / Linux 路径见下）
saas_domains:
  - jira.acme.internal
  - gitlab.acme.internal

path_rules:
  - category: company_dev_paths
    needles:
      - "/work/acme/"
    label: "公司项目路径"
    recommendation: "review_required"
```

例子文件名：
- Windows: `%APPDATA%\OffboardAssistant\.offboard-assistant\rules\overrides.yaml`
- macOS: `~/Library/Application Support/OffboardAssistant/.offboard-assistant/rules/overrides.yaml`
- Linux: `~/.config/OffboardAssistant/.offboard-assistant/rules/overrides.yaml`

### 查看当前规则

CLI：

```powershell
python .\offboard_assistant.py list-rules
```

GUI：**清理清单 → 当前规则** 标签页，Reload 按钮重新读取 overrides.yaml。

### 隐私保护

`config.json` 和 `overrides.yaml` **都不会**进入加密同步包（不在 `sync_bundle.SYNC_FILES` allowlist 里）。规则是本地用户视角，不会上云。

### 0 依赖

整套 YAML 解析器用 stdlib 实现，**不引 PyYAML**。支持 `# 注释`、嵌套 mapping、sequence、单/双引号字符串。
