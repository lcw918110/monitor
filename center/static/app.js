(() => {
  const REFRESH_MS = 15000;
  let selectedId = null;
  let allHosts = [];
  let resourceGroups = [];
  let currentTab = "realtime"; // realtime | period | reports
  let hostSortKey = "";
  let hostSortDir = "desc";
  let periodSortKey = "";
  let periodSortDir = "desc";
  let periodUtilData = null;
  const RATE_SORT_KEYS = {
    cpu_percent: true,
    mem_percent: true,
    disk_percent: true,
    accel_util: true,
    load1: true,
    last_seen: true,
    samples: true,
    cpu_avg: true,
    cpu_p95: true,
    cpu_busy: true,
    mem_avg: true,
    disk_avg: true,
    accel_avg: true,
    accel_p95: true,
    accel_busy: true,
    rx_pct: true,
    tx_pct: true,
  };
  /** 时段统计时间窗（时段页与单机时段详情共用） */
  let detailMinutes = 120;
  let periodMode = "preset"; // preset | custom
  let periodFromTs = null;
  let periodToTs = null;
  let busyCpu = 80;
  let busyAccel = 80;
  const PERIOD_OPTIONS = [
    { minutes: 60, label: "1 小时" },
    { minutes: 120, label: "2 小时" },
    { minutes: 360, label: "6 小时" },
    { minutes: 1440, label: "24 小时" },
    { minutes: 10080, label: "7 天" },
  ];
  let thresholds = {
    cpu_warn_percent: 80,
    cpu_critical_percent: 95,
    mem_warn_percent: 85,
    mem_critical_percent: 95,
    disk_warn_percent: 85,
    disk_critical_percent: 95,
    accel_util_warn_percent: 95,
    accel_temp_warn_c: 85,
    accel_temp_critical_c: 95,
    accel_mem_warn_percent: 90,
    accel_mem_critical_percent: 98,
    cpu_load_per_core_warn: 2,
  };

  const el = {
    anomalySummary: document.getElementById("anomalySummary"),
    attentionList: document.getElementById("attentionList"),
    attentionNames: document.getElementById("attentionNames"),
    stats: document.getElementById("stats"),
    hostBody: document.getElementById("hostBody"),
    hostCount: document.getElementById("hostCount"),
    detailTitle: document.getElementById("detailTitle"),
    detailBody: document.getElementById("detailBody"),
    btnRefresh: document.getElementById("btnRefresh"),
    filterQ: document.getElementById("filterQ"),
    filterType: document.getElementById("filterType"),
    filterStatus: document.getElementById("filterStatus"),
    filterGroup: document.getElementById("filterGroup"),
    filterOwner: document.getElementById("filterOwner"),
    btnGroups: document.getElementById("btnGroups"),
    btnHidden: document.getElementById("btnHidden"),
    btnImport: document.getElementById("btnImport"),
    hiddenPanel: document.getElementById("hiddenPanel"),
    hiddenList: document.getElementById("hiddenList"),
    importPanel: document.getElementById("importPanel"),
    importFile: document.getElementById("importFile"),
    btnImportPreview: document.getElementById("btnImportPreview"),
    btnImportApply: document.getElementById("btnImportApply"),
    importResult: document.getElementById("importResult"),
    groupPanel: document.getElementById("groupPanel"),
    groupList: document.getElementById("groupList"),
    newGroupName: document.getElementById("newGroupName"),
    btnAddGroup: document.getElementById("btnAddGroup"),
    periodUtilControls: document.getElementById("periodUtilControls"),
    periodUtilBody: document.getElementById("periodUtilBody"),
    periodDetailTitle: document.getElementById("periodDetailTitle"),
    periodDetailBody: document.getElementById("periodDetailBody"),
    viewRealtime: document.getElementById("viewRealtime"),
    viewPeriod: document.getElementById("viewPeriod"),
    viewReports: document.getElementById("viewReports"),
    tabBtnRealtime: document.getElementById("tabBtnRealtime"),
    tabBtnPeriod: document.getElementById("tabBtnPeriod"),
    tabBtnReports: document.getElementById("tabBtnReports"),
    reportList: document.getElementById("reportList"),
    reportPreview: document.getElementById("reportPreview"),
    reportActions: document.getElementById("reportActions"),
    reportWindowHint: document.getElementById("reportWindowHint"),
    reportWindowDate: document.getElementById("reportWindowDate"),
    btnGenerateReport: document.getElementById("btnGenerateReport"),
  };
  let reportPeriod = "day";
  let activeReportId = null;
  let activeReport = null;
  let attentionKey = "";
  const UX = window.MonitorUX || {
    accelHealthLabel: function (health) {
      return health || "-";
    },
    attentionTags: function () {
      return [];
    },
    parseLocationHash: function () {
      return { tab: "realtime", hostId: "", focus: "" };
    },
  };

  function fmtPct(v) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
    return Number(v).toFixed(1) + "%";
  }

  function fmtMbps(v) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
    const n = Number(v);
    if (Math.abs(n) >= 100) return n.toFixed(0);
    if (Math.abs(n) >= 10) return n.toFixed(1);
    return n.toFixed(2);
  }

  function fmtNum(v, digits) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
    return Number(v).toFixed(digits == null ? 1 : digits);
  }

  function fmtTime(ts) {
    if (!ts) return "-";
    const d = new Date(Number(ts) * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return (
      d.getFullYear() +
      "-" +
      p(d.getMonth() + 1) +
      "-" +
      p(d.getDate()) +
      " " +
      p(d.getHours()) +
      ":" +
      p(d.getMinutes()) +
      ":" +
      p(d.getSeconds())
    );
  }

  function fmtUptime(sec) {
    sec = Number(sec || 0);
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d > 0) return d + "天 " + h + "小时";
    if (h > 0) return h + "小时 " + m + "分";
    return m + "分";
  }

  function utilOutOfRange(pct) {
    if (pct === null || pct === undefined || pct === "") return false;
    const n = Number(pct);
    return Number.isNaN(n) || n < 0 || n > 100;
  }

  function bar(pct) {
    if (utilOutOfRange(pct)) {
      return '<div class="bar" title="无效"></div>';
    }
    if (pct === null || pct === undefined || pct === "" || Number.isNaN(Number(pct))) {
      return '<div class="bar" title="无采样"></div>';
    }
    const v = Math.max(0, Math.min(100, Number(pct) || 0));
    return (
      '<div class="bar" title="' +
      fmtPct(v) +
      '"><i style="width:' +
      v +
      '%"></i></div>'
    );
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function numOrNull(v) {
    if (v === null || v === undefined || v === "") return null;
    const n = Number(v);
    return Number.isNaN(n) ? null : n;
  }

  function cmpSort(a, b, dir) {
    const aNil = a === null || a === undefined || a === "";
    const bNil = b === null || b === undefined || b === "";
    if (aNil && bNil) return 0;
    if (aNil) return 1;
    if (bNil) return -1;
    if (typeof a === "number" && typeof b === "number") {
      return dir === "asc" ? a - b : b - a;
    }
    const sa = String(a).toLowerCase();
    const sb = String(b).toLowerCase();
    if (sa < sb) return dir === "asc" ? -1 : 1;
    if (sa > sb) return dir === "asc" ? 1 : -1;
    return 0;
  }

  function syncSortHeaders(table, key, dir) {
    if (!table) return;
    Array.from(table.querySelectorAll("thead th[data-sort]")).forEach((th) => {
      th.classList.remove("sort-asc", "sort-desc");
      if (th.getAttribute("data-sort") === key) {
        th.classList.add(dir === "asc" ? "sort-asc" : "sort-desc");
      }
    });
  }

  function applyHeaderSort(currentKey, currentDir, nextKey) {
    if (currentKey === nextKey) {
      return { key: nextKey, dir: currentDir === "asc" ? "desc" : "asc" };
    }
    return { key: nextKey, dir: RATE_SORT_KEYS[nextKey] ? "desc" : "asc" };
  }

  function hostSortValue(h, key) {
    if (key === "online") return h.online ? 1 : 0;
    if (key === "status") {
      const rank = { critical: 0, warn: 1, unknown: 2, normal: 3 };
      return rank[(h.anomaly && h.anomaly.overall) || "unknown"];
    }
    if (key === "host") {
      return (h.address || h.hostname || h.host_id || "").toLowerCase();
    }
    if (key === "group") return (h.group_name || "").toLowerCase();
    if (key === "owner") return (h.owner || "").toLowerCase();
    if (key === "purpose") return (h.purpose || "").toLowerCase();
    if (key === "type") return normalizedType(h.host_type);
    if (key === "cpu_percent") return numOrNull(h.cpu_percent);
    if (key === "mem_percent") return numOrNull(h.mem_percent);
    if (key === "disk_percent") return numOrNull(h.disk_percent);
    if (key === "disk_count") return numOrNull(h.disk_count);
    if (key === "accel") return accelIdentityText(h).toLowerCase();
    if (key === "accel_util") return numOrNull(accelUtilOf(h));
    if (key === "load1") return numOrNull(h.load1);
    if (key === "last_seen") return numOrNull(h.last_seen);
    return null;
  }

  function sortHostRows(list, key, dir) {
    if (!key) return list;
    return list.slice().sort((a, b) => cmpSort(hostSortValue(a, key), hostSortValue(b, key), dir));
  }

  function periodMetric(h, name, field) {
    const block = (h.metrics || {})[name] || {};
    return numOrNull(block[field]);
  }

  function periodSortValue(h, key) {
    if (key === "host") return (h.address || h.hostname || h.host_id || "").toLowerCase();
    if (key === "group") return (h.group_name || "").toLowerCase();
    if (key === "owner") return (h.owner || "").toLowerCase();
    if (key === "purpose") return (h.purpose || "").toLowerCase();
    if (key === "samples") return numOrNull(h.sample_count);
    if (key === "cpu_avg") return periodMetric(h, "cpu_percent", "avg");
    if (key === "cpu_p95") return periodMetric(h, "cpu_percent", "p95");
    if (key === "cpu_busy") return periodMetric(h, "cpu_percent", "busy_ratio");
    if (key === "mem_avg") return periodMetric(h, "mem_percent", "avg");
    if (key === "disk_avg") return periodMetric(h, "disk_percent", "avg");
    if (key === "accel_id") return (h.accel_summary || "").toLowerCase();
    if (key === "accel_avg") return periodMetric(h, "accel_util_avg", "avg");
    if (key === "accel_p95") return periodMetric(h, "accel_util_avg", "p95");
    if (key === "accel_busy") return periodMetric(h, "accel_util_avg", "busy_ratio");
    if (key === "rx_pct") return periodMetric(h, "net_rx_percent", "avg");
    if (key === "tx_pct") return periodMetric(h, "net_tx_percent", "avg");
    return null;
  }

  function accelCountOf(h) {
    if (!h) return 0;
    if (h.accel_count != null && h.accel_count !== "") {
      const n = Number(h.accel_count);
      if (!Number.isNaN(n)) return n;
    }
    return (h.npu_count || 0) + (h.gpu_count || 0);
  }

  function accelUtilOf(h) {
    if (!h) return null;
    if (h.accel_util_avg != null) return h.accel_util_avg;
    if (h.npu_util_avg != null) return h.npu_util_avg;
    return h.gpu_util_avg;
  }

  function accelSummaryOf(h) {
    return String((h && h.accel_summary) || "").trim();
  }

  function accelListInventory(cards) {
    const groups = {};
    const order = [];
    (cards || []).forEach((c) => {
      const vendor = String(
        (c && (c.vendor_label || c.vendor)) || "加速卡"
      ).trim();
      const name = String((c && c.name) || "").trim() || "GPU";
      const key = vendor + "\0" + name;
      if (!groups[key]) {
        groups[key] = { vendor: vendor, name: name, count: 0 };
        order.push(key);
      }
      groups[key].count += 1;
    });
    return order
      .map((k) => {
        const g = groups[k];
        return g.vendor + " ×" + g.count + " " + g.name;
      })
      .join(" · ");
  }

  function accelIdentityText(h) {
    const summary = accelSummaryOf(h);
    if (summary) return summary;
    const count = accelCountOf(h);
    return count ? "×" + count : "";
  }

  function accelIdentityCell(h) {
    const idText = accelIdentityText(h);
    if (!idText) return "-";
    return (
      '<div class="accel-id" title="' +
      escapeHtml(idText) +
      '">' +
      escapeHtml(idText) +
      "</div>"
    );
  }

  function accelUtilCell(h) {
    if (!accelCountOf(h)) return "-";
    return fmtPct(accelUtilOf(h));
  }

  function hostAddressCell(h) {
    // 完整地址由中心 resolve_host_address 给出（部署清单 / host_id 内嵌 IP
    // 优先于 Agent primary_ip，并跳过 198.18/15 等隧道假 IP）。
    const addr = String(h.address || "").trim();
    const name = String(h.hostname || "").trim();
    const hid = String(h.host_id || "").trim();
    const assetName = String((h.asset && h.asset.name) || "").trim();
    const primary = addr || name || hid || "-";
    const extras = [];
    if (name && name !== primary) extras.push(name);
    if (assetName && assetName !== primary && assetName !== name) extras.push(assetName);
    if (hid && hid !== primary && hid !== name && hid !== assetName) extras.push(hid);
    let html = '<div class="host-addr">' + escapeHtml(primary) + "</div>";
    if (extras.length) {
      html += '<div class="muted">' + escapeHtml(extras.join(" · ")) + "</div>";
    }
    return html;
  }

  function fillGroupFilter() {
    const sel = el.filterGroup;
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML =
      '<option value="">全部资源组</option>' +
      '<option value="__none__">未分组</option>' +
      resourceGroups
        .map(
          (g) =>
            '<option value="' +
            g.id +
            '">' +
            escapeHtml(g.name) +
            "</option>"
        )
        .join("");
    if (cur && Array.from(sel.options).some((o) => o.value === cur)) {
      sel.value = cur;
    }
  }

  function fillOwnerFilter() {
    const sel = el.filterOwner;
    if (!sel) return;
    const cur = sel.value;
    const seen = {};
    const owners = [];
    allHosts.forEach((h) => {
      const name = String(h.owner || "").trim();
      if (!name || seen[name]) return;
      seen[name] = true;
      owners.push(name);
    });
    owners.sort((a, b) => a.localeCompare(b, "zh"));
    sel.innerHTML =
      '<option value="">全部负责人</option>' +
      '<option value="__none__">未填负责人</option>' +
      owners
        .map(
          (name) =>
            '<option value="' +
            escapeHtml(name) +
            '">' +
            escapeHtml(name) +
            "</option>"
        )
        .join("");
    if (cur && Array.from(sel.options).some((o) => o.value === cur)) {
      sel.value = cur;
    }
  }

  function groupSelectHtml(h) {
    const cur = h.group_id == null ? "" : String(h.group_id);
    let html =
      '<select class="host-group-select" data-id="' +
      encodeURIComponent(h.host_id) +
      '"><option value="">未分组</option>';
    resourceGroups.forEach((g) => {
      html +=
        '<option value="' +
        g.id +
        '"' +
        (String(g.id) === cur ? " selected" : "") +
        ">" +
        escapeHtml(g.name) +
        "</option>";
    });
    html += "</select>";
    return html;
  }

  function renderGroupPanel() {
    if (!el.groupList) return;
    if (!resourceGroups.length) {
      el.groupList.innerHTML = '<span class="muted">暂无资源组，输入名称后点添加</span>';
      return;
    }
    el.groupList.innerHTML = resourceGroups
      .map(
        (g) =>
          '<span class="group-chip">' +
          escapeHtml(g.name) +
          ' <span class="muted">' +
          (g.host_count || 0) +
          "</span>" +
          '<button type="button" data-del-group="' +
          g.id +
          '" title="删除">删除</button></span>'
      )
      .join("");
    Array.from(el.groupList.querySelectorAll("[data-del-group]")).forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = Number(btn.getAttribute("data-del-group"));
        if (!id) return;
        deleteGroup(id);
      });
    });
  }

  async function loadGroups() {
    try {
      const res = await fetch("/api/v1/groups");
      const data = await res.json();
      resourceGroups = data.groups || [];
    } catch (e) {
      resourceGroups = [];
    }
    fillGroupFilter();
    renderGroupPanel();
  }

  async function createGroup() {
    const name = ((el.newGroupName && el.newGroupName.value) || "").trim();
    if (!name) return;
    try {
      const res = await fetch("/api/v1/groups", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name }),
      });
      const data = await res.json();
      if (!res.ok) {
        window.alert((data && data.error) || "添加失败");
        return;
      }
      if (el.newGroupName) el.newGroupName.value = "";
      await loadGroups();
      renderHosts(allHosts);
    } catch (e) {
      window.alert("添加失败：" + e.message);
    }
  }

  async function deleteGroup(groupId) {
    if (!window.confirm("删除该资源组？主机将变为未分组。")) return;
    try {
      const res = await fetch("/api/v1/groups/" + encodeURIComponent(groupId), {
        method: "DELETE",
      });
      if (!res.ok) {
        const data = await res.json();
        window.alert((data && data.error) || "删除失败");
        return;
      }
      if (el.filterGroup && el.filterGroup.value === String(groupId)) {
        el.filterGroup.value = "";
      }
      await loadGroups();
      const hostsRes = await fetch("/api/v1/hosts");
      const hostsData = await hostsRes.json();
      renderHosts(hostsData.hosts || []);
    } catch (e) {
      window.alert("删除失败：" + e.message);
    }
  }

  async function assignHostGroup(hostId, groupId) {
    try {
      const res = await fetch(
        "/api/v1/hosts/" + encodeURIComponent(hostId) + "/group",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            group_id: groupId === "" || groupId == null ? null : Number(groupId),
          }),
        }
      );
      const data = await res.json();
      if (!res.ok) {
        window.alert((data && data.error) || "分组失败");
        return;
      }
      allHosts.forEach((h) => {
        if (h.host_id === hostId) {
          h.group_id = data.group_id;
          h.group_name = data.group_name;
        }
      });
      await loadGroups();
      renderHosts(allHosts);
    } catch (e) {
      window.alert("分组失败：" + e.message);
    }
  }

  function statusLabel(s) {
    const map = {
      normal: "正常",
      warn: "偏高",
      critical: "异常",
      unknown: "未知",
    };
    return map[s] || s || "-";
  }

  function statusBadge(s) {
    const st = s || "unknown";
    return (
      '<span class="status-badge ' +
      escapeHtml(st) +
      '">' +
      statusLabel(st) +
      "</span>"
    );
  }

  function typeTag(t) {
    const map = { app: "cpu", npu: "gpu" };
    const nt = map[t] || t || "cpu";
    if (nt === "gpu") return '<span class="tag gpu">GPU</span>';
    if (nt === "auto") return '<span class="tag auto">auto</span>';
    return '<span class="tag cpu">CPU</span>';
  }

  /** 越高越危险：超 critical→红，超 warn→黄 */
  function levelHi(v, warn, crit) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "normal";
    const n = Number(v);
    if (crit != null && n >= Number(crit)) return "critical";
    if (warn != null && n >= Number(warn)) return "warn";
    return "normal";
  }

  function metricSpan(text, level, title) {
    const tip = title ? ' title="' + escapeHtml(title) + '"' : "";
    return (
      '<span class="metric ' +
      escapeHtml(level || "normal") +
      '"' +
      tip +
      ">" +
      text +
      "</span>"
    );
  }

  function loadPerCoreHigh(load, cores) {
    if (typeof UX.loadPerCoreHigh === "function") {
      return UX.loadPerCoreHigh(load, cores, thresholds.cpu_load_per_core_warn);
    }
    return false;
  }

  function hostLoadCell(h) {
    const l1 = loadPerCoreHigh(h.load1, h.cpu_count);
    const l5 = loadPerCoreHigh(h.load5, h.cpu_count);
    let title = "";
    if (l1 && l5) title = "load1 与 load5 相对核数达到阈值";
    else if (l1) title = "load1/核数达到阈值";
    else if (l5) title = "load5/核数达到阈值";
    return metricSpan(fmtNum(h.load1, 2), l1 || l5 ? "warn" : "normal", title);
  }

  function loadWindowSpan(value, cores) {
    const high = loadPerCoreHigh(value, cores);
    return metricSpan(
      fmtNum(value, 2),
      high ? "warn" : "normal",
      high ? "相对核数达到负载阈值" : ""
    );
  }

  function loadCoreRatio(load, cores) {
    if (load === null || load === undefined || load === "" || Number.isNaN(Number(load))) {
      return metricSpan("-", "normal");
    }
    const c = Number(cores);
    if (!c) return metricSpan("-", "normal");
    const ratio = Number(load) / c;
    const high = loadPerCoreHigh(load, cores);
    return metricSpan(ratio.toFixed(2), high ? "warn" : "normal");
  }

  function metricPct(v, warn, crit) {
    if (utilOutOfRange(v)) return metricSpan("无效", "bad");
    return metricSpan(fmtPct(v), levelHi(v, warn, crit));
  }

  function metricTemp(v, warn, crit) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) {
      return metricSpan("-", "normal");
    }
    return metricSpan(Number(v).toFixed(0) + "°C", levelHi(v, warn, crit));
  }

  function sparkline(values, color) {
    const nums = (values || [])
      .map((v) => (v === null || v === undefined ? null : Number(v)))
      .filter((v) => v !== null && !Number.isNaN(v));
    if (nums.length < 2) {
      return '<svg viewBox="0 0 100 40" preserveAspectRatio="none"><text x="8" y="24" fill="#8b9bb0" font-size="10">暂无足够历史点</text></svg>';
    }
    const min = Math.min(...nums);
    const max = Math.max(...nums);
    const span = max - min || 1;
    const pts = nums
      .map((v, i) => {
        const x = (i / (nums.length - 1)) * 100;
        const y = 36 - ((v - min) / span) * 28;
        return x.toFixed(2) + "," + y.toFixed(2);
      })
      .join(" ");
    return (
      '<svg viewBox="0 0 100 40" preserveAspectRatio="none">' +
      '<polyline fill="none" stroke="' +
      color +
      '" stroke-width="1.8" points="' +
      pts +
      '" /></svg>'
    );
  }

  function periodQuery(extra) {
    const params = [];
    if (periodMode === "custom" && periodFromTs && periodToTs) {
      params.push("from_ts=" + periodFromTs);
      params.push("to_ts=" + periodToTs);
    } else {
      params.push("minutes=" + detailMinutes);
    }
    params.push("busy_cpu=" + encodeURIComponent(busyCpu));
    params.push("busy_accel=" + encodeURIComponent(busyAccel));
    if (extra) params.push(extra);
    return params.join("&");
  }

  function toDatetimeLocal(ts) {
    if (!ts) return "";
    const d = new Date(Number(ts) * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return (
      d.getFullYear() +
      "-" +
      p(d.getMonth() + 1) +
      "-" +
      p(d.getDate()) +
      "T" +
      p(d.getHours()) +
      ":" +
      p(d.getMinutes())
    );
  }

  function fromDatetimeLocal(value) {
    if (!value) return null;
    const ms = new Date(value).getTime();
    if (Number.isNaN(ms)) return null;
    return Math.floor(ms / 1000);
  }

  function fmtBusy(v) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
    return (Number(v) * 100).toFixed(0) + "%";
  }

  function periodLabel(minutes) {
    const hit = PERIOD_OPTIONS.find((x) => x.minutes === minutes);
    return hit ? hit.label : minutes + " 分钟";
  }

  function currentWindowLabel(stats) {
    if (periodMode === "custom" && periodFromTs && periodToTs) {
      return "自定义 " + fmtTime(periodFromTs) + " ~ " + fmtTime(periodToTs);
    }
    return periodLabel(detailMinutes);
  }

  function fmtStatNum(v, suffix) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
    const n = Number(v);
    const text = Math.abs(n) >= 100 ? n.toFixed(0) : n.toFixed(1);
    return text + (suffix || "");
  }

  function hostById(id) {
    return allHosts.find((h) => h.host_id === id) || null;
  }

  function locationState() {
    return UX.parseLocationHash(location.hash);
  }

  function tabFromHash() {
    return locationState().tab;
  }

  function syncShellNav() {
    const parsed = locationState();
    const key =
      parsed.tab === "reports" ? "reports" : parsed.focus === "hosts" ? "hosts" : "overview";
    Array.from(document.querySelectorAll(".shell-link")).forEach((link) => {
      const on = link.getAttribute("data-shell") === key;
      link.classList.toggle("active", on);
      if (on) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  function replaceHash(hash) {
    if (location.hash === hash) return;
    if (history.replaceState) history.replaceState(null, "", hash);
    else location.hash = hash.replace(/^#/, "");
  }

  function applyTabVisibility(next) {
    currentTab = next === "period" || next === "reports" ? next : "realtime";
    if (el.viewRealtime) el.viewRealtime.hidden = currentTab !== "realtime";
    if (el.viewPeriod) el.viewPeriod.hidden = currentTab !== "period";
    if (el.viewReports) el.viewReports.hidden = currentTab !== "reports";
    [el.tabBtnRealtime, el.tabBtnPeriod, el.tabBtnReports].forEach((btn) => {
      if (!btn) return;
      const on = btn.getAttribute("data-tab") === currentTab;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  function setTab(name, opts) {
    const next = name === "period" || name === "reports" ? name : "realtime";
    const fromHash = opts && opts.fromHash;
    applyTabVisibility(next);
    if (!fromHash) {
      replaceHash("#" + next);
    }
    syncShellNav();
    if (next === "period") {
      loadPeriodUtil();
      if (selectedId) loadPeriodHostDetail(selectedId);
    } else if (next === "reports") {
      loadReportList();
      refreshReportWindowHint();
    } else if (selectedId) {
      loadDetail(selectedId);
    }
  }

  function renderPeriodButtons() {
    return PERIOD_OPTIONS.map((opt) => {
      const active =
        periodMode === "preset" && opt.minutes === detailMinutes ? " active" : "";
      return (
        '<button type="button" class="period-btn' +
        active +
        '" data-minutes="' +
        opt.minutes +
        '">' +
        opt.label +
        "</button>"
      );
    }).join("");
  }

  function renderPeriodControls() {
    if (!el.periodUtilControls) return;
    const fromVal = toDatetimeLocal(
      periodFromTs || Math.floor(Date.now() / 1000) - detailMinutes * 60
    );
    const toVal = toDatetimeLocal(periodToTs || Math.floor(Date.now() / 1000));
    el.periodUtilControls.innerHTML =
      renderPeriodButtons() +
      '<span class="period-custom">' +
      '<span class="muted">自定义</span>' +
      '<input id="periodFrom" type="datetime-local" value="' +
      fromVal +
      '" />' +
      '<span class="muted">至</span>' +
      '<input id="periodTo" type="datetime-local" value="' +
      toVal +
      '" />' +
      '<button type="button" class="period-btn" id="btnPeriodCustom">应用</button>' +
      "</span>" +
      '<span class="muted">繁忙阈值</span>' +
      '<label class="muted">CPU <input id="busyCpu" type="number" min="0" max="100" step="1" value="' +
      busyCpu +
      '" /></label>' +
      '<label class="muted">加速卡 <input id="busyAccel" type="number" min="0" max="100" step="1" value="' +
      busyAccel +
      '" /></label>' +
      '<a class="link" id="periodExport" href="/api/v1/export/period-stats.csv?' +
      periodQuery() +
      '">导出 CSV</a>';
    bindPeriodControls();
  }

  function bindPeriodControls() {
    Array.from(el.periodUtilControls.querySelectorAll(".period-btn[data-minutes]")).forEach(
      (btn) => {
        btn.addEventListener("click", () => {
          const m = Number(btn.getAttribute("data-minutes"));
          if (!m) return;
          periodMode = "preset";
          detailMinutes = m;
          periodFromTs = null;
          periodToTs = null;
          renderPeriodControls();
          reloadPeriodViews();
        });
      }
    );
    const apply = document.getElementById("btnPeriodCustom");
    if (apply) {
      apply.addEventListener("click", applyCustomPeriod);
    }
    ["periodFrom", "periodTo"].forEach((id) => {
      const input = document.getElementById(id);
      if (input) {
        input.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter") applyCustomPeriod();
        });
      }
    });
    const cpuInput = document.getElementById("busyCpu");
    const accelInput = document.getElementById("busyAccel");
    function applyBusy() {
      const c = Number(cpuInput && cpuInput.value);
      const a = Number(accelInput && accelInput.value);
      if (!Number.isNaN(c) && c >= 0) busyCpu = c;
      if (!Number.isNaN(a) && a >= 0) busyAccel = a;
      updatePeriodExportLink();
      reloadPeriodViews();
    }
    if (cpuInput) cpuInput.addEventListener("change", applyBusy);
    if (accelInput) accelInput.addEventListener("change", applyBusy);
  }

  function applyCustomPeriod() {
    const fromEl = document.getElementById("periodFrom");
    const toEl = document.getElementById("periodTo");
    const fromTs = fromDatetimeLocal(fromEl && fromEl.value);
    const toTs = fromDatetimeLocal(toEl && toEl.value);
    if (!fromTs || !toTs || fromTs >= toTs) {
      el.periodUtilBody.innerHTML =
        '<p class="muted">自定义时间范围无效：开始时间必须早于结束时间</p>';
      return;
    }
    periodMode = "custom";
    periodFromTs = fromTs;
    periodToTs = toTs;
    renderPeriodControls();
    reloadPeriodViews();
  }

  function reloadPeriodViews() {
    loadPeriodUtil();
    if (selectedId) loadPeriodHostDetail(selectedId);
  }

  const PERIOD_METRIC_ROWS = [
    ["CPU 利用率", "cpu_percent", "%"],
    ["内存利用率", "mem_percent", "%"],
    ["磁盘利用率", "disk_percent", "%"],
    ["负载 load1", "load1", ""],
    ["加速卡利用率", "accel_util_avg", "%"],
    ["加速卡最高温度", "accel_temp_max", "°C"],
    ["网络入向", "net_rx_mbps", " Mbps"],
    ["网络出向", "net_tx_mbps", " Mbps"],
    ["入向占额定", "net_rx_percent", "%"],
    ["出向占额定", "net_tx_percent", "%"],
    ["额定带宽", "net_rated_mbps", " Mbps"],
  ];

  function renderPeriodStatsTable(stats) {
    const metrics = (stats && stats.metrics) || {};
    const count = (stats && stats.sample_count) || 0;
    const win = (stats && stats.window) || {};
    let meta =
      '<p class="muted period-meta">样本 ' +
      count +
      " 点 · " +
      currentWindowLabel(stats);
    if (stats && stats.from_ts && stats.to_ts) {
      meta += " · 实际覆盖 " + fmtTime(stats.from_ts) + " ~ " + fmtTime(stats.to_ts);
    }
    if (win.clamped) {
      meta += " · 已按保留期裁剪";
    }
    const busy = (stats && stats.busy_thresholds) || {};
    if (busy.cpu_percent != null || busy.accel_util_avg != null) {
      meta +=
        " · 繁忙：CPU≥" +
        (busy.cpu_percent != null ? busy.cpu_percent : "-") +
        "% / 加速卡≥" +
        (busy.accel_util_avg != null ? busy.accel_util_avg : "-") +
        "%";
    }
    meta += "</p>";
    if (!count) {
      return meta + '<p class="muted">该时间段暂无历史上报，无法统计</p>';
    }
    let html =
      meta +
      '<table class="period-stats-table"><thead><tr>' +
      "<th>指标</th><th>平均</th><th>最低</th><th>最高</th><th>P95</th><th>繁忙占比</th>" +
      "</tr></thead><tbody>";
    PERIOD_METRIC_ROWS.forEach(([label, key, suffix]) => {
      const a = metrics[key] || {};
      html +=
        "<tr><td>" +
        label +
        "</td><td>" +
        fmtStatNum(a.avg, suffix) +
        "</td><td>" +
        fmtStatNum(a.min, suffix) +
        "</td><td>" +
        fmtStatNum(a.max, suffix) +
        "</td><td>" +
        fmtStatNum(a.p95, suffix) +
        "</td><td>" +
        fmtBusy(a.busy_ratio) +
        "</td></tr>";
    });
    html += "</tbody></table>";
    return html;
  }

  function updatePeriodExportLink() {
    const link = document.getElementById("periodExport");
    if (link) link.setAttribute("href", "/api/v1/export/period-stats.csv?" + periodQuery());
  }

  function renderPeriodUtil(data) {
    periodUtilData = data;
    if (!el.periodUtilBody) return;
    updatePeriodExportLink();
    if (!data || data.ok === false) {
      el.periodUtilBody.innerHTML =
        '<p class="muted">' +
        escapeHtml((data && data.error) || "时段统计加载失败") +
        "</p>";
      return;
    }
    const cluster = data.cluster || {};
    const hosts = data.hosts || [];
    let html = "";
    const clusterSummary =
      cluster.accel_summary || data.accel_summary || "";
    html +=
      '<p class="muted period-cluster-note">' +
      escapeHtml(data.rollup_note || "集群汇总按样本加权") +
      " · 主机 " +
      (data.hosts_with_samples || 0) +
      "/" +
      (data.host_count || 0) +
      " 有样本 · 共 " +
      (data.sample_count || 0) +
      " 点" +
      (clusterSummary
        ? " · 加速卡 " + escapeHtml(clusterSummary)
        : "") +
      "</p>";
    html += '<div class="section-title">集群汇总</div>';
    html += renderPeriodStatsTable({
      metrics: cluster.metrics,
      sample_count: cluster.sample_count || data.sample_count,
      from_ts: data.window && data.window.from_ts,
      to_ts: data.window && data.window.to_ts,
      window: data.window,
      busy_thresholds: data.busy_thresholds,
    });
    html += '<div class="section-title">各主机</div>';
    if (!hosts.length) {
      html += '<p class="muted">暂无主机</p>';
      el.periodUtilBody.innerHTML = html;
      return;
    }
    const sorted = hosts.slice().sort((a, b) =>
      cmpSort(periodSortValue(a, periodSortKey), periodSortValue(b, periodSortKey), periodSortDir)
    );
    html +=
      '<div class="table-wrap"><table class="period-hosts-table"><thead><tr>' +
      '<th class="sortable" data-sort="host">主机 / 地址</th>' +
      '<th class="sortable" data-sort="group">资源组</th>' +
      '<th class="sortable" data-sort="owner">负责人</th>' +
      '<th class="sortable" data-sort="purpose">用途</th>' +
      '<th class="sortable" data-sort="accel_id">加速卡</th>' +
      '<th class="sortable" data-sort="samples">样本</th>' +
      '<th class="sortable" data-sort="cpu_avg">CPU 均</th>' +
      '<th class="sortable" data-sort="cpu_p95">CPU P95</th>' +
      '<th class="sortable" data-sort="cpu_busy">CPU 繁忙</th>' +
      '<th class="sortable" data-sort="mem_avg">内存均</th>' +
      '<th class="sortable" data-sort="disk_avg">磁盘均</th>' +
      '<th class="sortable" data-sort="accel_avg">加速卡均</th>' +
      '<th class="sortable" data-sort="accel_p95">加速卡 P95</th>' +
      '<th class="sortable" data-sort="accel_busy">加速卡繁忙</th>' +
      '<th class="sortable" data-sort="rx_pct">入向占额定</th>' +
      '<th class="sortable" data-sort="tx_pct">出向占额定</th>' +
      "</tr></thead><tbody>";
    sorted.forEach((h) => {
      const m = h.metrics || {};
      const cpu = m.cpu_percent || {};
      const mem = m.mem_percent || {};
      const disk = m.disk_percent || {};
      const accel = m.accel_util_avg || {};
      const rxp = m.net_rx_percent || {};
      const txp = m.net_tx_percent || {};
      const live = hostById(h.host_id) || {};
      const row = Object.assign({}, h, {
        address: h.address || live.address,
        group_name: h.group_name || live.group_name,
        owner: h.owner || live.owner,
        purpose: h.purpose || live.purpose,
        accel_summary: h.accel_summary || live.accel_summary,
        accel_count: h.accel_count != null ? h.accel_count : live.accel_count,
      });
      const active = h.host_id === selectedId ? " active" : "";
      const accelId = accelSummaryOf(row) || (accelCountOf(row) ? "×" + accelCountOf(row) : "-");
      html +=
        '<tr data-id="' +
        encodeURIComponent(h.host_id) +
        '" class="' +
        active.trim() +
        '"><td>' +
        hostAddressCell(row) +
        "</td><td>" +
        escapeHtml(row.group_name || "未分组") +
        '</td><td class="col-clip" title="' +
        escapeHtml(row.owner || h.owner || "") +
        '">' +
        escapeHtml(row.owner || h.owner || "—") +
        '</td><td class="col-clip" title="' +
        escapeHtml(row.purpose || h.purpose || "") +
        '">' +
        escapeHtml(row.purpose || h.purpose || "—") +
        '</td><td class="accel-id-cell" title="' +
        escapeHtml(accelId) +
        '">' +
        escapeHtml(accelId) +
        "</td><td>" +
        (h.sample_count || 0) +
        "</td><td>" +
        fmtStatNum(cpu.avg, "%") +
        "</td><td>" +
        fmtStatNum(cpu.p95, "%") +
        "</td><td>" +
        fmtBusy(cpu.busy_ratio) +
        "</td><td>" +
        fmtStatNum(mem.avg, "%") +
        "</td><td>" +
        fmtStatNum(disk.avg, "%") +
        "</td><td>" +
        fmtStatNum(accel.avg, "%") +
        "</td><td>" +
        fmtStatNum(accel.p95, "%") +
        "</td><td>" +
        fmtBusy(accel.busy_ratio) +
        "</td><td>" +
        fmtStatNum(rxp.avg, "%") +
        "</td><td>" +
        fmtStatNum(txp.avg, "%") +
        "</td></tr>";
    });
    html += "</tbody></table></div>";
    el.periodUtilBody.innerHTML = html;
    const periodTable = el.periodUtilBody.querySelector(".period-hosts-table");
    syncSortHeaders(periodTable, periodSortKey, periodSortDir);
    if (periodTable) {
      Array.from(periodTable.querySelectorAll("thead th[data-sort]")).forEach((th) => {
        th.addEventListener("click", (ev) => {
          ev.stopPropagation();
          const next = applyHeaderSort(
            periodSortKey,
            periodSortDir,
            th.getAttribute("data-sort")
          );
          periodSortKey = next.key;
          periodSortDir = next.dir;
          if (periodUtilData) renderPeriodUtil(periodUtilData);
        });
      });
    }
    Array.from(el.periodUtilBody.querySelectorAll("tr[data-id]")).forEach((tr) => {
      tr.addEventListener("click", () => {
        selectedId = decodeURIComponent(tr.getAttribute("data-id"));
        loadPeriodHostDetail(selectedId);
        renderHosts(allHosts);
        Array.from(el.periodUtilBody.querySelectorAll("tr")).forEach((r) =>
          r.classList.remove("active")
        );
        tr.classList.add("active");
      });
    });
  }

  async function loadPeriodUtil() {
    if (!el.periodUtilBody) return;
    try {
      const res = await fetch("/api/v1/period-stats?" + periodQuery());
      const data = await res.json();
      renderPeriodUtil(data);
    } catch (e) {
      el.periodUtilBody.innerHTML =
        '<p class="muted">时段统计加载失败：' + escapeHtml(e.message) + "</p>";
    }
  }

  function renderPeriodCharts(pts) {
    pts = pts || [];
    let html =
      '<div class="section-title">' + currentWindowLabel() + " 趋势</div>";
    html += '<div class="charts">';
    html +=
      '<div class="chart-card"><div class="title">CPU %</div>' +
      sparkline(
        pts.map((x) => x.cpu_percent),
        "#3d9cfd"
      ) +
      "</div>";
    html +=
      '<div class="chart-card"><div class="title">加速卡利用率 %</div>' +
      sparkline(
        pts.map((x) => x.accel_util_avg || x.npu_util_avg || x.gpu_util_avg),
        "#c9a0ff"
      ) +
      "</div>";
    html +=
      '<div class="chart-card"><div class="title">加速卡最高温度 °C</div>' +
      sparkline(
        pts.map((x) => x.accel_temp_max || x.npu_temp_max || x.gpu_temp_max),
        "#e25c5c"
      ) +
      "</div>";
    html +=
      '<div class="chart-card"><div class="title">内存 %</div>' +
      sparkline(
        pts.map((x) => x.mem_percent),
        "#46c2b0"
      ) +
      "</div>";
    html +=
      '<div class="chart-card"><div class="title">网络入向 Mbps</div>' +
      sparkline(
        pts.map((x) => x.net_rx_mbps),
        "#7ec8e3"
      ) +
      "</div>";
    html +=
      '<div class="chart-card"><div class="title">网络出向 Mbps</div>' +
      sparkline(
        pts.map((x) => x.net_tx_mbps),
        "#e3c07e"
      ) +
      "</div>";
    html += "</div>";
    return html;
  }

  async function loadPeriodHostDetail(hostId) {
    if (!el.periodDetailBody) return;
    if (!hostId) {
      if (el.periodDetailTitle) el.periodDetailTitle.textContent = "请选择主机";
      el.periodDetailBody.innerHTML =
        '<p class="empty">点选左侧主机，查看该机时段指标与趋势</p>';
      return;
    }
    const host = hostById(hostId);
    if (el.periodDetailTitle) {
      el.periodDetailTitle.textContent =
        (host && (host.address || host.hostname || host.host_id)) || hostId;
    }
    let hist = { points: [] };
    let periodStats = null;
    let histMinutes = detailMinutes;
    if (periodMode === "custom" && periodFromTs && periodToTs) {
      histMinutes = Math.max(1, Math.ceil((periodToTs - periodFromTs) / 60));
    }
    const histLimit = Math.min(
      2000,
      Math.max(240, Math.ceil(histMinutes / 2))
    );
    try {
      const base = "/api/v1/hosts/" + encodeURIComponent(hostId);
      const [hr, sr] = await Promise.all([
        fetch(base + "/history?" + periodQuery("limit=" + histLimit)),
        fetch(base + "/period-stats?" + periodQuery()),
      ]);
      if (hr.ok) hist = await hr.json();
      if (sr.ok) periodStats = await sr.json();
    } catch (e) {
      el.periodDetailBody.innerHTML =
        '<p class="muted">时段统计加载失败：' + escapeHtml(e.message) + "</p>";
      return;
    }
    const pts = hist.points || [];
    let html = '<div class="section-title">时段指标</div>';
    const liveSummary =
      (periodStats && periodStats.accel_summary) ||
      (host && host.accel_summary) ||
      "";
    if (liveSummary) {
      html +=
        '<p class="accel-inv">' +
        escapeHtml(liveSummary) +
        ((periodStats && periodStats.accel_count)
          ? " · 共 " + periodStats.accel_count + " 张"
          : "") +
        "</p>";
    }
    html += renderPeriodStatsTable(periodStats);
    html += renderPeriodCharts(pts);
    el.periodDetailBody.innerHTML = html;
  }

  function hostDiskCell(h) {
    return metricPct(
      h.disk_percent,
      thresholds.disk_warn_percent,
      thresholds.disk_critical_percent
    );
  }

  function hostDiskCountCell(h) {
    const n = numOrNull(h.disk_count);
    if (n == null) return "-";
    return String(n);
  }

  function diskEntries(sys) {
    const list = Array.isArray(sys.disks) ? sys.disks.slice() : [];
    return list.filter((d) => d && d.mount);
  }

  function renderDiskBlock(sys) {
    const disks = diskEntries(sys).sort((a, b) => {
      const dp = (Number(b.percent) || 0) - (Number(a.percent) || 0);
      if (dp) return dp;
      return String(a.mount || "").localeCompare(String(b.mount || ""));
    });
    let html =
      '<div class="k">磁盘利用率</div><div class="v">' +
      metricPct(
        sys.disk_percent,
        thresholds.disk_warn_percent,
        thresholds.disk_critical_percent
      ) +
      " " +
      bar(sys.disk_percent) +
      "</div>";
    html +=
      '<div class="k">磁盘容量</div><div class="v">' +
      '<span class="rt-tag">已用</span> ' +
      fmtNum(sys.disk_used_gb, 2) +
      " GB " +
      '<span class="rt-tag">总量</span> ' +
      fmtNum(sys.disk_total_gb, 2) +
      " GB</div>";
    if (!disks.length) return html;
    html += '<div class="spec-block"><div class="spec-label">挂载</div><div class="disk-list">';
    html +=
      '<div class="disk-row disk-head"><span>挂载</span><span>容量</span><span>利用率</span></div>';
    disks.forEach((d) => {
      const sub = [d.device, d.fstype].filter(Boolean).join(" · ");
      html +=
        '<div class="disk-row"><div class="disk-mount">' +
        escapeHtml(d.mount || "-") +
        (sub ? '<div class="muted">' + escapeHtml(sub) + "</div>" : "") +
        '</div><div class="disk-cap">' +
        fmtNum(d.used_gb, 1) +
        "/" +
        fmtNum(d.total_gb, 1) +
        ' GB</div><div class="disk-usage">' +
        metricPct(
          d.percent,
          thresholds.disk_warn_percent,
          thresholds.disk_critical_percent
        ) +
        " " +
        bar(d.percent) +
        "</div></div>";
    });
    html += "</div></div>";
    return html;
  }

  function hostAttention(h) {
    return UX.attentionTags(h, thresholds);
  }

  function hostJumpButton(label, hostId) {
    if (!hostId) return escapeHtml(label);
    return (
      '<button type="button" class="host-jump" data-host-id="' +
      encodeURIComponent(hostId) +
      '">' +
      escapeHtml(label) +
      "</button>"
    );
  }

  function openHost(id) {
    if (!id) return;
    selectedId = id;
    replaceHash("#host=" + encodeURIComponent(id));
    if (currentTab !== "realtime") {
      setTab("realtime", { fromHash: true });
    } else {
      loadDetail(selectedId);
      syncShellNav();
    }
    renderHosts(allHosts);
    const row =
      el.hostBody &&
      el.hostBody.querySelector('tr[data-id="' + encodeURIComponent(id) + '"]');
    if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
  }

  function renderAttention(hosts) {
    if (!el.attentionList) return;
    const defs = [
      ["offline", "离线"],
      ["disk", "磁盘快满"],
      ["accel", "卡异常"],
      ["load", "高负载"],
    ];
    const buckets = { offline: [], disk: [], accel: [], load: [] };
    (hosts || []).forEach((h) => {
      hostAttention(h).forEach((tag) => {
        if (buckets[tag]) buckets[tag].push(h);
      });
    });
    el.attentionList.innerHTML = defs
      .map((pair) => {
        const key = pair[0];
        const label = pair[1];
        const on = attentionKey === key ? " active" : "";
        return (
          '<button type="button" class="attention-chip' +
          on +
          '" data-attention="' +
          key +
          '">' +
          label +
          '<span class="count">' +
          buckets[key].length +
          "</span></button>"
        );
      })
      .join("");
    const shown = attentionKey ? buckets[attentionKey] || [] : [];
    const names = [];
    const seen = {};
    shown.forEach((h) => {
      if (!h || seen[h.host_id]) return;
      seen[h.host_id] = true;
      names.push(h);
    });
    if (!el.attentionNames) return;
    if (!attentionKey) {
      el.attentionNames.innerHTML =
        '<span class="muted">点上面的数字可筛到主机表。点主机名打开详情。</span>';
      return;
    }
    if (!names.length) {
      el.attentionNames.innerHTML = '<span class="muted">这一类现在没有主机。</span>';
      return;
    }
    el.attentionNames.innerHTML = names
      .slice(0, 12)
      .map((h) => {
        const label = h.address || h.hostname || h.host_id;
        return (
          '<button type="button" class="attention-name" data-host-id="' +
          encodeURIComponent(h.host_id) +
          '">' +
          escapeHtml(label) +
          "</button>"
        );
      })
      .join("");
    if (names.length > 12) {
      el.attentionNames.innerHTML +=
        '<span class="muted">另有 ' + (names.length - 12) + " 台，用筛选看全表</span>";
    }
  }

  function renderAnomalySummary(data) {
    if (data && data.thresholds) {
      thresholds = Object.assign({}, thresholds, data.thresholds);
    }
    const s = (data && data.summary) || {};
    const current = el.filterStatus ? el.filterStatus.value : "";
    function anomalyPill(level, label, count) {
      const on = current === level ? " active" : "";
      return (
        '<button type="button" class="pill ' +
        level +
        on +
        '" data-anomaly="' +
        level +
        '">' +
        label +
        " " +
        (count || 0) +
        "</button>"
      );
    }
    el.anomalySummary.innerHTML =
      anomalyPill("normal", "正常", s.normal) +
      anomalyPill("warn", "偏高", s.warn) +
      anomalyPill("critical", "异常", s.critical) +
      anomalyPill("unknown", "未知/离线", s.unknown) +
      '<span class="muted threshold-hint">阈值：CPU≥' +
      (thresholds.cpu_warn_percent || 80) +
      "% 黄 / ≥" +
      (thresholds.cpu_critical_percent || 95) +
      "% 红；内存磁盘同理；负载 load1 或 load5 / 核 ≥" +
      (thresholds.cpu_load_per_core_warn || 2) +
      " 黄；加速卡温度≥" +
      (thresholds.accel_temp_warn_c || thresholds.npu_temp_warn_c || 85) +
      "°C 黄</span>";
  }

  function renderStats(s) {
    const cards = [
      ["主机总数", s.host_total],
      ["在线主机", s.host_online],
      ["在线 CPU 核", s.cpu_cores_online],
      ["加速卡数", s.accel_cards != null ? s.accel_cards : (s.npu_cards || 0) + (s.gpu_cards || 0)],
      ["平均 CPU", fmtPct(s.avg_cpu_percent)],
      [
        "平均加速卡利用率",
        fmtPct(
          s.avg_accel_util_percent != null
            ? s.avg_accel_util_percent
            : s.avg_npu_util_percent || s.avg_gpu_util_percent
        ),
      ],
    ];
    el.stats.innerHTML = cards
      .map(
        ([label, value]) =>
          '<div class="stat"><div class="label">' +
          label +
          '</div><div class="value">' +
          (value === null || value === undefined ? "-" : value) +
          "</div></div>"
      )
      .join("");
  }

  function normalizedType(t) {
    if (t === "app" || t === "npu") return t === "app" ? "cpu" : "gpu";
    return t || "cpu";
  }

  function filteredHosts() {
    const q = (el.filterQ.value || "").trim().toLowerCase();
    const t = el.filterType.value;
    const s = el.filterStatus.value;
    const g = el.filterGroup ? el.filterGroup.value : "";
    const ownerFilter = el.filterOwner ? el.filterOwner.value : "";
    return sortHostRows(
      allHosts.filter((h) => {
        const nt = normalizedType(h.host_type);
        if (t && nt !== t && h.host_type !== t) return false;
        const overall = (h.anomaly && h.anomaly.overall) || "unknown";
        if (s && overall !== s) return false;
        if (g === "__none__") {
          if (h.group_id != null && h.group_id !== "") return false;
        } else if (g) {
          if (String(h.group_id) !== String(g)) return false;
        }
        if (ownerFilter === "__none__") {
          if ((h.owner || "").trim()) return false;
        } else if (ownerFilter && (h.owner || "") !== ownerFilter) {
          return false;
        }
        if (attentionKey && hostAttention(h).indexOf(attentionKey) < 0) return false;
        if (!q) return true;
        const asset = h.asset || {};
        const hay = [
          h.hostname,
          h.host_id,
          h.address,
          h.group_name,
          h.owner,
          h.purpose,
          h.accel_summary,
          asset.name,
          asset.model,
          asset.notes,
          (h.previous_ips || [])
            .map((item) => (item && item.ip) || item || "")
            .join(" "),
          (h.ips || []).join(" "),
        ]
          .join(" ")
          .toLowerCase();
        return hay.indexOf(q) >= 0;
      }),
      hostSortKey,
      hostSortDir
    );
  }

  
  function offlineMetricCell() {
    return '<span class="muted" title="主机离线，不展示实时指标">—</span>';
  }

function renderHosts(hosts) {
    allHosts = hosts || [];
    renderAttention(allHosts);
    fillOwnerFilter();
    const view = filteredHosts();
    el.hostCount.textContent = "显示 " + view.length + " / " + allHosts.length;
    const hostTable = el.hostBody && el.hostBody.closest("table");
    syncSortHeaders(hostTable, hostSortKey, hostSortDir);
    if (!view.length) {
      const hint = attentionKey
        ? "今日关注里这一类没有主机"
        : "无匹配主机（可调整筛选或部署 Agent）";
      el.hostBody.innerHTML =
        '<tr><td colspan="15" class="muted">' + hint + "</td></tr>";
      return;
    }
    el.hostBody.innerHTML = view
      .map((h) => {
        const active = h.host_id === selectedId ? " active" : "";
        const overall = (h.anomaly && h.anomaly.overall) || "unknown";
        const cpuLevel = levelHi(
          h.cpu_percent,
          thresholds.cpu_warn_percent,
          thresholds.cpu_critical_percent
        );
        return (
          '<tr data-id="' +
          encodeURIComponent(h.host_id) +
          '" class="' +
          active.trim() +
          '">' +
          "<td><span class=\"dot " +
          (h.online ? "on" : "off") +
          '"></span>' +
          (h.online ? "在线" : "离线") +
          "</td>" +
          "<td>" +
          statusBadge(overall) +
          "</td>" +
          "<td>" +
          hostAddressCell(h) +
          "</td>" +
          "<td>" +
          groupSelectHtml(h) +
          "</td>" +
          '<td class="col-clip" title="' +
          escapeHtml(h.owner || "") +
          '">' +
          escapeHtml(h.owner || "—") +
          "</td>" +
          '<td class="col-clip" title="' +
          escapeHtml(h.purpose || "") +
          '">' +
          escapeHtml(h.purpose || "—") +
          "</td>" +
          "<td>" +
          typeTag(h.host_type) +
          "</td>" +
          "<td>" +
          (h.online
            ? metricSpan(fmtPct(h.cpu_percent), cpuLevel)
            : offlineMetricCell()) +
          "</td>" +
          "<td>" +
          (h.online
            ? metricPct(
                h.mem_percent,
                thresholds.mem_warn_percent,
                thresholds.mem_critical_percent
              )
            : offlineMetricCell()) +
          "</td>" +
          "<td>" +
          (h.online ? hostDiskCell(h) : offlineMetricCell()) +
          "</td>" +
          "<td>" +
          hostDiskCountCell(h) +
          "</td>" +
          "<td>" +
          accelIdentityCell(h) +
          "</td>" +
          "<td>" +
          (h.online ? accelUtilCell(h) : offlineMetricCell()) +
          "</td>" +
          "<td>" +
          (h.online ? hostLoadCell(h) : offlineMetricCell()) +
          "</td>" +
          "<td>" +
          fmtTime(h.last_seen) +
          "</td>" +
          "</tr>"
        );
      })
      .join("");

    Array.from(el.hostBody.querySelectorAll("tr[data-id]")).forEach((tr) => {
      tr.addEventListener("click", () => {
        openHost(decodeURIComponent(tr.getAttribute("data-id")));
      });
    });
    Array.from(el.hostBody.querySelectorAll(".host-group-select")).forEach((sel) => {
      sel.addEventListener("click", (ev) => ev.stopPropagation());
      sel.addEventListener("change", (ev) => {
        ev.stopPropagation();
        const hostId = decodeURIComponent(sel.getAttribute("data-id"));
        assignHostGroup(hostId, sel.value);
      });
    });
  }

  function renderDetail(data) {
    if (!data) {
      el.detailTitle.textContent = "请选择左侧主机";
      el.detailBody.innerHTML = '<p class="empty">暂无选中主机</p>';
      return;
    }
    const p = data.payload || {};
    const sys = p.system || {};
    const cards =
      p.accelerators && p.accelerators.length
        ? p.accelerators
        : [].concat(p.npus || [], p.gpus || []);
    const anomaly = data.anomaly || {};
    if (anomaly.thresholds) {
      thresholds = Object.assign({}, thresholds, anomaly.thresholds);
    }
    el.detailTitle.textContent =
      (data.address || data.hostname || data.host_id) +
      " · " +
      (data.online ? "在线" : "离线") +
      " · " +
      statusLabel(anomaly.overall);

    const accelStatus = (anomaly.accel || anomaly.npu || {}).status;

    let html = "";
    html += '<div class="section-title">资源异常判定（黄=偏高 / 红=异常）</div>';
    html +=
      "<div class=\"kv\"><div class=\"k\">总体</div><div class=\"v\">" +
      statusBadge(anomaly.overall) +
      '</div><div class="k">CPU</div><div class="v">' +
      statusBadge((anomaly.cpu || {}).status) +
      '</div><div class="k">内存/磁盘</div><div class="v">' +
      statusBadge((anomaly.system || {}).status || "normal") +
      '</div><div class="k">加速卡</div><div class="v">' +
      statusBadge(accelStatus || "normal") +
      "</div></div>";
    const findings = anomaly.findings || [];
    if (findings.length) {
      html +=
        '<ul class="finding-list">' +
        findings
          .map(
            (f) =>
              '<li class="finding ' +
              escapeHtml(f.level || "") +
              '">' +
              escapeHtml(f.message || "") +
              "</li>"
          )
          .join("") +
        "</ul>";
    } else {
      html += '<p class="muted">当前指标未见异常</p>';
    }
    html +=
      '<p class="muted"><a class="link" href="#period" id="gotoPeriodTab">查看该机时段统计</a></p>';

    html += renderAddressSection(data);
    html += renderAssetSection(data);

    html +=
      '<div class="section-title">CPU / 基础资源（' +
      (data.online ? "实时用量" : "末次快照，非实时") +
      " + 额定/总量）</div>";
    html += '<div class="kv">';
    html +=
      '<div class="k">地址</div><div class="v">' +
      escapeHtml(data.address || data.hostname || data.host_id || "-") +
      "</div>";
    html +=
      '<div class="k">主机名</div><div class="v">' +
      escapeHtml(data.hostname || "-") +
      "</div>";
    html +=
      '<div class="k">资源组</div><div class="v">' +
      escapeHtml(data.group_name || "未分组") +
      "</div>";
    html +=
      '<div class="k">CPU 利用率</div><div class="v">' +
      metricPct(
        sys.cpu_percent,
        thresholds.cpu_warn_percent,
        thresholds.cpu_critical_percent
      ) +
      " " +
      bar(sys.cpu_percent) +
      "</div>";
    html +=
      '<div class="k">CPU 核数</div><div class="v">' +
      (sys.cpu_count != null && sys.cpu_count !== "" ? sys.cpu_count : "-") +
      " 核</div>";
    if (sys.cpu_freq_mhz != null) {
      html +=
        '<div class="k">CPU 频率</div><div class="v">' +
        fmtNum(sys.cpu_freq_mhz, 0) +
        " MHz</div>";
    }
    if (sys.cpu_freq_max_mhz != null) {
      html +=
        '<div class="k">CPU 额定频率</div><div class="v">' +
        fmtNum(sys.cpu_freq_max_mhz, 0) +
        " MHz</div>";
    }
    if (sys.cpu_model) {
      html +=
        '<div class="k">处理器</div><div class="v">' +
        escapeHtml(sys.cpu_model) +
        "</div>";
    }
    html +=
      '<div class="k">架构 / 系统</div><div class="v">' +
      escapeHtml(sys.cpu_arch || "-") +
      " · " +
      escapeHtml(sys.os_name || "-") +
      "</div>";
    html +=
      '<div class="k">负载</div><div class="v">' +
      "1m=" +
      loadWindowSpan(sys.load1, sys.cpu_count) +
      " · 5m=" +
      loadWindowSpan(sys.load5, sys.cpu_count) +
      " · 15m=" +
      fmtNum(sys.load15, 2) +
      "</div>";
    if (sys.cpu_count) {
      html +=
        '<div class="k">负载/核数</div><div class="v">1m ' +
        loadCoreRatio(sys.load1, sys.cpu_count) +
        " · 5m " +
        loadCoreRatio(sys.load5, sys.cpu_count) +
        "（黄：≥ " +
        (thresholds.cpu_load_per_core_warn || 2) +
        "，与异常判定同一阈值）</div>";
    }
    html +=
      '<div class="k">内存利用率</div><div class="v">' +
      metricPct(
        sys.mem_percent,
        thresholds.mem_warn_percent,
        thresholds.mem_critical_percent
      ) +
      " " +
      bar(sys.mem_percent) +
      "</div>";
    html +=
      '<div class="k">内存容量</div><div class="v">' +
      '<span class="rt-tag">已用</span> ' +
      fmtNum(sys.mem_used_mb, 1) +
      " MB " +
      '<span class="rt-tag">总量</span> ' +
      fmtNum(sys.mem_total_mb, 1) +
      " MB</div>";
    html += renderDiskBlock(sys);
    const netUtil = Math.max(
      Number(sys.net_rx_percent) || 0,
      Number(sys.net_tx_percent) || 0
    );
    let netRated = sys.net_rated_mbps != null ? fmtMbps(sys.net_rated_mbps) + " Mbps" : "-";
    if (
      sys.net_link_mbps != null &&
      sys.net_rated_mbps != null &&
      Number(sys.net_link_mbps) !== Number(sys.net_rated_mbps)
    ) {
      netRated += "（主链路 " + fmtMbps(sys.net_link_mbps) + " Mbps）";
    }
    html +=
      '<div class="k">网络吞吐</div><div class="v">入 ' +
      fmtMbps(sys.net_rx_mbps) +
      " / 出 " +
      fmtMbps(sys.net_tx_mbps) +
      " Mbps</div>";
    html += '<div class="k">额定带宽</div><div class="v">' + netRated + "</div>";
    if (sys.net_rx_percent != null || sys.net_tx_percent != null) {
      html +=
        '<div class="k">网络利用率</div><div class="v">入 ' +
        fmtPct(sys.net_rx_percent) +
        " · 出 " +
        fmtPct(sys.net_tx_percent) +
        " " +
        bar(netUtil) +
        "</div>";
    }
    const ifaces = sys.net_ifaces || [];
    if (ifaces.length) {
      html +=
        '<div class="spec-block"><div class="spec-label">网卡</div>' +
        '<table class="iface-table"><thead><tr><th>名称</th><th>状态</th><th>速率</th></tr></thead><tbody>';
      ifaces.forEach((n) => {
        const spd =
          n.speed_mbps != null ? fmtMbps(n.speed_mbps) + " Mbps" : "速率未知";
        html +=
          "<tr><td>" +
          escapeHtml(n.name || "?") +
          "</td><td>" +
          (n.up ? "UP" : "DOWN") +
          "</td><td>" +
          spd +
          "</td></tr>";
      });
      html += "</tbody></table></div>";
    }
    html +=
      '<div class="k">运行时长</div><div class="v">' +
      fmtUptime(sys.uptime_sec) +
      "</div>";
    html +=
      '<div class="k">最近上报</div><div class="v">' +
      fmtTime(data.last_seen) +
      "</div>";
    html += "</div>";

    html +=
      '<div class="section-title">加速卡明细（英伟达 / AMD / 华为 / 寒武纪 / 瑞芯微 RKNN）</div>';
    if (!cards.length) {
      html +=
        '<p class="muted">未检测到加速卡（cpu 类型不采集；或本机无 nvidia-smi / rocm-smi / amd-smi / npu-smi / cnmon / rknpu）</p>';
    } else {
      const invText = accelSummaryOf(data) || accelListInventory(cards);
      if (invText) {
        html +=
          '<p class="accel-inv">' +
          escapeHtml(invText) +
          " · 共 " +
          cards.length +
          " 张</p>";
      }
      html +=
        '<div class="table-wrap"><table class="gpu-table"><thead><tr>' +
        "<th>#</th><th>厂商</th><th>名称</th><th>Health</th><th>核数</th><th>利用率</th><th>频率</th><th>额定频率</th><th>显存利用率</th><th>显存</th><th>温度</th><th>功耗</th><th>功耗上限</th>" +
        "</tr></thead><tbody>";
      const uw = thresholds.accel_util_warn_percent || thresholds.npu_util_warn_percent;
      const tw = thresholds.accel_temp_warn_c || thresholds.npu_temp_warn_c;
      const tc = thresholds.accel_temp_critical_c || thresholds.npu_temp_critical_c;
      const mw = thresholds.accel_mem_warn_percent || thresholds.npu_mem_warn_percent;
      const mc = thresholds.accel_mem_critical_percent || thresholds.npu_mem_critical_percent;
      cards.forEach((n) => {
        let memPct = n.mem_percent;
        if (
          (memPct === null || memPct === undefined) &&
          n.mem_used_mb != null &&
          n.mem_total_mb
        ) {
          memPct = (Number(n.mem_used_mb) * 100) / Number(n.mem_total_mb);
        }
        let coreUtil = "";
        if (n.cores && n.cores.length) {
          coreUtil =
            '<div class="muted">分核 ' +
            n.cores
              .map(
                (c) =>
                  "C" +
                  (c.index ?? "?") +
                  ":" +
                  (c.util_percent == null ? "-" : Number(c.util_percent).toFixed(0) + "%")
              )
              .join(" ") +
            "</div>";
        }
        const coreN =
          n.core_count != null && n.core_count !== ""
            ? n.core_count
            : n.cores && n.cores.length
              ? n.cores.length
              : null;
        const freqText =
          n.freq_mhz != null && !Number.isNaN(Number(n.freq_mhz))
            ? Number(n.freq_mhz).toFixed(0) + " MHz"
            : "-";
        const freqMaxText =
          n.freq_max_mhz != null && !Number.isNaN(Number(n.freq_max_mhz))
            ? Number(n.freq_max_mhz).toFixed(0) + " MHz"
            : "-";
        const memCap =
          n.mem_used_mb == null && n.mem_total_mb == null
            ? "-"
            : fmtNum(n.mem_used_mb, 0) + " / " + fmtNum(n.mem_total_mb, 0) + " MB";
        const powerText =
          n.power_w != null && !Number.isNaN(Number(n.power_w))
            ? fmtNum(n.power_w, 1) + " W"
            : "-";
        const powerLimitText =
          n.power_limit_w != null && !Number.isNaN(Number(n.power_limit_w))
            ? fmtNum(n.power_limit_w, 1) + " W"
            : "-";
        html +=
          "<tr><td>" +
          (n.index ?? "-") +
          "</td><td>" +
          escapeHtml(n.vendor_label || n.vendor || "-") +
          "</td><td>" +
          escapeHtml(n.name || "-") +
          (n.note
            ? '<div class="muted">' + escapeHtml(n.note) + "</div>"
            : "") +
          "</td><td>" +
          '<span title="原始健康：' +
          escapeHtml(n.health || "未上报") +
          '">' +
          escapeHtml(UX.accelHealthLabel(n.health, n.note)) +
          "</span>" +
          "</td><td>" +
          (coreN == null ? "-" : coreN) +
          "</td><td>" +
          metricPct(n.util_percent, uw, null) +
          " " +
          bar(n.util_percent) +
          coreUtil +
          "</td><td>" +
          freqText +
          "</td><td>" +
          freqMaxText +
          "</td><td>" +
          metricPct(memPct, mw, mc) +
          " " +
          bar(memPct) +
          "</td><td>" +
          memCap +
          "</td><td>" +
          metricTemp(n.temp_c, tw, tc) +
          "</td><td>" +
          powerText +
          "</td><td>" +
          powerLimitText +
          "</td></tr>";
      });
      html += "</tbody></table></div>";
    }

    el.detailBody.innerHTML = html;
    const gotoPeriod = document.getElementById("gotoPeriodTab");
    if (gotoPeriod) {
      gotoPeriod.addEventListener("click", (ev) => {
        ev.preventDefault();
        setTab("period");
      });
    }
    bindAssetActions(data);
    bindReaddress(data);
  }

  function renderAddressSection(data) {
    const prev = data.previous_ips || [];
    let hist = "无";
    if (prev.length) {
      hist = prev
        .map((item) => {
          const ip = (item && item.ip) || item || "";
          const extra = [item && item.date, item && item.note].filter(Boolean).join(" ");
          return extra ? ip + "（" + extra + "）" : String(ip);
        })
        .join("；");
    }
    return (
      '<div class="section-title">地址</div>' +
      '<div class="kv">' +
      '<div class="k">当前地址</div><div class="v">' +
      escapeHtml(data.address || "-") +
      '</div><div class="k">历史 IP</div><div class="v" id="previousIps">' +
      escapeHtml(hist) +
      '</div><div class="k">监控 ID</div><div class="v">' +
      escapeHtml(data.host_id || "") +
      "</div></div>" +
      '<div class="asset-actions">' +
      '<button type="button" class="btn btn-sm" id="btnReaddress">更换 IP</button>' +
      (data.readdress_undo
        ? ' <button type="button" class="btn btn-sm" id="btnReaddressUndo">撤销更换 IP</button>'
        : "") +
      '<span class="muted">旧 IP 留在历史里，监控 ID 和历史上报不改。新 IP 上的部署目标会改指向这台主机，旧 IP 目标停用但不删除。撤销会按当时记下的 host_id 和停用状态恢复，并清掉这次写入的手工地址和那一条历史 IP。</span>' +
      "</div>"
    );
  }

  function bindReaddress(data) {
    const btn = document.getElementById("btnReaddress");
    if (btn && data && data.host_id) {
      btn.addEventListener("click", () => readdressHost(data));
    }
    const undo = document.getElementById("btnReaddressUndo");
    if (undo && data && data.host_id) {
      undo.addEventListener("click", () => undoReaddress(data));
    }
  }

  async function undoReaddress(data) {
    const url = "/api/v1/hosts/" + encodeURIComponent(data.host_id) + "/readdress/undo";
    try {
      const previewRes = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ apply: false }),
      });
      const preview = await previewRes.json();
      if (!previewRes.ok || !preview.ok) {
        throw new Error((preview && preview.error) || "无法预览撤销");
      }
      const lines = [
        "撤销主机 " + preview.host_id + " 的上一次更换 IP。监控 ID 和历史上报不改。",
        "手工地址会清空。",
      ];
      if (preview.remove_previous_ip) {
        lines.push("去掉历史 IP：" + preview.remove_previous_ip);
      }
      (preview.targets || []).forEach((t) => {
        lines.push(
          "部署目标 #" +
            t.id +
            " " +
            (t.ip || "") +
            " host_id " +
            (t.host_id_from || "（空）") +
            " → " +
            (t.host_id_to || "（空）") +
            "，停用 " +
            (t.retired_from ? "是" : "否") +
            " → " +
            (t.retired_to ? "是" : "否")
        );
      });
      lines.push("确认撤销？");
      if (!window.confirm(lines.join("\n"))) return;
      const applyRes = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ apply: true }),
      });
      const applied = await applyRes.json();
      if (!applyRes.ok || !applied.ok) {
        throw new Error((applied && applied.error) || "撤销失败");
      }
      await refresh();
    } catch (e) {
      window.alert(e.message || "撤销失败");
    }
  }

  async function readdressHost(data) {
    const hostId = data.host_id;
    const current = data.address || "";
    const entered = window.prompt(
      "新的 IP 地址（当前 " + (current || "未知") + "）",
      ""
    );
    if (entered == null) return;
    const newIp = String(entered).trim();
    if (!newIp) return;
    const note = window.prompt("备注（可留空，例如同机改址）", "同机改址");
    if (note == null) return;
    const url = "/api/v1/hosts/" + encodeURIComponent(hostId) + "/readdress";
    const body = { new_ip: newIp, note: note };
    try {
      const previewRes = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ apply: false }, body)),
      });
      const preview = await previewRes.json();
      if (!previewRes.ok || !preview.ok) {
        throw new Error((preview && preview.error) || "无法预览");
      }
      const lines = [
        "主机 " + preview.host_id + " 的监控 ID 不变，历史上报不改。",
        "展示地址：" + (preview.old_ip || "（空）") + " → " + preview.new_ip,
      ];
      (preview.deploy_targets_retargeted || []).forEach((t) => {
        lines.push(
          "部署目标 #" +
            t.id +
            " " +
            t.ip +
            " 的 host_id：" +
            (t.host_id_from || "（空）") +
            " → " +
            t.host_id_to
        );
      });
      (preview.deploy_targets_retired || []).forEach((t) => {
        lines.push("部署目标 #" + t.id + " " + t.ip + " 将停用（保留，不删除）");
      });
      if (
        !(preview.deploy_targets_retargeted || []).length &&
        !(preview.deploy_targets_retired || []).length
      ) {
        lines.push("没有需要改动的部署目标。");
      }
      lines.push("确认执行？");
      if (!window.confirm(lines.join("\n"))) return;
      const applyRes = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ apply: true }, body)),
      });
      const applied = await applyRes.json();
      if (!applyRes.ok || !applied.ok) {
        throw new Error((applied && applied.error) || "更换失败");
      }
      await refresh();
    } catch (e) {
      window.alert(e.message || "更换失败");
    }
  }

  const ASSET_FIELDS = [
    ["owner", "负责人", "text"],
    ["purpose", "用途", "text"],
    ["name", "名称", "text"],
    ["category", "分类", "text"],
    ["vm_host", "宿主机", "text"],
    ["cpu", "CPU（手工）", "text"],
    ["memory", "内存（手工）", "text"],
    ["disk", "硬盘（手工）", "area"],
    ["gpu", "GPU（手工）", "area"],
    ["model", "型号 / 资产编号", "area"],
    ["notes", "备注", "area"],
  ];

  function assetValue(data, key) {
    const asset = (data && data.asset) || {};
    return asset[key] == null ? "" : String(asset[key]);
  }

  function fmtMemTotal(mb) {
    const n = Number(mb);
    if (!n || Number.isNaN(n)) return "-";
    if (n >= 1024) return (n / 1024).toFixed(1) + " GB";
    return n.toFixed(0) + " MB";
  }

  function renderCollectedHardware(hw) {
    hw = hw || {};
    const disks = hw.disks || [];
    let diskText = "-";
    if (disks.length) {
      diskText = disks
        .map((d) => {
          const mount = d.mount || "（挂载名为空）";
          const total =
            d.total_gb == null || d.total_gb === ""
              ? ""
              : " " + fmtNum(d.total_gb, 1) + " GB";
          return mount + total;
        })
        .join("；");
    } else if (hw.disk_total_gb != null) {
      diskText = fmtNum(hw.disk_total_gb, 1) + " GB";
      if (hw.disk_count) diskText += " · " + hw.disk_count + " 块";
    }
    const cpu =
      [hw.cpu_model || "", hw.cpu_count != null && hw.cpu_count !== "" ? hw.cpu_count + " 核" : ""]
        .filter(Boolean)
        .join(" · ") || "-";
    return (
      '<div class="kv">' +
      '<div class="k">CPU</div><div class="v">' +
      escapeHtml(cpu) +
      "</div>" +
      '<div class="k">内存</div><div class="v">' +
      escapeHtml(fmtMemTotal(hw.mem_total_mb)) +
      "</div>" +
      '<div class="k">硬盘</div><div class="v">' +
      escapeHtml(diskText) +
      "</div>" +
      '<div class="k">加速卡</div><div class="v">' +
      escapeHtml(hw.accel_summary || (hw.accel_count ? "×" + hw.accel_count : "无")) +
      "</div></div>"
    );
  }

  function renderAssetSection(data) {
    let fields = "";
    ASSET_FIELDS.forEach((spec) => {
      const key = spec[0];
      const label = spec[1];
      const kind = spec[2];
      const value = escapeHtml(assetValue(data, key));
      const control =
        kind === "area"
          ? '<textarea id="asset-' +
            key +
            '" rows="2">' +
            value +
            "</textarea>"
          : '<input id="asset-' + key + '" type="text" value="' + value + '" />';
      fields +=
        '<label class="asset-field"><span>' +
        escapeHtml(label) +
        "</span>" +
        control +
        "</label>";
    });
    const flag = assetValue(data, "monitor_flag");
    return (
      '<div class="section-title">资产信息（手工维护，Agent 不会覆盖）</div>' +
      '<div class="asset-grid">' +
      '<div><form id="assetForm" class="asset-form">' +
      fields +
      (flag
        ? '<p class="muted">表格「是否资源监控」：' + escapeHtml(flag) + "</p>"
        : "") +
      '<div class="asset-actions">' +
      '<button type="button" class="btn btn-sm" id="btnSaveAsset">保存</button>' +
      '<span class="muted" id="assetSaveHint"></span></div></form>' +
      '<div class="asset-actions asset-danger">' +
      '<button type="button" class="btn btn-sm" id="btnHideHost">从列表隐藏</button>' +
      '<label class="muted"><input type="checkbox" id="hideAlsoDeploy" /> 同时移除部署清单里对应的目标</label>' +
      '<button type="button" class="btn btn-sm btn-danger" id="btnDeleteHost">彻底删除…</button>' +
      "</div></div>" +
      '<div><div class="spec-label">采集规格（Agent 上报，只读）</div>' +
      renderCollectedHardware(data.hardware) +
      "</div></div>"
    );
  }

  function bindAssetActions(data) {
    const hostId = data && data.host_id;
    const save = document.getElementById("btnSaveAsset");
    if (save && hostId) {
      save.addEventListener("click", () => saveAsset(hostId));
    }
    const hide = document.getElementById("btnHideHost");
    if (hide && hostId) {
      hide.addEventListener("click", () => hideHost(hostId));
    }
    const del = document.getElementById("btnDeleteHost");
    if (del && hostId) {
      del.addEventListener("click", () => deleteHost(hostId, data));
    }
  }

  async function saveAsset(hostId) {
    const body = {};
    ASSET_FIELDS.forEach((spec) => {
      const node = document.getElementById("asset-" + spec[0]);
      body[spec[0]] = node ? node.value : "";
    });
    const hint = document.getElementById("assetSaveHint");
    try {
      const res = await fetch("/api/v1/hosts/" + encodeURIComponent(hostId) + "/meta", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = await res.json();
      if (!res.ok) throw new Error((payload && payload.error) || "保存失败");
      if (hint) hint.textContent = "已保存";
      await refresh();
    } catch (e) {
      if (hint) hint.textContent = e.message || "保存失败";
    }
  }

  async function hideHost(hostId) {
    if (
      !window.confirm(
        "从列表隐藏这台主机？它不再计入在线/离线统计和之后的诊断报告。历史上报保留，可以恢复。"
      )
    ) {
      return;
    }
    const also = document.getElementById("hideAlsoDeploy");
    try {
      const res = await fetch("/api/v1/hosts/" + encodeURIComponent(hostId) + "/hide", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          remove_deploy_target: !!(also && also.checked),
        }),
      });
      const payload = await res.json();
      if (!res.ok) throw new Error((payload && payload.error) || "隐藏失败");
      if (selectedId === hostId) selectedId = null;
      await refresh();
      if (el.hiddenPanel && !el.hiddenPanel.hidden) await loadHiddenHosts();
    } catch (e) {
      window.alert(e.message || "隐藏失败");
    }
  }

  async function deleteHost(hostId, data) {
    const label = (data && (data.address || data.hostname)) || hostId;
    if (
      !window.confirm(
        "彻底删除 " + label + " 及其历史上报？这一步不能恢复。"
      )
    ) {
      return;
    }
    const typed = window.prompt("输入 delete 确认硬删除");
    if (String(typed || "").trim().toLowerCase() !== "delete") return;
    const also = document.getElementById("hideAlsoDeploy");
    let url = "/api/v1/hosts/" + encodeURIComponent(hostId) + "?confirm=delete";
    if (also && also.checked) url += "&remove_deploy_target=1";
    try {
      const res = await fetch(url, { method: "DELETE" });
      const payload = await res.json();
      if (!res.ok) throw new Error((payload && payload.error) || "删除失败");
      if (selectedId === hostId) selectedId = null;
      await refresh();
    } catch (e) {
      window.alert(e.message || "删除失败");
    }
  }

  async function loadHiddenHosts() {
    if (!el.hiddenList) return;
    try {
      const res = await fetch("/api/v1/hosts?hidden=1");
      const data = await res.json();
      const hosts = data.hosts || [];
      if (!hosts.length) {
        el.hiddenList.innerHTML = '<p class="muted">没有已隐藏的主机</p>';
        return;
      }
      el.hiddenList.innerHTML = hosts
        .map((h) => {
          return (
            '<div class="hidden-row">' +
            "<div><strong>" +
            escapeHtml(h.address || h.hostname || h.host_id) +
            "</strong> " +
            '<span class="muted">' +
            escapeHtml(h.host_id) +
            " · " +
            escapeHtml(h.owner || "未填负责人") +
            " · " +
            escapeHtml(h.purpose || "未填用途") +
            "</span></div>" +
            '<button type="button" class="btn btn-sm" data-restore="' +
            encodeURIComponent(h.host_id) +
            '">恢复</button></div>'
          );
        })
        .join("");
      Array.from(el.hiddenList.querySelectorAll("[data-restore]")).forEach((btn) => {
        btn.addEventListener("click", () => {
          restoreHost(decodeURIComponent(btn.getAttribute("data-restore")));
        });
      });
    } catch (e) {
      el.hiddenList.innerHTML =
        '<p class="muted">加载失败：' + escapeHtml(e.message) + "</p>";
    }
  }

  async function restoreHost(hostId) {
    try {
      const res = await fetch(
        "/api/v1/hosts/" + encodeURIComponent(hostId) + "/restore",
        { method: "POST" }
      );
      const payload = await res.json();
      if (!res.ok) throw new Error((payload && payload.error) || "恢复失败");
      await loadHiddenHosts();
      await refresh();
    } catch (e) {
      window.alert(e.message || "恢复失败");
    }
  }

  async function postImport(apply) {
    const file = el.importFile && el.importFile.files && el.importFile.files[0];
    if (!file) {
      window.alert("先选择 CSV 或 xlsx");
      return;
    }
    const buf = await file.arrayBuffer();
    const url = "/api/v1/hosts/meta/import" + (apply ? "?apply=1" : "");
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: buf,
    });
    const payload = await res.json();
    if (!res.ok) {
      throw new Error((payload && payload.error) || "导入失败");
    }
    return payload;
  }

  function renderImportResult(payload) {
    if (!el.importResult) return;
    const summary = (payload && payload.summary) || {};
    const ignored = (payload && payload.ignored_columns) || [];
    let html =
      "<p>匹配 " +
      (summary.matched || 0) +
      "，将更新 " +
      (summary.will_update != null ? summary.will_update : summary.updated || 0) +
      "，未匹配 " +
      (summary.unmatched || 0) +
      "，多台命中 " +
      (summary.ambiguous || 0) +
      "，跳过 " +
      (summary.skipped || 0) +
      "。</p>";
    if (ignored.length) {
      html +=
        '<p class="muted">已忽略凭据列：' +
        escapeHtml(ignored.join("、")) +
        "（不会入库）</p>";
    }
    const rows = []
      .concat(payload.matched || [])
      .concat(payload.unmatched || [])
      .concat(payload.ambiguous || [])
      .concat(payload.skipped || []);
    if (rows.length) {
      html +=
        '<div class="table-wrap"><table class="import-table"><thead><tr>' +
        "<th>行</th><th>IP</th><th>主机</th><th>结果</th><th>负责人</th><th>用途</th>" +
        "</tr></thead><tbody>";
      rows.forEach((row) => {
        let state = "未匹配";
        if (row.host_id && row.skipped_apply) state = "同一 IP 的后续行会覆盖";
        else if (row.host_id) state = row.hidden ? "匹配（当前已隐藏）" : "匹配";
        if (row.host_ids) state = "多台主机，不写入";
        if (!row.ip && row.reason) state = row.reason;
        if (row.reason && !row.host_id && !row.host_ids) state = row.reason;
        const patch = row.patch || {};
        html +=
          "<tr><td>" +
          (row.row || "") +
          "</td><td>" +
          escapeHtml(row.ip || "") +
          "</td><td>" +
          escapeHtml(row.address || row.host_id || "") +
          "</td><td>" +
          escapeHtml(state) +
          "</td><td>" +
          escapeHtml(patch.owner || "") +
          "</td><td>" +
          escapeHtml(patch.purpose || "") +
          "</td></tr>";
      });
      html += "</tbody></table></div>";
    }
    if (payload.apply && payload.applied) {
      html +=
        "<p>已写入 " + (payload.applied.updated || []).length + " 台。</p>";
    }
    el.importResult.innerHTML = html;
  }

  async function loadDetail(hostId) {
    if (!hostId) {
      renderDetail(null);
      return;
    }
    try {
      const res = await fetch("/api/v1/hosts/" + encodeURIComponent(hostId));
      if (!res.ok) throw new Error("详情请求失败");
      const data = await res.json();
      renderDetail(data);
    } catch (e) {
      el.detailBody.innerHTML =
        '<p class="empty">加载详情失败：' + escapeHtml(e.message) + "</p>";
    }
  }

  async function refresh() {
    try {
      const [statsRes, hostsRes, anomalyRes] = await Promise.all([
        fetch("/api/v1/stats"),
        fetch("/api/v1/hosts"),
        fetch("/api/v1/anomaly"),
        loadGroups(),
      ]);
      renderStats(await statsRes.json());
      renderAnomalySummary(await anomalyRes.json());
      const hostsData = await hostsRes.json();
      const hosts = hostsData.hosts || [];
      const wanted = locationState().hostId;
      if (wanted && hosts.some((h) => h.host_id === wanted)) {
        selectedId = wanted;
      } else if (!selectedId && hosts.length) {
        selectedId = hosts[0].host_id;
      }
      renderHosts(hosts);
      if (currentTab === "period") {
        await loadPeriodUtil();
        if (selectedId) await loadPeriodHostDetail(selectedId);
      } else if (currentTab === "reports") {
        // 诊断预览来自已入库报告，定时刷新不重算、不打断阅读
      } else if (selectedId) {
        const editing =
          document.activeElement &&
          document.activeElement.closest &&
          document.activeElement.closest("#assetForm");
        if (!editing) await loadDetail(selectedId);
      }
    } catch (e) {
      console.error(e);
    }
  }

  function fmtReportTime(ts) {
    if (ts == null || Number.isNaN(Number(ts))) return "-";
    try {
      return new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(new Date(Number(ts) * 1000));
    } catch (e) {
      return String(ts);
    }
  }

  function shanghaiYMD(offsetDays) {
    const shifted = new Date(Date.now() + 8 * 3600 * 1000 + (offsetDays || 0) * 86400000);
    return shifted.toISOString().slice(0, 10);
  }

  function defaultReportWindowDate(period) {
    if (period === "month") {
      const today = shanghaiYMD(0).split("-").map(Number);
      return new Date(Date.UTC(today[0], today[1] - 2, 1)).toISOString().slice(0, 10);
    }
    if (period === "week") {
      const today = shanghaiYMD(0).split("-").map(Number);
      const utc = new Date(Date.UTC(today[0], today[1] - 1, today[2]));
      const sinceMon = (utc.getUTCDay() + 6) % 7;
      const start = new Date(Date.UTC(today[0], today[1] - 1, today[2] - sinceMon - 7));
      return start.toISOString().slice(0, 10);
    }
    return shanghaiYMD(-1);
  }

  function reportSourceLabel(source) {
    return source === "scheduled" ? "定时" : "手动";
  }

  function renderReportDocument(report) {
    if (!el.reportPreview) return;
    activeReport = report || null;
    const sections = (report && report.payload && report.payload.sections) || [];
    const failed = report && report.status === "failed";
    let html = "<h3>" + escapeHtml(report.title || "使用诊断") + "</h3>";
    html +=
      '<p class="muted">' +
      escapeHtml(reportSourceLabel(report.source)) +
      " · " +
      (failed ? "失败" : "成功") +
      " · 实际生成 " +
      escapeHtml(fmtReportTime(report.generated_at || report.created_at)) +
      "</p>";
    if (failed && report.error) {
      html += '<p class="empty">失败原因：' + escapeHtml(report.error) + "</p>";
    }
    if (!sections.length && report.markdown) {
      html += "<pre>" + escapeHtml(report.markdown) + "</pre>";
    }
    sections.forEach((sec) => {
      html += "<h3>" + escapeHtml(sec.title || "") + "</h3>";
      (sec.paragraphs || []).forEach((p) => {
        html += "<p>" + escapeHtml(p) + "</p>";
      });
      (sec.tables || []).forEach((table) => {
        if (table.caption) {
          html += '<p class="report-caption">' + escapeHtml(table.caption) + "</p>";
        }
        const headers = table.headers || [];
        html += "<div class='table-wrap'><table><thead><tr>";
        headers.forEach((h) => {
          html += "<th>" + escapeHtml(h) + "</th>";
        });
        html += "</tr></thead><tbody>";
        const hostIds = table.host_ids || [];
        (table.rows || []).forEach((row, rowIndex) => {
          const hostId = hostIds[rowIndex] || "";
          html += "<tr>";
          headers.forEach((_, i) => {
            const cell = row[i] == null ? "" : row[i];
            html +=
              "<td>" +
              (i === 0 ? hostJumpButton(cell, hostId) : escapeHtml(cell)) +
              "</td>";
          });
          html += "</tr>";
        });
        html += "</tbody></table></div>";
      });
      (sec.notes || []).forEach((n) => {
        html += '<p class="muted">' + escapeHtml(n) + "</p>";
      });
    });
    el.reportPreview.innerHTML = html;
    Array.from(el.reportPreview.querySelectorAll(".host-jump")).forEach((btn) => {
      btn.addEventListener("click", () => {
        openHost(decodeURIComponent(btn.getAttribute("data-host-id") || ""));
      });
    });
    if (el.reportActions) {
      const id = report.id;
      el.reportActions.innerHTML =
        '<button type="button" class="btn" id="btnRegenReport">重新生成</button>' +
        '<a class="link" href="/api/v1/reports/' +
        id +
        '/markdown">下载 Markdown</a>' +
        '<a class="link" href="/api/v1/reports/' +
        id +
        '/rankings.csv">下载排行 CSV</a>';
      const regen = document.getElementById("btnRegenReport");
      if (regen) {
        regen.addEventListener("click", () => {
          const from = Number(report.requested_from_ts);
          const windowDate = Number.isNaN(from)
            ? el.reportWindowDate && el.reportWindowDate.value
            : new Date(from * 1000 + 8 * 3600 * 1000).toISOString().slice(0, 10);
          generateReport({
            period: report.period_type || reportPeriod,
            windowDate: windowDate,
          });
        });
      }
    }
  }

  function renderReportList(reports) {
    if (!el.reportList) return;
    if (!reports.length) {
      el.reportList.innerHTML =
        '<p class="muted">还没有报告。选择周期后点「生成并保存」。</p>';
      return;
    }
    el.reportList.innerHTML = reports
      .map((r) => {
        const active = Number(r.id) === Number(activeReportId) ? " active" : "";
        const clamp = r.clamped ? " · 已按保留期裁剪" : "";
        const failed = r.status === "failed";
        const badge =
          '<span class="report-badge' +
          (failed ? " bad" : "") +
          '">' +
          escapeHtml(reportSourceLabel(r.source)) +
          " · " +
          (failed ? "失败" : "成功") +
          "</span>";
        const reason = failed && r.error ? " · " + r.error : "";
        return (
          '<button type="button" class="report-item' +
          active +
          '" data-report-id="' +
          r.id +
          '"><strong>' +
          escapeHtml(r.title || r.period_type || "报告") +
          "</strong>" +
          badge +
          '<span class="muted">生成于 ' +
          escapeHtml(fmtReportTime(r.generated_at || r.created_at)) +
          clamp +
          escapeHtml(reason) +
          "</span></button>"
        );
      })
      .join("");
    Array.from(el.reportList.querySelectorAll("[data-report-id]")).forEach((btn) => {
      btn.addEventListener("click", () => {
        loadReport(btn.getAttribute("data-report-id"));
      });
    });
  }

  async function loadReportList() {
    if (!el.reportList) return;
    try {
      const res = await fetch("/api/v1/reports");
      const data = await res.json();
      if (!res.ok) throw new Error((data && data.error) || "列表失败");
      renderReportList(data.reports || []);
    } catch (e) {
      el.reportList.innerHTML =
        '<p class="empty">报告列表加载失败：' + escapeHtml(e.message) + "</p>";
    }
  }

  async function loadReport(id) {
    activeReportId = id;
    if (!el.reportPreview) return;
    el.reportPreview.innerHTML = '<p class="muted">读取已保存报告…</p>';
    try {
      const res = await fetch("/api/v1/reports/" + encodeURIComponent(id));
      const data = await res.json();
      if (!res.ok) throw new Error((data && data.error) || "读取失败");
      renderReportDocument(data.report || {});
      const listed = el.reportList
        ? Array.from(el.reportList.querySelectorAll("[data-report-id]"))
        : [];
      listed.forEach((btn) => {
        btn.classList.toggle("active", btn.getAttribute("data-report-id") === String(id));
      });
    } catch (e) {
      el.reportPreview.innerHTML =
        '<p class="empty">读取报告失败：' + escapeHtml(e.message) + "</p>";
    }
  }

  async function refreshReportWindowHint() {
    if (!el.reportWindowHint) return;
    const date =
      el.reportWindowDate && el.reportWindowDate.value
        ? el.reportWindowDate.value
        : "";
    try {
      let url =
        "/api/v1/reports/preview?period=" + encodeURIComponent(reportPeriod);
      if (date) url += "&window_date=" + encodeURIComponent(date);
      const res = await fetch(url);
      const data = await res.json();
      if (!res.ok) throw new Error((data && data.error) || "窗口预览失败");
      let text = (data.label || "") + " " + (data.range_text || "");
      if (data.clamped) text += "（将被保留期裁剪）";
      if (data.window_error) text += "（" + data.window_error + "）";
      el.reportWindowHint.textContent = text;
    } catch (e) {
      el.reportWindowHint.textContent = e.message || "";
    }
  }

  async function generateReport(opts) {
    if (!el.btnGenerateReport) return;
    const period = (opts && opts.period) || reportPeriod;
    const windowDate =
      (opts && opts.windowDate) ||
      (el.reportWindowDate && el.reportWindowDate.value) ||
      "";
    el.btnGenerateReport.disabled = true;
    const regen = document.getElementById("btnRegenReport");
    if (regen) regen.disabled = true;
    if (el.reportPreview) {
      el.reportPreview.innerHTML = "<p class=\"muted\">正在按原始历史样本计算并入库…</p>";
    }
    try {
      const body = { period: period };
      if (windowDate) body.window_date = windowDate;
      const res = await fetch("/api/v1/reports/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      const report = (data && data.report) || {};
      if (report.id) {
        activeReportId = report.id;
        await loadReportList();
        await loadReport(report.id);
      }
      if (!res.ok && !report.id) {
        throw new Error((data && data.error) || "生成失败");
      }
    } catch (e) {
      if (el.reportPreview) {
        el.reportPreview.innerHTML =
          '<p class="empty">生成失败：' + escapeHtml(e.message) + "</p>";
      }
    } finally {
      el.btnGenerateReport.disabled = false;
    }
  }

  el.btnRefresh.addEventListener("click", refresh);
  el.filterQ.addEventListener("input", () => renderHosts(allHosts));
  el.filterType.addEventListener("change", () => renderHosts(allHosts));
  el.filterStatus.addEventListener("change", () => renderHosts(allHosts));
  if (el.filterGroup) {
    el.filterGroup.addEventListener("change", () => renderHosts(allHosts));
  }
  if (el.filterOwner) {
    el.filterOwner.addEventListener("change", () => renderHosts(allHosts));
  }
  if (el.btnHidden && el.hiddenPanel) {
    el.btnHidden.addEventListener("click", () => {
      el.hiddenPanel.hidden = !el.hiddenPanel.hidden;
      if (!el.hiddenPanel.hidden) loadHiddenHosts();
    });
  }
  if (el.btnImport && el.importPanel) {
    el.btnImport.addEventListener("click", () => {
      el.importPanel.hidden = !el.importPanel.hidden;
    });
  }
  if (el.btnImportPreview) {
    el.btnImportPreview.addEventListener("click", async () => {
      try {
        const payload = await postImport(false);
        renderImportResult(payload);
        if (el.btnImportApply) {
          el.btnImportApply.disabled = !((payload.summary || {}).will_update > 0);
        }
      } catch (e) {
        window.alert(e.message || "预览失败");
      }
    });
  }
  if (el.btnImportApply) {
    el.btnImportApply.addEventListener("click", async () => {
      try {
        const payload = await postImport(true);
        renderImportResult(payload);
        el.btnImportApply.disabled = true;
        await refresh();
      } catch (e) {
        window.alert(e.message || "写入失败");
      }
    });
  }
  if (el.importFile && el.btnImportApply) {
    el.importFile.addEventListener("change", () => {
      el.btnImportApply.disabled = true;
      if (el.importResult) el.importResult.innerHTML = "";
    });
  }
  if (el.btnGroups && el.groupPanel) {
    el.btnGroups.addEventListener("click", () => {
      el.groupPanel.hidden = !el.groupPanel.hidden;
    });
  }
  if (el.btnAddGroup) {
    el.btnAddGroup.addEventListener("click", createGroup);
  }
  if (el.newGroupName) {
    el.newGroupName.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") createGroup();
    });
  }
  const hostTable = document.getElementById("hostTable");
  if (hostTable) {
    Array.from(hostTable.querySelectorAll("thead th[data-sort]")).forEach((th) => {
      th.addEventListener("click", () => {
        const next = applyHeaderSort(
          hostSortKey,
          hostSortDir,
          th.getAttribute("data-sort")
        );
        hostSortKey = next.key;
        hostSortDir = next.dir;
        renderHosts(allHosts);
      });
    });
  }
  [el.tabBtnRealtime, el.tabBtnPeriod, el.tabBtnReports].forEach((btn) => {
    if (!btn) return;
    btn.addEventListener("click", () => setTab(btn.getAttribute("data-tab")));
  });
  Array.from(document.querySelectorAll("[data-report-period]")).forEach((btn) => {
    btn.addEventListener("click", () => {
      reportPeriod = btn.getAttribute("data-report-period") || "day";
      Array.from(document.querySelectorAll("[data-report-period]")).forEach((b) => {
        b.classList.toggle("active", b === btn);
      });
      if (el.reportWindowDate) {
        el.reportWindowDate.value = defaultReportWindowDate(reportPeriod);
      }
      refreshReportWindowHint();
    });
  });
  if (el.reportWindowDate) {
    el.reportWindowDate.value = defaultReportWindowDate(reportPeriod);
    el.reportWindowDate.addEventListener("change", refreshReportWindowHint);
  }
  if (el.btnGenerateReport) {
    el.btnGenerateReport.addEventListener("click", () => generateReport());
  }
  if (el.anomalySummary) {
    el.anomalySummary.addEventListener("click", (ev) => {
      const btn = ev.target.closest && ev.target.closest("button[data-anomaly]");
      if (!btn || !el.filterStatus) return;
      const level = btn.getAttribute("data-anomaly") || "";
      el.filterStatus.value = el.filterStatus.value === level ? "" : level;
      attentionKey = "";
      renderHosts(allHosts);
      Array.from(el.anomalySummary.querySelectorAll("button[data-anomaly]")).forEach((item) => {
        item.classList.toggle("active", item.getAttribute("data-anomaly") === el.filterStatus.value);
      });
    });
  }
  const attentionRoot = document.getElementById("attention");
  if (attentionRoot) {
    attentionRoot.addEventListener("click", (ev) => {
      const chip = ev.target.closest && ev.target.closest("button[data-attention]");
      const nameBtn = ev.target.closest && ev.target.closest(".attention-name");
      if (chip) {
        const key = chip.getAttribute("data-attention") || "";
        attentionKey = attentionKey === key ? "" : key;
        if (attentionKey && el.filterStatus) {
          el.filterStatus.value = "";
          if (el.anomalySummary) {
            Array.from(el.anomalySummary.querySelectorAll("button[data-anomaly]")).forEach(
              (item) => item.classList.remove("active")
            );
          }
        }
        renderHosts(allHosts);
        return;
      }
      if (nameBtn) {
        openHost(decodeURIComponent(nameBtn.getAttribute("data-host-id") || ""));
      }
    });
  }
  window.addEventListener("hashchange", () => {
    const parsed = locationState();
    if (parsed.tab !== currentTab) setTab(parsed.tab, { fromHash: true });
    else syncShellNav();
    if (parsed.hostId && parsed.hostId !== selectedId) {
      selectedId = parsed.hostId;
      renderHosts(allHosts);
      if (currentTab === "realtime") loadDetail(selectedId);
    }
    if (parsed.focus === "hosts") {
      const table = document.getElementById("hostTable");
      if (table && table.scrollIntoView) table.scrollIntoView({ block: "start" });
    }
  });
  renderPeriodControls();
  applyTabVisibility(tabFromHash());
  syncShellNav();
  if (tabFromHash() === "reports") {
    loadReportList();
    refreshReportWindowHint();
  }
  if (locationState().focus === "hosts") {
    const table = document.getElementById("hostTable");
    if (table && table.scrollIntoView) table.scrollIntoView({ block: "start" });
  }
  if (typeof UX.applyHealthVersion === "function") UX.applyHealthVersion();
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
