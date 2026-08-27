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
        self._ai_chats_in_session = 0
        
        # 并发控制 —— 使用 asyncio.Lock 确保 AI 触发严格串行排队
        self._lock = asyncio.Lock()
        
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
            self._ai_chats_in_session += 1
            force_new_chat = self._ai_chats_in_session > config.notion_ai_max_chats_per_session
            if force_new_chat:
                 self._ai_chats_in_session = 1
                 logger.info(f"🔄 Reached session chat limit ({config.notion_ai_max_chats_per_session}), forcing a new chat conversation.")
                 
            self._last_ai_trigger_time = time.time()
            try:
                await self._do_trigger_ai(action=action, force_new_chat=force_new_chat)
            except Exception as e:
                import traceback
                logger.error(f"❌ Failed to trigger Notion AI:\n{traceback.format_exc()}")
        finally:
            self._lock.release()

    async def debounce_loop(self):
        """后台防抖循环：从 IPC 队列消费 AI 触发信号，结合防抖和强制间隔触发 Notion AI"""
        logger.info("⏰ Notion AI debounce loop started.")

        # 启动时触发一次 AI（处理重启前积压的未处理邮件，保证不漏触发）
        asyncio.create_task(self.execute_ai_trigger("Startup Batch"))

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
                    logger.debug(f"Received {drained} AI trigger signal(s). Batch progress: {self._uploaded_in_batch}/{config.notion_ai_batch_size}")

                now = time.time()

                # 场景 1：批次阈值达到，立即触发
                if self._uploaded_in_batch >= config.notion_ai_batch_size:
                    logger.info(f"🚨 Batch threshold reached ({self._uploaded_in_batch}/{config.notion_ai_batch_size} mails). "
                               f"Force triggering Notion AI chat!")
                    self._has_pending_ai_trigger = False
                    self._uploaded_in_batch = 0
                    asyncio.create_task(self.execute_ai_trigger(
                        f"Batch Threshold ({config.notion_ai_batch_size} mails)"))

                # 场景 2：静默期到达，触发
                elif self._has_pending_ai_trigger and self._last_email_sync_time > 0:
                    quiet_elapsed = now - self._last_email_sync_time
                    if quiet_elapsed >= config.debounce_quiet_sec:
                        logger.info(f"🔔 Quiet period of {config.debounce_quiet_sec}s reached "
                                   f"with no new emails. Triggering Notion AI...")
                        self._has_pending_ai_trigger = False
                        self._uploaded_in_batch = 0
                        asyncio.create_task(self.execute_ai_trigger("Debounced Batch"))

                # 场景 3：强制时间间隔（独立于场景 1/2）
                force_elapsed = now - self._last_ai_trigger_time
                if force_elapsed >= config.debounce_force_sec:
                    logger.info(f"🔔 Force trigger interval of {config.debounce_force_sec}s reached. "
                               f"Triggering Notion AI...")
                    self._has_pending_ai_trigger = False
                    self._uploaded_in_batch = 0
                    self._last_ai_trigger_time = now
                    asyncio.create_task(self.execute_ai_trigger("Forced Interval Batch"))

                await asyncio.sleep(1)  # 每秒检查一次

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in debounce loop: {e}")
                await asyncio.sleep(5)

    async def _click_new_chat(self):
        """点击左下角的 New Chat 按钮启动独立对话"""
        try:
            if not self.page:
                return False
            await self._close_all_overlays()
            logger.info("🆕 Clicking 'New chat' button to start an isolated session...")
            new_chat_btn = self.page.locator("div[role='button']:has-text('New chat'), button:has-text('New chat'), [aria-label='New chat']").first
            if await new_chat_btn.is_visible(timeout=5000):
                await new_chat_btn.click(force=True, delay=100)
                await asyncio.sleep(random.uniform(1.5, 2.5))
                return True
            else:
                logger.warning("⚠️ 'New chat' button not found, continuing in current view.")
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

            # 2. 点击页面安全空白区域 (如左上角非按钮区)，触发失焦收起
            try:
                await self.page.mouse.click(10, 10)
                await asyncio.sleep(0.2)
            except Exception:
                pass

            # 3. 隐藏桌面客户端引导弹窗
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

            # 先聚焦聊天输入框以激活工具栏状态
            try:
                chat_input = self.page.locator("div[contenteditable='true'], [role='textbox']").locator("visible=true").last
                if await chat_input.count() > 0:
                    await chat_input.click(force=True, delay=50, timeout=3000)
                    await asyncio.sleep(0.5)
            except Exception:
                pass

            # 获取模型选择器按钮
            btn = await self._get_model_selector_button()
            if not btn:
                logger.warning("⚠️ Model selector button not found on Notion AI page.")
                return []

            # 点击打开模型下拉列表
            await btn.click(force=True, delay=100)
            await asyncio.sleep(1.5)

            # 展开 Older models 提取更完整的可用模型
            try:
                older_btn = self.page.locator('.notion-overlay-container [role="menuitem"], .notion-overlay-container [role="button"]').filter(has_text='Older models').first
                if await older_btn.count() > 0 and await older_btn.is_visible(timeout=1500):
                    await older_btn.scroll_into_view_if_needed()
                    await older_btn.click(delay=100)
                    await asyncio.sleep(1.0)
            except Exception:
                pass

            # 从弹出菜单中提取所有模型项
            raw_items = await self.page.evaluate("""() => {
                const overlay = document.querySelector('.notion-overlay-container');
                const items = overlay ? overlay.querySelectorAll('[role="menuitem"], [role="option"], [role="menuitemradio"]') : [];
                const results = [];
                items.forEach(el => {
                    const text = el.innerText || '';
                    const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
                    if (lines.length > 0) {
                        results.push(lines);
                    }
                });
                return results;
            }""")

            models = []
            for lines in raw_items:
                if not lines:
                    continue
                first = lines[0]
                if first.lower() == "older models":
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

            # 彻底收起所有菜单和遮罩层
            await self._close_all_overlays()

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

            # 聚焦输入框以激活状态
            try:
                chat_input = self.page.locator("div[contenteditable='true'], [role='textbox']").locator("visible=true").last
                if await chat_input.count() > 0:
                    await chat_input.click(force=True, delay=50, timeout=3000)
                    await asyncio.sleep(0.3)
            except Exception:
                pass

            btn = await self._get_model_selector_button()
            if not btn:
                logger.warning("⚠️ Model selector button not found, skipping switch.")
                return False

            current_text = (await btn.inner_text()).strip()
            if target_model.lower() == current_text.lower() or (target_model.lower() in current_text.lower() and len(target_model) > 3):
                logger.info(f"ℹ️ Current model is already '{current_text}', no switch needed.")
                await self._close_all_overlays()
                return True

            # 点击展开菜单
            await btn.click(force=True, delay=100)
            await asyncio.sleep(1.0)

            # 支持多种 ARIA 角色与元素类型的选择器
            item_sel = '.notion-overlay-container [role="menuitem"], .notion-overlay-container [role="option"], .notion-overlay-container [role="menuitemradio"], .notion-overlay-container [role="button"], .notion-overlay-container div[tabindex]'

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

            # 确保彻底收起所有菜单和遮罩层
            await self._close_all_overlays()
            return switched

        except Exception as e:
            logger.error(f"❌ Error switching AI model to '{target_model}': {e}")
            await self._close_all_overlays()
            return False

    async def _do_trigger_ai(self, action: str = None, force_new_chat: bool = False):
        """实际在持久化的浏览器中输入 Prompt"""
        try:
            success = await self._ensure_browser()
            if not success or not self.page:
                return
                
            script_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            page = self.page
            
            if force_new_chat:
                logger.info(f"🔄 Reached maximum chats per session ({config.notion_ai_max_chats_per_session}), restarting browser for stability...")
                await self.close()
                await asyncio.sleep(2)
                success = await self._ensure_browser()
                if not success or not self.page:
                    return
                page = self.page
                # Explicitly click 'New chat' after restarting
                await self._click_new_chat()
                    
            # 1. 读取 prompt
            if action == "scheduled_daily_sync":
                # For daily sync, we always want a fresh chat regardless of limits
                if not force_new_chat:
                    await self._click_new_chat()
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

            # 再次确认关闭所有可能残留的菜单与遮罩层
            await self._close_all_overlays()

            # 4. 寻找 AI 输入框
            chat_input = page.locator("div[contenteditable='true'], [role='textbox']").locator("visible=true").last
            
            try:
                # 等待输入框就绪
                await chat_input.wait_for(state="visible", timeout=15000)
            except Exception:
                logger.error("❌ Could not locate a visible Notion AI Chat input box!")
                screenshot_path = os.path.join(script_dir, "error_screenshot.png")
                await page.screenshot(path=screenshot_path)
                logger.info(f"📸 Saved error screenshot to: {screenshot_path}")
                return
                
            # 5. 输入 prompt 并发送
            logger.info("🚀 Submitting prompt to Notion AI...")
            try:
                await chat_input.click(force=True, delay=random.randint(50, 150))
            except Exception:
                await chat_input.focus()
            await asyncio.sleep(random.uniform(0.3, 0.8))
            
            # 模拟人类打字
            if len(prompt_text) > 50:
                await page.keyboard.insert_text(prompt_text)
                await asyncio.sleep(random.uniform(0.5, 1.5))
            else:
                for char in prompt_text:
                    await page.keyboard.type(char, delay=random.randint(30, 80))
                await asyncio.sleep(random.uniform(0.5, 1.0))
            
            submit_btn = page.locator("[aria-label*='Submit' i], [aria-label*='Send' i]").first
            if await submit_btn.is_visible():
                await submit_btn.click(force=True, delay=random.randint(50, 150))
            else:
                await page.keyboard.press("Enter", delay=random.randint(50, 150))
            
            # 4. 等待生成完成
            await asyncio.sleep(random.uniform(2.5, 4.0))
            
            stop_btn = page.locator("[aria-label*='Stop' i]").first
            is_generating = await stop_btn.is_visible()
            
            if is_generating:
                try:
                    await stop_btn.wait_for(state="hidden", timeout=config.notion_ai_wait_timeout * 1000)
                    logger.info("✅ Notion AI response generation completed.")
                except Exception as e:
                    logger.warning(f"⚠️ Issue encountered while waiting for AI response: {e}")
            else:
                # 如果没有 Stop 按钮，检查是否报错
                error_msg = page.locator("text=/An error occurred|请重试/i").last
                if await error_msg.is_visible():
                    logger.error("❌ Notion AI returned an error indicator (An error occurred)!")
                    screenshot_path = os.path.join(script_dir, "error_screenshot.png")
                    await page.screenshot(path=screenshot_path)
                    logger.info(f"📸 Error screenshot saved to: {screenshot_path}")
                else:
                    wait_sec = config.notion_ai_fallback_wait_sec
                    await asyncio.sleep(wait_sec)
                    logger.info("✅ Notion AI generation completed.")
                
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

