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
    var guard = host.data_guard || {};
    if (guard.attention) tags.push("data_exfil");
    return tags;
  }

  /**
   * 把图表横坐标比例（0～1）落到最近的历史上报点。
   * 空值也算命中，这样空隙处可以显示时间并写成横线，而不是跳到旁边的数。
   */
  function nearestTrendPoint(points, ratio, gapSeconds) {
    var list = points || [];
    var timed = [];
    var i;
    for (i = 0; i < list.length; i++) {
      var point = list[i];
      if (!point || point.ts === null || point.ts === undefined) continue;
      var ts = Number(point.ts);
      if (isNaN(ts)) continue;
      timed.push({ index: i, ts: ts, value: point.value, backfill: !!point.backfill });
    }
    if (!timed.length) return null;
    var t0 = timed[0].ts;
    var t1 = timed[0].ts;
    for (i = 1; i < timed.length; i++) {
      if (timed[i].ts < t0) t0 = timed[i].ts;
      if (timed[i].ts > t1) t1 = timed[i].ts;
    }
    var span = t1 - t0;
    var r = Number(ratio);
    if (isNaN(r)) r = 0;
    if (r < 0) r = 0;
    if (r > 1) r = 1;
    var target = t0 + r * span;
    var best = timed[0];
    var bestDist = Math.abs(best.ts - target);
    for (i = 1; i < timed.length; i++) {
      var dist = Math.abs(timed[i].ts - target);
      if (dist < bestDist) {
        best = timed[i];
        bestDist = dist;
      }
    }
    var gap = Number(gapSeconds);
    if (isNaN(gap) || gap <= 0) gap = 90;
    if (bestDist > gap) {
      return { index: -1, ts: Math.round(target), value: null, gap: "no_data", backfill: false };
    }
    var value = best.value;
    if (value === undefined || value === "") value = null;
    else if (value !== null) {
      var num = Number(value);
      value = isNaN(num) ? null : num;
    }
    return { index: best.index, ts: best.ts, value: value, backfill: !!best.backfill, gap: "" };
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

  /** 无数据、失败的日/周/月报告仍留在列表里，不因为没有样本而隐藏。 */
  function reportListItemVisible(report) {
    return !!(report && (report.id || report.id === 0));
  }

  /**
   * full：有样本，按章节渲染。
   * empty：窗口没有上报，只留报告壳和一句说明。
   * failed：生成失败，仍留壳和原因。
   */
  function reportPresentation(report) {
    report = report || {};
    var payload = report.payload || {};
    var title = report.title || payload.title || "使用诊断";
    if (report.status === "failed") {
      return {
        mode: "failed",
        title: title,
        message: report.error || payload.error || "生成失败，报告仍保留在列表中。",
      };
    }
    var samples = payload.sample_count;
    var noSamples =
      report.empty === true ||
      report.empty === 1 ||
      payload.empty === true ||
      samples === 0 ||
      samples === "0";
    var sections = payload.sections || [];
    var markdown = report.markdown || payload.markdown || "";
    if (noSamples || (!sections.length && !markdown)) {
      return {
        mode: "empty",
        title: title,
        message:
          "这一窗口没有可汇总的上报。报告已留在列表里，日、周、月都一样，不会消失，也不编造利用率。",
      };
    }
    return { mode: "full", title: title, message: "" };
  }

  function offlinePart(block, fallbackCount, fallbackSeconds) {
    var part = block || {};
    return {
      count: part.interval_count != null ? part.interval_count : fallbackCount || 0,
      seconds: part.total_seconds != null ? part.total_seconds : fallbackSeconds || 0,
    };
  }

  /**
   * 时段页拆成两块：list 只有「集群」和主机；summary 只有汇总数字。
   * 名词解释不进这个模型。
   */
  function periodViewModel(data) {
    data = data || {};
    var off = data.offline || {};
    var nodata = offlinePart(off.no_data, off.interval_count, off.total_seconds);
    var filled = offlinePart(off.backfill, 0, 0);
    var hosts = data.hosts || [];
    var list = [{ kind: "cluster", id: "", label: "集群" }];
    var i;
    for (i = 0; i < hosts.length; i++) {
      var host = hosts[i] || {};
      list.push({
        kind: "host",
        id: host.host_id || "",
        label: host.address || host.hostname || host.host_id || "-",
      });
    }
    var cluster = data.cluster || {};
    return {
      list: list,
      summary: {
        hostsWithSamples: data.hosts_with_samples || 0,
        hostCount: data.host_count || 0,
        sampleCount: data.sample_count || 0,
        noDataCount: nodata.count || 0,
        noDataSeconds: nodata.seconds || 0,
        backfillCount: filled.count || 0,
        backfillSeconds: filled.seconds || 0,
        noDataLabel: "离线（无数据）",
        backfillLabel: "断网（已补报）",
        accelSummary: cluster.accel_summary || data.accel_summary || "",
        rollupNote: data.rollup_note || "集群汇总按样本加权",
      },
    };
  }

  /** 预览里整节收起：附录、口径、名词解释、用语说明。正文数字仍展开。 */
  function reportSectionCollapsed(title) {
    var text = String(title || "");
    return (
      text.indexOf("附录") >= 0 ||
      text.indexOf("名词解释") >= 0 ||
      text.indexOf("口径") >= 0 ||
      text.indexOf("用语说明") >= 0
    );
  }

  /** 开着的章节里，仍把纯解释段收进「说明」。带台数的结论不收。 */
  function reportParagraphGlossary(text) {
    var t = String(text || "").replace(/^\s+/, "");
    if (t.indexOf("口径：") === 0 || t.indexOf("口径:") === 0) return true;
    if (t.indexOf("重点数据守护只发现并提示") === 0) return true;
    if (t.indexOf("只统计被标为重点数据机") === 0) return true;
    if (t.indexOf("若已启用诊断报告推送，本节会随") === 0) return true;
    return false;
  }

  function reportPreviewPlan(sections) {
    return (sections || []).map(function (sec) {
      var title = (sec && sec.title) || "";
      var paras = (sec && sec.paragraphs) || [];
      var glossary = 0;
      var i;
      for (i = 0; i < paras.length; i++) {
        if (reportParagraphGlossary(paras[i])) glossary += 1;
      }
      return {
        title: title,
        collapsed: reportSectionCollapsed(title),
        glossaryParagraphs: glossary,
        openParagraphs: paras.length - glossary,
      };
    });
  }

  var HELP = {
    realtime: {
      title: "说明",
      items: [
        {
          term: "今日关注",
          text: "按当前快照。类别是离线、磁盘快满、卡异常、高负载、数据外传异常。点一类筛到主机表。长期空闲仍看使用诊断。",
        },
        {
          term: "离线",
          text: "实时页只有这一个状态。超过离线阈值没有上报就标为离线。读不到的数显示「-」，不用最后一次快照冒充当前利用率。",
        },
        {
          term: "数据外传异常",
          text: "只提示重点数据机上的迹象，不能拦截，也不能保证数据出不去。",
        },
        {
          term: "磁盘占用",
          text: "磁盘占用看容量。磁盘读取、写入和磁盘 I/O 利用率另计，不进入闲忙。闲忙只看 CPU 和加速卡。",
        },
        {
          term: "负载",
          text: "load1 或 load5 / 核数达到阈值时变黄。读不到显示「-」。",
        },
        {
          term: "字段说明",
          text: "各列含义和单位见可下载的字段说明。",
          href: "/api/v1/export/field-guide.txt",
          hrefLabel: "字段说明",
        },
        {
          term: "范围",
          text: "聚焦 CPU / NPU 监测与统计。异常仅判定展示。不引入外部监控软件。",
        },
      ],
    },
    period: {
      title: "说明",
      items: [
        {
          term: "离线（无数据）",
          text: "事后才分开计。这段没有样本，也不会再补上。机器或采集端停了，或本机队列把样本丢掉了。第一次上报之前不算。实时页在中断期间只显示「离线」。",
        },
        {
          term: "断网（已补报）",
          text: "机器和采集端当时还在采样，样本先留在本机，恢复后按原来的采集时间补上。这些点和平时一样进入平均、P95 和繁忙占比。",
        },
        {
          term: "闲 / 忙",
          text: "只看 CPU 和加速卡。磁盘占用、磁盘读写和内存不进入闲忙。",
        },
        {
          term: "磁盘占用",
          text: "磁盘占用是容量。磁盘读取、写入是 Mbps，磁盘 I/O 利用率是盘忙不忙。读不到显示「-」。",
        },
        {
          term: "集群",
          text: "左侧列表只放集群和主机。点「集群」后，右侧是汇总数字和指标表。集群汇总按样本加权。",
        },
        {
          term: "趋势",
          text: "悬停显示上海时间（Asia/Shanghai）和数值。没有补上的空档是「离线（无数据）」；补上的点是「断网（已补报）」。",
        },
      ],
    },
    charts: {
      title: "说明",
      items: [
        {
          term: "悬停",
          text: "悬停显示上海时间（Asia/Shanghai）和该点数值。",
        },
        {
          term: "空档",
          text: "没有补上的空档是「离线（无数据）」，数值是横线。补上的点写「断网（已补报）」。",
        },
      ],
    },
    reports: {
      title: "说明",
      items: [
        {
          term: "自动生成",
          text: "中心按配置自动生成上一完整周期（默认每日 00:10、每周一 00:20、每月 1 日 00:30，上海时间）。错过不补。同一窗口只保留一份，重新生成会覆盖。",
        },
        {
          term: "推送",
          text: "推送到企业微信、钉钉或飞书在「设置」里配置，未配置则不外发。",
        },
        {
          term: "口径",
          text: "闲忙只看 CPU 和加速卡。磁盘占用和磁盘读写不进入闲忙。缺数写样本不足，不编造。读不到显示「-」。",
        },
        {
          term: "离线（无数据）",
          text: "窗口里没有样本、也不会再补上的空档。",
        },
        {
          term: "断网（已补报）",
          text: "本机留存后按原采集时间补上的时段，已计入利用率。",
        },
        {
          term: "预览",
          text: "名词解释、口径和附录默认收起。下载的 Markdown 仍保留这些说明。",
        },
      ],
    },
    guard: {
      title: "说明",
      items: [
        {
          term: "重点数据机",
          text: "只对勾选的主机或整个资源组生效。主机上单独保存的设置优先于资源组。清除本条之后，这台主机改回跟随资源组。",
        },
        {
          term: "数据外传异常",
          text: "只发现并提示重点数据可能正在离开的迹象，不能拦截，也不能保证数据出不去。看不到加密隧道里的内容，也不能指出究竟是哪一个文件离开了。备份和同步必须写进白名单，否则会报数据外传异常。",
        },
        {
          term: "读不到",
          text: "读失败的路径显示「-」。",
        },
      ],
    },
  };

  function helpTopic(id) {
    return HELP[id] || null;
  }

  function helpText(id) {
    var topic = helpTopic(id);
    if (!topic) return "";
    return (topic.items || [])
      .map(function (item) {
        return item.term + " " + item.text;
      })
      .join("\n");
  }

  function helpCatalogText() {
    var keys = [];
    var id;
    for (id in HELP) {
      if (Object.prototype.hasOwnProperty.call(HELP, id)) keys.push(id);
    }
    return keys
      .map(function (key) {
        return helpText(key);
      })
      .join("\n");
  }

  function escapeHelp(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function installHelp(doc) {
    if (!doc || !doc.body || doc.__monitorHelp) return;
    doc.__monitorHelp = true;
    var pop = doc.createElement("div");
    pop.className = "help-pop";
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", "说明");
    pop.hidden = true;
    doc.body.appendChild(pop);

    function closePop() {
      pop.hidden = true;
      pop.removeAttribute("data-for");
    }

    function openPop(topicId, anchor) {
      var topic = helpTopic(topicId);
      if (!topic) return;
      var html = "<h3>" + escapeHelp(topic.title || "说明") + "</h3><dl>";
      (topic.items || []).forEach(function (item) {
        html += "<dt>" + escapeHelp(item.term) + "</dt><dd>" + escapeHelp(item.text);
        if (item.href) {
          html +=
            ' <a class="link" href="' +
            escapeHelp(item.href) +
            '">' +
            escapeHelp(item.hrefLabel || "打开") +
            "</a>";
        }
        html += "</dd>";
      });
      html += "</dl>";
      pop.innerHTML = html;
      pop.hidden = false;
      pop.setAttribute("data-for", topicId);
      var rect = anchor.getBoundingClientRect();
      var width = Math.min(360, (doc.defaultView || window).innerWidth - 16);
      pop.style.width = width + "px";
      var view = doc.defaultView || window;
      var left = rect.left;
      if (left + width > view.innerWidth - 8) left = view.innerWidth - width - 8;
      if (left < 8) left = 8;
      var top = rect.bottom + 6;
      pop.style.left = left + "px";
      pop.style.top = top + "px";
      var height = pop.offsetHeight || 0;
      if (top + height > view.innerHeight - 8) {
        top = Math.max(8, rect.top - height - 6);
        pop.style.top = top + "px";
      }
    }

    doc.addEventListener("click", function (ev) {
      var target = ev.target;
      var btn = target && target.closest ? target.closest("[data-help]") : null;
      if (btn && doc.body.contains(btn)) {
        ev.preventDefault();
        var id = btn.getAttribute("data-help") || "";
        if (!pop.hidden && pop.getAttribute("data-for") === id) {
          closePop();
          return;
        }
        openPop(id, btn);
        return;
      }
      if (!pop.hidden && !pop.contains(target)) closePop();
    });
    doc.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") closePop();
    });
  }

  if (typeof document !== "undefined") installHelp(document);

  return {
    accelHealthLabel: accelHealthLabel,
    attentionTags: attentionTags,
    nearestTrendPoint: nearestTrendPoint,
    parseLocationHash: parseLocationHash,
    loadPerCoreHigh: loadPerCoreHigh,
    applyHealthVersion: applyHealthVersion,
    reportListItemVisible: reportListItemVisible,
    reportPresentation: reportPresentation,
    periodViewModel: periodViewModel,
    reportSectionCollapsed: reportSectionCollapsed,
    reportParagraphGlossary: reportParagraphGlossary,
    reportPreviewPlan: reportPreviewPlan,
    helpTopic: helpTopic,
    helpText: helpText,
    helpCatalogText: helpCatalogText,
    installHelp: installHelp,
  };
});
