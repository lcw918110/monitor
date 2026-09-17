(() => {
  const REFRESH_MS = 15000;
  let selectedId = null;
  let allHosts = [];
  /** 时段利用时间窗（与详情统计共用） */
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
    periodUtilControls: document.getElementById("periodUtilControls"),
    periodUtilBody: document.getElementById("periodUtilBody"),
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

  function rtRated(realtimeText, ratedText) {
    return (
      '<span class="rt-tag">实时</span> ' +
      realtimeText +
      ' <span class="rt-tag">额定</span> ' +
      ratedText
    );
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

  function renderPeriodPicker() {
    return (
      '<div class="period-bar">' +
      '<span class="muted">统计时间段（与上方「时段利用」相同）</span>' +
      renderPeriodButtons() +
      "</div>"
    );
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
          loadPeriodUtil();
          if (selectedId) loadDetail(selectedId);
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
      loadPeriodUtil();
      if (selectedId) loadDetail(selectedId);
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
    loadPeriodUtil();
    if (selectedId) loadDetail(selectedId);
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

  function bindPeriodPicker(hostId) {
    Array.from(el.detailBody.querySelectorAll(".period-btn")).forEach((btn) => {
      btn.addEventListener("click", () => {
        const m = Number(btn.getAttribute("data-minutes"));
        if (!m) return;
        periodMode = "preset";
        detailMinutes = m;
        periodFromTs = null;
        periodToTs = null;
        renderPeriodControls();
        loadDetail(hostId);
        loadPeriodUtil();
      });
    });
  }

  function updatePeriodExportLink() {
    const link = document.getElementById("periodExport");
    if (link) link.setAttribute("href", "/api/v1/export/period-stats.csv?" + periodQuery());
  }

  function renderPeriodUtil(data) {
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
    html +=
      '<p class="muted period-cluster-note">' +
      escapeHtml(data.rollup_note || "集群汇总按样本加权") +
      " · 主机 " +
      (data.hosts_with_samples || 0) +
      "/" +
      (data.host_count || 0) +
      " 有样本 · 共 " +
      (data.sample_count || 0) +
      " 点</p>";
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
    html +=
      '<div class="table-wrap"><table class="period-hosts-table"><thead><tr>' +
      "<th>主机</th><th>样本</th><th>CPU 均</th><th>CPU P95</th><th>CPU 繁忙</th>" +
      "<th>内存均</th><th>磁盘均</th><th>加速卡均</th><th>加速卡 P95</th><th>加速卡繁忙</th>" +
      "<th>入向占额定</th><th>出向占额定</th>" +
      "</tr></thead><tbody>";
    hosts.forEach((h) => {
      const m = h.metrics || {};
      const cpu = m.cpu_percent || {};
      const mem = m.mem_percent || {};
      const disk = m.disk_percent || {};
      const accel = m.accel_util_avg || {};
      const rxp = m.net_rx_percent || {};
      const txp = m.net_tx_percent || {};
      const active = h.host_id === selectedId ? " active" : "";
      html +=
        '<tr data-id="' +
        encodeURIComponent(h.host_id) +
        '" class="' +
        active.trim() +
        '"><td>' +
        escapeHtml(h.hostname || h.host_id) +
        '<div class="muted">' +
        escapeHtml(h.host_id) +
        "</div></td><td>" +
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
    Array.from(el.periodUtilBody.querySelectorAll("tr[data-id]")).forEach((tr) => {
      tr.addEventListener("click", () => {
        selectedId = decodeURIComponent(tr.getAttribute("data-id"));
        loadDetail(selectedId);
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

  function hostDiskCell(h) {
    const pct = metricPct(
      h.disk_percent,
      thresholds.disk_warn_percent,
      thresholds.disk_critical_percent
    );
    const n = Number(h.disk_count);
    if (n > 1) {
      return pct + ' <span class="muted">· ' + n + "盘</span>";
    }
    return pct;
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
    const count = disks.length;
    const maxDisk = disks[0];
    let usageText = rtRated(
      fmtNum(sys.disk_used_gb, 2) + " GB",
      fmtNum(sys.disk_total_gb, 2) + " GB"
    );
    let extra = "";
    if (count > 1) {
      usageText =
        "合计 " +
        fmtNum(sys.disk_used_gb, 2) +
        " / " +
        fmtNum(sys.disk_total_gb, 2) +
        " GB";
      const mnt = maxDisk && maxDisk.mount ? maxDisk.mount : "";
      extra =
        ' <span class="muted">' +
        (mnt ? escapeHtml(mnt) + " · " : "") +
        count +
        " 挂载</span>";
    }
    let html =
      '<div class="k">磁盘</div><div class="v">' +
      usageText +
      " " +
      metricPct(
        sys.disk_percent,
        thresholds.disk_warn_percent,
        thresholds.disk_critical_percent
      ) +
      extra +
      " " +
      bar(sys.disk_percent);
    if (count > 1) {
      html +=
        '<div class="table-wrap disk-wrap"><table class="disk-table"><thead><tr>' +
        "<th>挂载</th><th>已用 / 总量</th><th>利用率</th></tr></thead><tbody>";
      disks.forEach((d) => {
        const sub = [d.device, d.fstype].filter(Boolean).join(" · ");
        html +=
          "<tr><td>" +
          escapeHtml(d.mount || "-") +
          (sub ? '<div class="muted">' + escapeHtml(sub) + "</div>" : "") +
          "</td><td>" +
          fmtNum(d.used_gb, 2) +
          " / " +
          fmtNum(d.total_gb, 2) +
          " GB</td><td>" +
          metricPct(
            d.percent,
            thresholds.disk_warn_percent,
            thresholds.disk_critical_percent
          ) +
          " " +
          bar(d.percent) +
          "</td></tr>";
      });
      html += "</tbody></table></div>";
    }
    html += "</div>";
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
      ["加速卡数", (s.npu_cards || 0) + (s.gpu_cards || 0)],
      ["平均 CPU", fmtPct(s.avg_cpu_percent)],
      ["平均加速卡利用率", fmtPct(s.avg_npu_util_percent || s.avg_gpu_util_percent)],
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
    return allHosts.filter((h) => {
      const nt = normalizedType(h.host_type);
      if (t && nt !== t && h.host_type !== t) return false;
      const overall = (h.anomaly && h.anomaly.overall) || "unknown";
      if (s && overall !== s) return false;
      if (!q) return true;
      const hay = ((h.hostname || "") + " " + (h.host_id || "")).toLowerCase();
      return hay.indexOf(q) >= 0;
    });
  }

  function renderHosts(hosts) {
    allHosts = hosts || [];
    const view = filteredHosts();
    el.hostCount.textContent = "显示 " + view.length + " / " + allHosts.length;
    if (!view.length) {
      el.hostBody.innerHTML =
        '<tr><td colspan="8" class="muted">无匹配主机（可调整筛选或部署 Agent）</td></tr>';
      return;
    }
    el.hostBody.innerHTML = view
      .map((h) => {
        const active = h.host_id === selectedId ? " active" : "";
        const overall = (h.anomaly && h.anomaly.overall) || "unknown";
        const cardCount = (h.npu_count || 0) + (h.gpu_count || 0);
        const cardText =
          cardCount > 0
            ? cardCount + " 卡 / " + fmtPct(h.npu_util_avg || h.gpu_util_avg)
            : "-";
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
          escapeHtml(h.hostname || h.host_id) +
          '<div class="muted">' +
          escapeHtml(h.host_id) +
          "</div></td>" +
          "<td>" +
          typeTag(h.host_type) +
          "</td>" +
          "<td>" +
          metricSpan(
            "实时 " +
              fmtPct(h.cpu_percent) +
              (h.cpu_count ? " / 额定 " + h.cpu_count + "核" : ""),
            cpuLevel
          ) +
          "</td>" +
          "<td>" +
          cardText +
          "</td>" +
          "<td>" +
          hostDiskCell(h) +
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
  }

  async function renderDetail(data) {
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
      (data.hostname || data.host_id) +
      " · " +
      (data.online ? "在线" : "离线") +
      " · " +
      statusLabel(anomaly.overall);

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
      const base =
        "/api/v1/hosts/" + encodeURIComponent(data.host_id);
      const [hr, sr] = await Promise.all([
        fetch(
          base +
            "/history?" +
            periodQuery("limit=" + histLimit)
        ),
        fetch(base + "/period-stats?" + periodQuery()),
      ]);
      if (hr.ok) hist = await hr.json();
      if (sr.ok) periodStats = await sr.json();
    } catch (e) {
      /* ignore */
    }
    const pts = hist.points || [];
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

    html += '<div class="section-title">时段利用</div>';
    html += renderPeriodPicker();
    html += renderPeriodStatsTable(periodStats);

    html +=
      '<div class="section-title">' +
      currentWindowLabel(periodStats) +
      " 趋势</div>";
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
      '<div class="chart-card"><div class="title">内存 %（实时/总量）</div>' +
      sparkline(
        pts.map((x) => x.mem_percent),
        "#46c2b0"
      ) +
      "</div>";
    html +=
      '<div class="chart-card"><div class="title">网络入向 Mbps（实时）</div>' +
      sparkline(
        pts.map((x) => x.net_rx_mbps),
        "#7ec8e3"
      ) +
      "</div>";
    html +=
      '<div class="chart-card"><div class="title">网络出向 Mbps（实时）</div>' +
      sparkline(
        pts.map((x) => x.net_tx_mbps),
        "#e3c07e"
      ) +
      "</div>";
    html += "</div>";

    html += '<div class="section-title">CPU / 基础资源（实时用量 + 额定/总量）</div>';
    html += '<div class="kv">';
    const cpuFreqRt =
      sys.cpu_freq_mhz != null
        ? " · " + fmtNum(sys.cpu_freq_mhz, 0) + " MHz"
        : "";
    const cpuFreqRated =
      sys.cpu_freq_max_mhz != null
        ? " · " + fmtNum(sys.cpu_freq_max_mhz, 0) + " MHz"
        : "";
    html +=
      '<div class="k">CPU</div><div class="v">' +
      rtRated(
        metricPct(
          sys.cpu_percent,
          thresholds.cpu_warn_percent,
          thresholds.cpu_critical_percent
        ) + cpuFreqRt,
        (sys.cpu_count ?? "-") + " 核" + cpuFreqRated
      ) +
      " " +
      bar(sys.cpu_percent) +
      "</div>";
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
    let loadRated = "";
    if (sys.cpu_count) {
      const l1 = Number(sys.load1);
      if (!Number.isNaN(l1)) {
        loadRated =
          " （相对额定 " +
          sys.cpu_count +
          " 核约 " +
          fmtPct((l1 / Number(sys.cpu_count)) * 100) +
          "）";
      } else {
        loadRated = " （额定 " + sys.cpu_count + " 核）";
      }
    }
    html +=
      '<div class="k">负载</div><div class="v">' +
      '<span class="rt-tag">实时</span> 1m=' +
      (sys.load1 ?? "-") +
      " · 5m=" +
      (sys.load5 ?? "-") +
      " · 15m=" +
      (sys.load15 ?? "-") +
      loadRated +
      "</div>";
    html +=
      '<div class="k">内存</div><div class="v">' +
      rtRated(
        fmtNum(sys.mem_used_mb, 1) + " MB",
        fmtNum(sys.mem_total_mb, 1) + " MB"
      ) +
      " " +
      metricPct(
        sys.mem_percent,
        thresholds.mem_warn_percent,
        thresholds.mem_critical_percent
      ) +
      " " +
      bar(sys.mem_percent) +
      "</div>";
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
      '<div class="k">网络</div><div class="v">' +
      rtRated(
        "入 " +
          fmtMbps(sys.net_rx_mbps) +
          " / 出 " +
          fmtMbps(sys.net_tx_mbps) +
          " Mbps",
        netRated
      );
    if (sys.net_rx_percent != null || sys.net_tx_percent != null) {
      html +=
        " 利用率 入 " +
        fmtPct(sys.net_rx_percent) +
        " · 出 " +
        fmtPct(sys.net_tx_percent);
    }
    html += " " + bar(netUtil);
    const ifaces = sys.net_ifaces || [];
    if (ifaces.length) {
      html +=
        '<div class="iface-list">' +
        ifaces
          .map((n) => {
            const spd =
              n.speed_mbps != null ? fmtMbps(n.speed_mbps) + " Mbps" : "速率未知";
            return (
              escapeHtml(n.name || "?") +
              " " +
              (n.up ? "UP" : "DOWN") +
              " " +
              spd
            );
          })
          .join(" · ") +
        "</div>";
    }
    html += "</div>";
    html +=
      '<div class="k">运行时长</div><div class="v">' +
      fmtUptime(sys.uptime_sec) +
      "</div>";
    html +=
      '<div class="k">最近上报</div><div class="v">' +
      fmtTime(data.last_seen) +
      "</div>";
    html += "</div>";

    html += '<div class="section-title">加速卡明细（英伟达 / 华为 / 寒武纪 / 瑞芯微 RKNN）</div>';
    if (!cards.length) {
      html +=
        '<p class="muted">未检测到加速卡（cpu 类型不采集；或本机无 nvidia-smi / npu-smi / cnmon / rknpu）</p>';
    } else {
      html +=
        '<div class="table-wrap"><table class="gpu-table"><thead><tr>' +
        "<th>#</th><th>厂商</th><th>名称</th><th>Health</th><th>利用率</th><th>内存</th><th>温度</th><th>功耗</th>" +
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
        let utilExtra = "";
        if (n.cores && n.cores.length) {
          utilExtra =
            " <span class=\"muted\">(" +
            n.cores
              .map(
                (c) =>
                  "C" +
                  (c.index ?? "?") +
                  ":" +
                  (c.util_percent == null ? "-" : Number(c.util_percent).toFixed(0) + "%")
              )
              .join(" ") +
            ")</span>";
        }
        if (n.core_count) {
          utilExtra +=
            ' <span class="muted">额定 ' + n.core_count + " 核</span>";
        }
        if (n.freq_mhz != null || n.freq_max_mhz != null) {
          utilExtra +=
            ' <span class="muted">' +
            (n.freq_mhz != null
              ? "实时 " + Number(n.freq_mhz).toFixed(0) + " MHz"
              : "") +
            (n.freq_max_mhz != null
              ? " / 额定 " + Number(n.freq_max_mhz).toFixed(0) + " MHz"
              : "") +
            "</span>";
        }
        let powerText = "-";
        if (n.power_w != null || n.power_limit_w != null) {
          if (n.power_limit_w != null) {
            powerText =
              "实时 " +
              (n.power_w != null ? fmtNum(n.power_w, 1) : "-") +
              " / 额定 " +
              fmtNum(n.power_limit_w, 1) +
              " W";
          } else {
            powerText = fmtNum(n.power_w, 1) + " W";
          }
        }
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
          metricPct(n.util_percent, uw, null) +
          utilExtra +
          " " +
          bar(n.util_percent) +
          "</td><td>" +
          metricPct(memPct, mw, mc) +
          " " +
          rtRated(
            fmtNum(n.mem_used_mb, 0) + " MB",
            fmtNum(n.mem_total_mb, 0) + " MB"
          ) +
          "</td><td>" +
          metricTemp(n.temp_c, tw, tc) +
          "</td><td>" +
          powerText +
          "</td></tr>";
      });
      html += "</tbody></table></div>";
    }

    el.detailBody.innerHTML = html;
    bindPeriodPicker(data.host_id);
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
      await renderDetail(data);
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
      ]);
      renderStats(await statsRes.json());
      renderAnomalySummary(await anomalyRes.json());
      const hostsData = await hostsRes.json();
      renderHosts(hostsData.hosts || []);
      await loadPeriodUtil();
      if (selectedId) {
        await loadDetail(selectedId);
      } else if ((hostsData.hosts || []).length) {
        selectedId = hostsData.hosts[0].host_id;
        await loadDetail(selectedId);
      }
    } catch (e) {
      console.error(e);
    }
  }

  el.btnRefresh.addEventListener("click", refresh);
  el.filterQ.addEventListener("input", () => renderHosts(allHosts));
  el.filterType.addEventListener("change", () => renderHosts(allHosts));
  el.filterStatus.addEventListener("change", () => renderHosts(allHosts));
  renderPeriodControls();
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
