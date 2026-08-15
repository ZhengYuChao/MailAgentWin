import os
import asyncio
import time
import random
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
                auth_state_path = os.path.join(script_dir, "notion_auth.json")
                user_agent_path = os.path.join(script_dir, "user_agent.txt")

                if not os.path.exists(auth_state_path):
                    logger.error(f"❌ Auth state file does not exist: {auth_state_path}. Please run python notion_auth.py to login first!")
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
                logger.info("✅ Initial page loaded, waiting 10 seconds to ensure routing and AI panel are fully initialized...")
                await asyncio.sleep(10)
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
                    logger.info(f"📊 Received {drained} AI trigger signal(s). "
                               f"Batch progress: {self._uploaded_in_batch}/{config.notion_ai_batch_size}")

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
            logger.info("🆕 Clicking 'New chat' button to start an isolated session...")
            new_chat_btn = self.page.locator("div[role='button']:has-text('New chat'), button:has-text('New chat'), [aria-label='New chat']").first
            if await new_chat_btn.is_visible(timeout=5000):
                await new_chat_btn.click(delay=100)
                import asyncio, random
                await asyncio.sleep(random.uniform(1.5, 2.5))
                return True
            else:
                logger.warning("⚠️ 'New chat' button not found, continuing in current view.")
                return False
        except Exception as e:
            logger.warning(f"⚠️ Failed to click 'New chat': {e}")
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
                    
            # 1. 读取 prompt
            prompt_text = "Summarize this email and suggest a reply."
            prompt_file = os.path.join(script_dir, "prompt.txt")
            
            if action == "scheduled_daily_sync":
                await self._click_new_chat()
                schedule_file = os.path.join(script_dir, "prompt_schedule.txt")
                if os.path.exists(schedule_file):
                    prompt_file = schedule_file
                
            if os.path.exists(prompt_file):
                with open(prompt_file, "r", encoding="utf-8") as f:
                    prompt_text = f.read().strip()
                    
            # 移除可能拦截点击的弹窗 (如 "Open in Notion's desktop app?")
            # 注意：只移除包含 "desktop app" 等提示文字的弹窗，
            # 不能移除所有 overlay-container，因为模型选择下拉菜单也使用 overlay 渲染
            try:
                removed = await page.evaluate('''() => {
                    let count = 0;
                    document.querySelectorAll('.notion-overlay-container').forEach(el => {
                        const text = el.innerText || '';
                        if (text.includes('desktop app') || text.includes('Open in')) {
                            el.remove();
                            count++;
                        }
                    });
                    return count;
                }''')
                if removed > 0:
                    logger.info(f"ℹ️ Cleaned up {removed} Notion overlay container(s) with desktop app prompt.")
                else:
                    logger.info("ℹ️ No blocking Notion overlay containers found.")
            except Exception as e:
                logger.warning(f"⚠️ Failed to clean up overlay containers: {e}")

            # 尝试寻找并切换 AI 模型
            if action == "scheduled_daily_sync":
                target_model_name = config.ai_model_daily_summary.strip()
            else:
                target_model_name = config.ai_model_email_sync.strip()
                
            if target_model_name.lower() == "auto":
                logger.info("ℹ️ Target AI_MODEL configured as Auto, skipping model switch.")
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
                            # 使用 :has-text() 替代 :text-is() 以支持部分匹配 (如 "Claude 3.5 Sonnet")
                            # 为了避免匹配到大块文本容器，我们严格限制在 button 和具有 popup 属性等交互元素上
                            selector_parts.append(f"[role='button']:has-text('{m}')")
                            selector_parts.append(f"button:has-text('{m}')")
                            selector_parts.append(f"[aria-haspopup]:has-text('{m}')")
                            selector_parts.append(f"div[class*='model']:has-text('{m}')")
                    
                    full_selector = ", ".join(selector_parts)
                    all_triggers = page.locator(full_selector)
                    
                    # 在所有匹配中找到最小（最精确）的可见元素作为触发器
                    trigger = None
                    found = False
                    best_area = float('inf')
                    
                    try:
                        count = await all_triggers.count()
                        logger.info(f"ℹ️ Found {count} potential model selector candidates")
                        for i in range(count):
                            candidate = all_triggers.nth(i)
                            try:
                                if not await candidate.is_visible(timeout=500):
                                    continue
                                box = await candidate.bounding_box()
                                if not box:
                                    continue
                                area = box['width'] * box['height']
                                text = (await candidate.inner_text()).strip()
                                tag = await candidate.evaluate("el => el.tagName.toLowerCase()")
                                cls = await candidate.evaluate("el => (el.className || '').toString().slice(0, 60)")
                                logger.info(f"  Candidate[{i}]: tag={tag}, class={cls}, size={box['width']:.0f}x{box['height']:.0f}, text='{text[:30]}'")
                                # 选最小的可见元素（模型选择器按钮通常很小，< 200x50 像素）
                                if area < best_area and box['width'] < 300 and box['height'] < 80:
                                    best_area = area
                                    trigger = candidate
                                    found = True
                            except Exception:
                                continue
                    except Exception:
                        # 回退：使用 .first
                        trigger = all_triggers.first
                        try:
                            found = await trigger.is_visible(timeout=5000)
                        except Exception:
                            pass
                    
                    if found and trigger:
                        current_text = await trigger.inner_text()
                        logger.info(f"ℹ️ Found model selector, currently showing: '{current_text.strip()}'")
                        
                        # 如果当前已经是目标模型，则跳过
                        if target_model_name.lower() in current_text.strip().lower():
                            logger.info(f"✅ Current model is already '{target_model_name}', no switch needed.")
                        else:
                            # 点击展开模型下拉菜单 - 尝试多种点击方式
                            dropdown_opened = False
                            
                            for click_attempt, click_method in enumerate(["click", "force_click", "js_click"], 1):
                                logger.info(f"🖱️ Clicking model selector (attempt {click_attempt}: {click_method})...")
                                
                                if click_method == "click":
                                    await trigger.click(delay=random.randint(50, 150))
                                elif click_method == "force_click":
                                    await trigger.click(force=True, delay=random.randint(50, 150))
                                else:  # js_click
                                    await trigger.evaluate("el => el.click()")
                                
                                await asyncio.sleep(random.uniform(1.5, 2.5))
                                
                                # 验证下拉菜单是否真的打开了
                                # Notion 的下拉菜单会出现在 .notion-overlay-container 中
                                overlay_count = await page.evaluate('''() => {
                                    return document.querySelectorAll('.notion-overlay-container').length;
                                }''')
                                
                                # 也检查是否有任何新的浮动面板
                                has_popup = await page.evaluate('''() => {
                                    const overlays = document.querySelectorAll('.notion-overlay-container');
                                    for (const o of overlays) {
                                        // 检查是否包含模型名文本（说明是模型选择下拉）
                                        if (o.innerText && (o.innerText.includes('GPT') || o.innerText.includes('Claude') || o.innerText.includes('Sonnet') || o.innerText.includes('Gemini') || o.innerText.includes('Opus') || o.innerText.includes('Auto'))) {
                                            return true;
                                        }
                                    }
                                    return false;
                                }''')
                                
                                logger.info(f"  overlay_count={overlay_count}, has_model_popup={has_popup}")
                                
                                if has_popup:
                                    dropdown_opened = True
                                    break
                                    
                                # 如果没有打开，按 Escape 清理可能的半开状态，再重试
                                if click_attempt < 3:
                                    await page.keyboard.press("Escape")
                                    await asyncio.sleep(0.5)
                            
                            # 截图记录下拉菜单打开后的状态（调试用）
                            model_debug_path = os.path.join(script_dir, "model_switch_debug.png")
                            await page.screenshot(path=model_debug_path)
                            logger.info(f"📸 Dropdown {'opened' if dropdown_opened else 'FAILED to open'} screenshot saved to: {model_debug_path}")
                            
                            # ---- 辅助函数：在页面中查找目标模型菜单项 ----
                            async def _find_model_item(model_name: str) -> tuple:
                                """尝试多种策略查找目标模型菜单项，返回 (locator, found_bool)"""
                                
                                # 策略0：直接在 .notion-overlay-container 内搜索（Notion 的下拉菜单渲染在这里）
                                try:
                                    overlay = page.locator(".notion-overlay-container")
                                    overlay_count = await overlay.count()
                                    logger.info(f"  ℹ Found {overlay_count} notion-overlay-container(s)")
                                    for oi in range(overlay_count):
                                        ov = overlay.nth(oi)
                                        try:
                                            if not await ov.is_visible(timeout=300):
                                                continue
                                            item = ov.get_by_text(model_name, exact=False).first
                                            if await item.is_visible(timeout=500):
                                                logger.info(f"  ✓ Found via .notion-overlay-container[{oi}]")
                                                return item, True
                                        except Exception:
                                            continue
                                except Exception:
                                    pass
                                
                                # 策略1：标准 ARIA 角色选择器
                                role_selector = ", ".join([
                                    f"[role='option']:has-text('{model_name}')",
                                    f"[role='menuitem']:has-text('{model_name}')",
                                    f"[role='menuitemradio']:has-text('{model_name}')",
                                    f"[role='listbox'] >> text='{model_name}'",
                                ])
                                item = page.locator(role_selector).first
                                try:
                                    if await item.is_visible(timeout=2000):
                                        logger.info(f"  ✓ Found via ARIA role selector")
                                        return item, True
                                except Exception:
                                    pass
                                
                                # 策略2：在弹出层/覆盖层容器内搜索（通用选择器）
                                popup_selectors = [
                                    "div[class*='overlay']",
                                    "div[class*='popup']",
                                    "div[class*='dropdown']",
                                    "div[class*='popover']",
                                    "div[class*='menu']",
                                    "div[style*='position: fixed']",
                                    "div[style*='position: absolute']",
                                ]
                                for ps in popup_selectors:
                                    try:
                                        containers = page.locator(ps)
                                        cnt = await containers.count()
                                        for ci in range(cnt):
                                            container = containers.nth(ci)
                                            if await container.is_visible(timeout=300):
                                                item = container.get_by_text(model_name, exact=False).first
                                                if await item.is_visible(timeout=300):
                                                    logger.info(f"  ✓ Found via popup container ({ps}[{ci}])")
                                                    return item, True
                                    except Exception:
                                        continue
                                
                                # 策略3：用 get_by_role 配合 name 匹配
                                for role in ["option", "menuitem", "menuitemradio"]:
                                    try:
                                        item = page.get_by_role(role, name=model_name)
                                        if await item.first.is_visible(timeout=500):
                                            logger.info(f"  ✓ Found via get_by_role('{role}')")
                                            return item.first, True
                                    except Exception:
                                        continue
                                
                                # 策略4：宽泛文本匹配，遍历所有可见匹配项
                                try:
                                    all_matches = page.get_by_text(model_name, exact=False)
                                    count = await all_matches.count()
                                    logger.info(f"  ℹ get_by_text found {count} matches for '{model_name}'")
                                    for i in range(count):
                                        candidate = all_matches.nth(i)
                                        try:
                                            if not await candidate.is_visible(timeout=300):
                                                continue
                                            tag = await candidate.evaluate("el => el.tagName.toLowerCase()")
                                            role_attr = await candidate.evaluate("el => el.getAttribute('role') || ''")
                                            class_attr = await candidate.evaluate("el => el.className || ''")
                                            text = (await candidate.inner_text()).strip()
                                            logger.info(f"  ℹ Match[{i}]: tag={tag}, role={role_attr}, class={class_attr[:60]}, text='{text[:50]}'")
                                            
                                            # 跳过明显是大容器的元素
                                            if len(text) > 80:
                                                continue
                                            if model_name.lower() in text.lower():
                                                logger.info(f"  ✓ Found via broad text match at index {i}")
                                                return candidate, True
                                        except Exception:
                                            continue
                                except Exception as e:
                                    logger.info(f"  ✗ Broad text search failed: {e}")
                                
                                # 策略5：通过 JS 直接在 overlay 内按文本查找并点击
                                # 这是最后的兜底手段
                                try:
                                    clicked = await page.evaluate(f'''(targetText) => {{
                                        const overlays = document.querySelectorAll('.notion-overlay-container');
                                        for (const overlay of overlays) {{
                                            const walker = document.createTreeWalker(overlay, NodeFilter.SHOW_TEXT);
                                            while (walker.nextNode()) {{
                                                const node = walker.currentNode;
                                                if (node.textContent && node.textContent.trim().toLowerCase().includes(targetText.toLowerCase())) {{
                                                    // 找到包含目标文本的最近的可点击元素
                                                    let el = node.parentElement;
                                                    while (el && el !== overlay) {{
                                                        const style = window.getComputedStyle(el);
                                                        if (style.cursor === 'pointer' || el.onclick || el.getAttribute('role')) {{
                                                            el.click();
                                                            return 'clicked: ' + el.tagName + ' / ' + (el.textContent || '').trim().slice(0, 50);
                                                        }}
                                                        el = el.parentElement;
                                                    }}
                                                    // 如果没找到可点击的父元素，直接点击文本节点的父元素
                                                    if (node.parentElement) {{
                                                        node.parentElement.click();
                                                        return 'clicked-parent: ' + node.parentElement.tagName + ' / ' + node.textContent.trim().slice(0, 50);
                                                    }}
                                                }}
                                            }}
                                        }}
                                        return null;
                                    }}''', target_model_name)
                                    if clicked:
                                        logger.info(f"  ✓ Found and clicked via JS DOM traversal: {clicked}")
                                        return None, True  # 已经通过 JS 点击了，不需要再 click
                                except Exception as e:
                                    logger.info(f"  ✗ JS DOM traversal failed: {e}")
                                
                                return None, False
                            
                            # ---- 第一轮查找 ----
                            logger.info(f"🔍 [Round 1] Searching for '{target_model_name}' in dropdown...")
                            menu_item, menu_found = await _find_model_item(target_model_name)
                            
                            # ---- 第二轮：尝试滚动下拉列表 + Older models ----
                            if not menu_found:
                                logger.info("ℹ️ Model not found in primary list, attempting to scroll dropdown and look for 'Older models'...")
                                
                                # 尝试用 ArrowDown 在菜单内滚动（比 PageDown 更安全，不会关闭菜单）
                                for _ in range(8):
                                    await page.keyboard.press("ArrowDown")
                                    await asyncio.sleep(0.15)
                                
                                await asyncio.sleep(0.5)
                                
                                # 检查是否有 "Older models" 按钮
                                older_models_btn = page.get_by_text("Older models", exact=False).first
                                try:
                                    if await older_models_btn.is_visible(timeout=1500):
                                        logger.info("ℹ️ Clicking 'Older models' to reveal more options...")
                                        await older_models_btn.click(delay=random.randint(50, 150))
                                        await asyncio.sleep(random.uniform(1.5, 2.5))
                                except Exception:
                                    pass
                                
                                logger.info(f"🔍 [Round 2] Searching again for '{target_model_name}'...")
                                menu_item, menu_found = await _find_model_item(target_model_name)
                            
                            # ---- 第三轮：尝试用模型名的部分文本匹配 ----
                            if not menu_found:
                                # 有时下拉里显示的模型名可能和配置中的不完全一致
                                # 例如 "GPT-5.6 Sol" 可能显示为 "GPT‑5.6 Sol"（连字符 vs 减号）
                                # 尝试只用关键部分匹配
                                parts = target_model_name.split()
                                if len(parts) > 1:
                                    # 用最后一个有意义的词作为关键词（如 "Sol"）
                                    for keyword in reversed(parts):
                                        if len(keyword) >= 2 and keyword.lower() not in ("the", "and", "for"):
                                            logger.info(f"🔍 [Round 3] Trying partial match with keyword '{keyword}'...")
                                            menu_item, menu_found = await _find_model_item(keyword)
                                            if menu_found:
                                                if menu_item is None:
                                                    # JS 已经点击了，直接退出
                                                    break
                                                # 二次确认：验证完整模型名
                                                try:
                                                    item_text = (await menu_item.inner_text()).strip()
                                                    # 使用更宽松的比较（忽略特殊连字符/空格差异）
                                                    import unicodedata
                                                    normalized_target = unicodedata.normalize("NFKC", target_model_name).lower()
                                                    normalized_item = unicodedata.normalize("NFKC", item_text).lower()
                                                    if normalized_target not in normalized_item:
                                                        logger.info(f"  ✗ Partial match '{item_text}' doesn't contain full target, skipping")
                                                        menu_found = False
                                                        continue
                                                except Exception:
                                                    pass
                                            if menu_found:
                                                break
                            
                            if menu_found and menu_item:
                                # 通过 Playwright locator 点击
                                logger.info(f"✅ Found target model '{target_model_name}' in dropdown menu, selecting...")
                                await menu_item.click(delay=random.randint(50, 150))
                                await asyncio.sleep(random.uniform(0.5, 1.0))
                            elif menu_found and menu_item is None:
                                # 已经通过 JS 直接点击了
                                logger.info(f"✅ Target model '{target_model_name}' was selected via JS click.")
                                await asyncio.sleep(random.uniform(0.5, 1.0))
                            else:
                                logger.warning(f"⚠️ Target model '{target_model_name}' not found in dropdown menu, keeping current model.")
                                # 截图辅助调试（此时菜单仍然打开）
                                await page.screenshot(path=model_debug_path)
                                logger.info(f"📸 Model switch FAILED debug screenshot saved to: {model_debug_path}")
                            
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
            await chat_input.click(delay=random.randint(50, 150))
            await asyncio.sleep(random.uniform(0.3, 0.8))
            
            # 模拟人类打字
            if len(prompt_text) > 50:
                await page.keyboard.insert_text(prompt_text)
                await asyncio.sleep(random.uniform(0.5, 1.5))
            else:
                for char in prompt_text:
                    await page.keyboard.type(char, delay=random.randint(30, 80))
                await asyncio.sleep(random.uniform(0.5, 1.0))
            
            logger.info("🚀 Submitting to Notion AI...")
            submit_btn = page.locator("[aria-label*='Submit' i], [aria-label*='Send' i]").first
            if await submit_btn.is_visible():
                await submit_btn.click(delay=random.randint(50, 150))
            else:
                await page.keyboard.press("Enter", delay=random.randint(50, 150))
            
            # 4. 等待生成完成
            logger.info("⏳ Waiting for Notion AI response generation to complete...")
            await asyncio.sleep(random.uniform(2.5, 4.0))
            
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

