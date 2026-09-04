import os
import asyncio
import time
import random
from pathlib import Path
from queue import Empty as QueueEmpty
from loguru import logger
from playwright_stealth import Stealth
from src.config import config

class AIController:
    """
    Notion AI 控制器。
    集中管理 Playwright 无头浏览器的并发、会话防抖以及生命周期。
    """
    def __init__(self, ai_trigger_queue=None, shutdown_event=None):
        # IPC 通信
        self._ai_trigger_queue = ai_trigger_queue
        self._shutdown_event = shutdown_event

        # 防抖状态变量
        self._last_email_sync_time = 0.0          # 最后一次成功同步邮件的时间
        self._last_ai_trigger_time = time.time()  # 最后一次触发 AI 的时间
        self._has_pending_ai_trigger = False      # 是否有待触发的 AI 任务
        
        # 批处理与会话状态
        self._uploaded_in_batch = 0
        self._prompts_in_current_chat = 0          # 当前 Notion AI 对话内的 prompt 提交计数 (上限如 5 次)
        self._new_chats_count = 0                  # 已开启的 New Chat 会话计数 (上限如 8 次后重启浏览器)
        self._ai_chats_in_session = 0              # 兼容字段
        self._in_daily_summary_chat = False        # 标记当前是否处于每日总结的独立会话中
        
        # 并发控制 —— 使用 asyncio.Lock 确保 AI 触发严格串行排队
        self._lock = asyncio.Lock()
        
        # 任务执行队列 —— 顺序消费，防止并发冲突及锁等待超时丢失任务
        self._ai_task_queue = asyncio.Queue()
        self._last_heartbeat_log_time = 0.0
        
        # Playwright 持续化实例
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def _ensure_browser(self):
        """确保浏览器处于健康可用状态。任何组件异常都会触发完整重建，带重试机制。"""
        # 检查所有组件是否健康
        page_closed = False
        try:
            page_closed = self.page is not None and self.page.is_closed()
        except Exception:
            page_closed = True

        browser_healthy = (
            self.playwright is not None
            and self.browser is not None
            and self.browser.is_connected()
            and self.context is not None
            and self.page is not None
            and not page_closed
        )

        if browser_healthy:
            return True

        # 浏览器不健康，先清理残留状态再重建
        if self.playwright is not None:
            logger.warning("⚠️ Browser in unhealthy state, cleaning up before re-initialization...")
            await self.close()

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                from playwright.async_api import async_playwright
                logger.info(f"🌐 Initializing persistent Playwright browser... (attempt {attempt}/{max_retries})")
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(
                    headless=True,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--no-sandbox',
                        '--disable-dev-shm-usage'
                    ]
                )

                script_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                from src.setup.persistence import get_browser_dir
                browser_dir = get_browser_dir()

                auth_state_path = browser_dir / "notion_auth.json"
                if not auth_state_path.exists():
                    auth_state_path = Path(os.path.join(script_dir, "notion_auth.json"))

                user_agent_path = browser_dir / "user_agent.txt"
                if not user_agent_path.exists():
                    user_agent_path = Path(os.path.join(script_dir, "user_agent.txt"))

                if not os.path.exists(auth_state_path):
                    logger.error(f"❌ Auth state file does not exist: {auth_state_path}. Please sign in via Settings or Setup wizard first!")
                    await self.close()
                    return False

                context_args = {
                    "storage_state": auth_state_path,
                    "viewport": {"width": 1920, "height": 1080},
                    "locale": "zh-CN",
                    "timezone_id": "Asia/Shanghai"
                }
                if os.path.exists(user_agent_path):
                    with open(user_agent_path, "r", encoding="utf-8") as f:
                        context_args["user_agent"] = f.read().strip()

                self.context = await self.browser.new_context(**context_args)
                self.context.set_default_timeout(60000)
                self.page = await self.context.new_page()
                await Stealth().apply_stealth_async(self.page)

                page_url = config.notion_ai_page_url
                if not page_url:
                    logger.error("❌ NOTION_AI_PAGE_URL is not specified in configuration!")
                    await self.close()
                    return False

                logger.info(f"🌐 Accessing Notion page via headless browser: {page_url}")
                await self.page.goto(page_url, wait_until="load")
                logger.info("✅ Initial page loaded, waiting 8 seconds to ensure routing and AI panel are fully initialized...")
                await asyncio.sleep(8)
                self._prompts_in_current_chat = 0
                self._in_daily_summary_chat = False
                # Proactively discover and sync available AI models from Notion UI on startup
                try:
                    await self.sync_available_models()
                except Exception as sync_ex:
                    logger.warning(f"⚠️ Model discovery on browser startup encountered issue: {sync_ex}")
                return True

            except Exception as e:
                logger.error(f"❌ Browser initialization failed (attempt {attempt}/{max_retries}): {e}")
                await self.close()  # 清理半初始化状态，确保下次重试从干净状态开始
                if attempt < max_retries:
                    wait_sec = 10 * attempt  # 指数退避：10s, 20s
                    logger.info(f"⏳ Retrying browser initialization in {wait_sec}s...")
                    await asyncio.sleep(wait_sec)

        logger.critical("🚨 All browser initialization attempts failed! AIWorker will retry on next trigger.")
        return False


    async def execute_ai_trigger(self, subject: str, action: str = None):
        """处理会话上限逻辑，并调用底层的无头浏览器。使用 asyncio.Lock 确保多个触发严格串行排队。"""
        if self._lock.locked():
            logger.warning(f"⚠️ Notion AI is already running, queuing this trigger: '{subject}' (will execute after current task finishes)...")

        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=config.notion_ai_wait_timeout)
        except asyncio.TimeoutError:
            logger.error(f"❌ Timeout waiting for Notion AI lock ({config.notion_ai_wait_timeout}s), skipping trigger: '{subject}'")
            return

        try:
            restart_browser = False
            need_new_chat = False

            if action == "scheduled_daily_sync":
                # 每日总结使用独立对话
                need_new_chat = True
                self._in_daily_summary_chat = True
            else:
                if getattr(self, "_in_daily_summary_chat", False):
                    # 上一轮是每日总结会话，切回普通邮件处理时开启新对话
                    need_new_chat = True
                    self._prompts_in_current_chat = 1
                    self._in_daily_summary_chat = False
                    logger.info("🆕 Switching from daily summary to email processing. Opening fresh chat session...")
                else:
                    self._prompts_in_current_chat += 1
                    max_prompts = getattr(config, "notion_ai_max_chats_per_session", 5) or 5
                    max_new_chats = getattr(config, "notion_ai_max_new_chats_before_browser_restart", 8) or 8

                    # 判定是否需要开启 New Chat（满 max_prompts 次 prompt 时）
                    if self._prompts_in_current_chat > max_prompts:
                        self._prompts_in_current_chat = 1
                        self._new_chats_count += 1

                        if self._new_chats_count >= max_new_chats:
                            # 满 8 次 New Chat，彻底重启无头浏览器释放资源
                            self._new_chats_count = 0
                            restart_browser = True
                            logger.info(f"🔄 Reached browser session limit ({max_new_chats} new chats). Restarting browser for memory cleanup...")
                        else:
                            # 满 5 次 Prompt，仅在同一浏览器内开启新对话
                            need_new_chat = True
                            logger.info(f"🆕 Reached chat prompt limit ({max_prompts} in current chat). Opening New Chat ({self._new_chats_count}/{max_new_chats})...")
                    else:
                        logger.info(f"💬 Submitting prompt to current chat (Prompt {self._prompts_in_current_chat}/{max_prompts} in current session)...")

            self._last_ai_trigger_time = time.time()
            try:
                await self._do_trigger_ai(action=action, restart_browser=restart_browser, need_new_chat=need_new_chat)
            except Exception as e:
                import traceback
                logger.error(f"❌ Failed to trigger Notion AI:\n{traceback.format_exc()}")
        finally:
            self._lock.release()

    async def queue_ai_trigger(self, subject: str, action: str = None):
        """将 AI 触发任务放入顺序执行队列"""
        await self._ai_task_queue.put({"subject": subject, "action": action})
        q_len = self._ai_task_queue.qsize()
        logger.debug(f"📥 Queued AI trigger: '{subject}' (Current queue size: {q_len})")

    async def task_worker_loop(self):
        """后台顺序消费队列中的 AI 任务，确保 Notion AI 触发严格串行执行且不丢弃任何任务。"""
        logger.info("🤖 Notion AI sequential task worker started.")
        while True:
            try:
                if self._shutdown_event and self._shutdown_event.is_set():
                    break
                try:
                    # 使用 1 秒超时获取任务，以便响应 shutdown_event
                    item = await asyncio.wait_for(self._ai_task_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                subject = item.get("subject", "Unnamed Task")
                action = item.get("action", None)
                remaining_q = self._ai_task_queue.qsize()
                logger.info(f"▶️ Executing AI task: '{subject}' (Queued tasks remaining: {remaining_q})")
                try:
                    await self.execute_ai_trigger(subject, action=action)
                except Exception as e:
                    logger.error(f"❌ Error during AI task execution '{subject}': {e}")
                finally:
                    self._ai_task_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in task_worker_loop: {e}")
                await asyncio.sleep(2)
        logger.info("Notion AI sequential task worker stopped.")

    async def debounce_loop(self):
        """后台防抖循环：从 IPC 队列消费 AI 触发信号，结合防抖和强制间隔触发 Notion AI"""
        logger.info("⏰ Notion AI debounce loop started.")

        # 启动时触发一次 AI（处理重启前积压的未处理邮件，保证不漏触发）
        await self.queue_ai_trigger("Startup Batch")

        while True:
            try:
                # 检查关停信号
                if self._shutdown_event and self._shutdown_event.is_set():
                    logger.info("Shutdown event detected, stopping debounce loop.")
                    break

                # 非阻塞地排空 IPC 队列
                drained = 0
                if self._ai_trigger_queue is not None:
                    while True:
                        try:
                            msg = self._ai_trigger_queue.get_nowait()
                            drained += 1
                            self._uploaded_in_batch += 1
                            self._last_email_sync_time = msg.get("ts", time.time())
                            self._has_pending_ai_trigger = True
                        except QueueEmpty:
                            break

                if drained > 0:
                    logger.info(f"📬 Received {drained} AI trigger signal(s). Current backlog/progress: {self._uploaded_in_batch} mail(s).")

                now = time.time()
                batch_size = max(1, getattr(config, "notion_ai_batch_size", 2) or 2)

                # 场景 1：达到或超过批次阈值（支持大量积压邮件按每 batch_size 封拆分为多个任务顺序入队）
                if self._uploaded_in_batch >= batch_size:
                    num_batches = self._uploaded_in_batch // batch_size
                    processed_mails = num_batches * batch_size
                    self._uploaded_in_batch %= batch_size
                    self._has_pending_ai_trigger = (self._uploaded_in_batch > 0)
                    logger.info(
                        f"🚨 Batch threshold reached: splitting {processed_mails} mail(s) into {num_batches} Notion AI task(s) "
                        f"(batch size: {batch_size}, {self._uploaded_in_batch} remainder)."
                    )
                    for i in range(num_batches):
                        await self.queue_ai_trigger(f"Batch ({i+1}/{num_batches}) - {batch_size} mails")

                # 场景 2：静默期到达，处理不足一个批次的零头邮件
                elif self._has_pending_ai_trigger and self._last_email_sync_time > 0:
                    quiet_elapsed = now - self._last_email_sync_time
                    quiet_sec = getattr(config, "debounce_quiet_sec", 30) or 30
                    if quiet_elapsed >= quiet_sec:
                        rem = self._uploaded_in_batch
                        logger.info(
                            f"🔔 Quiet period of {quiet_sec}s reached with no new emails. "
                            f"Triggering Notion AI for remainder ({rem} mail(s))..."
                        )
                        self._has_pending_ai_trigger = False
                        self._uploaded_in_batch = 0
                        await self.queue_ai_trigger(f"Debounced Batch ({rem} mail(s))")

                # 场景 3：强制时间间隔（无新邮件时每隔一段时间自动触发 Notion AI 检查）
                force_elapsed = now - self._last_ai_trigger_time
                force_sec = getattr(config, "debounce_force_sec", 600) or 600
                if force_sec > 0 and force_elapsed >= force_sec:
                    logger.info(
                        f"🔔 Periodic check interval of {force_sec}s reached with no recent AI activity. "
                        f"Triggering Notion AI to inspect database for unhandled emails..."
                    )
                    self._has_pending_ai_trigger = False
                    self._uploaded_in_batch = 0
                    self._last_ai_trigger_time = now
                    await self.queue_ai_trigger(f"Forced Periodic Check ({force_sec}s timeout)")

                # 心跳日志：当系统处于空闲时，每隔 180 秒打印一次心跳提示，明确告知下一次自动检查倒计时
                elif force_sec > 0 and (now - self._last_heartbeat_log_time >= 180):
                    if not self._has_pending_ai_trigger and self._ai_task_queue.empty():
                        rem_sec = max(0, int(force_sec - force_elapsed))
                        logger.info(f"⏳ [AIWorker] Idle: waiting for new emails. Next periodic AI check in {rem_sec}s.")
                        self._last_heartbeat_log_time = now

                await asyncio.sleep(1)  # 每秒检查一次

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in debounce loop: {e}")
                await asyncio.sleep(5)

    async def _click_new_chat(self):
        """点击 New Chat 按钮启动独立对话（支持中英文界面及多种特征定位）"""
        try:
            if not self.page:
                return False
            await self._close_all_overlays()
            logger.info("🆕 Clicking 'New chat' button to start an isolated session...")

            selectors = [
                "[data-testid='new-chat-button']",
                "[data-testid='unified-chat-new-chat-button']",
                "[data-testid='chat-new-chat-button']",
                "[aria-label*='New chat' i]",
                "[aria-label*='新对话']",
                "[aria-label*='新建对话']",
                "[aria-label*='新建聊天']",
                "[aria-label*='开启新对话']",
                "[title*='New chat' i]",
                "[title*='新对话']",
                "[title*='新建对话']",
                "[title*='新建聊天']",
                "button:has-text('New chat')",
                "div[role='button']:has-text('New chat')",
                "button:has-text('新对话')",
                "div[role='button']:has-text('新对话')",
                "button:has-text('新建对话')",
                "div[role='button']:has-text('新建对话')",
                "button:has-text('新建聊天')",
                "div[role='button']:has-text('新建聊天')",
            ]

            for sel in selectors:
                try:
                    btn = self.page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click(force=True, delay=100)
                        await asyncio.sleep(random.uniform(1.5, 2.5))
                        logger.info(f"✅ Clicked 'New chat' button via selector: {sel}")
                        return True
                except Exception:
                    continue

            logger.warning("⚠️ 'New chat' button not found via UI selectors.")
            return False
        except Exception as e:
            logger.warning(f"⚠️ Failed to click 'New chat': {e}")
            return False

    async def _close_all_overlays(self):
        """
        彻底收起 Notion 页面上的所有下拉菜单、二级弹出层以及遮罩层 (Overlay Container)，
        避免残留的 backdrop 阻挡对输入框或按钮的点击事件。
        """
        if not self.page:
            return
        try:
            # 1. 连续按 Escape 键收起多级菜单 (如 Older models -> Model selector menu)
            for _ in range(3):
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.1)

            # 2. 隐藏桌面客户端引导弹窗
            await self.page.evaluate("""() => {
                const overlay = document.querySelector('.notion-overlay-container');
                if (overlay) {
                    const divs = overlay.querySelectorAll(':scope > div');
                    divs.forEach(d => {
                        const text = d.innerText || '';
                        if (text.includes('desktop app') || text.includes('Open in')) {
                            d.style.display = 'none';
                        }
                    });
                }
            }""")
        except Exception as e:
            logger.debug(f"Error in _close_all_overlays: {e}")

    async def _dismiss_desktop_app_prompt(self):
        """兼容旧调用：关闭或隐藏阻挡弹窗"""
        await self._close_all_overlays()

    async def _get_chat_input(self):
        """
        精准定位 Notion AI 聊天输入框 (contenteditable)。
        通过专属特征（placeholder/data-content-editable-leaf/testid 等）精确定位，
        避免被侧边栏、页面正文块或搜索框干扰。
        """
        if not self.page:
            return None
        try:
            # 1. 优先通过专属 placeholder 与 content-editable-leaf 特征精准查找
            selectors = [
                'div[contenteditable="true"][placeholder*="with AI" i]',
                'div[contenteditable="true"][placeholder*="AI" i]',
                'div[data-content-editable-leaf="true"][contenteditable="true"]',
                '[data-testid="unified-chat-input"]',
                'div[role="textbox"][contenteditable="true"]',
            ]
            for sel in selectors:
                loc = self.page.locator(sel).last
                if await loc.count() > 0 and await loc.is_visible():
                    return loc

            # 2. 回退：页面上最后一个可见的 contenteditable
            fallback = self.page.locator('div[contenteditable="true"], [role="textbox"]').last
            if await fallback.count() > 0 and await fallback.is_visible():
                return fallback
        except Exception as e:
            logger.debug(f"Error finding chat input: {e}")
        return None

    async def _focus_and_activate_chat(self):
        """
        确保界面焦点回到 Notion AI 聊天输入框，激活输入状态与光标位置。
        """
        chat_input = await self._get_chat_input()
        if not chat_input:
            return None
        try:
            await chat_input.scroll_into_view_if_needed()
            await chat_input.click(force=True, delay=50)
            await chat_input.focus()
            # 通过 JavaScript 强制激活光标与输入上下文
            await chat_input.evaluate("""el => {
                el.focus();
                const selection = window.getSelection();
                if (selection) {
                    const range = document.createRange();
                    range.selectNodeContents(el);
                    range.collapse(false);
                    selection.removeAllRanges();
                    selection.addRange(range);
                }
            }""")
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.debug(f"Focus chat input error: {e}")
        return chat_input

    async def _get_model_selector_button(self):
        """寻找 Notion AI 聊天输入框底部的模型选择按钮"""
        if not self.page:
            return None
        try:
            # 1. 优先使用专属属性 data-testid
            btn = self.page.locator('[data-testid="unified-chat-model-button"]').first
            if await btn.count() > 0 and await btn.is_visible(timeout=1000):
                return btn
            
            # 2. 回退：寻找带有 aria-haspopup="dialog" 的按钮
            dialog_btn = self.page.locator('div[role="button"][aria-haspopup="dialog"]').first
            if await dialog_btn.count() > 0 and await dialog_btn.is_visible(timeout=1000):
                return dialog_btn
                
            # 3. 回退：在工具栏中寻找文本匹配模型关键字的小按钮
            import re
            triggers = self.page.locator("[role='button'], button, [aria-haspopup]").filter(
                has_text=re.compile(r"Auto|Sonnet|Claude|GPT|Gemini|Opus|Kimi|Grok|DeepSeek|GLM", re.I)
            )
            count = await triggers.count()
            for i in range(count):
                t = triggers.nth(i)
                if await t.is_visible(timeout=500):
                    box = await t.bounding_box()
                    if box and box['width'] < 250 and box['height'] < 60:
                        return t
        except Exception as e:
            logger.debug(f"Error finding model selector button: {e}")
        return None

    async def _open_model_selector_dropdown(self, btn) -> bool:
        """
        可靠地点击打开模型选择下拉菜单，并验证弹窗菜单是否已成功挂载到 DOM。
        """
        if not btn or not self.page:
            return False

        for attempt in range(1, 4):
            try:
                await btn.scroll_into_view_if_needed()
                await btn.click(force=True, delay=100)
            except Exception:
                try:
                    await btn.evaluate("el => el.click()")
                except Exception:
                    pass

            await asyncio.sleep(1.0)

            # 检查菜单是否已挂载到 DOM
            has_menu = await self.page.evaluate("""() => {
                const containers = document.querySelectorAll('.notion-overlay-container, [role="menu"], [data-radix-popper-content-wrapper], [role="dialog"]');
                for (const c of containers) {
                    const items = c.querySelectorAll('[role="menuitem"], [role="option"], [role="menuitemradio"], div[role="button"], div[tabindex]');
                    if (items.length > 0) return true;
                }
                return false;
            }""")

            if has_menu:
                return True

            logger.debug(f"Model dropdown menu not visible on attempt {attempt}, retrying...")
            await asyncio.sleep(0.5)

        return False

    async def sync_available_models(self) -> list[str]:
        """
        每次启动/重连无头浏览器时自动执行：
        在 Notion AI 界面展开模型选择菜单，实时提取工作区支持的全部可用模型（含 Older models），
        并将提取到的模型列表持久化保存至 config.json，供 Settings 下拉列表可选。
        """
        if not self.page:
            logger.warning("Cannot sync available models: browser page not initialized.")
            return []

        logger.info("🔍 Proactively discovering available Notion AI models from page interface...")
        extracted_models = []

        try:
            await self._close_all_overlays()

            # 获取模型选择器按钮
            btn = await self._get_model_selector_button()
            if not btn:
                logger.warning("⚠️ Model selector button not found on Notion AI page.")
                return []

            # 点击打开模型下拉列表
            opened = await self._open_model_selector_dropdown(btn)
            if not opened:
                logger.warning("⚠️ Failed to open model dropdown menu.")
                await self._close_all_overlays()
                await self._focus_and_activate_chat()
                return []

            # 展开 Older models 提取更完整的可用模型
            item_sel = '.notion-overlay-container [role="menuitem"], .notion-overlay-container [role="option"], .notion-overlay-container [role="menuitemradio"], .notion-overlay-container [role="button"], [role="menu"] [role="menuitem"], [role="menu"] [role="option"], [role="menu"] [role="button"], [data-radix-popper-content-wrapper] [role="menuitem"]'
            try:
                import re
                older_btn = self.page.locator(item_sel).filter(has_text=re.compile(r"Older models|更多模型", re.I)).first
                if await older_btn.count() > 0 and await older_btn.is_visible():
                    try:
                        await older_btn.scroll_into_view_if_needed()
                        await older_btn.click(force=True, delay=100)
                    except Exception:
                        await older_btn.evaluate("el => el.click()")
                    await asyncio.sleep(1.0)
            except Exception:
                pass

            # 从所有弹出菜单容器中提取所有模型项
            raw_items = await self.page.evaluate("""() => {
                const containers = document.querySelectorAll('.notion-overlay-container, [role="menu"], [data-radix-popper-content-wrapper], [role="dialog"]');
                const results = [];
                containers.forEach(c => {
                    const items = c.querySelectorAll('[role="menuitem"], [role="option"], [role="menuitemradio"], div[role="button"]');
                    items.forEach(el => {
                        const text = el.innerText || el.textContent || '';
                        const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
                        if (lines.length > 0) {
                            results.push(lines);
                        }
                    });
                });
                return results;
            }""")

            models = []
            for lines in raw_items:
                if not lines:
                    continue
                first = lines[0]
                if first.lower() in ("older models", "更多模型"):
                    continue
                if first.lower() == "auto":
                    if "Auto" not in models:
                        models.append("Auto")
                    continue
                if len(lines) >= 2:
                    second = lines[1]
                    if len(second) > 25 or any(second.startswith(w) for w in ["Balances", "Best for", "Fastest", "Most powerful", "Hosted by", "Advanced", "Designed for"]):
                        name = first
                    else:
                        name = f"{first} {second}"
                else:
                    name = first
                if name and name not in models:
                    models.append(name)

            # 彻底收起所有菜单和遮罩层并重聚焦点到聊天窗口
            await self._close_all_overlays()
            await self._focus_and_activate_chat()

            if models:
                extracted_models = models
                from src.setup import persistence
                cfg = persistence.load_config()
                cfg.available_ai_models = extracted_models
                persistence.save_config(cfg)
                from src.config import config as cfg_proxy
                cfg_proxy.reload()
                logger.info(f"🤖 Successfully extracted and synced {len(extracted_models)} Notion AI models: {extracted_models}")
            else:
                logger.warning("⚠️ No models extracted from Notion AI popup.")

        except Exception as e:
            logger.error(f"❌ Failed to sync Notion AI models: {e}")
            await self._close_all_overlays()
            await self._focus_and_activate_chat()

        return extracted_models

    async def switch_ai_model(self, target_model_name: str) -> bool:
        """
        切换 Notion AI 当前使用的模型。
        若当前已是目标模型或目标为 'Auto'，则无需切换。
        """
        target_model = (target_model_name or "Auto").strip()
        logger.info(f"🤖 Checking/Switching Notion AI model to: '{target_model}'...")

        try:
            await self._close_all_overlays()

            btn = await self._get_model_selector_button()
            if not btn:
                logger.warning("⚠️ Model selector button not found, skipping switch.")
                await self._focus_and_activate_chat()
                return False

            current_text = (await btn.inner_text()).strip()
            if target_model.lower() == current_text.lower() or (target_model.lower() in current_text.lower() and len(target_model) > 3):
                logger.info(f"ℹ️ Current model is already '{current_text}', no switch needed.")
                await self._close_all_overlays()
                await self._focus_and_activate_chat()
                return True

            # 点击展开菜单
            opened = await self._open_model_selector_dropdown(btn)
            if not opened:
                logger.warning(f"⚠️ Failed to open model dropdown menu, keeping current '{current_text}'.")
                await self._close_all_overlays()
                await self._focus_and_activate_chat()
                return False

            # 支持多种 ARIA 角色与元素类型的选择器
            item_sel = '.notion-overlay-container [role="menuitem"], .notion-overlay-container [role="option"], .notion-overlay-container [role="menuitemradio"], .notion-overlay-container [role="button"], [role="menu"] [role="menuitem"], [role="menu"] [role="option"], [role="menu"] [role="button"], [data-radix-popper-content-wrapper] [role="menuitem"]'

            async def _find_item(name: str):
                import re
                # 1. 直接文本匹配
                it = self.page.locator(item_sel).filter(has_text=name).first
                if await it.count() > 0 and await it.is_visible():
                    return it
                # 2. 分词首尾组合匹配 (例如 "Gemini" + "Flash")
                parts = name.split()
                if len(parts) > 1:
                    it = self.page.locator(item_sel).filter(has_text=parts[0]).filter(has_text=parts[-1]).first
                    if await it.count() > 0 and await it.is_visible():
                        return it
                # 3. 正则忽略大小写
                it = self.page.locator(item_sel).filter(has_text=re.compile(re.escape(name), re.I)).first
                if await it.count() > 0 and await it.is_visible():
                    return it
                return None

            item = await _find_item(target_model)
            switched = False

            if item:
                try:
                    await item.scroll_into_view_if_needed()
                    await item.click(force=True, delay=100)
                except Exception:
                    await item.evaluate("el => el.click()")
                await asyncio.sleep(1.0)
                logger.info(f"✅ Successfully switched AI model to '{target_model}'.")
                switched = True
            else:
                # 尝试展开 Older models 再查找
                import re
                older_btn = self.page.locator(item_sel).filter(has_text=re.compile(r"Older models|更多模型", re.I)).first
                if await older_btn.count() > 0 and await older_btn.is_visible():
                    try:
                        await older_btn.scroll_into_view_if_needed()
                        await older_btn.click(force=True, delay=100)
                    except Exception:
                        await older_btn.evaluate("el => el.click()")
                    await asyncio.sleep(1.0)

                    item = await _find_item(target_model)
                    if item:
                        try:
                            await item.scroll_into_view_if_needed()
                            await item.click(force=True, delay=100)
                        except Exception:
                            await item.evaluate("el => el.click()")
                        await asyncio.sleep(1.0)
                        logger.info(f"✅ Successfully switched AI model to '{target_model}' (from Older models).")
                        switched = True
                    else:
                        logger.warning(f"⚠️ Target model '{target_model}' not found in menu, keeping current '{current_text}'.")
                else:
                    logger.warning(f"⚠️ Target model '{target_model}' not found in menu, keeping current '{current_text}'.")

            # 确保彻底收起所有菜单和遮罩层，并重新聚焦聊天输入框
            await self._close_all_overlays()
            await self._focus_and_activate_chat()
            return switched

        except Exception as e:
            logger.error(f"❌ Error switching AI model to '{target_model}': {e}")
            await self._close_all_overlays()
            await self._focus_and_activate_chat()
            return False

    async def _do_trigger_ai(self, action: str = None, restart_browser: bool = False, need_new_chat: bool = False):
        """实际在持久化的浏览器中输入 Prompt"""
        try:
            if restart_browser:
                logger.info("🔄 Restarting headless browser for stability and memory cleanup...")
                await self.close()
                await asyncio.sleep(2)
                success = await self._ensure_browser()
                if not success or not self.page:
                    return
                # 重启浏览器后天然进入初始新会话
                need_new_chat = False
            else:
                success = await self._ensure_browser()
                if not success or not self.page:
                    return

            script_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            page = self.page

            # 确认当前是否处于 Notion AI 页面
            current_url = page.url or ""
            target_url = config.notion_ai_page_url or "https://app.notion.com/ai"
            max_prompts = getattr(config, "notion_ai_max_chats_per_session", 5) or 5
            is_on_notion = ("notion.com" in current_url or "notion.so" in current_url) and not current_url.startswith("about:")

            if not is_on_notion:
                # 页面不在 Notion 域内（如初次启动或意外脱离），导航到目标页面
                logger.info(f"🌐 Not on Notion domain (current: '{current_url}'). Navigating to: {target_url}")
                await page.goto(target_url, wait_until="load")
                await asyncio.sleep(4)
                need_new_chat = False
            elif need_new_chat:
                # 处于 Notion 域内且需要开启新对话（满 5 次 prompt 或每日任务）：
                # 优先点击 UI 上的 New Chat 按钮，如未找到则通过重新导航到 target_url 作为保底
                logger.info(f"🆕 Opening a new chat (prompt limit {max_prompts} reached or new session requested)...")
                clicked = await self._click_new_chat()
                if not clicked:
                    logger.info(f"🌐 'New chat' button not clickable, navigating to {target_url} to open a fresh chat...")
                    await page.goto(target_url, wait_until="load")
                    await asyncio.sleep(4)
                else:
                    await asyncio.sleep(1.0)
            else:
                # 处于 Notion 域内且复用当前会话（第 1 ~ 5 次 Prompt）：
                # 严格复用当前会话！绝不调用 page.goto(target_url)！
                logger.info(f"💬 Continuing in current chat session on {current_url} (Prompt {self._prompts_in_current_chat}/{max_prompts})...")
                # 确保输入框可见且可用，若被遮罩阻挡则先清除
                chat_input = await self._get_chat_input()
                if not chat_input:
                    await self._close_all_overlays()
                    chat_input = await self._get_chat_input()
                if not chat_input:
                    logger.warning(f"⚠️ Chat input not visible in current session ({current_url}), recovering via {target_url}...")
                    await page.goto(target_url, wait_until="load")
                    await asyncio.sleep(4)

            # 1. 读取 prompt
            if action == "scheduled_daily_sync":
                prompt_text = (getattr(config, "prompt_daily", "") or "").strip()
                if not prompt_text:
                    schedule_file = os.path.join(script_dir, "prompt_daily.txt")
                    if os.path.exists(schedule_file):
                        with open(schedule_file, "r", encoding="utf-8") as f:
                            prompt_text = f.read().strip()
                if not prompt_text:
                    prompt_text = "Generate daily email summary."
            else:
                prompt_text = (getattr(config, "prompt_default", "") or "").strip()
                if not prompt_text:
                    prompt_file = os.path.join(script_dir, "prompt.txt")
                    if os.path.exists(prompt_file):
                        with open(prompt_file, "r", encoding="utf-8") as f:
                            prompt_text = f.read().strip()
                if not prompt_text:
                    prompt_text = "Summarize this email and suggest a reply."
                    
            # 2. 移除可能拦截点击的弹窗与遮罩层
            await self._close_all_overlays()

            # 3. 寻找并切换 AI 模型
            if action == "scheduled_daily_sync":
                target_model_name = (getattr(config, "ai_model_daily_summary", "") or "Auto").strip()
            else:
                target_model_name = (getattr(config, "ai_model_email_sync", "") or "Auto").strip()

            await self.switch_ai_model(target_model_name)

            # 4. 再次确认关闭遮罩，并精确定位与激活聊天输入框
            await self._close_all_overlays()
            chat_input = await self._focus_and_activate_chat()
            
            if not chat_input:
                logger.error("❌ Could not locate a visible Notion AI Chat input box!")
                screenshot_path = os.path.join(script_dir, "error_screenshot.png")
                await page.screenshot(path=screenshot_path)
                logger.info(f"📸 Saved error screenshot to: {screenshot_path}")
                return
                
            # 5. 输入 prompt 并发送
            logger.info("🚀 Submitting prompt to Notion AI...")
            
            # 清空可能存在的旧内容
            try:
                await page.keyboard.press("ControlOrMeta+A")
                await page.keyboard.press("Backspace")
            except Exception:
                pass
            await asyncio.sleep(0.2)

            # 模拟人类打字输入
            if len(prompt_text) > 50:
                await page.keyboard.insert_text(prompt_text)
                await asyncio.sleep(random.uniform(0.3, 0.6))
            else:
                for char in prompt_text:
                    await page.keyboard.type(char, delay=random.randint(20, 50))
                await asyncio.sleep(random.uniform(0.2, 0.4))

            # 验证输入框是否已填入内容，若未注入则调用 execCommand 强制注入
            has_text = False
            try:
                has_text = await chat_input.evaluate("el => ((el.innerText || el.textContent || '').trim().length > 0)")
            except Exception:
                pass

            if not has_text:
                logger.warning("⚠️ Text not detected in chat input via keyboard, using direct DOM insertion fallback...")
                await chat_input.evaluate("""(el, text) => {
                    el.focus();
                    document.execCommand('insertText', false, text);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""", prompt_text)
                await asyncio.sleep(0.5)
            
            # 提交 Prompt（优先点击发送按钮，其次按 Enter 键）
            submit_btn = page.locator("[data-testid='unified-chat-submit-button'], [aria-label*='Submit' i], [aria-label*='Send' i], [aria-label*='发送' i], [aria-label*='提交' i]").first
            submitted = False
            if await submit_btn.count() > 0 and await submit_btn.is_visible():
                try:
                    await submit_btn.click(force=True, delay=100)
                    submitted = True
                except Exception:
                    pass
            if not submitted:
                await page.keyboard.press("Enter", delay=100)
            
            # 6. 等待生成与工具调用完成（带心跳日志与多维度结束信号检测）
            logger.info("⏳ Waiting for Notion AI to execute tool steps and generate response...")
            start_wait = time.time()
            max_wait = getattr(config, "notion_ai_wait_timeout", 600) or 600
            completed = False

            while time.time() - start_wait < max_wait:
                await asyncio.sleep(2)
                elapsed = int(time.time() - start_wait)

                # 检查是否出现错误提示
                import re
                error_loc = page.get_by_text(re.compile(r"An error occurred|请重试|Something went wrong", re.I)).last
                if await error_loc.count() > 0 and await error_loc.is_visible():
                    logger.error(f"❌ Notion AI returned an error: {await error_loc.inner_text()}")
                    screenshot_path = os.path.join(script_dir, "error_screenshot.png")
                    await page.screenshot(path=screenshot_path)
                    logger.info(f"📸 Error screenshot saved to: {screenshot_path}")
                    completed = True
                    break

                # 检查 Stop 按钮（正在生成中）
                stop_btn = page.locator("[aria-label*='Stop' i], [aria-label*='停止' i], [data-testid='unified-chat-stop-button']").first
                is_generating = await stop_btn.count() > 0 and await stop_btn.is_visible()

                if is_generating:
                    if elapsed % 15 == 0:
                        logger.info(f"✍️ Notion AI is executing tool steps / generating response... ({elapsed}s elapsed)")
                    continue

                # 生成至少持续 6 秒后，检查是否已完成
                if elapsed >= 6:
                    btn_completion = page.locator("[aria-label*='Copy' i], [aria-label*='Undo' i], [aria-label*='复制' i], [aria-label*='撤销' i]").last
                    txt_completion = page.get_by_text(re.compile(r"Undo|全部完成|已处理完毕|已完成", re.I)).last

                    has_completion = False
                    if await btn_completion.count() > 0 and await btn_completion.is_visible():
                        has_completion = True
                    elif await txt_completion.count() > 0 and await txt_completion.is_visible():
                        has_completion = True

                    if has_completion or not is_generating:
                        logger.info(f"✅ Notion AI response generation completed in {elapsed}s.")
                        completed = True
                        break

            if not completed:
                logger.warning(f"⚠️ Reached timeout waiting for Notion AI ({max_wait}s).")

            try:
                debug_screenshot_path = os.path.join(script_dir, "debug_screenshot.png")
                await page.screenshot(path=debug_screenshot_path)
            except Exception:
                pass
                
        except Exception as ex:
            import traceback
            logger.error(f"❌ Exception encountered during Playwright execution:\n{traceback.format_exc()}")
            
    async def close(self):
        try:
            if self.browser:
                await self.browser.close()
        except Exception as e:
            logger.error(f"Error closing browser: {e}")
        finally:
            self.browser = None
            self.context = None
            self.page = None

        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            logger.error(f"Error stopping playwright: {e}")
        finally:
            self.playwright = None

