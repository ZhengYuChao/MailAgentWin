import uiautomation as auto
import pyautogui
import time
import sys

sys.stdout.reconfigure(encoding='utf-8')

def count_tabs_by_navigation():
    # 1. 定位 Notion 窗口
    root = auto.GetRootControl()
    notion_window = None
    for win in root.GetChildren():
        if win.ClassName == 'Chrome_WidgetWin_1' and any(k in win.Name for k in ["Notion", "邮件", "任务"]):
            notion_window = win
            break
            
    if not notion_window:
        print("Error: Could not find Notion window.")
        return 0

    # 2. 激活窗口
    notion_window.SetFocus()
    time.sleep(0.5)
    
    start_title = notion_window.Name
    print(f"Start Title: {start_title}")
    
    seen_titles = [start_title]
    
    # 最多尝试 20 次，防止死循环
    for i in range(20):
        # 按下 Ctrl+Tab 切换到下一个
        pyautogui.hotkey('ctrl', 'tab')
        time.sleep(0.5) # 等待渲染
        
        current_title = notion_window.Name
        print(f"  Step {i+1}: {current_title}")
        
        if current_title == start_title:
            # 回到了原点
            break
        
        if current_title not in seen_titles:
            seen_titles.append(current_title)
        else:
            # 如果标题重复了但不是起点，说明可能是有重名 Tab 或者到头了
            # 在 Notion 中，不同页面的标题通常是唯一的
            pass

    return len(seen_titles)

if __name__ == "__main__":
    count = count_tabs_by_navigation()
    print(f"\nTotal Unique Tabs Detected: {count}")
