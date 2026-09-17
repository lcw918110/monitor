(() => {
  let currentJobId = null;
  let pollTimer = null;
  let editingId = null;

  const DEFAULT_AGENT_DIR = "/opt/monitor-agent";

  const $ = (id) => document.getElementById(id);

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function statusText(s) {
    const map = {
      pending: "待部署",
      deploying: "部署中",
      success: "成功",
      failed: "失败",
    };
    return map[s] || s || "-";
  }

  function netBadge(t) {
    if (t.net_probed_at == null || t.net_probed_at === "") {
      return '<span class="status-badge unknown">未测</span>';
    }
    if (!t.net_reachable) {
      return '<span class="status-badge critical">不通</span>';
    }
    const q = t.net_quality || "ok";
    const label =
      { excellent: "优", good: "良", fair: "一般", poor: "差", ok: "通" }[q] ||
      "通";
    const cls = q === "poor" || q === "fair" ? "warn" : "normal";
    const rtt = t.net_rtt_ms != null ? t.net_rtt_ms + "ms" : "";
    return (
      '<span class="status-badge ' +
      cls +
      '">' +
      label +
      (rtt ? " " + rtt : "") +
      "</span>"
    );
  }

  function peersText(t) {
    const peers = t.peer_hosts || [];
    if (!peers.length) return '<span class="muted">-</span>';
    return escapeHtml(
      peers
        .map((p) =>
          p.name && p.name !== p.host ? p.name + "=" + p.host : p.host
        )
        .join("; ")
    );
  }

  async function loadSettings() {
    const res = await fetch("/api/v1/deploy/settings");
    const data = await res.json();
    const s = data.settings || {};
    let publicUrl = (s.public_center_url || "").trim();
    if (
      (!publicUrl || data.public_center_url_is_loopback) &&
      data.suggested_public_center_url
    ) {
      publicUrl = data.suggested_public_center_url;
    }
    $("publicUrl").value = publicUrl;
    $("sshUser").value = s.default_ssh_user || "root";
    $("sshPort").value = s.default_ssh_port || 22;
    $("remoteDir").value = s.default_remote_dir || DEFAULT_AGENT_DIR;
    $("sshKey").value = s.ssh_key_path || "";
    $("interval").value = s.interval_seconds || 15;
    $("sshPassword").value = "";
    $("sshPassword").placeholder = s.default_ssh_password_set
      ? "已保存默认密码（留空不修改）"
      : "可选：所有未单独填密码的客户端共用";
    if (data.suggested_public_center_url) {
      $("publicUrl").placeholder = data.suggested_public_center_url;
    }
  }

  async function saveSettings() {
    const publicUrl = $("publicUrl").value.trim();
    if (/127\.0\.0\.1|localhost/i.test(publicUrl)) {
      const ok = confirm(
        "当前填写的是本机回环地址（127.0.0.1/localhost）。\n" +
          "远端客户端无法用它访问中心，一般应填局域网 IP。\n\n仍要保存吗？"
      );
      if (!ok) return;
    }
    const body = {
      public_center_url: publicUrl,
      default_ssh_user: $("sshUser").value.trim() || "root",
      default_ssh_port: Number($("sshPort").value || 22),
      default_remote_dir: $("remoteDir").value.trim() || DEFAULT_AGENT_DIR,
      ssh_key_path: $("sshKey").value.trim(),
      interval_seconds: Number($("interval").value || 15),
    };
    const pwd = $("sshPassword").value;
    if (pwd) body.default_ssh_password = pwd;
    const res = await fetch("/api/v1/deploy/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      alert(data.error || "保存失败");
      return;
    }
    $("sshPassword").value = "";
    $("settingsHint").textContent =
      "设置已保存。" +
      (data.settings && data.settings.default_ssh_password_set
        ? " 默认密码已配置。"
        : " 未配置默认密码（可用私钥）。");
    loadSettings();
  }

  function authLabel(t) {
    if (t.auth_mode === "password" || t.ssh_password_set) return "密码(本机)";
    if (t.auth_mode === "key" && t.ssh_key_path) return "私钥(本机)";
    return "沿用全局";
  }

  async function loadTargets() {
    const res = await fetch("/api/v1/deploy/targets");
    const data = await res.json();
    const targets = data.targets || [];
    if (!targets.length) {
      $("targetBody").innerHTML =
        '<tr><td colspan="11" class="muted">暂无目标，请先添加或导入 Excel（含指定机器列）</td></tr>';
      return;
    }
    $("targetBody").innerHTML = targets
      .map((t) => {
        return (
          "<tr>" +
          '<td><input type="checkbox" class="chk" value="' +
          t.id +
          '" /></td>' +
          "<td>" +
          escapeHtml(t.ip) +
          "</td>" +
          "<td>" +
          escapeHtml(String(t.ssh_port || 22)) +
          "</td>" +
          "<td>" +
          escapeHtml(t.host_id) +
          "</td>" +
          "<td>" +
          escapeHtml(t.host_type) +
          "</td>" +
          "<td>" +
          escapeHtml(authLabel(t)) +
          (t.ssh_key_path
            ? '<div class="muted" style="max-width:160px;overflow:hidden;text-overflow:ellipsis;">' +
              escapeHtml(t.ssh_key_path) +
              "</div>"
            : "") +
          "</td>" +
          '<td class="muted" style="max-width:220px;white-space:normal;">' +
          peersText(t) +
          "</td>" +
          "<td>" +
          netBadge(t) +
          "</td>" +
          "<td><span class=\"status-badge " +
          (t.status === "success"
            ? "normal"
            : t.status === "failed"
            ? "critical"
            : t.status === "deploying"
            ? "warn"
            : "unknown") +
          '">' +
          statusText(t.status) +
          "</span></td>" +
          "<td class=\"muted\">" +
          (function () {
            const code = t.last_error_code || "";
            let msg = t.last_message || "";
            if (code && msg.indexOf("[" + code + "]") === 0) {
              msg = msg.slice(code.length + 2).replace(/^\s+/, "");
            }
            return (
              (code ? "<code>" + escapeHtml(code) + "</code> " : "") +
              escapeHtml(msg)
            );
          })() +
          "</td>" +
          "<td>" +
          '<button type="button" class="btn btn-sm" data-edit="' +
          t.id +
          '">编辑</button> ' +
          '<button type="button" class="btn btn-sm" data-test-row="' +
          t.id +
          '">测试</button> ' +
          '<button type="button" class="btn btn-sm" data-del="' +
          t.id +
          '">删除</button>' +
          "</td>" +
          "</tr>"
        );
      })
      .join("");

    Array.from(document.querySelectorAll("[data-del]")).forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("确认删除该目标？")) return;
        await fetch("/api/v1/deploy/targets/" + btn.getAttribute("data-del"), {
          method: "DELETE",
        });
        loadTargets();
      });
    });

    Array.from(document.querySelectorAll("[data-edit]")).forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = Number(btn.getAttribute("data-edit"));
        try {
          const res = await fetch("/api/v1/deploy/targets/" + id);
          const data = await res.json();
          if (!res.ok || !data.ok || !data.target) {
            alert((data && data.error) || "加载目标失败");
            return;
          }
          startEdit(data.target);
        } catch (e) {
          alert("加载目标失败");
        }
      });
    });

    Array.from(document.querySelectorAll("[data-test-row]")).forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = Number(btn.getAttribute("data-test-row"));
        $("testHint").textContent = "正在测试 #" + id + " ...";
        const res = await fetch("/api/v1/deploy/test-ssh", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target_id: id }),
        });
        const data = await res.json();
        const r = (data && data.result) || {};
        $("testHint").textContent = r.message || data.error || "测试完成";
        $("testHint").style.color = r.ok ? "var(--ok)" : "var(--bad)";
      });
    });
  }

  function startEdit(t) {
    editingId = t.id;
    $("tIp").value = t.ip || "";
    $("tHostId").value = t.host_id || "";
    $("tHostname").value = t.hostname || "";
    $("tType").value = t.host_type || "auto";
    $("tUser").value = t.ssh_user || "";
    $("tPort").value = t.ssh_port || 22;
    $("tPassword").value = t.ssh_password || "";
    $("tPassword").placeholder = t.ssh_password
      ? "已载入保存的密码（可修改）"
      : "有则用密码登录；空则用默认密码/密钥";
    $("tKey").value = t.ssh_key_path || "";
    $("tRemoteDir").value = t.remote_dir || "";
    $("tPeers").value = t.peer_hosts_text || "";
    $("btnAdd").textContent = "保存修改";
    $("btnCancelEdit").hidden = false;
    $("testHint").textContent = "正在编辑 #" + t.id + " " + (t.ip || "");
    $("testHint").style.color = "";
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function clearEdit() {
    editingId = null;
    $("btnAdd").textContent = "添加到清单";
    $("btnCancelEdit").hidden = true;
    $("tIp").value = "";
    $("tHostId").value = "";
    $("tHostname").value = "";
    $("tPeers").value = "";
    $("tPassword").value = "";
    $("tPassword").placeholder = "有则用密码登录；空则用默认密码/密钥";
    $("tKey").value = "";
    $("tRemoteDir").value = "";
    $("tPort").value = "22";
    $("testHint").textContent = "";
  }

  async function addTarget() {
    const ip = $("tIp").value.trim();
    if (!ip) {
      alert("请填写 IP");
      return;
    }
    const body = {
      ip,
      host_id: $("tHostId").value.trim() || ip,
      hostname: $("tHostname").value.trim() || $("tHostId").value.trim() || ip,
      host_type: $("tType").value,
      ssh_user: $("tUser").value.trim() || $("sshUser").value.trim() || "root",
      ssh_port: Number($("tPort").value || $("sshPort").value || 22),
      ssh_key_path: $("tKey").value.trim(),
      remote_dir:
        $("tRemoteDir").value.trim() ||
        $("remoteDir").value.trim() ||
        DEFAULT_AGENT_DIR,
      peer_hosts: $("tPeers").value.trim(),
    };
    const pwd = $("tPassword").value;
    if (pwd) body.ssh_password = pwd;

    let res;
    if (editingId) {
      res = await fetch("/api/v1/deploy/targets/" + editingId, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } else {
      if (pwd) body.ssh_password = pwd;
      res = await fetch("/api/v1/deploy/targets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    }
    const data = await res.json();
    if (!res.ok || !data.ok) {
      alert(data.error || (editingId ? "保存失败" : "添加失败"));
      return;
    }
    clearEdit();
    loadTargets();
  }

  function formSshBody() {
    const ip = $("tIp").value.trim();
    const body = {
      ip,
      ssh_user: $("tUser").value.trim() || $("sshUser").value.trim() || "root",
      ssh_port: Number($("tPort").value || $("sshPort").value || 22),
      ssh_key_path: $("tKey").value.trim() || $("sshKey").value.trim(),
      remote_dir:
        $("tRemoteDir").value.trim() ||
        $("remoteDir").value.trim() ||
        DEFAULT_AGENT_DIR,
    };
    const pwd = $("tPassword").value || $("sshPassword").value;
    if (pwd) body.ssh_password = pwd;
    return body;
  }

  async function testSshFromForm() {
    const body = formSshBody();
    if (!body.ip) {
      alert("请先填写 IP");
      return;
    }
    $("testHint").textContent = "正在测试连通与写权限...";
    $("testHint").style.color = "";
    const res = await fetch("/api/v1/deploy/test-ssh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    const r = (data && data.result) || {};
    $("testHint").textContent = r.message || data.error || "测试完成";
    $("testHint").style.color = r.ok ? "var(--ok)" : "var(--bad)";
  }

  async function importExcelFile(file) {
    if (!file) return;
    $("excelHint").textContent = "正在导入 " + file.name + " ...";
    const buf = await file.arrayBuffer();
    const res = await fetch("/api/v1/deploy/targets/import-excel", {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Filename": file.name,
      },
      body: buf,
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      $("excelHint").textContent = data.error || "导入失败";
      alert(data.error || "导入失败");
      return;
    }
    $("excelHint").textContent = "成功导入 " + data.imported + " 台（含指定机器）";
    loadTargets();
  }

  function selectedIds() {
    return Array.from(document.querySelectorAll(".chk:checked")).map((x) =>
      Number(x.value)
    );
  }

  async function runDeploy(ids) {
    if (!(await ensureSettingsSaved())) return;
    const body =
      ids === "all" ? { target_ids: "all" } : { target_ids: ids };
    if (ids !== "all" && (!ids || !ids.length)) {
      alert("请先勾选目标");
      return;
    }
    const res = await fetch("/api/v1/deploy/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      alert(data.error || "启动部署失败");
      return;
    }
    currentJobId = data.job_id;
    $("jobMeta").textContent = "任务 #" + currentJobId + " 运行中";
    $("jobLog").textContent = "任务已启动...\n";
    startPoll();
  }

  async function ensureSettingsSaved() {
    if (!$("publicUrl").value.trim()) {
      alert("请先填写并保存「中心对外访问地址」");
      return false;
    }
    await saveSettings();
    return true;
  }

  async function pollJob() {
    if (!currentJobId) return;
    const res = await fetch("/api/v1/deploy/jobs/" + currentJobId);
    const data = await res.json();
    if (!res.ok) return;
    const job = data.job || {};
    $("jobLog").textContent = job.log_text || "";
    $("jobMeta").textContent =
      "任务 #" +
      currentJobId +
      " · " +
      (job.status || "") +
      (job.finished_at ? " · 已结束" : " · 进行中");
    if (job.status && job.status !== "running") {
      stopPoll();
      loadTargets();
    }
  }

  function startPoll() {
    stopPoll();
    pollTimer = setInterval(pollJob, 1500);
    pollJob();
  }

  function stopPoll() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function refreshAll() {
    await loadSettings();
    await loadTargets();
    if (currentJobId) pollJob();
  }

  $("btnSaveSettings").addEventListener("click", saveSettings);
  $("btnAdd").addEventListener("click", addTarget);
  $("btnTestSsh").addEventListener("click", testSshFromForm);
  $("btnCancelEdit").addEventListener("click", clearEdit);
  document.querySelectorAll(".pwd-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-target");
      const input = id ? $(id) : null;
      if (!input) return;
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.setAttribute("aria-pressed", show ? "true" : "false");
      btn.title = show ? "隐藏密码" : "显示密码";
      const eye = btn.querySelector(".icon-eye");
      const eyeOff = btn.querySelector(".icon-eye-off");
      if (eye) eye.hidden = show;
      if (eyeOff) eyeOff.hidden = !show;
    });
  });
  $("excelFile").addEventListener("change", (e) => {
    const f = e.target.files && e.target.files[0];
    importExcelFile(f);
    e.target.value = "";
  });
  $("btnDeploySelected").addEventListener("click", () =>
    runDeploy(selectedIds())
  );
  $("btnDeployAll").addEventListener("click", () => runDeploy("all"));
  $("btnRefresh").addEventListener("click", refreshAll);
  $("chkAll").addEventListener("change", () => {
    const on = $("chkAll").checked;
    Array.from(document.querySelectorAll(".chk")).forEach((c) => {
      c.checked = on;
    });
  });

  refreshAll();
})();
