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

  /** 与 center/anomaly.py 相同：null 不比较；阈值为空或 0 时用 2.0。 */
  function loadPerCoreHigh(load, cores, limit) {
    var l = num(load);
    var c = num(cores);
    var lim = num(limit);
    if (lim == null || lim === 0) lim = 2;
    if (l == null || c == null || c <= 0) return false;
    return l / c >= lim;
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
    var disk = num(host.disk_percent);
    if (codes.disk_critical || (disk != null && disk >= diskCrit)) tags.push("disk");
    if (codes.accel_health) tags.push("accel");
    var cpu = num(host.cpu_percent);
    var cores = num(host.cpu_count) || 0;
    var loadHigh =
      !!codes.cpu_load_high ||
      loadPerCoreHigh(host.load1, cores, th.cpu_load_per_core_warn) ||
      loadPerCoreHigh(host.load5, cores, th.cpu_load_per_core_warn);
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

  /**
   * 用 /api/v1/health 的 version 替换副标题里的 vX.Y.Z。
   * 请求失败、版本为空或格式不对时保留页面上的原文。
   */
  function applyHealthVersion(options) {
    var opts = options || {};
    var root = opts.document;
    if (!root && typeof document !== "undefined") root = document;
    var fetchFn = opts.fetch;
    if (!fetchFn && typeof fetch === "function") fetchFn = fetch;
    if (!root || typeof root.querySelector !== "function" || typeof fetchFn !== "function") {
      return Promise.resolve(false);
    }
    var sub = root.querySelector("header .sub");
    if (!sub) return Promise.resolve(false);
    var original = String(sub.textContent || "");
    return Promise.resolve()
      .then(function () {
        return fetchFn("/api/v1/health");
      })
      .then(function (res) {
        if (!res || !res.ok || typeof res.json !== "function") return null;
        return res.json();
      })
      .then(function (data) {
        if (!data || typeof data !== "object") return false;
        var version = data.version;
        if (version === null || version === undefined || version === "") return false;
        version = String(version);
        if (!/^\d+\.\d+\.\d+$/.test(version)) return false;
        sub.textContent = original.replace(/v\d+\.\d+\.\d+/, "v" + version);
        return true;
      })
      .catch(function () {
        sub.textContent = original;
        return false;
      });
  }

  return {
    accelHealthLabel: accelHealthLabel,
    attentionTags: attentionTags,
    parseLocationHash: parseLocationHash,
    loadPerCoreHigh: loadPerCoreHigh,
    applyHealthVersion: applyHealthVersion,
  };
});
