"""总览体验与运维卫生：白话健康、今日关注、版本串、备份排序。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.main import build_payload
from agent import __version__ as agent_version
from center.anomaly import judge_host_payload
from center.status_text import accel_health_label


class HealthLabelTests(unittest.TestCase):
    def test_notes_outrank_raw_health(self) -> None:
        self.assertEqual(accel_health_label("abnormal", "驱动/管理工具缺失"), "驱动缺失")
        self.assertEqual(accel_health_label("abnormal", "nvidia-smi 不在 PATH"), "工具不在 PATH")
        self.assertEqual(accel_health_label("abnormal", "设备无响应 (rev ff)"), "硬件异常")
        self.assertEqual(accel_health_label("Good", ""), "正常")
        self.assertEqual(accel_health_label("", ""), "未上报")
        self.assertEqual(accel_health_label("abnormal", ""), "异常")

    def test_finding_keeps_raw_health_value(self) -> None:
        judged = judge_host_payload(
            {
                "system": {"cpu_percent": 10, "mem_percent": 10, "disk_percent": 10},
                "accelerators": [
                    {
                        "index": 0,
                        "vendor_label": "寒武纪",
                        "health": "abnormal",
                        "note": "驱动/管理工具缺失",
                        "util_percent": None,
                    }
                ],
            },
            online=True,
        )
        finding = next(item for item in judged["findings"] if item["code"] == "accel_health")
        self.assertIn("驱动缺失", finding["message"])
        self.assertNotIn("Health=", finding["message"])
        self.assertEqual(finding["value"], "abnormal")


class AgentVersionTests(unittest.TestCase):
    def test_payload_tracks_package_version(self) -> None:
        from center import __version__ as center_version

        payload = build_payload({"host_id": "h", "host_type": "cpu"})
        self.assertEqual(payload["agent_version"], agent_version)
        self.assertEqual(agent_version, center_version)
        self.assertEqual(agent_version, "1.13.0")
        self.assertNotEqual(payload["agent_version"], "1.4.0")
        pages = {
            "center/static/index.html": "中心控制台 · CPU / 加速卡监测 · v" + center_version,
            "center/static/deploy.html": "运维 · 客户端部署 · v" + center_version,
        }
        for rel, snippet in pages.items():
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn(snippet, page)
            self.assertIn("v" + center_version, page)
        with open(os.path.join(ROOT, "center/static/app.js"), encoding="utf-8") as fh:
            app_js = fh.read()
        with open(os.path.join(ROOT, "center/static/deploy.js"), encoding="utf-8") as fh:
            deploy_js = fh.read()
        with open(os.path.join(ROOT, "center/static/deploy.html"), encoding="utf-8") as fh:
            deploy_html = fh.read()
        self.assertIn("applyHealthVersion", app_js)
        self.assertIn("periodDetailToken", app_js)
        self.assertIn("离线概览", app_js)
        self.assertIn('data-kind="cluster"', app_js)
        self.assertIn("renderPeriodSummary", app_js)
        self.assertIn("renderPeriodList", app_js)
        self.assertNotIn("点上面的数字可筛到主机表", app_js)
        self.assertIn("reportPresentation", app_js)
        with open(os.path.join(ROOT, "center/static/index.html"), encoding="utf-8") as fh:
            console = fh.read()
        with open(os.path.join(ROOT, "center/static/ux.js"), encoding="utf-8") as fh:
            ux_js = fh.read()
        self.assertIn('id="periodSummary"', console)
        self.assertIn('data-tip-id="attention"', console)
        self.assertIn('data-tip-id="period-list"', console)
        self.assertIn('data-tip-id="trend"', console)
        self.assertIn('data-tip-id="reports"', console)
        self.assertNotIn("data-help=", console)
        self.assertNotIn(">说明</button>", console)
        self.assertNotIn("按当前快照。重点数据机上的数据外传异常也在这里", console)
        self.assertNotIn("悬停显示上海时间和数值；没有补上的空档", console)
        self.assertNotIn("中心按配置自动生成上一完整周期（默认每日 00:10", console)
        self.assertIn('data-tip-id="readdress"', app_js)
        self.assertNotIn("旧 IP 留在历史里", app_js)
        self.assertIn("旧 IP 留在历史里", ux_js)
        self.assertIn("reportParagraphView", app_js)
        with open(os.path.join(ROOT, "center/static/settings.html"), encoding="utf-8") as fh:
            settings = fh.read()
        self.assertIn('data-tip-id="guard"', settings)
        self.assertIn("测试发送", settings)
        self.assertIn('data-tip-id="push-test"', settings)
        self.assertIn('id="pushLast"', settings)
        self.assertNotIn("只向已经保存的渠道和 Webhook 发一条短消息", settings)
        self.assertIn("只向已经保存的渠道和 Webhook 发一条短消息", ux_js)
        self.assertIn("最近一次推送结果", ux_js)
        self.assertNotIn("data-help=", settings)
        self.assertNotIn("它不能保证数据出不去，也不拦截连接", settings)
        with open(os.path.join(ROOT, "center/static/settings.js"), encoding="utf-8") as fh:
            settings_js = fh.read()
        self.assertNotIn('<p class="param-desc">', settings_js)
        self.assertNotIn("loadPeriodHostDetail(selectedId);\n        renderHosts(allHosts);", app_js)
        self.assertIn("applyHealthVersion", deploy_js)
        self.assertNotIn("scrollTo", deploy_js)
        self.assertIn('id="dlgAdd"', deploy_html)
        self.assertIn('id="dlgExcel"', deploy_html)
        self.assertIn('id="dlgSettings"', deploy_html)
        self.assertIn('id="btnOpenAdd"', deploy_html)
        self.assertIn('id="rowTestHint"', deploy_html)
        self.assertIn("补跑脚本", deploy_html)
        self.assertIn('data-tip-id="ops-rerun"', deploy_html)
        self.assertNotIn('id="opsAdd"', deploy_html)
        self.assertNotIn('data-shell="hosts"', deploy_html)
        self.assertLess(deploy_html.find('id="opsTargets"'), deploy_html.find('id="dlgAdd"'))
        self.assertIn("点「加入一台机器」", ux_js)
        self.assertNotIn("在下方加入一台机器", ux_js)
        self.assertNotIn("可以在左边覆盖", ux_js)
        self.assertIn("不会卸载", ux_js)
        self.assertNotIn('data-shell="hosts"', console)
        self.assertNotIn('data-shell="hosts"', settings)
        self.assertNotIn(">主机</a>", console)
        self.assertNotIn(">主机</a>", settings)
        self.assertNotIn(">主机</a>", deploy_html)
        self.assertIn(">主机资源<", console)
        self.assertIn('data-shell="realtime"', console)
        self.assertIn('data-detail-tab="hardware"', app_js)
        self.assertIn('data-detail-tab="asset"', app_js)
        self.assertIn("硬件基本信息", app_js)
        self.assertIn(">资产信息</button>", app_js)
        self.assertIn("renderAddressSection(data) + renderAssetSection(data)", app_js)
        self.assertNotIn(
            "renderCollectedHardware",
            app_js.split("function renderAssetSection", 1)[1].split("function bindAssetActions", 1)[0],
        )
        self.assertIn('data-tip-id="asset-collected"', app_js)
        self.assertIn('data-tip-id="asset-manual"', app_js)
        self.assertNotIn(">说明</button>", deploy_html)
        guide = os.path.join(ROOT, "docs", "17-external-user-guide.md")
        with open(guide, encoding="utf-8") as fh:
            user_guide = fh.read()
        self.assertIn("硬件基本信息", user_guide)
        self.assertIn("不再单列「主机」", user_guide)
        self.assertNotIn("往下才是「加入一台机器」", user_guide)
        with open(os.path.join(ROOT, "docs", "16-external-deploy-guide.md"), encoding="utf-8") as fh:
            deploy_guide = fh.read()
        self.assertIn("关掉仍停在清单", deploy_guide)
        self.assertNotIn("往下才是「加入一台机器」", deploy_guide)
        self.assertNotIn("不会卸载已经装好的采集端", deploy_html)
        self.assertIn("部署勾选", deploy_html)
        self.assertIn('src="/ux.js"', deploy_html)
        self.assertLess(deploy_html.find('src="/ux.js"'), deploy_html.find('src="/deploy.js"'))

    def test_health_version_matches_package(self) -> None:
        from center import __version__ as center_version
        from center.server import make_handler
        from center.storage import Storage

        tmp = tempfile.TemporaryDirectory()
        storage = Storage(os.path.join(tmp.name, "t.db"))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(storage, ""))
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%s" % port
            with urlopen(base + "/api/v1/health", timeout=5) as resp:
                health = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(health["ok"])
            self.assertEqual(health["version"], center_version)
            with urlopen(base + "/", timeout=5) as resp:
                page = resp.read().decode("utf-8")
            self.assertIn("v" + center_version, page)
        finally:
            httpd.shutdown()
            httpd.server_close()
            tmp.cleanup()

    def test_readme_upgrade_path_is_push_upgrade(self) -> None:
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        self.assertNotIn("优先用 `scripts/lib/sync_tree.sh`", readme)
        self.assertIn("scripts/push_upgrade.sh", readme)
        self.assertIn("unittest discover -s tests", readme)
        with open(os.path.join(ROOT, "docs", "13-diagnostic-reports.md"), encoding="utf-8") as fh:
            reports = fh.read()
        self.assertNotIn("systemd 重启方式", reports)
        self.assertIn("push_upgrade.sh", reports)


class UxScriptTests(unittest.TestCase):
    def test_node_labels_attention_and_hash(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不在 PATH")
        script = r"""
