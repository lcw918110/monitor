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
  };

  const el = {
    anomalySummary: document.getElementById("anomalySummary"),
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
    btnGroups: document.getElementById("btnGroups"),
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
    btnGenerateReport: document.getElementById("btnGenerateReport"),
  };
  let reportPeriod = "day";
  let activeReportId = null;

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

  function bar(pct) {
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
    const primary = addr || name || hid || "-";
    const extras = [];
    if (name && name !== primary) extras.push(name);
    if (hid && hid !== primary && hid !== name) extras.push(hid);
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

  function metricSpan(text, level) {
    return (
      '<span class="metric ' +
      escapeHtml(level || "normal") +
      '">' +
      text +
      "</span>"
    );
  }

  function metricPct(v, warn, crit) {
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

  function tabFromHash() {
    if (location.hash === "#period") return "period";
    if (location.hash === "#reports") return "reports";
    return "realtime";
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
      const hash = "#" + next;
      if (location.hash !== hash) {
        if (history.replaceState) {
          history.replaceState(null, "", hash);
        } else {
          location.hash = next;
        }
      }
    }
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

  function renderAnomalySummary(data) {
    if (data && data.thresholds) {
      thresholds = Object.assign({}, thresholds, data.thresholds);
    }
    const s = (data && data.summary) || {};
    el.anomalySummary.innerHTML =
      '<span class="pill normal">正常 ' +
      (s.normal || 0) +
      '</span><span class="pill warn">偏高 ' +
      (s.warn || 0) +
      '</span><span class="pill critical">异常 ' +
      (s.critical || 0) +
      '</span><span class="pill unknown">未知/离线 ' +
      (s.unknown || 0) +
      '</span><span class="muted threshold-hint">阈值：CPU≥' +
      (thresholds.cpu_warn_percent || 80) +
      "% 黄 / ≥" +
      (thresholds.cpu_critical_percent || 95) +
      "% 红；内存磁盘同理；加速卡温度≥" +
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
        if (!q) return true;
        const hay = [
          h.hostname,
          h.host_id,
          h.address,
          h.group_name,
          h.accel_summary,
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
    const view = filteredHosts();
    el.hostCount.textContent = "显示 " + view.length + " / " + allHosts.length;
    const hostTable = el.hostBody && el.hostBody.closest("table");
    syncSortHeaders(hostTable, hostSortKey, hostSortDir);
    if (!view.length) {
      el.hostBody.innerHTML =
        '<tr><td colspan="13" class="muted">无匹配主机（可调整筛选或部署 Agent）</td></tr>';
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
          (h.online ? fmtNum(h.load1, 2) : offlineMetricCell()) +
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
        selectedId = decodeURIComponent(tr.getAttribute("data-id"));
        loadDetail(selectedId);
        Array.from(el.hostBody.querySelectorAll("tr")).forEach((r) =>
          r.classList.remove("active")
        );
        tr.classList.add("active");
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
      (sys.load1 ?? "-") +
      " · 5m=" +
      (sys.load5 ?? "-") +
      " · 15m=" +
      (sys.load15 ?? "-") +
      "</div>";
    if (sys.cpu_count) {
      const l1 = Number(sys.load1);
      if (!Number.isNaN(l1)) {
        html +=
          '<div class="k">负载占核数</div><div class="v">' +
          fmtPct((l1 / Number(sys.cpu_count)) * 100) +
          "</div>";
      }
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
          "</td><td>" +
          escapeHtml(n.health || "-") +
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
      if (!selectedId && hosts.length) {
        selectedId = hosts[0].host_id;
      }
      renderHosts(hosts);
      if (currentTab === "period") {
        await loadPeriodUtil();
        if (selectedId) await loadPeriodHostDetail(selectedId);
      } else if (currentTab === "reports") {
        // 诊断预览来自已入库报告，定时刷新不重算、不打断阅读
      } else if (selectedId) {
        await loadDetail(selectedId);
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

  function renderReportDocument(report) {
    if (!el.reportPreview) return;
    const sections = (report && report.payload && report.payload.sections) || [];
    let html = "<h3>" + escapeHtml(report.title || "使用诊断") + "</h3>";
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
        (table.rows || []).forEach((row) => {
          html += "<tr>";
          headers.forEach((_, i) => {
            html += "<td>" + escapeHtml(row[i] == null ? "" : row[i]) + "</td>";
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
    if (el.reportActions) {
      const id = report.id;
      el.reportActions.innerHTML =
        '<a class="link" href="/api/v1/reports/' +
        id +
        '/markdown">下载 Markdown</a>' +
        '<a class="link" href="/api/v1/reports/' +
        id +
        '/rankings.csv">下载排行 CSV</a>';
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
        return (
          '<button type="button" class="report-item' +
          active +
          '" data-report-id="' +
          r.id +
          '"><strong>' +
          escapeHtml(r.title || r.period_type || "报告") +
          '</strong><span class="muted">保存于 ' +
          escapeHtml(fmtReportTime(r.created_at)) +
          clamp +
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
    try {
      const res = await fetch(
        "/api/v1/reports/preview?period=" + encodeURIComponent(reportPeriod)
      );
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

  async function generateReport() {
    if (!el.btnGenerateReport) return;
    el.btnGenerateReport.disabled = true;
    if (el.reportPreview) {
      el.reportPreview.innerHTML = "<p class=\"muted\">正在按原始历史样本计算并入库…</p>";
    }
    try {
      const res = await fetch("/api/v1/reports/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ period: reportPeriod }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error((data && data.error) || "生成失败");
      const report = data.report || {};
      activeReportId = report.id;
      await loadReportList();
      if (report.id) await loadReport(report.id);
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
      refreshReportWindowHint();
    });
  });
  if (el.btnGenerateReport) {
    el.btnGenerateReport.addEventListener("click", generateReport);
  }
  window.addEventListener("hashchange", () => {
    const name = tabFromHash();
    if (name !== currentTab) setTab(name, { fromHash: true });
  });
  renderPeriodControls();
  applyTabVisibility(tabFromHash());
  if (tabFromHash() === "reports") {
    loadReportList();
    refreshReportWindowHint();
  }
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
