"""文件名排在测试模块最前：导入时就打开沙箱，后面的用例继承这些变量。

upgrade_center.sh 跑预同步测试时也会导出同一组变量，并在 PATH 前放假 systemctl。
"""

import os
import unittest

os.environ.setdefault("MONITOR_TEST_SANDBOX", "1")
os.environ.setdefault("MONITOR_CLEANUP_SKIP_SYSTEMCTL", "1")
os.environ.setdefault("MONITOR_CLEANUP_SKIP_PKILL", "1")
os.environ.setdefault("MONITOR_AGENT_PID_DIRS", "")


class SandboxEnvTests(unittest.TestCase):
    def test_sandbox_env_is_on(self) -> None:
        self.assertEqual(os.environ.get("MONITOR_TEST_SANDBOX"), "1")
        self.assertEqual(os.environ.get("MONITOR_CLEANUP_SKIP_SYSTEMCTL"), "1")
        self.assertEqual(os.environ.get("MONITOR_CLEANUP_SKIP_PKILL"), "1")
        self.assertEqual(os.environ.get("MONITOR_AGENT_PID_DIRS"), "")
