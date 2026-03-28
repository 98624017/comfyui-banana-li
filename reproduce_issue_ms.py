import requests
import json
import time

API_KEY = "ms-448aca6b-3838-4ee0-a5e5-26c889e9522d"
BASE_URL = "https://api-inference.modelscope.cn"
IMAGE_GENERATION_URL = f"{BASE_URL}/v1/images/generations"
TASK_STATUS_URL = f"{BASE_URL}/v1/tasks/{{task_id}}"

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 120.0
POLL_INTERVAL_SECONDS = 2.5
POLL_MAX_SECONDS = 420.0

def _build_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-ModelScope-Async-Mode": "true",
    }

def _poll_task(session, task_id, headers):
    print(f"Start polling task: {task_id}")
    start = time.time()
    poll_headers = dict(headers)
    poll_headers["X-ModelScope-Task-Type"] = "image_generation"

    while True:
        print(f"Polling... {time.time() - start:.1f}s")
        try:
            resp = session.get(
                TASK_STATUS_URL.format(task_id=task_id),
                headers=poll_headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            if resp.status_code != 200:
                print(f"Task query failed: {resp.status_code} {resp.text}")
                return

            payload = resp.json()
            # print(f"Poll response: {json.dumps(payload, ensure_ascii=False)}")
            
            status = payload.get("task_status") or payload.get("status")
            print(f"Status: {status}")
            
            if status == "SUCCEED":
                print("Task SUCCEED!")
                print(payload)
                return
            if status == "FAILED":
                print(f"Task FAILED: {payload}")
                return
            
        except Exception as e:
            print(f"Polling Exception: {e}")

        if time.time() - start > POLL_MAX_SECONDS:
            print("Polling timeout")
            return
            
        time.sleep(POLL_INTERVAL_SECONDS)

def main():
    session = requests.Session()
    headers = _build_headers(API_KEY)
    
    payload = {
        "model": "Tongyi-MAI/Z-Image-Turbo",
        "prompt": "A beautiful landscape",
        "size": "1280x1280",
        "steps": 10,
        "guidance": 1.5,
        "n": 1,
        "seed": 123456
    }
    
    print(f"Submitting request to {IMAGE_GENERATION_URL}")
    print(f"Payload: {json.dumps(payload, ensure_ascii=False)}")
    
    try:
        response = session.post(
            IMAGE_GENERATION_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        
        print(f"Submit response status: {response.status_code}")
        # print(f"Submit response text: {response.text}")
        
        if response.status_code != 200:
             print("Submission failed")
             print(response.text)
             return

        data = response.json()
        print(f"Response: {data}")
        
        if "task_id" in data:
            _poll_task(session, str(data["task_id"]), headers)
        elif "output_images" in data:
             print("Images returned immediately (Sync mode?)")
             print(data)
        elif "images" in data:
             print("Images returned immediately (Sync mode 2?)") 
             print(data)
        else:
             print("Unknown response structure")
             print(data)

    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    main()
