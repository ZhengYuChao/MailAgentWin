# MailAgent 运行指南 (Howto)

本指南旨在详细介绍如何在 Windows 环境下配置、诊断以及运行 MailAgent 办公助手。本指南涉及当前最新的核心更新（包括基于优先级任务池的消费、LIFO 排队、多级降级线程关联、EML 压缩包 WAF 避让等机制）。

---

## 📋 运行前置条件

1. **操作系统**：Windows 10 或 Windows 11。
2. **邮件客户端**：必须安装并配置好 **Classic Outlook** 客户端。
3. **Office 套件**：若需使用 Office 附件转 PDF 预览功能，请确保本地已安装并激活了 Word、Excel 和 PowerPoint。
4. **Python 环境**：Python 3.10+。
5. **内网穿透（可选，但推荐）**：由于 Notion 的 Buttons 需要反向触发本地 Outlook 回信动作，如果启用了反向操作，请在本地安装并配置好 `ngrok` 或 `cloudflare` (cloudflared)。

---

## 🛠️ 第一步：安装依赖与初始化

1. 打开 PowerShell 并克隆/解压项目到本地目录。
2. 进入项目根目录，安装依赖：
   ```powershell
   pip install -r requirements.txt
   ```
3. 初始化 Playwright 无头 Chromium 浏览器：
   ```powershell
   playwright install chromium
   ```

---

## 📝 第二步：编辑环境配置文件

打开并编辑根目录下的 [.env](.env) 文件，确保以下核心配置准确无误：

### 1. Notion API 配置
*   `NOTION_TOKEN`: 你的 Notion 机器人 Integration Token。
*   `EMAIL_DATABASE_ID`: 用于接收邮件同步的 Notion 数据库 ID。
*   `NOTION_AI_PAGE_URL`: 用于唤起 AI 的 Notion 页面 URL（例如：`https://www.notion.so/ai`）。

### 2. Outlook 匹配配置
*   `USER_EMAIL`: 你的 Outlook 邮箱地址（例如 `example@domain.com`）。
*   `MAIL_ACCOUNT_NAME`: Outlook 中对应的账户显示名称（通常就是你的邮箱地址，或者是 `Exchange`）。
*   `MAIL_INBOX_NAME`: 收件箱的本地化文件夹名（中文 Windows 环境默认为 `收件箱`，英文为 `Inbox`）。
*   `MAIL_SENT_NAME`: 已发送邮件文件夹名（中文默认为 `已发送`，英文为 `Sent Items`）。
*   `SYNC_MAILBOXES`: 配置为 `收件箱,已发送`（用逗号分隔）即可同时双向同步收件和发件。

### 3. 反向 Webhook 与穿透配置 (核心改动)
如果你需要通过 Notion 的按钮直接让本地 Outlook 自动草稿/发信，请配置以下项：
*   `REVERSE_PROXY`: 填入 `ngrok` 或 `cloudflare`（视本地安装的客户端而定）。留空则关闭反向 Webhook 服务。
*   `NOTION_ACTION_CREATE_DRAFT`: 填入 Notion 数据库中 `"Create Draft"` 按钮对应的 Action ID。
*   `NOTION_ACTION_REPLY_ALL`: 填入 Notion 数据库中 `"Reply All"` 按钮对应的 Action ID。
*   `NOTION_ACTION_REPLY`: 填入 Notion 数据库中 `"Reply"` 按钮对应的 Action ID。

---

## 📂 Notion 页面与数据库配置要求 (核心依赖)

MailAgent 依赖于 Notion API 写入邮件，并使用 Notion AI 完成智能处理。你需要提前在 Notion 中建立以下页面或数据库，并确保与 [prompt.txt](prompt.txt) / [prompt_schedule.txt](prompt_schedule.txt) 中所指示的字段和数据库名称完全一致。

