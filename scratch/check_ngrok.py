import urllib.request
import json

try:
    with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=5) as response:
        data = json.load(response)
        print(json.dumps(data, indent=2))
except Exception as e:
    print(f"Error: {e}")
