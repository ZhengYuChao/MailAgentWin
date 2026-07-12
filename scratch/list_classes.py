import win32gui
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def callback(hwnd, extra):
    if win32gui.IsWindowVisible(hwnd):
        title = win32gui.GetWindowText(hwnd)
        classname = win32gui.GetClassName(hwnd)
        if title:
            print(f"Title: '{title}', Class: '{classname}'")

win32gui.EnumWindows(callback, None)