### 1. 邮件同步数据库
*   **数据库命名**：建议命名为 `《我的邮件｜[Adam] Email》`（与 `prompt.txt` 中的名称匹配）。
*   **必需字段属性 (Properties)**：
    *   `Processing Status` (Select 或 Status 类型)：可选项必须包含 `未处理`、`AI Reviewed`、`已同步` 等。
    *   `AI Summary` (Text 类型)：存放 AI 总结。
    *   `Key Points` (Text 类型)：存放邮件关键点。
    *   `Category` (Select 类型)：存放邮件类别。
    *   `Language` (Select 类型)：存放邮件语言。
    *   `Sender Priority` (Select 类型)：存放发送人优先级。
    *   `Priority` (Select 类型)：存放优先级（如 `💥领导`、`🔴紧急` 等）。
    *   `Action Required` (Checkbox 类型)：指示是否需要行动。
    *   `Action Type` (Select 类型)：存放行动类型。
    *   `Reply Suggestion` (Text 类型)：AI 建议的回信草稿内容。
    *   `Urgency Reason` (Text 类型)：紧急原因。
    *   `Message ID` / `Thread ID` (Text 类型)：保存邮件的唯一标识，在反向 Webhook 中必选。

### 2. 会议记录页面/数据库 (用于定时任务)
*   **数据库/页面命名**：建议命名为 `[Adam]专属AI会议记录`（与 `prompt_schedule.txt` 中的名称匹配）。
*   **必需字段属性 (Properties)**：
    *   `Status` (Select 或 Status 类型)：可选项必须包含 `进行中`、`已完成`。
    *   `Meeting Theme` (Text/Title 类型)：会议主题。
    *   页面正文：存放会议录音、转写或会议内容。

### 3. Prompt 配置与工作原理
在项目的根目录下有两个核心 Prompt 文件，它们与 Notion 页面之间的对应关系如下：
*   **[prompt.txt](prompt.txt) (实时触发)**：
    *   当有新邮件被同步时，MailAgent 会在静默期（`DEBOUNCE_QUIET_SEC`）后唤起 Notion AI，并将 `prompt.txt` 中的内容作为提问发送给 Notion AI。
    *   Notion AI 接收到指令后，会主动去 `《我的邮件｜[Adam] Email》` 数据库中检索状态为 `未处理`/`AI Reviewed` 的邮件，阅读正文并自动补齐字段（如生成 `AI Summary`，若需回复则生成 `Reply Suggestion`），并将状态更新为 `已同步`。
*   **[prompt_schedule.txt](prompt_schedule.txt) (每日定时任务)**：
    *   每日早上 `07:00` 时，定时任务会把 `prompt_schedule.txt` 的内容发给 Notion AI。
    *   Notion AI 会读取 `[Adam]专属AI会议记录` 并整理完成状态和主题。
*   **`NOTION_AI_PAGE_URL` 页面建议**：
    *   这应该是一个可以启用 Notion AI Chat 的页面链接（例如 `https://app.notion.com/ai` 或你在 Notion 中自建的一个专门的 AI page）。
    *   **非常重要**：要保证 Notion AI 能够正常读取和修改上述的邮件数据库和会议记录数据库，该页面最好是这些数据库的父页面，或者与数据库处于同一个 Workspace，确保 Notion AI 拥有检索和更新上述数据库的权限。

---

## 🔍 第三步：环境诊断与预检

在正式运行服务前，强烈建议运行项目自带的预检脚本，来检查 Outlook COM 服务连通性、Notion Token 连通性以及飞书通知配置：
```powershell
python scripts/preflight_check.py
```
*   若控制台输出 `🚀 All systems GO!`，说明环境就绪。
*   若报错，请按照提示修改 Outlook 权限或 [.env](.env) 配置。

---

## 🔐 第四步：进行首次登录授权 (核心步骤)

