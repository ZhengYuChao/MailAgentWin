import os
import asyncio
import time
from loguru import logger
from src.config import config

class AIController:
    """
    Notion AI 控制器。
    集中管理 Playwright 无头浏览器的并发、会话防抖以及生命周期。
    """
    def __init__(self):
        # 防抖状态变量
        self._last_email_sync_time = 0.0          # 最后一次成功同步邮件的时间
        self._last_ai_trigger_time = time.time()  # 最后一次触发 AI 的时间
        self._has_pending_ai_trigger = False      # 是否有待触发的 AI 任务
        
        # 批处理与会话状态
        self._uploaded_in_batch = 0
        self._ai_chats_in_session = 0
        
        # 并发控制
        self._is_running = False
        
        # Playwright 持续化实例
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def _ensure_browser(self):
        if not self.playwright:
            from playwright.async_api import async_playwright
            logger.info("🌐 Initializing persistent Playwright browser...")
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
            auth_state_path = os.path.join(script_dir, "notion_auth.json")
            user_agent_path = os.path.join(script_dir, "user_agent.txt")
            
            if not os.path.exists(auth_state_path):
                logger.error(f"❌ Auth state file does not exist: {auth_state_path}. Please run python notion_auth.py to login first!")
                return False
                
            context_args = {"storage_state": auth_state_path}
            if os.path.exists(user_agent_path):
                with open(user_agent_path, "r", encoding="utf-8") as f:
                    context_args["user_agent"] = f.read().strip()
                    
            self.context = await self.browser.new_context(**context_args)
            await self.context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            self.context.set_default_timeout(60000)
            self.page = await self.context.new_page()
            
            page_url = config.notion_ai_page_url
            if not page_url:
                logger.error("❌ NOTION_AI_PAGE_URL is not specified in configuration!")
                return False
                
            logger.info(f"🌐 Accessing Notion page via headless browser: {page_url}")
            await self.page.goto(page_url, wait_until="load")
            logger.info("✅ Initial page loaded, waiting 5 seconds to ensure routing and AI panel are fully initialized...")
            await asyncio.sleep(5)
            return True
            
        if self.page and self.page.is_closed():
            logger.warning("⚠️ Browser page was closed, re-initializing page...")
            try:
                self.page = await self.context.new_page()
                await self.page.goto(config.notion_ai_page_url, wait_until="load")
                await asyncio.sleep(5)
                return True
            except Exception as e:
                logger.warning(f"⚠️ Failed to re-initialize page ({e}), forcing full browser restart...")
                await self.close()
                return await self._ensure_browser()
            
        return True

    def schedule_ai_trigger(self):
        """当一封新邮件同步成功后调用，调度 AI 触发"""
        self._uploaded_in_batch += 1
        logger.info(f"📊 Notion AI Batch Progress: {self._uploaded_in_batch}/{config.notion_ai_batch_size}")
        
        if self._uploaded_in_batch >= config.notion_ai_batch_size:
            logger.info(f"🚨 Batch threshold reached ({config.notion_ai_batch_size}/{config.notion_ai_batch_size} mails). Force triggering Notion AI chat!")
            self._has_pending_ai_trigger = False
            self._uploaded_in_batch = 0 # 重置批次计数
            # 异步触发，不阻塞当前流程
            asyncio.create_task(self.execute_ai_trigger(f"Batch Threshold ({config.notion_ai_batch_size} mails)"))
        else:
            self._last_email_sync_time = time.time()
            self._has_pending_ai_trigger = True
            logger.info(f"⏳ Sync completed. Notion AI trigger scheduled (waiting {config.debounce_quiet_sec}s quiet period)...")

    async def execute_ai_trigger(self, subject: str, action: str = None):
        """处理会话上限逻辑，并调用底层的无头浏览器"""
        if self._is_running:
            logger.warning("⚠️ Notion AI is already running, waiting for the current task to finish before executing this trigger...")
            wait_start = time.time()
            while self._is_running:
                if time.time() - wait_start > config.notion_ai_wait_timeout:
                    logger.error(f"❌ Timeout waiting for Notion AI task to finish ({config.notion_ai_wait_timeout}s), forcing state reset and continuing...")
                    break
                await asyncio.sleep(2)

        self._is_running = True
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
            self._is_running = False

    async def debounce_loop(self):
        """后台防抖循环：监听静默期和强制时间间隔，触发 Notion AI"""
        logger.info("⏰ Notion AI debounce loop started.")
        while True:
            try:
                await asyncio.sleep(1)  # 每秒检查一次
                now = time.time()
                
                # 场景 1：如果存在待触发的 AI 任务，且距离最后一次同步已过去 config.debounce_quiet_sec 秒
                if self._has_pending_ai_trigger and self._last_email_sync_time > 0:
                    quiet_elapsed = now - self._last_email_sync_time
                    if quiet_elapsed >= config.debounce_quiet_sec:
                        logger.info(f"🔔 Quiet period of {config.debounce_quiet_sec}s reached with no new emails. Triggering Notion AI...")
                        self._has_pending_ai_trigger = False
                        self._uploaded_in_batch = 0 # 防抖触发后清空批次
                        asyncio.create_task(self.execute_ai_trigger("Debounced Batch"))
                
                # 场景 2：强制时间间隔。如果距离上一次触发 AI 已过去 config.debounce_force_sec 秒
                force_elapsed = now - self._last_ai_trigger_time
                if force_elapsed >= config.debounce_force_sec:
                    if self._has_pending_ai_trigger:
                        logger.info(f"🔔 Force trigger interval of {config.debounce_force_sec}s reached. Triggering Notion AI...")
                        self._has_pending_ai_trigger = False
                        self._uploaded_in_batch = 0 # 触发后清空批次
                        asyncio.create_task(self.execute_ai_trigger("Forced Interval Batch"))
                    # 重置时间，防止无 pending 时也不断判定为 force
                    self._last_ai_trigger_time = now
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in debounce loop: {e}")
                await asyncio.sleep(5)

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
                    
            # 1. 读取 prompt
            prompt_text = "Summarize this email and suggest a reply."
            prompt_file = os.path.join(script_dir, "prompt.txt")
            
            if action == "scheduled_daily_sync":
                schedule_file = os.path.join(script_dir, "prompt_schedule.txt")
                if os.path.exists(schedule_file):
                    prompt_file = schedule_file
                
            if os.path.exists(prompt_file):
                with open(prompt_file, "r", encoding="utf-8") as f:
                    prompt_text = f.read().strip()
                    
            # 尝试寻找并切换 AI 模型
            target_model_name = config.ai_model.strip()
            if target_model_name.lower() == "auto":
                logger.info("ℹ️ AI_MODEL configured as Auto, skipping model switch.")
            else:
                try:
                    logger.info(f"🔍 Attempting to switch AI model to target: {target_model_name}")
                    
                    # Notion AI 的模型选择器位于聊天输入框底部工具栏，
                    # 是一个可点击的小元素，文本内容为当前模型名（如 "Auto"、"Sonnet 5" 等）。
                    # 使用多种选择器策略来定位：
                    known_models = ["Auto", "Claude", "GPT", "Sonnet", "Gemini", "o3", "o4-mini", target_model_name]
                    # 去重并构建选择器
                    seen = set()
                    selector_parts = []
                    for m in known_models:
                        if m not in seen:
                            seen.add(m)
                            # 使用 :has-text() 进行宽松的文本匹配，匹配任何包含模型关键词的可点击元素
                            selector_parts.append(f"[role='button']:has-text('{m}')")
                            selector_parts.append(f"button:has-text('{m}')")
                            selector_parts.append(f"div[class*='model']:has-text('{m}')")
                            selector_parts.append(f"span:has-text('{m}')")
                    
                    full_selector = ", ".join(selector_parts)
                    trigger = page.locator(full_selector).first
                    
                    found = False
                    try:
                        found = await trigger.is_visible(timeout=5000)
                    except Exception:
                        pass
                    
                    if found:
                        current_text = await trigger.inner_text()
                        logger.info(f"ℹ️ Found model selector, currently showing: '{current_text.strip()}'")
                        
                        # 如果当前已经是目标模型，则跳过
                        if target_model_name.lower() in current_text.strip().lower():
                            logger.info(f"✅ Current model is already '{target_model_name}', no switch needed.")
                        else:
                            # 点击展开模型下拉菜单
                            await trigger.click()
                            await asyncio.sleep(1.5)
                            
                            # 在展开的下拉菜单中寻找目标模型
                            # 使用多种方式匹配菜单项
                            menu_item = page.locator(f"[role='option']:has-text('{target_model_name}'), [role='menuitem']:has-text('{target_model_name}'), [role='menuitemradio']:has-text('{target_model_name}'), div[class*='option']:has-text('{target_model_name}')").first
                            
                            menu_found = False
                            try:
                                menu_found = await menu_item.is_visible(timeout=3000)
                            except Exception:
                                pass
                            
                            if not menu_found:
                                # 回退：用更宽泛的文本匹配
                                menu_item = page.get_by_text(target_model_name, exact=False).first
                                try:
                                    menu_found = await menu_item.is_visible(timeout=2000)
                                except Exception:
                                    pass
                            
                            if menu_found:
                                logger.info(f"✅ Found target model '{target_model_name}' in dropdown menu, selecting...")
                                await menu_item.click()
                                await asyncio.sleep(0.5)
                            else:
                                logger.warning(f"⚠️ Target model '{target_model_name}' not found in dropdown menu, keeping current model.")
                                # 截图辅助调试
                                model_debug_path = os.path.join(script_dir, "model_switch_debug.png")
                                await page.screenshot(path=model_debug_path)
                                logger.info(f"📸 Model switch debug screenshot saved to: {model_debug_path}")
                            
                            # 按 Escape 确保菜单收起
                            await page.keyboard.press("Escape")
                            await asyncio.sleep(0.5)
                    else:
                        logger.info("ℹ️ Model selector element not found, skipping model switch.")
                        # 截图辅助调试
                        model_debug_path = os.path.join(script_dir, "model_switch_debug.png")
                        await page.screenshot(path=model_debug_path)
                        logger.info(f"📸 Model selector debug screenshot saved to: {model_debug_path}")
                except Exception as e:
                    logger.warning(f"⚠️ Exception occurred while trying to switch AI model (ignorable): {e}")

            # 2. 寻找 AI 输入框
            logger.info("🎯 Locating AI Chat input box...")
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
                
            # 3. 输入 prompt 并发送
            logger.info("✍️ Typing prompt (simulating keyboard input)...")
            await chat_input.click()
            await asyncio.sleep(0.5)
            
            await page.keyboard.insert_text(prompt_text)
            await asyncio.sleep(1.5)
            
            logger.info("🚀 Submitting to Notion AI...")
            submit_btn = page.locator("[aria-label*='Submit' i], [aria-label*='Send' i]").first
            if await submit_btn.is_visible():
                await submit_btn.click()
            else:
                await page.keyboard.press("Enter")
            
            # 4. 等待生成完成
            logger.info("⏳ Waiting for Notion AI response generation to complete...")
            await asyncio.sleep(3)
            
            stop_btn = page.locator("[aria-label*='Stop' i]").first
            is_generating = await stop_btn.is_visible()
            
            if is_generating:
                logger.info("✍️ AI is writing (Stop button detected)...")
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
                    logger.info(f"ℹ️ No clear 'Stop' button detected, using {wait_sec} seconds fallback wait...")
                    await asyncio.sleep(wait_sec)
                    logger.info("✅ Fallback wait finished.")
                
            try:
                debug_screenshot_path = os.path.join(script_dir, "debug_screenshot.png")
                await page.screenshot(path=debug_screenshot_path)
                logger.info(f"📸 Final screenshot saved to: {debug_screenshot_path}")
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

# 暴露一个全局实例
global_ai_controller = AIController()
