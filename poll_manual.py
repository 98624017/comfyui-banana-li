import requests
import json
import time

API_KEY = "ms-448aca6b-3838-4ee0-a5e5-26c889e9522d"
BASE_URL = "https://api-inference.modelscope.cn"
TASK_STATUS_URL = f"{BASE_URL}/v1/tasks/{{task_id}}"
TASK_ID = "4893679"

def _build_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-ModelScope-Task-Type": "image_generation",
    }

def main():
    session = requests.Session()
    headers = _build_headers(API_KEY)
    
    print(f"Checking task: {TASK_ID}")
    try:
        resp = session.get(
            TASK_STATUS_URL.format(task_id=TASK_ID),
            headers=headers,
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"Task query failed: {resp.status_code} {resp.text}")
            return

        payload = resp.json()
        print(f"Full Response: {json.dumps(payload, ensure_ascii=False, indent=2)}")
        
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    main()
