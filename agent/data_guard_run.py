"""采集端上的重点数据守护调度：读中心下发的策略，失败时落盘。"""

import json
import os
import time
from typing import Any, Dict, Optional

from agent.data_guard_spool import (
    clear_records,
    load_records,
    load_state,
    persist_records,
    save_state,
)
from agent.metrics.data_guard import collect_observation
from common.data_guard import evaluate, public_policy, sanitize


def resolve_state_dir(cfg):
    # type: (Dict[str, Any]) -> str
    explicit = cfg.get("data_guard_state_dir") if isinstance(cfg, dict) else None
    if explicit:
        return str(explicit)
    env = os.environ.get("MONITOR_AGENT_STATE")
    if env:
        return os.path.join(env, "data-guard")
    return os.path.join(os.path.expanduser("~"), ".monitor-agent", "data-guard")


class DataGuardRunner(object):
    def __init__(self, state_dir):
        # type: (str) -> None
        self.state_dir = state_dir
        self.state = load_state(state_dir)
        policy = self.state.get("policy")
        self.policy = policy if isinstance(policy, dict) else {"enabled": False}
        self._pending = None  # type: Optional[Dict[str, Any]]

    def collect(self, system, now=None):
        # type: (Dict[str, Any], Optional[int]) -> Optional[Dict[str, Any]]
        if not self.policy.get("enabled"):
            self._pending = None
            return None
        now = int(now if now is not None else time.time())
        net_tx = None
        if isinstance(system, dict):
            net_tx = system.get("net_tx_mbps")
        observation = collect_observation(self.policy, self.state, net_tx, now)
        events = evaluate(self.policy, observation, ts=now)
        recovered = []
        for item in load_records(self.state_dir, now):
            if item.get("ts") == now:
                continue
            recovered.append(
                {
                    "ts": item.get("ts"),
                    "observation": item.get("observation") or {},
                }
            )
        block = sanitize(
            {
                "observation": observation,
                "events": events,
                "recovered": recovered,
            }
        )
        self._pending = {"ts": now, "observation": observation, "events": events}
        self.state["policy"] = self.policy
        save_state(self.state_dir, self.state)
        return block

    def on_result(self, code, body, now=None, spooled=False):
        # type: (int, str, Optional[int], bool) -> None
        now = int(now if now is not None else time.time())
        if 200 <= int(code) < 300:
            self._apply_reply(body)
            clear_records(self.state_dir)
            self._pending = None
            return
        # 整笔指标（含 data_guard）已经进 agent.spool.Spool。
        # 不再写第二份 2 小时文件，避免和 24 小时补报各算一次。
        if spooled:
            self._pending = None
            return
        if self._pending:
            persist_records(self.state_dir, [self._pending], now)
            self._pending = None

    def _apply_reply(self, body):
        # type: (str) -> None
        try:
            data = json.loads(body or "{}")
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        guard = data.get("data_guard")
        if not isinstance(guard, dict):
            return
        if guard.get("enabled"):
            self.policy = dict(guard)
            self.policy["enabled"] = True
        else:
            self.policy = {"enabled": False}
        self.policy = public_policy(self.policy) if self.policy.get("enabled") else {"enabled": False}
        self.state["policy"] = self.policy
        save_state(self.state_dir, self.state)
