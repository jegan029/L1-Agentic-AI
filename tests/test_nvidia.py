@'
import os
from pathlib import Path
from dotenv import load_dotenv
import requests

root = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=root / ".env")

api_key  = os.getenv("LLM_API_KEY")
endpoint = os.getenv("LLM_ENDPOINT")
model    = os.getenv("LLM_MODEL")

if not api_key or api_key == "CHANGE_ME":
    raise ValueError("LLM_API_KEY not set in .env")

print(f"Endpoint : {endpoint}")
print(f"Model    : {model}")
print(f"Key      : {api_key[:12]}...{api_key[-4:]}")
print("Sending request to NVIDIA... (may take 10-30s)")

try:
    resp = requests.post(
        f"{endpoint}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Reply: NVIDIA L1 Agent Ready"}],
            "max_tokens": 20,
            "temperature": 0.2
        },
        timeout=60
    )

    print(f"Status   : {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        print(f"Response : {data['choices'][0]['message']['content']}")
        print("\n✅ NVIDIA NIM connected successfully!")
    elif resp.status_code == 401:
        print("❌ 401 Unauthorized — API key is invalid or expired")
    elif resp.status_code == 429:
        print("❌ 429 Rate Limited — too many requests, wait and retry")
    else:
        print(f"❌ Unexpected status: {resp.status_code}")
        print(resp.text)

except requests.exceptions.Timeout:
    print("❌ Request timed out after 60s — check your network/VPN")
except requests.exceptions.ConnectionError as e:
    print(f"❌ Connection error: {e}")
except Exception as e:
    print(f"❌ Unexpected error: {e}")
'@ | Out-File -FilePath tests\test_nvidia.py -Encoding utf8