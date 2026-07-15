# MailAgent Windows

MailAgent 是一个运行在 Windows 平台上的自动化办公助手，能够将 Microsoft Outlook 的邮件实时同步到 Notion 数据库，并自动触发 Notion AI 进行总结和处理。

---

> [!NOTE]
> **致谢与声明**：本项目的 Windows 版本是在 macOS 版本的 [MailAgent](https://github.com/ChenyqThu/MailAgent) 项目基础之上，针对 Windows 环境的 Microsoft Outlook (COM)、本地 Office (DCOM) 转换及相关运行机制进行适配、重构与调整而来的。在此向原作者表示感谢并致敬，以尊重开源版权。

---

## 🚀 核心功能

*   **Outlook 实时同步与事件雷达**：
    *   基于 Windows 原生 COM 接口 (`pywin32`)，[com_radar.py](src/mail/com_radar.py) 实时订阅收件箱的 `OnNewMailEx` 事件和已发送邮件文件夹的 `OnItemAdd` 事件。
    *   引入**全局优先级任务队列** ([task_pool.py](src/scheduler/task_pool.py))：实时接收的邮件或发件赋予中优先级（Priority 2）即时入队；启动补查或历史回填任务赋予低优先级（Priority 3）排队，避免高频任务相互阻塞；反向 Webhook 回调发信/草稿指令赋予高优先级（Priority 1）立即消费。
    *   **LIFO 同步机制**：同一优先级下按时间倒序（最新优先）出队，保证优先处理最紧急、最新的邮件。
*   **智能断点续传与补查**：
    *   启动时根据 `STARTUP_LOOKBACK_DAYS` 自动补查历史遗漏的邮件。
    *   比对本地 SQLite 数据库 ([sync_store.py](src/mail/sync_store.py)) 记录防重，按邮件的时间戳有序加入低优先级任务队列，断电重启也不遗漏。
*   **Notion 线程关系三级还原**：
    *   [sync.py](src/notion/sync.py) 根据邮件的 References 与 thread_id 等信息，将同一会话（Thread）的邮件聚合，自动建立 Notion 数据库中 `"Parent item"` 与 `"Sub-item"` 的父子关系，最新的一封邮件被自动提升为主节点，历史回复全部级联归档。
    *   **三级降级寻找关系**：
        1.  **会话树匹配**：利用 `ConversationIndex` 链条与 SQLite 索引还原最精确的回复层级。
        2.  **Message-ID 匹配 (In-Reply-To)**：查找 Notion 数据库中 `"Message ID"` 等于当前邮件 `in_reply_to` 字段的页面作为父页面。
        3.  **标题正则匹配**：自动剥离标题前缀（如 `Re:`, `Fwd:`, `回复:`, `转发:`），精确寻找最原始的无前缀标题页面进行关联。
*   **附件处理、WAF 自动避让与 Office 转换**：
    *   自动提取邮件附件并调用 Notion API 进行上传。
    *   **WAF 403 自动避让**：在上传 `.eml` 邮件包时如果被 Cloudflare WAF 等防护机制拦截（通常返回 403 Forbidden），程序会自动切换为 `.eml.zip` 压缩包格式避开特征检测，确保可靠同步。
    *   **Office 本地自动化转换**：若启用 `OFFICE_CONVERT_ENABLED`，将自动调用本机 Office Word、Excel、PowerPoint 组件 ([office_converter_win.py](src/converter/office_converter_win.py))，把 `docx/doc/pptx/ppt/xlsx/xls` 文档转换为 PDF 附件上传，实现在 Notion 中直接进行页面内嵌预览。
*   **Playwright 无头 Notion AI 自动化联动**：
    *   **后台静默交互**：基于 Playwright 驱动后台 Chromium 浏览器静默执行，无需干扰日常办公。
    *   **一次登录持久化授权**：通过运行 [notion_auth.py](notion_auth.py) 脚本一次性扫码/账号登录，状态自动写入 `notion_auth.json` 与 `user_agent.txt` 供 Playwright 复用。
    *   **智能防抖与超限强推**：
        *   `DEBOUNCE_QUIET_SEC`（默认 60 秒）：邮件同步后进入静默防抖区，在此期间没有新邮件才触发 AI 控制器 ([controller.py](src/ai/controller.py)) 提问。
        *   `DEBOUNCE_FORCE_SEC`（默认 1800 秒）：强制触发间隔，即便一直有邮件源源不断同步，达此间隔也会强推触发一次 AI 交互。
        *   `NOTION_AI_BATCH_SIZE`（默认 5 封）：当待总结邮件批次满 5 封时，无视防抖，直接触发 AI。
    *   **浏览器会话自动回收**：通过 `NOTION_AI_MAX_CHATS_PER_SESSION` (默认 10 次) 限制单次浏览器会话的最大 prompt 提交数，超出后自动回收并重启 Chromium 进程，彻底杜绝内存泄露与会话长度导致的幻觉。
    *   **早报定时汇总**：内置每日早上 `07:00` 定时任务 ([daily.py](src/scheduler/daily.py))，调用专用的 `prompt_schedule.txt` 模板触发早报总结。
*   **Notion 动作反向 Webhook 回调**：
    *   本地拉起一个轻量级 Web 监听服务 ([server.py](src/api/server.py))，由内置的 **Ngrok** 或 **Cloudflare Quick Tunnel** 反向代理至公网，安全机制只接受域名相同的 Host 以及校验 database ID。
    *   当在 Notion 页面上点击自定义按钮（如新建草稿、全部回复、单人回复）时，会调用 Webhook。
    *   [draft_handler.py](src/mail/draft_handler.py) 在独立背景线程中运行 COM 操作防止主线程卡死，解析动作并在本地 Outlook 执行 `ReplyAll()` 或 `Reply()` 自动填充 AI 建议的正文，实现**静默保存草稿**或**直接发送邮件**。
*   **飞书通知**：收件箱收到重要邮件同步成功后，自动推送富文本卡片通知至飞书，附带 AI 判定优先级及摘要。

---

## 📊 系统架构与工作流程

MailAgent Windows 在后台采用 **Supervisor 主进程 + 2 子进程**（多线程/asyncio）的架构运行，整体架构如下：

```mermaid
graph TD
    %% Define Styles & Classes
    classDef input fill:#e1f5fe,stroke:#01579b,stroke-width:2px;
    classDef process fill:#fff9c4,stroke:#fbc02d,stroke-width:2px;
    classDef output fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px;
    classDef module fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px;

    %% Subgraph Inputs
    subgraph Inputs ["【输入源】"]
        OutlookEvents["Outlook COM 事件 OnNewMailEx 和 OnItemAdd"]:::input
        OutlookHistory["Outlook 历史邮件 startup_lookback_days"]:::input
        NotionWebhook["Notion 反向 Webhook 操作回调"]:::input
        DailyTimer["每日定时器 07:00 早报"]:::input
    end

    Supervisor["ProcessManager 主进程<br/>(负责监控与自动重启)"]:::module

    %% Subgraph Process A
    subgraph ProcessA ["【进程 A: Mail Worker】"]
        direction TB
        Radar["COM Radar 监听器"]:::module
        API_Server["API Webhook 服务器"]:::module
        TaskPool["优先级任务池"]:::module
        Watcher["WindowsWatcher 核心循环"]:::module
        OutlookArm["Outlook COM 驱动"]:::module
        NotionSync["Notion 同步驱动"]:::module
    end

    %% Subgraph Process B
    subgraph ProcessB ["【进程 B: AI Worker】"]
        direction TB
        Debounce["防抖循环与并发控制"]:::module
        AIController["Notion AI 控制器<br/>(Playwright)"]:::module
    end

    %% Subgraph Outputs
    subgraph Outputs ["【输出结果】"]
        NotionPage["Notion 数据库页面树形线程"]:::output
        OutlookDraft["Outlook 草稿或发送邮件"]:::output
        NotionAIChat["Notion AI 侧边栏对话总结"]:::output
    end

    %% Connections
    Supervisor -->|启动/监控| ProcessA
    Supervisor -->|启动/监控| ProcessB

    OutlookEvents -->|实时捕获| Radar
    OutlookHistory -->|启动扫描| Watcher
    NotionWebhook -->|公网请求| API_Server
    DailyTimer -->|定时触发| ProcessB

    Radar -->|添加同步任务| TaskPool
    API_Server -->|添加草稿任务| TaskPool
    TaskPool -->|出队分发| Watcher
    
    Watcher --> OutlookArm
    Watcher --> NotionSync
    
    NotionSync -->|写入数据| NotionPage
    OutlookArm -->|执行发信| OutlookDraft
    
    Watcher -->|IPC 队列(email_synced)| Debounce
    Debounce --> AIController
    AIController -->|静默交互| NotionAIChat
```

### 1. 模块分工说明

| 模块名称 | 所在路径/文件 | 核心职责 | 输入 | 处理流程 | 输出 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **进程管理器 (Supervisor)** | [process_manager.py](process_manager.py) | 全局生命周期管理，启动和监控 MailWorker 和 AIWorker。 | 系统信号 | 1. 启动进程 A 和 进程 B<br>2. 监控存活状态<br>3. 自动重启 crash 的子进程（带指数退避策略） | 启动与监控日志 |
| **MailWorker (进程 A)** | [workers/mail_worker.py](workers/mail_worker.py) | 核心通信与同步进程，专注于处理 COM 和 I/O 交互。 | `.env` 配置 | 1. 启动内网穿透隧道与 API Webhook 服务<br>2. 启动 `WindowsWatcher` 主事件循环进行邮件同步 | 向进程 B 发送 IPC 队列信号 |
| **AIWorker (进程 B)** | [workers/ai_worker.py](workers/ai_worker.py) | 自动化 Playwright 浏览器和定时任务专用进程。 | IPC 队列信号 | 1. 从队列读取同步完成信号并触发防抖逻辑<br>2. 达到条件时运行 Notion AI 控制器交互 | Playwright 浏览器操作 |
| **内网穿透隧道** | [src/api/tunnel.py](src/api/tunnel.py) | 将本地 HTTP API 暴露至公网，获取唯一的允许 Host 名以确保回调安全性。 | 配置 `REVERSE_PROXY` (`ngrok` 或 `cloudflare`) | 1. 检测已有隧道进程，或 Popen 启动对应隧道进程<br>2. 提取并记录公网 URL，更新 Security Host 校验关键字 | 公网映射地址及 Allowed Host |
| **API 回调服务器** | [src/api/server.py](src/api/server.py) | 接收 Notion 页面上的动作 Webhook 回调，并在任务队列中排队。 | Notion 触发的 HTTP 回调请求 | 1. 验证 Host 是否符合 Allowed Host，校验 database ID<br>2. 解析 Payload 获得 Message ID、动作类型等<br>3. 包装成 Task，以 **Priority 1 (HIGH)** 插入任务队列 | 成功入队响应 |
| **优先级任务池** | [src/scheduler/task_pool.py](src/scheduler/task_pool.py) | 维护全局线程安全的优先级队列，在同一优先级内通过负时间戳实现 LIFO（最新优先）。 | 各种 Task 实体 | 1. 按照 Priority (1 > 2 > 3) 自动排序<br>2. 内部时间反向（-timestamp）降序出队 | 排序后的任务对象 |
| **Windows Watcher** | [src/mail/new_watcher_win.py](src/mail/new_watcher_win.py) | 核心事件循环，消费任务池任务，限制邮件同步并发数，防止附件上传引发资源耗尽。 | 全局任务池中出队的任务 | 1. 启动 COM 实时雷达并执行启动补查<br>2. 开启最大并发为 3 的同步信号量<br>3. 调用同步、通知和 AI 流程 | 调度并完成单次任务处理 |
| **Outlook COM 雷达** | [src/mail/com_radar.py](src/mail/com_radar.py) | 订阅 Outlook 实时新收信/发信事件。 | Outlook 实时收发事件 | 1. 订阅 `NewMailEx` 与 Sent Items 文件夹的 `ItemAdd`<br>2. 以 **Priority 2 (MEDIUM)** 插入同步任务 | 任务队列投递 |
| **Outlook COM 驱动** | [src/mail/outlook_com_arm.py](src/mail/outlook_com_arm.py) | 封装对 Outlook COM 的底层读取、标记已读和扫描。 | `EntryID`, `StoreID`, 扫描天数 | 1. 使用 fast MAPI Table 或 Restrict 进行邮件内容及元数据提取<br>2. 支持标记已读、获取未读邮件数量等功能 | 邮件实体及附件原始数据 |
| **草稿/发信处理器** | [src/mail/draft_handler.py](src/mail/draft_handler.py) | 接收反向 Webhook 回调命令，在独立 COM 线程中对 Outlook 邮件执行草稿拟定/发送。 | 回调 Payload | 1. 根据 Message ID/ConversationID 在收件箱/已发送中定位原邮件<br>2. 执行 `ReplyAll()` / `Reply()` 或 fallback 新建邮件<br>3. 填充 AI 建议并执行 `Save()` 或 `Send()` | 本地 Outlook 动作执行 |
| **Notion 同步驱动** | [src/notion/sync.py](src/notion/sync.py) | 封装 Notion 数据库写入、三级线程关系关联、附件转换和上传。 | `Email` 实体模型 | 1. 将 HTML 转换为 Notion Blocks 结构<br>2. 上传附件（Office 转 PDF，EML 遇 403 自动压缩为 zip 上传）<br>3. 多级寻找 Thread 关联并更新 `"Parent item"` 和 `"Sub-item"` 属性 | 成功创建的 Notion 页面 |
| **Notion AI 控制器** | [src/ai/controller.py](src/ai/controller.py) | 使用 Playwright 无头 Chromium 操纵 Notion AI 侧边栏完成任务。 | AI 触发信号，`prompt.txt` 模板 | 1. 确保后台浏览器已就绪并复用 cookies<br>2. 应用防抖（静默期、强制时间）和批次控制机制<br>3. 定位 AI 框模拟键盘输入 prompt 并追踪停止状态<br>4. 当会话数达到上限重构会话以防止浏览器内存泄露 | Notion 侧边栏中 AI 的汇总数据与截图 |

### 2. 任务类型与优先级说明

系统内的所有工作均抽象为 Task 放入全局任务池 ([task_pool.py](src/scheduler/task_pool.py))。队列基于 `PriorityQueue`（最小堆）实现，`priority_level` 越小，优先级越高，越优先出队消费。

| 任务类型 (`TaskType`) | 优先级 (`TaskPriority`) | 触发来源 | 核心行为与时序特性 |
| :--- | :--- | :--- | :--- |
| `WEBHOOK_DRAFT` | **High (1)** | Notion 数据库页面上按钮点击触发的反向 Webhook | **高优先级抢占**：接收到回调后，不等待其他同步任务，主循环在独立后台线程中立即唤起 COM 进程执行 `ReplyAll()` / `Reply()` 生成并保存草稿或直接发送。 |
| `MAIL_SYNC` | **Medium (2)** | 本地 Outlook 实时收/发件箱事件触发 (COM Radar) | **实时同步**：实时捕获新到达的邮件 (`OnNewMailEx`) 或自己发出的新邮件 (`OnItemAdd`)，进行提取、转换、上传至 Notion，并自动触发 Notion AI 交互防抖及强推机制。 |
| `DAILY_SCHEDULE` | **Medium (2)** | 每日早上 `07:00` 的定时汇总任务 | **定时汇总**：将预设的任务排入队列，拉起后台 Chromium 执行早报 AI 生成。 |
| `MAIL_SYNC` | **Low (3)** | 软件启动时的 `STARTUP_LOOKBACK_DAYS` 补查扫描 | **低优先级后台回填**：扫描过去 N 天内缺漏的邮件并排队同步。**LIFO (最新优先)**：补查任务均携带原邮件时间戳，由于在队列中使用负时间戳排序，时间越近的遗漏邮件越先被消费，确保近期的紧急邮件优先处理。 |

---

## 🛠️ 环境要求

*   **操作系统**：Windows 10 / 11
*   **软件依赖**：
    *   **Classic Outlook**（不支持 Outlook New，新版砍掉了 COM 接口）
    *   **Microsoft Office**（Word、Excel、PowerPoint，用于附件格式转换为 PDF）
    *   Python 3.10+
*   **Notion 配置**：
    *   Notion Integration Token
    *   关联的电子邮件 Notion 数据库（配置好 `"Parent item"`, `"Sub-item"`, `"Message ID"`, `"Thread ID"`, `"Subject"` 等属性）

---

## 📦 安装与配置

### 1. 安装依赖并初始化 Playwright
```powershell
pip install -r requirements.txt
playwright install chromium
```

### 2. 诊断与前置检查 (推荐)
可以使用预检脚本，自动检查 Outlook COM 组件、Notion API Token 连通性以及飞书机器人 Webhook：
```powershell
python scripts/preflight_check.py
```

### 3. 环境配置项说明 (.env)

| 环境变量名 | 类型 | 默认值 | 描述 |
| :--- | :--- | :--- | :--- |
| `NOTION_TOKEN` | `str` | **必填** | Notion Integration Token |
| `EMAIL_DATABASE_ID` | `str` | **必填** | 同步邮件的 Notion 数据库 ID |
| `USER_EMAIL` | `str` | **必填** | 您的 Outlook 邮箱地址（用于定位账户） |
| `MAIL_ACCOUNT_NAME` | `str` | `Exchange` | 匹配的 Outlook 账户显示名称（例如邮箱地址或 "Exchange"） |
| `MAIL_INBOX_NAME` | `str` | `收件箱` | 收件箱文件夹的本地化名称（在中文 Outlook 下通常填 `收件箱` 或 `Inbox`） |
| `MAIL_SENT_NAME` | `str` | `已发送` | 发件箱文件夹的本地化名称（中文填 `已发送` 或 `Sent Items`） |
| `SYNC_MAILBOXES` | `str` | `收件箱` | 要同步的文件夹（支持逗号分隔，如 `收件箱,已发送`） |
| `STARTUP_LOOKBACK_DAYS` | `int` | `1` | 启动时自动补查的天数（防漏） |
| `NOTION_AI_TRIGGER_HISTORICAL`| `bool` | `False` | 补查历史邮件时是否也触发 Notion AI 提问 |
| `OFFICE_CONVERT_ENABLED` | `bool` | `True` | 是否启用本地 Office 文档格式转换为 PDF 预览 |
| `REVERSE_PROXY` | `str` | `""` | 内网穿透服务商。可选 `ngrok` 或 `cloudflare`，留空代表禁用 API 服务与隧道 |
| `NOTION_AI_PAGE_URL` | `str` | `""` | 用于唤起 Notion AI 侧边栏的页面 URL |
| `NOTION_AI_BATCH_SIZE` | `int` | `5` | 同步新邮件数量达到该值时，忽略防抖立即触发 Notion AI |
| `NOTION_AI_MAX_CHATS_PER_SESSION`| `int` | `10` | 浏览器单次会话内调用 Notion AI 的最大次数，超出后重组浏览器以防内存泄漏 |
| `DEBOUNCE_QUIET_SEC` | `int` | `120` | 静默期（秒），新邮件同步后等待若干秒无新邮件则触发 AI |
| `DEBOUNCE_FORCE_SEC` | `int` | `1800` | 强推时间（秒），在此间隔内即便一直同步邮件也必须执行一次 AI 总结 |
| `NOTION_AI_FALLBACK_WAIT_SEC` | `int` | `120` | 页面上未检测到 AI 停止撰写按钮时的保守等待秒数 |
| `NOTION_AI_WAIT_TIMEOUT` | `int` | `600` | 等待 Notion AI 当前生成完成的最大超时时间（秒） |
| `NOTION_ACTION_CREATE_DRAFT` | `str` | `35d15375-830d-806b-b799-005aa8637e7e` | Notion 页面上 `"Create Draft"` 按钮对应的 Action ID |
| `NOTION_ACTION_REPLY_ALL` | `str` | `32c15375-830d-8065-8fbf-005a31068639` | Notion 页面上 `"Reply All"` 按钮对应的 Action ID |
| `NOTION_ACTION_REPLY` | `str` | `39915375-830d-8079-8c98-005af593110f` | Notion 页面上 `"Reply"` 按钮对应的 Action ID |
| `FEISHU_NOTIFY_ENABLED` | `bool` | `False` | 是否启用飞书通知卡片 |
| `FEISHU_APP_ID`/`FEISHU_APP_SECRET` | `str` | `""` | 飞书开放平台应用的凭证（用以获取 Token 进行高级发送） |
| `FEISHU_WEBHOOK_URL` | `str` | `""` | 飞书自定义群机器人的 Webhook 地址 |

---

## 🏃 使用指南

### 第一步：首次运行登录授权 (必须)
为了让 Playwright 无头 Chromium 能够继承授权状态访问您的 Notion 工作区，您必须完成首次登录：
```powershell
python notion_auth.py
```
1. 运行后会弹出一个有头模式的 Chrome 窗口，请在其中手动登录 Notion 账号（可扫码、密码登录）。
2. 加载完毕并显式打开工作区后，返回命令行控制台，按下 **回车键 (Enter)**。
3. 凭证将自动保存至本地的 `notion_auth.json` 与 `user_agent.txt`。

### 第二步：启动服务
```powershell
python main.py
```
运行后，程序会输出实时日志，并自动启动 Radar 线程与 Tunnel。若启用了 `REVERSE_PROXY`，会将映射出的公网 API Webhook URL（如 `https://xxxx.ngrok-free.app`）输出到控制台及本地日志中，供您配置在 Notion 的 Buttons 触发动作中。

### 💡 可选操作

*   **历史数据批量回填**：
    若要手动补查并同步过去 30 天内的所有邮件，请使用以下脚本：
    ```powershell
    python scripts/initial_sync_win.py 30
    ```

---

## 📂 项目结构

*   `main.py`: 服务的统一入口，加载启动 `process_manager.py`。
*   `process_manager.py`: Supervisor 主进程，负责启动、监控和自动重启子进程。
*   `notion_auth.py`: 首次运行的 Notion 登录授权助手，保存登录凭证用于无头模式。
*   `workers/`: 包含两个核心工作进程的入口：
    *   `mail_worker.py`: 进程 A (MailWorker)，负责 COM 事件、Webhook 监听和 Notion 同步。
    *   `ai_worker.py`: 进程 B (AIWorker)，负责后台 Playwright 无头浏览器、防抖以及定时任务。
*   `src/`:
    *   `mail/`:
        *   `com_radar.py`: Outlook 原生 COM 雷达监听器，实时捕捉收发件事件并投递至任务池。
        *   `outlook_com_arm.py`: Outlook COM 接口驱动，封装获取邮件、标记已读和高速扫描。
        *   `draft_handler.py`: COM 草稿生成与发送器（在后台独立线程执行，规避 COM 卡死问题）。
        *   `new_watcher_win.py`: 主同步工作流，管理核心事件循环与全局任务消费。
        *   `conversation_index.py`: 邮件 `ConversationIndex` 会话索引解析器。
        *   `attachment_handler.py`: 邮件普通附件处理与 Notion 上传。
        *   `sync_store.py`: 本地已同步邮件 SQLite 记录查询与写入。
    *   `notion/`:
        *   `sync.py`: Notion 数据库交互核心逻辑（HTML 块解析，三级降级线程处理，WAF 403 zip 避让）。
        *   `client.py` & `rate_limiter.py`: Notion API 客户端及其自适应流控。
    *   `ai/`:
        *   `controller.py`: Notion AI Playwright 控制器，实现防抖、强推、阈值并发控制以及会话主动回收。
    *   `api/`:
        *   `server.py`: Notion Webhook API 服务器，提供安全 Host 及 database ID 验证，并将命令推入高优先级队列。
        *   `tunnel.py`: 内网穿透隧道管理器（自动管理 Ngrok 或 Cloudflared 子进程）。
    *   `notify/`:
        *   `feishu.py`: 飞书富文本消息通知。
    *   `scheduler/`:
        *   `task_pool.py`: 统一的任务池，支持优先级排序与 LIFO（最新优先）出队。
        *   `daily.py`: 每日定时调度器（早报触发）。
    *   `converter/`:
        *   `office_converter_win.py`: 基于本机 COM 对 docx/xlsx/pptx 进行 PDF 自动转换。
        *   `html_converter.py`: 邮件富文本 HTML 解析并序列化为 Notion 专用 Block JSON。
        *   `eml_generator.py`: 本地生成邮件的 `.eml` 文件。
*   `data/`: 存放 `sync_store.db` 的本地 SQLite 数据库目录。
*   `logs/`: 系统运行日志目录。

---

## ⚠️ 注意事项

*   **Office 运行权限**：本工具直接利用 Word/Excel/PowerPoint 本地应用组件进行转换，因此执行程序的用户必须在 Windows 系统中对这些程序拥有完全的 DCOM 执行权限，且本地 Office 已经正常激活。
*   **Outlook 安全弹窗**：首次使用 MAPI COM 读取 Outlook 时，微软安全机制可能会询问您“有第三方程序正尝试读取您的联系人或发送邮件”，此时需要选择“允许访问”并勾选最长允许时长（推荐 10 分钟或永久允许）。
*   **内网穿透保活**：如果使用 Ngrok，免费版账号的公开 URL 会在进程重启后随机发生变化，需要在 Notion 的 Button 中同步更新 Webhook 的触发 URL。
