import sys

with open('src/mail/new_watcher_win.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
for i, line in enumerate(lines):
    if 167 <= i <= 228:
        # These are lines 168 to 229 (1-indexed)
        if line.strip():
            new_lines.append('    ' + line)
        else:
            new_lines.append(line)
    elif i == 229:
        # Line 230: `        try:` -> we remove it!
        continue
    else:
        new_lines.append(line)

with open('src/mail/new_watcher_win.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