const ux = require("./center/static/ux.js");
const assert = require("assert");
assert.strictEqual(ux.accelHealthLabel("abnormal", "驱动/管理工具缺失"), "驱动缺失");
assert.strictEqual(ux.accelHealthLabel("abnormal", "npu-smi 不在 PATH"), "工具不在 PATH");
assert.strictEqual(ux.accelHealthLabel("abnormal", "设备无响应 (rev ff)"), "硬件异常");
assert.strictEqual(ux.accelHealthLabel("Good", ""), "正常");
const offline = ux.attentionTags({ online: false, disk_percent: 99, anomaly: { findings: [] } }, {});
assert.deepStrictEqual(offline, ["offline"]);
const disk = ux.attentionTags({
  online: true,
  disk_percent: 96,
  anomaly: { findings: [{ code: "disk_critical" }] },
}, { disk_critical_percent: 95 });
assert.ok(disk.indexOf("disk") >= 0);
assert.ok(disk.indexOf("offline") < 0);
const card = ux.attentionTags({
  online: true,
  disk_percent: 10,
  cpu_percent: 10,
  anomaly: { findings: [{ code: "accel_health" }] },
}, {});
assert.deepStrictEqual(card, ["accel"]);
const load = ux.attentionTags({
  online: true,
  cpu_percent: 96,
  load1: 1,
  cpu_count: 8,
  anomaly: { findings: [{ code: "cpu_util_critical" }] },
}, { cpu_critical_percent: 95 });
assert.deepStrictEqual(load, ["load"]);
const guard = ux.attentionTags({
  online: true,
  cpu_percent: 10,
  data_guard: { attention: true, marked: true },
  anomaly: { findings: [] },
}, {});
assert.deepStrictEqual(guard, ["data_exfil"]);
const guardOffline = ux.attentionTags({
  online: false,
  data_guard: { attention: true, marked: true },
  anomaly: { findings: [] },
}, {});
assert.deepStrictEqual(guardOffline, ["offline"]);
assert.strictEqual(ux.parseLocationHash("#reports").tab, "reports");
const emptyDay = ux.reportPresentation({
  id: 1, title: "使用诊断日报", status: "ok", empty: true, source: "manual",
  payload: { empty: true, sample_count: 0, sections: [{ title: "1. 抬头" }] },
});
assert.strictEqual(emptyDay.mode, "empty");
assert.ok(emptyDay.message.indexOf("没有") >= 0);
assert.strictEqual(ux.reportListItemVisible({ id: 2, empty: true, period_type: "week" }), true);
assert.strictEqual(ux.reportListItemVisible({ id: 3, status: "failed", period_type: "month" }), true);
const failed = ux.reportPresentation({ id: 3, title: "周报", status: "failed", error: "超出保留期", payload: { sections: [] } });
assert.strictEqual(failed.mode, "failed");
const full = ux.reportPresentation({ id: 4, title: "月报", status: "ok", payload: { sample_count: 3, sections: [{ title: "1" }], markdown: "x" } });
assert.strictEqual(full.mode, "full");
assert.strictEqual(ux.reportListItemVisible({}), false);
assert.strictEqual(ux.parseLocationHash("#hosts").focus, "hosts");
assert.strictEqual(ux.parseLocationHash("#period").tab, "period");
var trend = [
  { ts: 100, value: 1 },
  { ts: 200, value: null },
  { ts: 300, value: 9 },
];
assert.strictEqual(ux.nearestTrendPoint(trend, 0).ts, 100);
assert.strictEqual(ux.nearestTrendPoint(trend, 0).value, 1);
assert.strictEqual(ux.nearestTrendPoint(trend, 0.5).value, null);
assert.strictEqual(ux.nearestTrendPoint(trend, 1).ts, 300);
assert.strictEqual(ux.nearestTrendPoint(trend, 1).value, 9);
assert.strictEqual(ux.nearestTrendPoint([], 0.2), null);
assert.strictEqual(ux.nearestTrendPoint([{ ts: 5, value: 0 }], 0).value, 0);
assert.strictEqual(ux.parseLocationHash("#host=gpu%2F1").hostId, "gpu/1");
assert.strictEqual(ux.parseLocationHash("#host=gpu%2F1").tab, "realtime");
assert.strictEqual(ux.loadPerCoreHigh(null, 8, 2), false);
assert.strictEqual(ux.loadPerCoreHigh(0, 8, 2), false);
assert.strictEqual(ux.loadPerCoreHigh(16, 8, 2), true);
assert.strictEqual(ux.loadPerCoreHigh(20, 8, 4), false);
const load5 = ux.attentionTags({
  online: true,
  cpu_percent: 10,
  load1: 1,
  load5: 20,
  cpu_count: 8,
  anomaly: { findings: [] },
}, { cpu_load_per_core_warn: 2 });
assert.deepStrictEqual(load5, ["load"]);
const quiet = ux.attentionTags({
  online: true,
  cpu_percent: 10,
  load1: null,
  load5: null,
  cpu_count: 8,
  anomaly: { findings: [] },
}, {});
assert.deepStrictEqual(quiet, []);
const zeros = ux.attentionTags({
  online: true,
  cpu_percent: 10,
  load1: 0,
  load5: 0,
  cpu_count: 4,
  anomaly: { findings: [] },
}, {});
assert.deepStrictEqual(zeros, []);
const model = ux.periodViewModel({
  host_count: 2,
  hosts_with_samples: 1,
  sample_count: 10,
  rollup_note: "集群汇总按样本加权",
  hosts: [{ host_id: "a", address: "10.0.0.8" }],
  offline: {
    no_data: { interval_count: 2, total_seconds: 30, label: "离线（无数据）" },
    backfill: { interval_count: 1, total_seconds: 10, label: "断网（已补报）" },
  },
  cluster: { accel_summary: "GPU ×1" },
});
assert.strictEqual(model.list[0].kind, "cluster");
assert.strictEqual(model.list[0].label, "集群");
assert.strictEqual(model.list[1].kind, "host");
assert.strictEqual(model.list[1].id, "a");
assert.strictEqual(model.summary.noDataLabel, "离线（无数据）");
assert.strictEqual(model.summary.backfillLabel, "断网（已补报）");
assert.strictEqual(model.summary.noDataCount, 2);
assert.strictEqual(model.summary.noDataSeconds, 30);
assert.strictEqual(model.summary.backfillCount, 1);
assert.strictEqual(model.summary.hostsWithSamples, 1);
assert.strictEqual(model.summary.hostCount, 2);
assert.ok(model.list.every(function (row) {
  return String(row.label).indexOf("没有样本") < 0;
}));
const help = ux.helpCatalogText();
[
  "离线（无数据）", "断网（已补报）", "数据外传异常", "不能拦截",
  "闲", "忙", "磁盘占用", "「-」", "Asia/Shanghai",
  "00:10", "周一 00:20", "每月 1 日 00:30", "错过不补", "设置",
  "今日关注", "名词解释",
].forEach(function (term) {
  assert.ok(help.indexOf(term) >= 0, "help missing " + term);
});
assert.strictEqual(ux.reportSectionCollapsed("9. 附录"), true);
assert.strictEqual(ux.reportSectionCollapsed("名词解释"), true);
assert.strictEqual(ux.reportSectionCollapsed("2. 总览"), false);
assert.strictEqual(ux.reportParagraphGlossary("口径：集群 avg。闲忙两极只看 CPU 与加速卡。"), true);
assert.strictEqual(ux.reportParagraphGlossary("窗口在线率 1 / 2。"), false);
assert.strictEqual(ux.reportParagraphGlossary("简评里的高负荷 / 偏低只对 CPU 与加速卡。"), true);
var onlineRate = ux.reportParagraphView("窗口在线率 1 / 2（50.0%）。窗口内至少有一条历史上报即计入；没有单独的在线心跳，采样中断算未上报。");
assert.strictEqual(onlineRate.show, "窗口在线率 1 / 2（50.0%）。");
assert.strictEqual(onlineRate.anchor, "窗口在线率");
assert.ok(onlineRate.show.indexOf("没有单独的在线心跳") < 0);
assert.ok(onlineRate.tip.indexOf("没有单独的在线心跳") >= 0);
var generated = ux.reportParagraphView("生成时刻在线 1、离线 1。闲忙里的「离线」和「基本不用」仍按生成时刻是否在线，不回溯窗口内状态。离线主机不把末次快照当成实时用量。");
assert.strictEqual(generated.show, "生成时刻在线 1、离线 1。");
assert.strictEqual(generated.anchor, "离线");
assert.ok(generated.tip.indexOf("末次快照") >= 0);
assert.strictEqual(ux.reportParagraphView("窗口在线率 1 / 2。").tip, "");
assert.strictEqual(ux.reportParagraphView("口径：样本加权。").show, "");
assert.strictEqual(ux.reportSectionLong({ tables: [{ rows: [] }], paragraphs: [] }), true);
assert.strictEqual(ux.reportSectionLong({ tables: [], paragraphs: ["短"] }), false);
assert.ok(ux.tipEntry("readdress").text.indexOf("监控 ID") >= 0);
assert.ok(ux.tipEntry("nodata").text.indexOf("离线（无数据）") >= 0);
const plan = ux.reportPreviewPlan([
  { title: "1. 抬头", paragraphs: ["时间窗：昨天。", "口径：样本加权。"] },
  { title: "2. 总览", paragraphs: ["窗口在线率 1 / 2。"] },
  { title: "9. 附录", paragraphs: ["集群样本数 3。"] },
]);
assert.strictEqual(plan[0].collapsed, false);
assert.strictEqual(plan[0].glossaryParagraphs, 1);
assert.strictEqual(plan[0].openParagraphs, 1);
assert.strictEqual(plan[1].collapsed, false);
assert.strictEqual(plan[2].collapsed, true);
function fakeDoc(text) {
  var sub = { textContent: text };
  return {
    sub: sub,
    querySelector: function (sel) {
      return sel === "header .sub" ? sub : null;
    },
  };
}
function jsonRes(body, ok) {
  return {
    ok: ok !== false,
    json: function () { return Promise.resolve(body); },
  };
}
Promise.all([
  ux.applyHealthVersion({
    document: fakeDoc("中心控制台 · v1.10.0"),
    fetch: function () { return Promise.resolve(jsonRes({ version: "9.9.9" })); },
  }).then(function (ok) {
    assert.strictEqual(ok, true);
  }),
  (function () {
    var doc = fakeDoc("运维 · 客户端部署 · v1.10.0");
    return ux.applyHealthVersion({
      document: doc,
      fetch: function () { return Promise.reject(new Error("offline")); },
    }).then(function (ok) {
      assert.strictEqual(ok, false);
      assert.strictEqual(doc.sub.textContent, "运维 · 客户端部署 · v1.10.0");
    });
  })(),
  (function () {
    var doc = fakeDoc("中心控制台 · v1.10.0");
    return ux.applyHealthVersion({
      document: doc,
      fetch: function () { return Promise.resolve(jsonRes({ version: "" })); },
    }).then(function (ok) {
      assert.strictEqual(ok, false);
      assert.strictEqual(doc.sub.textContent, "中心控制台 · v1.10.0");
    });
  })(),
  (function () {
    var doc = fakeDoc("中心控制台 · v1.10.0");
    return ux.applyHealthVersion({
      document: doc,
      fetch: function () { return Promise.resolve(jsonRes({ version: "9.9.9" })); },
    }).then(function (ok) {
      assert.strictEqual(ok, true);
      assert.strictEqual(doc.sub.textContent, "中心控制台 · v9.9.9");
    });
  })(),
]).catch(function (err) {
  console.error(err);
  process.exit(1);
});
"""
        proc = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)


class BackupSortTests(unittest.TestCase):
    def test_dashed_name_sorts_after_earlier_compact_stamp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="backup-sort-")
        names = [
            "monitor-backup-20200101-020202",
            "monitor-backup-20200101010101",
            "monitor-backup-20200101030303",
            "monitor-backup-100",
        ]
        try:
            paths = []
            for name in names:
                path = os.path.join(tmp, name)
                os.makedirs(path)
                paths.append(path)
            script = """
set -euo pipefail
. "%s/scripts/lib/backup_prune.sh"
backup_paths_oldest_first monitor %s
""" % (
                ROOT,
                " ".join('"%s"' % path for path in paths),
            )
            proc = subprocess.run(
                ["bash", "-c", script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            ordered = [os.path.basename(line) for line in proc.stdout.splitlines() if line.strip()]
            self.assertEqual(
                ordered,
                [
                    "monitor-backup-100",
                    "monitor-backup-20200101010101",
                    "monitor-backup-20200101-020202",
                    "monitor-backup-20200101030303",
                ],
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
