/* 监测台体验：白话健康、今日关注分类、地址栏。
   规则与 center/status_text.py 的 accel_health_label 保持一致。
   浏览器里挂到 window.MonitorUX；Node 测试用 module.exports。 */
(function (root, factory) {
  var api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.MonitorUX = api;
  }
})(typeof window !== "undefined" ? window : this, function () {
  function num(v) {
    if (v === null || v === undefined || v === "") return null;
    var n = Number(v);
    return isNaN(n) ? null : n;
  }

  function accelHealthLabel(health, note) {
    var text = String(note || "");
    var raw = String(health || "").trim();
    var upper = raw.toUpperCase();
    var lowerNote = text.toLowerCase();
    if (text.indexOf("不在 PATH") >= 0 || text.indexOf("未安装") >= 0) {
      return "工具不在 PATH";
    }
    if (text.indexOf("驱动") >= 0) return "驱动缺失";
    if (
      text.indexOf("无响应") >= 0 ||
      lowerNote.indexOf("rev ff") >= 0 ||
      text.indexOf("硬件") >= 0
    ) {
      return "硬件异常";
    }
    if (!raw || upper === "UNKNOWN" || upper === "NONE" || upper === "N/A" || upper === "NULL") {
      return "未上报";
    }
    if (
      upper === "OK" ||
      upper === "GOOD" ||
      upper === "HEALTHY" ||
      upper === "NORMAL" ||
      upper === "PASS"
    ) {
      return "正常";
    }
    if (upper === "CRITICAL" || upper === "ALARM") return "硬件异常";
    return "异常";
  }

  function findingCodes(host) {
    var codes = {};
    var findings = (host && host.anomaly && host.anomaly.findings) || [];
    for (var i = 0; i < findings.length; i++) {
      var code = findings[i] && findings[i].code;
      if (code) codes[code] = true;
    }
    return codes;
  }

  /** 今日关注。离线只进离线，不用末次快照再算磁盘或负载。长期空闲不在这里算。 */
  function attentionTags(host, thresholds) {
    var tags = [];
    var th = thresholds || {};
    if (!host || !host.online) {
      tags.push("offline");
      return tags;
    }
    var codes = findingCodes(host);
    var diskCrit = Number(th.disk_critical_percent || 95);
    var cpuCrit = Number(th.cpu_critical_percent || 95);
    var loadLimit = Number(th.cpu_load_per_core_warn || 2);
    var disk = num(host.disk_percent);
    if (codes.disk_critical || (disk != null && disk >= diskCrit)) tags.push("disk");
    if (codes.accel_health) tags.push("accel");
    var cpu = num(host.cpu_percent);
    var load1 = num(host.load1);
    var cores = num(host.cpu_count) || 0;
    var loadHigh =
      !!codes.cpu_load_high || (load1 != null && cores > 0 && load1 / cores >= loadLimit);
    var cpuHigh = !!codes.cpu_util_critical || (cpu != null && cpu >= cpuCrit);
    if (loadHigh || cpuHigh) tags.push("load");
    return tags;
  }

  function parseLocationHash(hash) {
    var raw = String(hash || "");
    if (raw === "#period" || raw.indexOf("#period") === 0) {
      return { tab: "period", hostId: "", focus: "" };
    }
    if (raw === "#reports" || raw.indexOf("#reports") === 0) {
      return { tab: "reports", hostId: "", focus: "" };
    }
    if (raw === "#hosts") {
      return { tab: "realtime", hostId: "", focus: "hosts" };
    }
    var mark = "#host=";
    if (raw.indexOf(mark) === 0) {
      var id = raw.slice(mark.length);
      try {
        id = decodeURIComponent(id);
      } catch (e) {
        /* 保留原文 */
      }
      return { tab: "realtime", hostId: id, focus: "host" };
    }
    return { tab: "realtime", hostId: "", focus: "" };
  }

  return {
    accelHealthLabel: accelHealthLabel,
    attentionTags: attentionTags,
    parseLocationHash: parseLocationHash,
  };
});
