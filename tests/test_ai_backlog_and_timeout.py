import asyncio
import unittest
from unittest.mock import AsyncMock
from multiprocessing import Queue as MPQueue
from src.ai.controller import AIController


class TestAIBacklogAndTimeout(unittest.IsolatedAsyncioTestCase):

    async def test_ai_controller_backlog_batch_splitting(self):
        """验证当积压数十封邮件时，AIController 正确将其拆分为每 2 封一组的任务"""
        ai_queue = MPQueue()
        # 模拟积压 10 封邮件信号
        for i in range(10):
            ai_queue.put({"type": "email_synced", "ts": 1000.0 + i})

        controller = AIController(ai_trigger_queue=ai_queue)

        # 启动 debounce 循环短时间运行
        debounce_task = asyncio.create_task(controller.debounce_loop())
        await asyncio.sleep(0.15)  # 让其执行排空与批次分片
        debounce_task.cancel()
        try:
            await debounce_task
        except asyncio.CancelledError:
            pass

        # 检查队列中的任务数：1 个 Startup Batch + 5 个分批任务 (10 封 / 每批 2 封)
        tasks = []
        while not controller._ai_task_queue.empty():
            tasks.append(controller._ai_task_queue.get_nowait())

        subjects = [t["subject"] for t in tasks]
        self.assertIn("Startup Batch", subjects)
        batch_tasks = [s for s in subjects if "Batch (" in s]
        self.assertEqual(len(batch_tasks), 5, f"Expected 5 batch tasks for 10 emails, got: {batch_tasks}")
        self.assertEqual(controller._uploaded_in_batch, 0)
        self.assertFalse(controller._has_pending_ai_trigger)

    async def test_ai_controller_odd_backlog_and_quiet_period(self):
        """验证当积压 5 封邮件时：4 封分为 2 批立即入队，剩余 1 封等待静默期"""
        ai_queue = MPQueue()
        for i in range(5):
            ai_queue.put({"type": "email_synced", "ts": 100.0})

        controller = AIController(ai_trigger_queue=ai_queue)

        debounce_task = asyncio.create_task(controller.debounce_loop())
        await asyncio.sleep(0.15)

        # 此时应已生成 Startup Batch + 2 批 (4 封)，剩余 1 封等待静默期
        self.assertEqual(controller._uploaded_in_batch, 1)
        self.assertTrue(controller._has_pending_ai_trigger)

        debounce_task.cancel()
        try:
            await debounce_task
        except asyncio.CancelledError:
            pass

    async def test_ai_controller_task_worker_loop(self):
        """验证 task_worker_loop 能够顺序消费 _ai_task_queue 中的任务"""
        controller = AIController()
        executed = []
        controller.execute_ai_trigger = AsyncMock(side_effect=lambda subj, action=None: executed.append(subj))

        await controller.queue_ai_trigger("Task 1")
        await controller.queue_ai_trigger("Task 2")

        worker_task = asyncio.create_task(controller.task_worker_loop())
        await asyncio.sleep(0.15)
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

        self.assertEqual(executed, ["Task 1", "Task 2"])
        self.assertTrue(controller._ai_task_queue.empty())

    async def test_ai_controller_periodic_timeout(self):
        """验证无邮件到达时，超过 debounce_force_sec 会自动触发一次 Notion AI 检查"""
        import time
        controller = AIController()
        # 模拟 700 秒前执行过最后一次 AI（已超过 600 秒的 force_sec）
        controller._last_ai_trigger_time = time.time() - 700

        debounce_task = asyncio.create_task(controller.debounce_loop())
        await asyncio.sleep(0.15)
        debounce_task.cancel()
        try:
            await debounce_task
        except asyncio.CancelledError:
            pass

        tasks = []
        while not controller._ai_task_queue.empty():
            tasks.append(controller._ai_task_queue.get_nowait())

        subjects = [t["subject"] for t in tasks]
        periodic_tasks = [s for s in subjects if "Forced Periodic Check" in s]
        self.assertEqual(len(periodic_tasks), 1, f"Expected 1 forced periodic check task, got: {subjects}")

    async def test_prompts_per_chat_session_counting_and_new_chat(self):
        """验证同一会话内最多提交 5 次 prompt，第 6 次触发 New chat 并重置计数"""
        controller = AIController()
        history = []

        async def fake_do_trigger(action=None, restart_browser=False, need_new_chat=False, **kwargs):
            history.append({
                "need_new_chat": need_new_chat,
                "prompts_count": controller._prompts_in_current_chat,
                "new_chats_count": controller._new_chats_count,
            })

        controller._do_trigger_ai = fake_do_trigger

        # 连续触发 6 次邮件同步 prompt
        for i in range(1, 7):
            await controller.execute_ai_trigger(f"Mail batch {i}")

        # 前 5 次应该在同一 chat 会话内，need_new_chat 为 False
        for i in range(5):
            self.assertFalse(history[i]["need_new_chat"], f"Step {i+1} should reuse chat")
            self.assertEqual(history[i]["prompts_count"], i + 1)

        # 第 6 次应该开启 New Chat，need_new_chat 为 True，计数重置为 1
        self.assertTrue(history[5]["need_new_chat"], "Step 6 should trigger new chat")
        self.assertEqual(history[5]["prompts_count"], 1)
        self.assertEqual(history[5]["new_chats_count"], 1)

    async def test_daily_sync_isolation_and_return_to_normal(self):
        """验证每日总结会话隔离，且后续邮件同步不会混入每日总结对话"""
        controller = AIController()
        history = []

        async def fake_do_trigger(action=None, restart_browser=False, need_new_chat=False, **kwargs):
            history.append({
                "action": action,
                "need_new_chat": need_new_chat,
                "prompts_count": controller._prompts_in_current_chat,
            })

        controller._do_trigger_ai = fake_do_trigger

        # 1. 邮件同步 prompt 1
        await controller.execute_ai_trigger("Mail 1")
        self.assertFalse(history[0]["need_new_chat"])
        self.assertEqual(history[0]["prompts_count"], 1)

        # 2. 每日总结调度（使用独立会话）
        await controller.execute_ai_trigger("Daily Digest", action="scheduled_daily_sync")
        self.assertTrue(history[1]["need_new_chat"])

        # 3. 后续邮件同步（返回时自动开启新会话，避免污染每日总结）
        await controller.execute_ai_trigger("Mail 2")
        self.assertTrue(history[2]["need_new_chat"])
        self.assertEqual(history[2]["prompts_count"], 1)

        # 4. 再下一次邮件同步（复用新会话）
        await controller.execute_ai_trigger("Mail 3")
        self.assertFalse(history[3]["need_new_chat"])
        self.assertEqual(history[3]["prompts_count"], 2)

    async def test_do_trigger_ai_reusing_session_without_goto(self):
        """验证当处于 Notion 对话中且无需 New Chat 时，绝对不会调用 page.goto(target_url)"""
        from unittest.mock import MagicMock
        controller = AIController()

        mock_page = MagicMock()
        mock_page.url = "https://app.notion.com/chat/test-uuid-12345"
        mock_page.goto = AsyncMock()
        mock_page.keyboard = MagicMock()
        mock_page.keyboard.press = AsyncMock()
        mock_page.keyboard.insert_text = AsyncMock()
        mock_page.locator = MagicMock()

        # 模拟 chat_input 可见
        mock_input = MagicMock()
        mock_input.count = AsyncMock(return_value=1)
        mock_input.is_visible = AsyncMock(return_value=True)
        mock_input.scroll_into_view_if_needed = AsyncMock()
        mock_input.click = AsyncMock()
        mock_input.focus = AsyncMock()
        mock_input.evaluate = AsyncMock(return_value=True)

        controller.page = mock_page
        controller._ensure_browser = AsyncMock(return_value=True)
        controller._get_chat_input = AsyncMock(return_value=mock_input)
        controller._close_all_overlays = AsyncMock()
        controller.switch_ai_model = AsyncMock(return_value=True)
        controller._click_new_chat = AsyncMock(return_value=True)

        # 模拟页面完成信号立即返回
        mock_btn_completion = MagicMock()
        mock_btn_completion.count = AsyncMock(return_value=1)
        mock_btn_completion.is_visible = AsyncMock(return_value=True)
        mock_btn_completion.last = mock_btn_completion

        mock_stop_btn = MagicMock()
        mock_stop_btn.count = AsyncMock(return_value=0)
        mock_stop_btn.is_visible = AsyncMock(return_value=False)
        mock_stop_btn.first = mock_stop_btn

        def mock_locator(sel):
            if "Stop" in sel or "停止" in sel:
                return mock_stop_btn
            if "Copy" in sel or "复制" in sel or "Undo" in sel or "撤销" in sel:
                return mock_btn_completion
            submit_mock = MagicMock()
            submit_mock.count = AsyncMock(return_value=1)
            submit_mock.is_visible = AsyncMock(return_value=True)
            submit_mock.click = AsyncMock()
            submit_mock.first = submit_mock
            return submit_mock

        mock_page.locator = MagicMock(side_effect=mock_locator)
        mock_page.screenshot = AsyncMock()
        mock_error_loc = MagicMock()
        mock_error_loc.count = AsyncMock(return_value=0)
        mock_error_loc.is_visible = AsyncMock(return_value=False)
        mock_error_loc.last = mock_error_loc
        mock_page.get_by_text = MagicMock(return_value=mock_error_loc)

        # 使用 patch 模拟 asyncio.sleep，避免在单元测试中真实等待
        from unittest.mock import patch
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with patch("time.time", side_effect=[100.0, 100.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0]):
                # 1. 模拟 need_new_chat = False：不应该调用 page.goto
                await controller._do_trigger_ai(action=None, restart_browser=False, need_new_chat=False)
                mock_page.goto.assert_not_called()
                controller._click_new_chat.assert_not_called()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with patch("time.time", side_effect=[100.0, 100.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0]):
                await controller._do_trigger_ai(action=None, restart_browser=False, need_new_chat=True)
                controller._click_new_chat.assert_called_once()
                mock_page.goto.assert_not_called()

    async def test_backlog_summary_and_pending_chats(self):
        """验证 get_backlog_summary 正确统计任务池积压邮件数与待办 Notion AI Chat 数"""
        controller = AIController()
        controller._mail_sync_backlog = 15  # 待从 Outlook 同步的邮件数
        controller._uploaded_in_batch = 1   # 本批已同步待 AI 处理的零头邮件数

        # 压入 2 个 Chat 批次任务 (每个代表 2 封)
        await controller.queue_ai_trigger("Batch (1/2) - 2 mails")
        await controller.queue_ai_trigger("Batch (2/2) - 2 mails")

        total_backlog, pending_chats = controller.get_backlog_summary()
        # 总积压 = 15 (待同步) + 1 (零头待AI) + 2 * 2 (队列中 2 批) = 20 封
        self.assertEqual(total_backlog, 20)
        # 待办 Chat = 2 (队列中) + 1 (零头 1 封需要 1 次 chat) = 3 次 Chat
        self.assertEqual(pending_chats, 3)

    def test_browser_init_timeout_config(self):
        """验证 browser_init_timeout_sec 在 Schema 和 Config 中的默认值是 180 秒"""
        from src.setup.schema import MailAgentConfig
        cfg = MailAgentConfig()
        self.assertEqual(cfg.browser_init_timeout_sec, 180)
        self.assertEqual(cfg.notion_ai_max_new_chats_before_browser_restart, 8)


if __name__ == "__main__":
    unittest.main()

