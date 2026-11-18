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

  /** 预览里整节先不铺开：附录、口径、名词解释、用语说明。带表的长节才点开。 */
  function reportSectionCollapsed(title) {
    var text = String(title || "");
    return (
      text.indexOf("附录") >= 0 ||
      text.indexOf("名词解释") >= 0 ||
      text.indexOf("口径") >= 0 ||
      text.indexOf("用语说明") >= 0
    );
  }

  /** 整段都是口径或名词解释，预览里不单独成段，改挂到章节标题的悬停。带台数的结论不收。 */
  function reportParagraphGlossary(text) {
    var t = String(text || "").replace(/^\s+/, "");
    if (t.indexOf("口径：") === 0 || t.indexOf("口径:") === 0) return true;
    var prefixes = [
      "重点数据守护只发现并提示",
      "只统计被标为重点数据机",
      "若已启用诊断报告推送，本节会随",
      "简评里的高负荷",
      "只看算力。",
      "基本不用条件：",
      "磁盘与内存不参与",
      "磁盘满不等于算力忙。多挂载",
      "地址用完整 IPv4",
      "负责人、用途来自中心",
      "本报告不读取部署任务日志",
      "已保存的诊断报告写在",
      "实际查询闭区间",
      "单机读取上限",
      "全量主机指标见下载",
      "集群 avg",
      "集群汇总",
    ];
    var i;
    for (i = 0; i < prefixes.length; i++) {
      if (t.indexOf(prefixes[i]) === 0) return true;
    }
    return false;
  }

  /** 带表，或纯文字长过悬停，才保留可点开的章节。 */
  function reportSectionLong(sec) {
    var tables = (sec && sec.tables) || [];
    if (tables.length) return true;
    var text = ((sec && sec.paragraphs) || []).join("\n");
    text += "\n" + ((sec && sec.notes) || []).join("\n");
    return text.length > 420;
  }

  function firstSentence(text) {
    var cut = text.indexOf("。");
    if (cut < 0) return null;
    return {
      head: text.slice(0, cut + 1),
      rest: text.slice(cut + 1).replace(/^\s+/, ""),
    };
  }

  /**
   * 预览一段话：show 是留在页面上的数字或状态，tip 是悬停才出现的定义。
   * anchor 是 show 里挂 tip 的那个词。下载的 Markdown 不走这里。
   */
  function reportParagraphView(text) {
    var t = String(text || "").replace(/^\s+|\s+$/g, "");
    if (!t) return { show: "", anchor: "", tip: "" };
    if (reportParagraphGlossary(t)) return { show: "", anchor: "", tip: t };

    var parts;
    if (
      t.indexOf("窗口在线率 ") === 0 ||
      t.indexOf("生成时刻在线 ") === 0 ||
      t.indexOf("类型：") === 0 ||
      t.indexOf("集群范围：") === 0 ||
      t.indexOf("算力：") === 0 ||
      t.indexOf("可用性：") === 0 ||
      t.indexOf("高负荷 ") === 0 ||
      t.indexOf("有高负荷主机。") === 0
    ) {
      parts = firstSentence(t);
      if (parts && parts.rest) {
        var anchor = "窗口在线率";
        if (t.indexOf("生成时刻在线 ") === 0) anchor = "离线";
        else if (t.indexOf("类型：") === 0) anchor = "时间窗";
        else if (t.indexOf("集群范围：") === 0) anchor = "已隐藏";
        else if (t.indexOf("算力：") === 0) anchor = "基本不用";
        else if (t.indexOf("可用性：") === 0) anchor = "离线";
        else if (t.indexOf("高负荷 ") === 0 || t.indexOf("有高负荷主机。") === 0) anchor = "基本不用";
        return { show: parts.head, anchor: anchor, tip: parts.rest };
      }
    }

    if (t.indexOf("窗口内「离线（无数据）」") === 0) {
      parts = firstSentence(t);
      if (parts && parts.rest) {
        var show = parts.head;
        var tip = parts.rest;
        var dropped = tip.match(/本地队列溢出丢掉 \d+ 条[^。]*。/);
        if (dropped) {
          show += dropped[0];
          tip = tip.replace(dropped[0], "").replace(/^\s+|\s+$/g, "");
        }
        return { show: show, anchor: "离线（无数据）", tip: tip };
      }
    }

    if (t.indexOf("独立容量节") === 0) {
      parts = firstSentence(t);
      if (parts && parts.rest) return { show: parts.rest, anchor: "磁盘", tip: parts.head };
    }

    if (t.indexOf("磁盘满不等于算力忙") >= 0 && t.indexOf("多挂载") < 0) {
      var at = t.indexOf("磁盘满不等于算力忙");
      var showDisk = t.slice(0, at).replace(/[；;，,]\s*$/, "");
      if (showDisk && showDisk.charAt(showDisk.length - 1) !== "。") showDisk += "。";
      return { show: showDisk, anchor: "磁盘", tip: "磁盘满不等于算力忙。" };
    }

    if (t.indexOf("建议分开看容量与算力") >= 0) {
      return {
        show: t.replace(/，建议分开看容量与算力。?/, "。"),
        anchor: "磁盘",
        tip: "容量和算力分开看。磁盘不进入闲忙。",
      };
    }

    if (t.indexOf("不要把磁盘空当成算力空") >= 0) {
      return {
        show: t.replace(/，不要把磁盘空当成算力空。?/, "。"),
        anchor: "磁盘",
        tip: "不要把磁盘空当成算力空。磁盘不进入闲忙。",
      };
    }

    if (t.indexOf("这不表示 CPU/加速卡忙") >= 0) {
      return {
        show: t.replace(/；这不表示 CPU\/加速卡忙。?/, "。"),
        anchor: "磁盘",
        tip: "这不表示 CPU/加速卡忙。",
      };
    }

    if (t.indexOf("（繁忙占比或 P95）") >= 0) {
      return {
        show: t.replace("（繁忙占比或 P95）", ""),
        anchor: "高负荷",
        tip: "高负荷看繁忙占比或 P95。",
      };
    }

    if (t.indexOf("分组存在中心") === 0) {
      var mark = t.indexOf("另有未分组主机");
      if (mark >= 0) {
        return {
          show: t.slice(mark).replace(/^另有/, ""),
          anchor: "资源组",
          tip: t.slice(0, mark).replace(/\s+$/, ""),
        };
      }
      return { show: "", anchor: "资源组", tip: t };
    }

    if (t.indexOf("当前没有重点数据机。") === 0 && t.length > "当前没有重点数据机。".length) {
      return {
        show: "当前没有重点数据机。",
        anchor: "重点数据机",
        tip: t.slice("当前没有重点数据机。".length).replace(/^\s+/, ""),
      };
    }

    if (t.indexOf("内存不进入闲忙") >= 0) {
      var cutMem = t.indexOf("内存不进入闲忙");
      var showMem = t.slice(0, cutMem).replace(/[。；]\s*$/, "");
      if (showMem && showMem.charAt(showMem.length - 1) !== "。") showMem += "。";
      return { show: showMem, anchor: "内存", tip: "内存不进入闲忙。" };
    }

    return { show: t, anchor: "", tip: "" };
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


  /** 悬停文案。页面上只留列表、数字和状态；移入对应的词才看解释，移开就收。 */
  var TIPS = {
    attention:
      "今日关注按当前快照。类别是离线、磁盘快满、卡异常、高负载、数据外传异常。点一类筛到主机表。长期空闲仍看使用诊断。",
    "offline-live":
      "实时页只有这一个状态。超过离线阈值没有上报就标为离线。读不到的数显示「-」，不用最后一次快照冒充当前利用率。",
    "disk-full": "磁盘占用达到临界。磁盘读写不在这一类，也不进入闲忙。",
    "accel-bad": "加速卡健康不是正常。利用率读不到显示「-」。",
    "load-high": "load1 或 load5 / 核数达到阈值时变黄。读不到显示「-」。",
    "data-exfil": "数据外传异常只提示重点数据机上的迹象，不能拦截，也不能保证数据出不去。",
    "disk-cap": "磁盘占用看容量。磁盘读取、写入和磁盘 I/O 利用率另计，不进入闲忙。闲忙只看 CPU 和加速卡。",
    "disk-count": "计入汇总的磁盘块数。读不到显示「-」。",
    "disk-rw": "磁盘读取、写入是 Mbps，不进入闲忙。读不到显示「-」。",
    "disk-io": "磁盘 I/O 利用率是盘忙不忙，不进入闲忙。读不到显示「-」。",
    "accel-id": "加速卡型号和数量。没有卡，或类型是 CPU，这里不列利用率。",
    "accel-util": "加速卡使用率。没有加速卡或读不到显示「-」。",
    "offline-metric": "主机离线，不展示实时指标。",
    "no-accel": "未检测到加速卡。cpu 类型不采集；或本机没有对应的厂商工具。",
    "anomaly-colors": "黄是偏高，红是异常。",
    scope: "聚焦 CPU / NPU 监测与统计。异常仅判定展示。不引入外部监控软件。",
    "export-csv": {
      text: "中文表头的当前主机快照。各列含义和单位见字段说明。",
      href: "/api/v1/export/field-guide.txt",
      hrefLabel: "字段说明",
    },
    "field-guide": "各列含义和单位。",
    groups: "分组只存在中心端，Agent 无需升级。",
    "hidden-hosts": "已隐藏的主机不计入在线/离线统计，也不进入之后的诊断报告。历史仍在，可恢复。",
    "import-assets": "按 IP 匹配已有主机。用户名、密码列会被忽略，不会入库。",
    "asset-manual": "资产信息手工维护，Agent 不会覆盖。",
    "asset-collected": "采集规格来自 Agent 上报，只读。",
    readdress:
      "旧 IP 留在历史里，监控 ID 和历史上报不改。新 IP 上的部署目标会改指向这台主机，旧 IP 目标停用但不删除。",
    "readdress-undo":
      "撤销会按当时记下的 host_id 和停用状态恢复，并清掉这次写入的手工地址和那一条历史 IP。监控 ID 和历史上报不改。",
    nodata:
      "离线（无数据）是事后才分开计的空档。这段没有样本，也不会再补上。机器或采集端停了，或本机队列把样本丢掉了。第一次上报之前不算。实时页在中断期间只显示「离线」。",
    backfill:
      "断网（已补报）是机器和采集端当时还在采样，样本先留在本机，恢复后按原来的采集时间补上。这些点和平时一样进入平均、P95 和繁忙占比。",
    cluster:
      "左侧列表只放集群和主机。点「集群」后，右侧是汇总数字和指标表。集群汇总按样本加权。",
    "period-list": "这一列是集群和主机。点一行，只换右侧汇总。",
    "busy-line": "繁忙阈值只用于 CPU 和加速卡的繁忙占比。磁盘占用、磁盘读写和内存不进入闲忙。",
    "busy-ratio": "繁忙占比只对 CPU 和加速卡有意义。其它指标这一格是「-」。",
    "period-cpu": "窗口内 CPU 平均。闲忙只看 CPU 和加速卡。",
    "period-accel": "窗口内加速卡平均。没有加速卡或读不到显示「-」。",
    trend:
      "悬停趋势显示上海时间（Asia/Shanghai）和该点数值。没有补上的空档是「离线（无数据）」，数值是横线。补上的点写「断网（已补报）」。",
    guard:
      "重点数据守护只对勾选的主机或整个资源组生效，主机上的设置优先于资源组。数据外传异常只发现迹象，不能拦截，也不能保证数据出不去。看不到加密隧道里的内容，也不能指出究竟是哪一个文件离开了。备份和同步必须写进白名单，否则会报数据外传异常。读失败的路径显示「-」。",
    "guard-empty": "到设置里勾选重点数据机之后，这里才会计数。",
    reports:
      "使用诊断由中心按配置自动生成上一完整周期（默认每日 00:10、每周一 00:20、每月 1 日 00:30，上海时间）。错过不补。同一窗口只保留一份，重新生成会覆盖。推送到企业微信、钉钉或飞书在「设置」里配置，未配置则不外发。闲忙只看 CPU 和加速卡。磁盘占用和磁盘读写不进入闲忙。缺数写样本不足，不编造。读不到显示「-」。",
    "report-open": "打开已保存的报告不会重算。",
    "report-preview":
      "名词解释和口径悬停在对应的词上。带表的附录点章节名才展开。下载的 Markdown 仍保留这些说明。",
    "report-empty-list":
      "选择日报、周报或月报后点「生成并保存」。没有上报时也会留下一份，并标明无数据。",
    "report-empty-preview": "选择日报、周报或月报后生成。结果写入中心数据库，再次打开直接读库存。",
    "report-window-empty": "这一窗口没有上报，生成后仍会留下报告。",
    "report-long": "这一节有表，点开查看。下载的 Markdown 仍是全文。",
    "settings-intro":
      "先改这三处就够处理日常：报告什么时候出、报告发到哪个群、谁能改配置。着色、保留天数和监听地址不常动。改动写入 config/center.json。报告时刻、空闲/高负荷线、时段繁忙线、异常着色和保留天数，保存后当前进程马上按新值工作。监听地址、端口和数据目录只写入文件，要重启 monitor-center 才换，这不是升级。没改过的项显示「默认」，可以单条、整组或全部恢复。恢复前会再确认。管理 Token 未设置时，保存和其它写操作不校验；设置之后，页面上方黄条里的口令会带到写操作。监测台读数仍然开放。Agent 上报 Token 只校验指标上报，和这里的管理 Token 不是同一把。",
    "settings-security":
      "推送开关和渠道最常改。管理 Token、Webhook 存在数据库，不进 center.json，接口不回显明文。Agent 上报 Token 写在配置文件里，和这两把钥匙分开。",
    "settings-push":
      "定时生成和页面上的「生成并保存」成功后，会把报告正文发到机器人。未启用或没有 Webhook 时不发请求，报告仍照常入库。推送失败不会把这份报告记成失败。正文过长会截断，并提示到监测台「使用诊断」看全文。钉钉若开了加签，当前版本不会计算签名，请改用自定义关键词，关键词包含「诊断」。下面一行是最近一次推送结果：成功或失败原因、北京时间，以及这是测试发送还是报告推送。",
    "push-test":
      "「测试发送」只向已经保存的渠道和 Webhook 发一条短消息，消息里包含「诊断」，不会生成新的诊断报告。未启用或没有 Webhook 时不访问网络。",
    "settings-token":
      "这是中心写操作的一把共享口令，不是登录账号，也不是 Agent 上报用的 token。上报仍只校验 POST /api/v1/metrics。未设置时，部署、改主机、生成报告、保存设置都不校验。设上之后，这些写操作都要带当前 Token；清空也要带。读监测数据不需要。页面上方黄条和这里是同一把口令：黄条只在这个标签页记住，关闭后要再填一次。保存后只留哈希，页面不再显示明文。忘记时，在中心机执行 sqlite3 data/monitor.db \"DELETE FROM admin_settings WHERE key='management_token_sha256';\"，然后立刻重新设置。也可以在首次启动前用环境变量 MONITOR_MANAGEMENT_TOKEN 写入（已有 Token 时不会覆盖）。",
    "settings-foot":
      "管理 Token、报告推送和 Agent 上报 Token 分开。中心参数写在 center.json；推送未配置时，诊断报告只留在本机。",
    "guard-host-follow": "这台主机还没有单独的设置。若它所在的资源组已标为重点数据机，会跟随资源组。",
    "guard-group-new": "这个资源组还没有设置。保存后，组里没有单独设置的主机会成为重点数据机。",
    "guard-paths": "每行一个目录或文件。",
    "guard-cidrs": "可选。每行一个 CIDR。留空则不按网段判断外连。",
    "guard-mbps": "单位 Mbps。留空表示不看速率。",
    "guard-sustain": "外传速率要连续保持这么多秒才记一次。",
    "guard-volume": "单位 MB。留空表示不看发送量。",
    "guard-volume-window": "发送量按这个秒数滚动统计。",
    "guard-baseline": "可选。留空表示不跟平时比，只看速率或发送量。",
    "guard-allow-dest": "备份、同步的网段或 IP，每行一个。必须进白名单，否则会报数据外传异常。",
    "guard-allow-proc": "只写进程名，例如 rsync，每行一个。",
    "ops-list":
      "确认中心地址是别的机器能访问的地址，不要填 127.0.0.1。把机器加进清单，或用表格一次导入。导入只改清单，不会马上安装。勾选后点「部署勾选」，点之前会再确认。采集端默认装在 /opt/monitor-agent，中心仍在 /opt/monitor。密码登录需要这台中心机已安装 sshpass，也可以改用密钥。清单里的「删除」只去掉这一行，不会卸载已经装好的采集端。",
    "ops-empty": "点「加入一台机器」，或点「用表格一次加入」。",
    "ops-log": "勾选机器后点「部署勾选」，过程会写在这里。",
    "ops-add":
      "改一台已有机器时，可以改 IP 和 host_id。另一台已经占用这个 IP 时会拒绝保存。密码留空不会清掉已经保存的密码。停用只是不再参与部署，密码还在。同一台机器换 IP，请到监测台主机详情里用「更换 IP」。对方没有 Python 3.6 或更新版本时，中心会带上便携解释器。失败时悬停清单里的短码。",
    "ops-excel":
      "表头含 ip、ssh_port、ssh_user、ssh_password、ssh_key_path。端口不填就是 22。导入前会再确认，确认后只写入清单。",
    "ops-settings":
      "中心地址和默认登录方式设好后很少再动。单台机器可以在「加入一台机器」里单独覆盖。中心地址是采集端回传数据用的。密码方式要在这台中心机安装 sshpass，或改用密钥。不要从自己的笔记本另拷一份安装包。",
    "ops-rerun": "补跑脚本只在清单部署已经把代码同步过去之后，到那台机器上再跑一次。它不是另一条安装办法。",
    "ops-foot":
      "唯一部署方式：本页 SSH 部署，或在中心机跑 deploy_fleet.sh / deploy_agent.sh。密码登录需中心机已装 sshpass（否则短码 sshpass_missing），或改用密钥。非 root 写 /opt 会 sudo。Agent 默认 /opt/monitor-agent。SSH 端口默认 22（不扫端口）。失败消息带短码，悬停短码可看怎么查。",
    "channel-wecom": "企业微信群机器人。消息类型为 markdown。",
    "channel-dingtalk": "钉钉自定义机器人。安全设置请用自定义关键词，并包含「诊断」。当前不算加签。",
    "channel-feishu": "飞书自定义机器人。报告以文本发送。",
  };

  function tipEntry(id) {
    var raw = TIPS[id];
    if (!raw) return null;
    if (typeof raw === "string") return { text: raw, href: "", hrefLabel: "" };
    return {
      text: raw.text || "",
      href: raw.href || "",
      hrefLabel: raw.hrefLabel || "打开",
    };
  }

  function helpCatalogText() {
    var parts = [];
    var id;
    for (id in TIPS) {
      if (!Object.prototype.hasOwnProperty.call(TIPS, id)) continue;
      var entry = tipEntry(id);
      if (entry && entry.text) parts.push(entry.text);
    }
    return parts.join("\n");
  }

  function escapeTip(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function tipFromHost(host) {
    if (!host || !host.getAttribute) return null;
    var id = host.getAttribute("data-tip-id") || "";
    var entry = id ? tipEntry(id) : null;
    var text = host.getAttribute("data-tip") || "";
    var href = host.getAttribute("data-tip-href") || "";
    var hrefLabel = host.getAttribute("data-tip-href-label") || "";
    if (entry) {
      if (!text) text = entry.text;
      if (!href) href = entry.href;
      if (!hrefLabel) hrefLabel = entry.hrefLabel;
    }
    if (!text && !href) return null;
    return { text: text, href: href, hrefLabel: hrefLabel || "打开" };
  }

  function installHelp(doc) {
    if (!doc || !doc.body || doc.__monitorHelp) return;
    doc.__monitorHelp = true;
    var pop = doc.createElement("div");
    pop.className = "tip-pop";
    pop.setAttribute("role", "tooltip");
    pop.hidden = true;
    doc.body.appendChild(pop);
    var current = null;
    var hideTimer = 0;

    function hideNow() {
      if (hideTimer) {
        clearTimeout(hideTimer);
        hideTimer = 0;
      }
      pop.hidden = true;
      pop.removeAttribute("data-for");
      current = null;
    }

    function place(anchor) {
      var rect = anchor.getBoundingClientRect();
      var view = doc.defaultView || window;
      var width = Math.min(320, view.innerWidth - 16);
      pop.style.width = width + "px";
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

    function show(anchor) {
      var tip = tipFromHost(anchor);
      if (!tip) return;
      if (hideTimer) {
        clearTimeout(hideTimer);
        hideTimer = 0;
      }
      var html = "<p>" + escapeTip(tip.text) + "</p>";
      if (tip.href) {
        html +=
          '<p><a class="link" href="' +
          escapeTip(tip.href) +
          '">' +
          escapeTip(tip.hrefLabel) +
          "</a></p>";
      }
      pop.innerHTML = html;
      pop.hidden = false;
      var id = anchor.getAttribute("data-tip-id") || anchor.getAttribute("data-tip") || "";
      pop.setAttribute("data-for", id);
      current = anchor;
      place(anchor);
    }

    function hostOf(node) {
      if (!node || !node.closest) return null;
      var host = node.closest("[data-tip],[data-tip-id]");
      if (!host || !doc.body.contains(host)) return null;
      return host;
    }

    function scheduleHide() {
      if (hideTimer) clearTimeout(hideTimer);
      hideTimer = setTimeout(hideNow, 120);
    }

    doc.addEventListener("mouseover", function (ev) {
      var host = hostOf(ev.target);
      if (host) {
        show(host);
        return;
      }
      if (!pop.hidden && pop.contains(ev.target)) {
        if (hideTimer) {
          clearTimeout(hideTimer);
          hideTimer = 0;
        }
      }
    });
    doc.addEventListener("mouseout", function (ev) {
      var host = hostOf(ev.target);
      var to = ev.relatedTarget;
      if (host) {
        if (to && (host.contains(to) || pop.contains(to))) return;
        if (pop.querySelector("a")) scheduleHide();
        else hideNow();
        return;
      }
      if (pop.contains(ev.target)) {
        if (to && (pop.contains(to) || hostOf(to))) return;
        hideNow();
      }
    });
    doc.addEventListener("focusin", function (ev) {
      var host = hostOf(ev.target);
      if (host) show(host);
    });
    doc.addEventListener("focusout", function (ev) {
      var host = hostOf(ev.target);
      if (!host) return;
      var to = ev.relatedTarget;
      if (to && (host.contains(to) || pop.contains(to))) return;
      hideNow();
    });
    doc.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") hideNow();
    });
    if (doc.defaultView) {
      doc.defaultView.addEventListener("scroll", hideNow, true);
    }
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
    reportParagraphView: reportParagraphView,
    reportSectionLong: reportSectionLong,
    reportPreviewPlan: reportPreviewPlan,
    tipEntry: tipEntry,
    helpCatalogText: helpCatalogText,
    installHelp: installHelp,
  };
});
