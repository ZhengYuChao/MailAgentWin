import pygetwindow as gw
import psutil
import win32process
import win32gui

def find_notion_window():
    candidates = []
    
    print("1. Trying gw.getWindowsWithTitle('Notion')")
    for w in gw.getWindowsWithTitle('Notion'):
        print(f"  Found: {w.title}, visible: {w.visible}, size: {w.width}x{w.height}")
        if w.visible and w.width > 200 and w.height > 200:
            candidates.append((w.width * w.height, w))
            
    print("2. Process-based search")
    notion_pids = [p.info['pid'] for p in psutil.process_iter(['name', 'pid']) if p.info['name'] and 'Notion' in p.info['name']]
    print(f"  Notion PIDs: {notion_pids}")
    
    def callback(hwnd, extra):
        if win32gui.IsWindowVisible(hwnd):
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid in notion_pids:
                rect = win32gui.GetWindowRect(hwnd)
                width = rect[2] - rect[0]
                height = rect[3] - rect[1]
                title = win32gui.GetWindowText(hwnd)
                print(f"  hwnd {hwnd} (pid {pid}): title='{title}', size: {width}x{height}")
                if width > 200 and height > 200 and title:
                    extra.append((width * height, gw.Win32Window(hwnd)))
    
    hwnds_with_area = []
    win32gui.EnumWindows(callback, hwnds_with_area)
    candidates.extend(hwnds_with_area)
    
    if not candidates:
        print("No candidates found.")
        return None
        
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_win = candidates[0][1]
    print(f"Best window: '{best_win.title}' (Area: {candidates[0][0]})")
    return best_win

find_notion_window()
