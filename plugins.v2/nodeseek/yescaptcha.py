from curl_cffi import requests
import time
from typing import Dict, Optional

class YesCaptchaSolverError(Exception):
    pass

class YesCaptchaSolver:
    def __init__(self, api_base_url: str = "https://api.yescaptcha.com", client_key: str = "", max_retries: int = 20, retry_interval: int = 3, timeout: int = 60, soft_id: Optional[str] = "62709", proxies: Optional[Dict[str, str]] = None):
        self.create_task_url = f"{api_base_url.rstrip('/')}/createTask"
        self.get_result_url = f"{api_base_url.rstrip('/')}/getTaskResult"
        self.client_key = client_key
        self.max_retries = max_retries
        self.retry_interval = retry_interval
        self.timeout = timeout
        self.soft_id = soft_id
        self.proxies = proxies

    def solve(self, url: str, sitekey: str, user_agent: Optional[str] = None, verbose: bool = False) -> str:
        task = {"type": "TurnstileTaskProxyless", "websiteURL": url, "websiteKey": sitekey}
        if user_agent:
            task["userAgent"] = user_agent
        payload = {"clientKey": self.client_key, "task": task}
        if self.soft_id:
            payload["softID"] = self.soft_id
        response = requests.post(self.create_task_url, json=payload, timeout=self.timeout, impersonate="chrome110", proxies=self.proxies)
        data = response.json()
        if data.get("errorId") != 0 or not data.get("taskId"):
            raise YesCaptchaSolverError(f"创建验证码任务失败：{data.get('errorCode', 'unknown')}")
        for _ in range(self.max_retries):
            response = requests.post(self.get_result_url, json={"clientKey": self.client_key, "taskId": data["taskId"]}, timeout=self.timeout, impersonate="chrome110", proxies=self.proxies)
            result = response.json()
            if result.get("errorId", 0) != 0:
                raise YesCaptchaSolverError(f"获取验证码结果失败：{result.get('errorCode', 'unknown')}")
            if result.get("status") == "ready":
                token = (result.get("solution") or {}).get("token")
                if token:
                    return token
                raise YesCaptchaSolverError("验证码服务返回空 token")
            time.sleep(self.retry_interval)
        raise YesCaptchaSolverError("验证码任务超时")
