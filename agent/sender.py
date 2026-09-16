"""向中心端回传指标（支持有限次重试）。"""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Tuple


def send_metrics(
    url: str,
    payload: Dict[str, Any],
    timeout: float = 10.0,
    retries: int = 2,
    retry_delay: float = 1.0,
) -> Tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_code = 0
    last_body = ""
    attempts = max(1, int(retries) + 1)

    for i in range(attempts):
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return resp.getcode(), body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_code, last_body = int(exc.code), body
            # 4xx 不重试（除 408/429）
            if last_code not in (408, 429) and 400 <= last_code < 500:
                return last_code, last_body
        except urllib.error.URLError as exc:
            last_code = 0
            last_body = str(exc.reason if hasattr(exc, "reason") else exc)
        except TimeoutError as exc:
            last_code = 0
            last_body = str(exc)

        if i + 1 < attempts:
            time.sleep(retry_delay * (i + 1))

    return last_code, last_body
