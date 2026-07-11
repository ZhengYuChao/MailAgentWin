import sys
import io
import pygetwindow as gw

# Force UTF-8 output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

print("Listing all visible windows:")
for win in gw.getAllWindows():
    try:
        if win.visible and win.title:
            print(f"Title: '{win.title}'")
    except:
        pass
