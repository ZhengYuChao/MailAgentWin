import psutil
import pygetwindow as gw
import win32process
import win32gui

print("--- PSUTIL CHECK ---")
notion_pids = []
for p in psutil.process_iter(['name', 'pid']):
    if p.info['name'] and 'Notion' in p.info['name']:
        print(f"Found process: {p.info['name']} (PID: {p.info['pid']})")
        notion_pids.append(p.info['pid'])

print("\n--- WINDOW CHECK ---")
def callback(hwnd, _):
    if win32gui.IsWindowVisible(hwnd):
        title = win32gui.GetWindowText(hwnd)
        if title:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid in notion_pids:
                print(f"MATCH: HWND {hwnd}, Title '{title}', PID {pid}")
            elif 'Notion' in title or '邮件' in title:
                print(f"TITLE MATCH ONLY: HWND {hwnd}, Title '{title}', PID {pid}")
    return True

win32gui.EnumWindows(callback, None)

print("\n--- PYGETWINDOW CHECK ---")
for w in gw.getAllWindows():
    if w.visible and w.title:
        if 'Notion' in w.title:
            print(f"Found by Title: '{w.title}'")
