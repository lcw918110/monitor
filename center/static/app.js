(() => {
  const REFRESH_MS = 15000;
  let selectedId = null;
  let allHosts = [];
  /** 主机详情时间段（分钟） */
  let detailMinutes = 120;
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
  };

  function fmtPct(v) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
    return Number(v).toFixed(1) + "%";
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

  function periodLabel(minutes) {
    const hit = PERIOD_OPTIONS.find((x) => x.minutes === minutes);
    return hit ? hit.label : minutes + " 分钟";
  }

  function fmtStatNum(v, suffix) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
    const n = Number(v);
    const text = Math.abs(n) >= 100 ? n.toFixed(0) : n.toFixed(1);
    return text + (suffix || "");
  }

  function renderPeriodPicker() {
    return (
      '<div class="period-bar">' +
      '<span class="muted">统计时间段</span>' +
      PERIOD_OPTIONS.map((opt) => {
        const active = opt.minutes === detailMinutes ? " active" : "";
        return (
          '<button type="button" class="period-btn' +
          active +
          '" data-minutes="' +
          opt.minutes +
          '">' +
          opt.label +
          "</button>"
        );
      }).join("") +
      "</div>"
    );
  }

  function renderPeriodStatsTable(stats) {
    const metrics = (stats && stats.metrics) || {};
    const rows = [
      ["CPU 利用率", metrics.cpu_percent, "%"],
      ["内存利用率", metrics.mem_percent, "%"],
      ["磁盘利用率", metrics.disk_percent, "%"],
      ["负载 load1", metrics.load1, ""],
      ["加速卡利用率", metrics.accel_util_avg, "%"],
      ["加速卡最高温度", metrics.accel_temp_max, "°C"],
      ["网络入向", metrics.net_rx_mbps, " Mbps"],
      ["网络出向", metrics.net_tx_mbps, " Mbps"],
    ];
    const count = (stats && stats.sample_count) || 0;
    let meta =
      '<p class="muted period-meta">样本 ' +
      count +
      " 点 · " +
      periodLabel(detailMinutes);
    if (stats && stats.from_ts && stats.to_ts) {
      meta +=
        " · " + fmtTime(stats.from_ts) + " ~ " + fmtTime(stats.to_ts);
    }
    meta += "</p>";
    if (!count) {
      return (
        meta +
        '<p class="muted">该时间段暂无历史上报，无法统计</p>'
      );
    }
    let html =
      meta +
      '<table class="period-stats-table"><thead><tr>' +
      "<th>指标</th><th>平均</th><th>最低</th><th>最高</th>" +
      "</tr></thead><tbody>";
    rows.forEach(([label, agg, suffix]) => {
      const a = agg || {};
      html +=
        "<tr><td>" +
        label +
        "</td><td>" +
        fmtStatNum(a.avg, suffix) +
        "</td><td>" +
        fmtStatNum(a.min, suffix) +
        "</td><td>" +
        fmtStatNum(a.max, suffix) +
        "</td></tr>";
    });
    html += "</tbody></table>";
    return html;
  }

  function bindPeriodPicker(hostId) {
    Array.from(el.detailBody.querySelectorAll(".period-btn")).forEach((btn) => {
      btn.addEventListener("click", () => {
        const m = Number(btn.getAttribute("data-minutes"));
        if (!m || m === detailMinutes) return;
        detailMinutes = m;
        loadDetail(hostId);
      });
    });
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
        '<tr><td colspan="7" class="muted">无匹配主机（可调整筛选或部署 Agent）</td></tr>';
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
            fmtPct(h.cpu_percent) + (h.cpu_count ? " / " + h.cpu_count + "核" : ""),
            cpuLevel
          ) +
          "</td>" +
          "<td>" +
          cardText +
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
    const histLimit = Math.min(
      2000,
      Math.max(240, Math.ceil(detailMinutes / 2))
    );
    try {
      const base =
        "/api/v1/hosts/" + encodeURIComponent(data.host_id);
      const [hr, sr] = await Promise.all([
        fetch(
          base +
            "/history?minutes=" +
            detailMinutes +
            "&limit=" +
            histLimit
        ),
        fetch(base + "/period-stats?minutes=" + detailMinutes),
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

    html += '<div class="section-title">时间段统计</div>';
    html += renderPeriodPicker();
    html += renderPeriodStatsTable(periodStats);

    html +=
      '<div class="section-title">近 ' +
      periodLabel(detailMinutes) +
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
      '<div class="chart-card"><div class="title">内存 %</div>' +
      sparkline(
        pts.map((x) => x.mem_percent),
        "#46c2b0"
      ) +
      "</div>";
    html += "</div>";

    html += '<div class="section-title">CPU / 基础资源</div>';
    html += '<div class="kv">';
    html +=
      '<div class="k">CPU</div><div class="v">' +
      metricPct(sys.cpu_percent, thresholds.cpu_warn_percent, thresholds.cpu_critical_percent) +
      " · " +
      (sys.cpu_count ?? "-") +
      " 核 · " +
      escapeHtml(sys.cpu_arch || "-") +
      " · " +
      escapeHtml(sys.os_name || "-") +
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
      '<div class="k">负载</div><div class="v">1m=' +
      (sys.load1 ?? "-") +
      " · 5m=" +
      (sys.load5 ?? "-") +
      " · 15m=" +
      (sys.load15 ?? "-") +
      "</div>";
    html +=
      '<div class="k">内存</div><div class="v">' +
      metricPct(sys.mem_percent, thresholds.mem_warn_percent, thresholds.mem_critical_percent) +
      " (" +
      (sys.mem_used_mb ?? "-") +
      " / " +
      (sys.mem_total_mb ?? "-") +
      " MB)</div>";
    html +=
      '<div class="k">磁盘</div><div class="v">' +
      metricPct(sys.disk_percent, thresholds.disk_warn_percent, thresholds.disk_critical_percent) +
      "</div>";
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
        '<table class="gpu-table"><thead><tr>' +
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
        } else if (n.freq_mhz != null) {
          utilExtra =
            ' <span class="muted">' + Number(n.freq_mhz).toFixed(0) + " MHz</span>";
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
          " (" +
          (n.mem_used_mb ?? "-") +
          " / " +
          (n.mem_total_mb ?? "-") +
          " MB)</td><td>" +
          metricTemp(n.temp_c, tw, tc) +
          "</td><td>" +
          (n.power_w ?? "-") +
          " W</td></tr>";
      });
      html += "</tbody></table>";
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
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
