"""客户端部署：目标清单、设置与任务状态（SQLite）。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

# Agent 产品默认目录：与同机 Center（/opt/monitor）分开，避免互相覆盖。
# 已有库若已写入 /opt/monitor 或用户显式填写，不自动改写。
DEFAULT_AGENT_REMOTE_DIR = "/opt/monitor-agent"
LEGACY_AGENT_REMOTE_DIR = "/opt/monitor"
DEFAULT_CENTER_INSTALL_DIR = "/opt/monitor"


class DeployStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS deploy_settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS deploy_targets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ip TEXT NOT NULL,
                        host_id TEXT NOT NULL,
                        hostname TEXT,
                        host_type TEXT NOT NULL DEFAULT 'auto',
                        ssh_user TEXT NOT NULL DEFAULT 'root',
                        ssh_port INTEGER NOT NULL DEFAULT 22,
                        remote_dir TEXT NOT NULL DEFAULT '/opt/monitor-agent',
                        status TEXT NOT NULL DEFAULT 'pending',
                        last_message TEXT,
                        last_error_code TEXT DEFAULT '',
                        last_deploy_at INTEGER,
                        created_at INTEGER NOT NULL,
                        peer_hosts TEXT DEFAULT '[]',
                        UNIQUE(ip, host_id)
                    );
                    CREATE TABLE IF NOT EXISTS deploy_jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        status TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        finished_at INTEGER,
                        target_ids TEXT NOT NULL,
                        log_text TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE IF NOT EXISTS net_hosts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        host TEXT NOT NULL UNIQUE,
                        note TEXT DEFAULT '',
                        created_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS net_probe_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        host TEXT NOT NULL,
                        name TEXT,
                        source TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        reachable INTEGER NOT NULL,
                        quality TEXT,
                        rtt_ms REAL,
                        ssh_ok INTEGER,
                        probed_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_net_probe_host_ts
                        ON net_probe_results(host, probed_at);
                    """
                )
                # 兼容旧库：为 deploy_targets 追加网络字段
                cols = {
                    r[1]
                    for r in conn.execute("PRAGMA table_info(deploy_targets)").fetchall()
                }
                alters = []
                if "net_reachable" not in cols:
                    alters.append("ALTER TABLE deploy_targets ADD COLUMN net_reachable INTEGER")
                if "net_quality" not in cols:
                    alters.append("ALTER TABLE deploy_targets ADD COLUMN net_quality TEXT")
                if "net_rtt_ms" not in cols:
                    alters.append("ALTER TABLE deploy_targets ADD COLUMN net_rtt_ms REAL")
                if "net_ssh_ok" not in cols:
                    alters.append("ALTER TABLE deploy_targets ADD COLUMN net_ssh_ok INTEGER")
                if "net_detail" not in cols:
                    alters.append("ALTER TABLE deploy_targets ADD COLUMN net_detail TEXT")
                if "net_probed_at" not in cols:
                    alters.append("ALTER TABLE deploy_targets ADD COLUMN net_probed_at INTEGER")
                if "peer_hosts" not in cols:
                    alters.append("ALTER TABLE deploy_targets ADD COLUMN peer_hosts TEXT DEFAULT '[]'")
                if "ssh_password" not in cols:
                    alters.append(
                        "ALTER TABLE deploy_targets ADD COLUMN ssh_password TEXT DEFAULT ''"
                    )
                if "ssh_key_path" not in cols:
                    alters.append(
                        "ALTER TABLE deploy_targets ADD COLUMN ssh_key_path TEXT DEFAULT ''"
                    )
                if "last_error_code" not in cols:
                    alters.append(
                        "ALTER TABLE deploy_targets ADD COLUMN last_error_code TEXT DEFAULT ''"
                    )
                for sql in alters:
                    conn.execute(sql)
                conn.commit()
            finally:
                conn.close()

    def get_settings(self, include_secrets: bool = False) -> Dict[str, Any]:
        defaults = {
            "public_center_url": "",
            "default_ssh_user": "root",
            "default_ssh_port": 22,
            "default_remote_dir": DEFAULT_AGENT_REMOTE_DIR,
            "ssh_key_path": "",
            "default_ssh_password": "",
            "interval_seconds": 15,
        }
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT key, value FROM deploy_settings").fetchall()
            finally:
                conn.close()
        for row in rows:
            key = row["key"]
            raw = row["value"]
            if key in ("default_ssh_port", "interval_seconds"):
                try:
                    defaults[key] = int(raw)
                except ValueError:
                    pass
            else:
                defaults[key] = raw
        if not include_secrets:
            defaults["default_ssh_password_set"] = bool(
                str(defaults.get("default_ssh_password") or "").strip()
            )
            defaults["default_ssh_password"] = ""
        return defaults

    def update_settings(self, data: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "public_center_url",
            "default_ssh_user",
            "default_ssh_port",
            "default_remote_dir",
            "ssh_key_path",
            "default_ssh_password",
            "interval_seconds",
        }
        with self._lock:
            conn = self._connect()
            try:
                for k, v in data.items():
                    if k not in allowed:
                        continue
                    # 密码空字符串：不覆盖已有密码（前端留空表示保持）
                    if k == "default_ssh_password" and str(v) == "":
                        continue
                    conn.execute(
                        """
                        INSERT INTO deploy_settings(key, value) VALUES(?, ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                        """,
                        (k, str(v)),
                    )
                conn.commit()
            finally:
                conn.close()
        return self.get_settings()

    def list_targets(self, include_secrets: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM deploy_targets ORDER BY id DESC"
                ).fetchall()
            finally:
                conn.close()
        return [
            self._normalize_target(dict(r), include_secrets=include_secrets) for r in rows
        ]

    def get_target(
        self, target_id: int, include_secrets: bool = False
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM deploy_targets WHERE id=?", (target_id,)
                ).fetchone()
            finally:
                conn.close()
        return (
            self._normalize_target(dict(row), include_secrets=include_secrets)
            if row
            else None
        )

    @staticmethod
    def normalize_peers(raw: Any) -> List[Dict[str, str]]:
        """接受 list / JSON 字符串 / 逗号分隔文本。"""
        if raw is None or raw == "":
            return []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, str):
            text = raw.strip()
            if not text:
                return []
            if text.startswith("["):
                try:
                    items = json.loads(text)
                except json.JSONDecodeError:
                    items = []
                    text2 = text
                else:
                    text2 = None
            else:
                items = []
                text2 = text
            if text2 is not None:
                # 支持: 10.0.0.1,10.0.0.2 或 网关=10.0.0.1;交换机=10.0.0.2
                parts = []
                for chunk in text2.replace("；", ";").replace("，", ",").split(";"):
                    parts.extend([p.strip() for p in chunk.split(",") if p.strip()])
                peers = []
                for p in parts:
                    if "=" in p:
                        name, host = p.split("=", 1)
                    elif ":" in p and not p.replace(".", "").replace(":", "").isdigit():
                        # 名称:IP （避免 IPv6 误伤，简单判断）
                        if p.count(":") == 1 and not p.split(":")[0].replace(".", "").isdigit():
                            name, host = p.split(":", 1)
                        else:
                            name, host = p, p
                    else:
                        name, host = p, p
                    host = host.strip()
                    name = name.strip() or host
                    if host:
                        peers.append({"host": host, "name": name})
                return peers
        else:
            return []

        peers = []
        for it in items:
            if isinstance(it, str):
                host = it.strip()
                if host:
                    peers.append({"host": host, "name": host})
            elif isinstance(it, dict):
                host = str(it.get("host") or "").strip()
                name = str(it.get("name") or host).strip()
                if host:
                    peers.append({"host": host, "name": name or host})
        return peers

    def _normalize_target(
        self, item: Dict[str, Any], include_secrets: bool = False
    ) -> Dict[str, Any]:
        peers = self.normalize_peers(item.get("peer_hosts"))
        item["peer_hosts"] = peers
        item["peer_hosts_text"] = ";".join(
            ("%s=%s" % (p["name"], p["host"])) if p["name"] != p["host"] else p["host"]
            for p in peers
        )
        item.setdefault("ssh_password", "")
        item.setdefault("ssh_key_path", "")
        item.setdefault("last_error_code", "")
        pwd_set = bool(str(item.get("ssh_password") or "").strip())
        key_set = bool(str(item.get("ssh_key_path") or "").strip())
        if pwd_set:
            item["auth_mode"] = "password"
        elif key_set:
            item["auth_mode"] = "key"
        else:
            item["auth_mode"] = "inherit"  # 用全局默认密码/私钥/本机默认钥
        item["ssh_password_set"] = pwd_set
        if not include_secrets:
            item["ssh_password"] = ""
        return item

    def add_target(self, data: Dict[str, Any]) -> Dict[str, Any]:
        from common.host_type import normalize_host_type

        now = int(time.time())
        ip = (data.get("ip") or "").strip()
        host_id = (data.get("host_id") or ip).strip()
        if not ip:
            raise ValueError("ip 不能为空")
        peers = self.normalize_peers(data.get("peer_hosts") or data.get("peers") or "")
        peers_json = json.dumps(peers, ensure_ascii=False)
        host_type = normalize_host_type(data.get("host_type") or "auto")
        ssh_password = str(data.get("ssh_password") or "")
        ssh_key_path = str(data.get("ssh_key_path") or "").strip()
        with self._lock:
            conn = self._connect()
            try:
                # upsert 时：若新密码为空则保留旧密码
                old = conn.execute(
                    "SELECT ssh_password FROM deploy_targets WHERE ip=? AND host_id=?",
                    (ip, host_id),
                ).fetchone()
                if not ssh_password and old is not None:
                    ssh_password = old["ssh_password"] or ""
                conn.execute(
                    """
                    INSERT INTO deploy_targets
                    (ip, host_id, hostname, host_type, ssh_user, ssh_port, remote_dir,
                     status, last_message, last_deploy_at, created_at, peer_hosts,
                     ssh_password, ssh_key_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', NULL, ?, ?, ?, ?)
                    ON CONFLICT(ip, host_id) DO UPDATE SET
                        hostname=excluded.hostname,
                        host_type=excluded.host_type,
                        ssh_user=excluded.ssh_user,
                        ssh_port=excluded.ssh_port,
                        remote_dir=excluded.remote_dir,
                        peer_hosts=excluded.peer_hosts,
                        ssh_password=excluded.ssh_password,
                        ssh_key_path=excluded.ssh_key_path
                    """,
                    (
                        ip,
                        host_id,
                        (data.get("hostname") or host_id).strip(),
                        host_type,
                        data.get("ssh_user") or "root",
                        int(data.get("ssh_port") or 22),
                        data.get("remote_dir") or DEFAULT_AGENT_REMOTE_DIR,
                        now,
                        peers_json,
                        ssh_password,
                        ssh_key_path,
                    ),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT id FROM deploy_targets WHERE ip=? AND host_id=?",
                    (ip, host_id),
                ).fetchone()
                tid = int(row["id"])
            finally:
                conn.close()
        target = self.get_target(tid)
        assert target is not None
        return target

    def update_target(self, target_id: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        from common.host_type import normalize_host_type

        cur = self.get_target(target_id, include_secrets=True)
        if not cur:
            return None
        peers = self.normalize_peers(
            data["peer_hosts"] if "peer_hosts" in data else cur.get("peer_hosts")
        )
        new_pwd = data.get("ssh_password", None)
        if new_pwd is None or str(new_pwd) == "":
            ssh_password = cur.get("ssh_password") or ""
        else:
            ssh_password = str(new_pwd)
        fields = {
            "hostname": data.get("hostname", cur.get("hostname")),
            "host_type": normalize_host_type(data.get("host_type", cur.get("host_type"))),
            "ssh_user": data.get("ssh_user", cur.get("ssh_user")),
            "ssh_port": int(data.get("ssh_port", cur.get("ssh_port") or 22)),
            "remote_dir": data.get("remote_dir", cur.get("remote_dir")),
            "peer_hosts": json.dumps(peers, ensure_ascii=False),
            "ssh_password": ssh_password,
            "ssh_key_path": str(
                data.get("ssh_key_path", cur.get("ssh_key_path") or "")
            ).strip(),
        }
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE deploy_targets SET
                      hostname=?, host_type=?, ssh_user=?, ssh_port=?,
                      remote_dir=?, peer_hosts=?, ssh_password=?, ssh_key_path=?
                    WHERE id=?
                    """,
                    (
                        fields["hostname"],
                        fields["host_type"],
                        fields["ssh_user"],
                        fields["ssh_port"],
                        fields["remote_dir"],
                        fields["peer_hosts"],
                        fields["ssh_password"],
                        fields["ssh_key_path"],
                        target_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get_target(target_id)

    def delete_target(self, target_id: int) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM deploy_targets WHERE id=?", (target_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def import_targets(self, text: str, defaults: Optional[Dict[str, Any]] = None) -> int:
        from center.deploy_errors import parse_inventory_line

        defaults = defaults or {}
        default_port = int(defaults.get("ssh_port") or 22)
        count = 0
        for raw in text.splitlines():
            parsed = parse_inventory_line(raw, default_port=default_port)
            if not parsed:
                continue
            self.add_target(
                {
                    "ip": parsed["ip"],
                    "host_id": parsed["host_id"],
                    "hostname": parsed["host_id"],
                    "host_type": defaults.get("host_type") or "auto",
                    "ssh_user": defaults.get("ssh_user") or "root",
                    "ssh_port": parsed["ssh_port"],
                    "remote_dir": defaults.get("remote_dir") or DEFAULT_AGENT_REMOTE_DIR,
                }
            )
            count += 1
        return count

    def update_target_status(
        self,
        target_id: int,
        status: str,
        message: str = "",
        error_code: str = "",
    ) -> None:
        if status == "success":
            error_code = ""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE deploy_targets
                    SET status=?, last_message=?, last_deploy_at=?, last_error_code=?
                    WHERE id=?
                    """,
                    (
                        status,
                        message[:2000],
                        int(time.time()),
                        error_code or "",
                        target_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def create_job(self, target_ids: List[int]) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    INSERT INTO deploy_jobs(status, created_at, finished_at, target_ids, log_text)
                    VALUES ('running', ?, NULL, ?, '')
                    """,
                    (int(time.time()), json.dumps(target_ids)),
                )
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def append_job_log(self, job_id: int, line: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT log_text FROM deploy_jobs WHERE id=?", (job_id,)
                ).fetchone()
                if not row:
                    return
                text = (row["log_text"] or "") + line
                # 限制日志大小
                if len(text) > 200000:
                    text = text[-180000:]
                conn.execute(
                    "UPDATE deploy_jobs SET log_text=? WHERE id=?", (text, job_id)
                )
                conn.commit()
            finally:
                conn.close()

    def finish_job(self, job_id: int, status: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE deploy_jobs SET status=?, finished_at=? WHERE id=?",
                    (status, int(time.time()), job_id),
                )
                conn.commit()
            finally:
                conn.close()

    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM deploy_jobs WHERE id=?", (job_id,)
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        item = dict(row)
        try:
            item["target_ids"] = json.loads(item.get("target_ids") or "[]")
        except json.JSONDecodeError:
            item["target_ids"] = []
        return item

    def list_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, status, created_at, finished_at, target_ids FROM deploy_jobs ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["target_ids"] = json.loads(item.get("target_ids") or "[]")
            except json.JSONDecodeError:
                item["target_ids"] = []
            result.append(item)
        return result

    def update_target_network(self, target_id: int, probe: Dict[str, Any]) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE deploy_targets SET
                      net_reachable=?, net_quality=?, net_rtt_ms=?,
                      net_ssh_ok=?, net_detail=?, net_probed_at=?
                    WHERE id=?
                    """,
                    (
                        1 if probe.get("reachable") else 0,
                        probe.get("quality"),
                        probe.get("rtt_ms"),
                        1 if probe.get("ssh_ok") else 0,
                        json.dumps(probe, ensure_ascii=False),
                        int(probe.get("probed_at") or time.time()),
                        target_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def add_net_host(self, name: str, host: str, note: str = "") -> Dict[str, Any]:
        host = (host or "").strip()
        name = (name or host).strip()
        if not host:
            raise ValueError("host 不能为空")
        now = int(time.time())
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO net_hosts(name, host, note, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(host) DO UPDATE SET
                      name=excluded.name,
                      note=excluded.note
                    """,
                    (name, host, note or "", now),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM net_hosts WHERE host=?", (host,)
                ).fetchone()
            finally:
                conn.close()
        return dict(row)

    def list_net_hosts(self) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM net_hosts ORDER BY id DESC"
                ).fetchall()
            finally:
                conn.close()
        return [dict(r) for r in rows]

    def delete_net_host(self, host_id: int) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM net_hosts WHERE id=?", (host_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def save_probe_result(
        self,
        host: str,
        name: str,
        source: str,
        probe: Dict[str, Any],
    ) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO net_probe_results
                    (host, name, source, result_json, reachable, quality, rtt_ms, ssh_ok, probed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        host,
                        name,
                        source,
                        json.dumps(probe, ensure_ascii=False),
                        1 if probe.get("reachable") else 0,
                        probe.get("quality"),
                        probe.get("rtt_ms"),
                        1 if probe.get("ssh_ok") else 0,
                        int(probe.get("probed_at") or time.time()),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def latest_probe_results(self) -> List[Dict[str, Any]]:
        """每个 host 取最新一条。"""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT r.* FROM net_probe_results r
                    INNER JOIN (
                      SELECT host, source, MAX(probed_at) AS mx
                      FROM net_probe_results GROUP BY host, source
                    ) t ON r.host=t.host AND r.source=t.source AND r.probed_at=t.mx
                    ORDER BY r.source ASC, r.reachable ASC, r.rtt_ms ASC
                    """
                ).fetchall()
            finally:
                conn.close()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["result"] = json.loads(item.get("result_json") or "{}")
            except json.JSONDecodeError:
                item["result"] = {}
            result.append(item)
        return result

    def import_target_dicts(
        self, items: List[Dict[str, Any]], defaults: Optional[Dict[str, Any]] = None
    ) -> int:
        defaults = defaults or {}
        count = 0
        for item in items:
            data = {
                "ip": item.get("ip"),
                "host_id": item.get("host_id") or item.get("ip"),
                "hostname": item.get("hostname") or item.get("host_id") or item.get("ip"),
                "host_type": item.get("host_type") or defaults.get("host_type") or "auto",
                "ssh_user": item.get("ssh_user")
                or defaults.get("ssh_user")
                or "root",
                "ssh_port": item.get("ssh_port")
                or defaults.get("ssh_port")
                or 22,
                "remote_dir": item.get("remote_dir")
                or defaults.get("remote_dir")
                or DEFAULT_AGENT_REMOTE_DIR,
                "peer_hosts": item.get("peer_hosts") or item.get("peers") or "",
                "ssh_password": item.get("ssh_password")
                or defaults.get("ssh_password")
                or "",
                "ssh_key_path": item.get("ssh_key_path")
                or defaults.get("ssh_key_path")
                or "",
            }
            if not data["ip"]:
                continue
            try:
                data["ssh_port"] = int(float(str(data["ssh_port"])))
            except (TypeError, ValueError):
                data["ssh_port"] = int(defaults.get("ssh_port") or 22)
            self.add_target(data)
            count += 1
        return count