由于无头 Chromium 浏览器需要在后台静默打开 Notion 页面并向 Notion AI 提问，我们需要人工生成一次包含登录状态的 Cookies 状态文件：
```powershell
python notion_auth.py
```
1. 此时系统会弹出一个**可见**的 Chrome 浏览器。
2. 请在弹出的网页中完成 Notion 的登录（支持扫码或账号密码）。
3. 登录成功且完整加载出你的 Notion 工作区后，选择你要的AI Model，若不选可以Auto，然后尝试发一条测试消息，接着回到命令行终端中，按下 **回车键 (Enter)**。
4. 脚本会自动捕捉并保存当前的 User-Agent 至 `user_agent.txt`，并将 Cookies 和 LocalStorage 状态保存至 `notion_auth.json`。随后浏览器将自动关闭。

---

## 🚀 第五步：运行 MailAgent 同步服务

当上述步骤全部就绪后，即可启动后台守护进程：
```powershell
python main.py
```

### 运行后系统的工作流程 (多进程架构)：
1. **Supervisor 启动与监控**：
   *   `main.py` 拉起 `ProcessManager` 主进程，主进程负责分别启动 **进程 A (MailWorker)** 和 **进程 B (AIWorker)**，并持续监控它们的存活状态，一旦崩溃即按退避策略（5s→60s）自动重启。
2. **进程 A：内网穿透、API 服务与邮件同步**：
   *   启动内网穿透服务获取公网域名，并在端口 `54321` 启动 Webhook API 服务。
   *   启动 COM 雷达监听器实时捕捉新邮件。
   *   启动核心主循环向后回溯补查，处理邮件实体提取、附件上传，并写入本地数据库。
   *   **进程间通信 (IPC)**：每当有一封邮件同步成功，进程 A 会通过 `multiprocessing.Queue` 向进程 B 发送一条轻量级的通知信号。
3. **进程 B：防抖控制、定时任务与 Notion AI 交互**：
   *   初始化 Playwright 无头浏览器加载凭证。
   *   监听 IPC 队列中的同步完成信号，根据 `NOTION_AI_BATCH_SIZE` 和 `DEBOUNCE_QUIET_SEC` / `DEBOUNCE_FORCE_SEC` 智能控制 AI 的触发时机。
   *   内置的每日调度器独立运行，到达 `07:00` 时直接通过 AI 控制器触发早报生成。

---

## 🔗 第六步：在 Notion 中配置反向按钮 (可选)

要在 Notion 中使用反向按钮（即时在本地生成 Outlook 草稿或发送信件）：
1. 观察 [main.py](main.py) 启动时的控制台输出，或者查看 `logs/mailagent.log` 日志文件，找到穿透出来的公网域名（例如：`https://xxxx.ngrok-free.app`）。
2. 在 Notion 数据库中，创建三个 Button（按钮）类型属性。例如在页面中可以直观呈现为交互按钮（参考图片：[button.png](./img/button.png)）：

   ![Notion 页面中的回复动作按钮](./img/button.png)

3. 配置按钮触发时的动作为：**“发送 HTTP 请求 (Send HTTP Request)”**。
   *   **请求类型**：`POST`
   *   **请求 URL**：`https://xxxx.ngrok-free.app`（直接填入你的公网域名即可）
4. 该请求的 Action ID 需和 [.env](.env) 中配置的值保持一致。具体的自动化接口触发参数及动作映射配置请参考下图（参考图片：[automation.png](./img/automation.png)）：

   ![Notion 自动化动作触发配置](./img/automation.png)

   > [!TIP]
   > 确保 Notion Webhook 的 Payload JSON 正确绑定了邮件页面的属性字段（如 `Message ID`、`Thread ID`、`Reply Suggestion` 等），以便后台 [server.py](src/api/server.py) 精确定位 Outlook 中的原始邮件进行答复。

---

## 🔄 附：如何手动进行历史邮件批量导入

若需手动回填过去指定天数的历史邮件（如补查过去 14 天的邮件），可另开一个终端直接运行：
```powershell
python scripts/initial_sync_win.py 14
```
该脚本将利用 [new_watcher_win.py](src/mail/new_watcher_win.py) 相同的同步接口，跳过 Notion AI 自动化汇总，只进行静默的邮件实体与附件同步。
