(function () {
  "use strict";

  var hints = {
    wecom: "企业微信群机器人。消息类型为 markdown。",
    dingtalk: "钉钉自定义机器人。安全设置请用自定义关键词，并包含「诊断」。当前不算加签。",
    feishu: "飞书自定义机器人。报告以文本发送。",
  };

  function $(id) {
    return document.getElementById(id);
  }

  function say(id, text) {
    var node = $(id);
    if (node) node.textContent = text || "";
  }

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function readJson(res) {
    try {
      return await res.json();
    } catch (err) {
      return {};
    }
  }

  function applyView(settings) {
    settings = settings || {};
    say(
      "tokenStatus",
      settings.management_token_set ? "已设置（不以明文保存，页面不再显示）" : "未设置"
    );
    var bootstrap = $("tokenBootstrap");
    if (bootstrap) {
      bootstrap.hidden = !!settings.management_token_set;
    }
    say(
      "pushStatus",
      settings.push_webhook_set
        ? (settings.push_channel_label || "已配置") +
          (settings.push_enabled ? " · 已启用 · " : " · 未启用 · ") +
          (settings.push_webhook_hint || "地址已保存")
        : "未配置"
    );
    var enabled = $("pushEnabled");
    if (enabled) enabled.checked = !!settings.push_enabled;
    var channel = $("pushChannel");
    if (channel && settings.push_channel) channel.value = settings.push_channel;
    var webhook = $("pushWebhook");
    if (webhook) {
      webhook.value = "";
      webhook.placeholder = settings.push_webhook_set
        ? "已保存，留空表示不修改"
        : "https://…";
    }
    refreshChannelHint();
  }

  function refreshChannelHint() {
    var channel = ($("pushChannel") && $("pushChannel").value) || "wecom";
    say("channelHint", hints[channel] || "");
  }

  function inputInitial(field) {
    if (field.kind === "bool") return field.value ? "1" : "0";
    if (field.kind === "secret") return "";
    if (field.value == null || field.value === "") return "";
    return String(field.value);
  }

  function controlHtml(field) {
    var initial = inputInitial(field);
    var common =
      ' data-key="' +
      esc(field.key) +
      '" data-kind="' +
      esc(field.kind) +
      '" data-initial="' +
      esc(initial) +
      '"';
    if (field.kind === "bool") {
      return (
        '<label class="check-line"><input type="checkbox"' +
        common +
        (field.value ? " checked" : "") +
        " />" +
        esc(field.label) +
        "</label>"
      );
    }
    var type = field.kind === "secret" ? "password" : field.kind === "string" ? "text" : "number";
    var extra = "";
    if (field.kind === "number" || field.kind === "optional_number") {
      if (field.min != null) extra += ' min="' + esc(field.min) + '"';
      if (field.max != null) extra += ' max="' + esc(field.max) + '"';
      if (field.step != null) extra += ' step="' + esc(field.step) + '"';
    }
    var placeholder = field.placeholder || "";
    if (field.kind === "secret" && field.hint) placeholder = field.hint + "。留空表示不修改";
    if (placeholder) extra += ' placeholder="' + esc(placeholder) + '"';
    var valueAttr = "";
    if (field.kind !== "secret" && initial !== "") valueAttr = ' value="' + esc(initial) + '"';
    var autocomplete = field.kind === "secret" ? ' autocomplete="new-password"' : ' autocomplete="off"';
    return "<input type=\"" + type + "\"" + common + extra + valueAttr + autocomplete + " />";
  }

  function badgeHtml(field) {
    var bits = [];
    if (field.using_default) bits.push('<span class="badge">默认</span>');
    else bits.push('<span class="badge badge-changed">已修改</span>');
    if (field.restart_required) bits.push('<span class="badge badge-restart">重启后生效</span>');
    return bits.join("");
  }

  function renderGroup(group) {
    var fields = (group.fields || [])
      .map(function (field) {
        var unit = field.unit ? '<span class="param-unit">' + esc(field.unit) + "</span>" : "";
        return (
          '<article class="param-field">' +
          '<div class="param-top">' +
          "<div><div class=\"param-label\">" +
          esc(field.label) +
          unit +
          "</div><div class=\"param-key\">" +
          esc(field.key) +
          "</div></div>" +
          "<div class=\"param-badges\">" +
          badgeHtml(field) +
          '<button type="button" class="btn btn-sm" data-restore="' +
          esc(field.key) +
          '">恢复默认</button></div></div>' +
          '<p class="param-desc">' +
          esc(field.description) +
          "</p>" +
          '<div class="param-control">' +
          controlHtml(field) +
          "</div></article>"
        );
      })
      .join("");
    return (
      '<section class="panel" id="group-' +
      esc(group.id) +
      '"><div class="panel-head"><h2>' +
      esc(group.title) +
      "</h2></div><div class=\"settings-copy\"><p>" +
      esc(group.description) +
      '</p></div><div class="param-list">' +
      fields +
      '</div><div class="form-actions"><button type="button" class="btn primary" data-save-group="' +
      esc(group.id) +
      '">保存本组</button><button type="button" class="btn" data-restore-group="' +
      esc(group.id) +
      '">恢复本组默认</button></div></section>'
    );
  }

  function addNav(label, href) {
    var nav = $("centerNav");
    if (!nav) return;
    var link = document.createElement("a");
    link.href = href;
    link.textContent = label;
    nav.appendChild(link);
  }

  function renderCenter(center) {
    var lead = $("centerLead");
    var security = $("centerSecurity");
    var tail = $("centerTail");
    var nav = $("centerNav");
    if (lead) lead.innerHTML = "";
    if (security) security.innerHTML = "";
    if (tail) tail.innerHTML = "";
    if (nav) nav.innerHTML = "";
    if (!center || !center.groups) {
      say("centerPath", "");
      return;
    }
    say("centerPath", center.config_path ? "文件 " + center.config_path : "");
    var securityNavDone = false;
    center.groups.forEach(function (group) {
      var host = lead;
      if (group.anchor === "security") host = security || lead;
      if (group.anchor === "tail") host = tail || lead;
      if (group.anchor === "security" && !securityNavDone) {
        addNav("诊断报告推送", "#sectionPush");
        addNav("管理 Token", "#sectionToken");
        securityNavDone = true;
      }
      if (host) host.insertAdjacentHTML("beforeend", renderGroup(group));
      addNav(group.title, "#group-" + group.id);
    });
    if (!securityNavDone) {
      addNav("诊断报告推送", "#sectionPush");
      addNav("管理 Token", "#sectionToken");
    }
  }

  function controlsIn(root) {
    if (!root) return [];
    return Array.prototype.slice.call(root.querySelectorAll("[data-key]"));
  }

  function allControls() {
    return controlsIn($("centerLead")).concat(controlsIn($("centerSecurity")), controlsIn($("centerTail")));
  }

  function isDirty(node) {
    var kind = node.getAttribute("data-kind");
    var init = node.getAttribute("data-initial") || "";
    if (kind === "bool") return (node.checked ? "1" : "0") !== init;
    if (kind === "secret") return String(node.value || "").trim() !== "";
    var now = String(node.value == null ? "" : node.value).trim();
    if (kind === "number" || kind === "optional_number") {
      if (now === "" && init === "") return false;
      if (now === "" || init === "") return now !== init;
      var left = Number(now);
      var right = Number(init);
      if (!isNaN(left) && !isNaN(right)) return Math.abs(left - right) > 1e-9;
    }
    return now !== init;
  }

  function payloadValue(node) {
    var kind = node.getAttribute("data-kind");
    if (kind === "bool") return !!node.checked;
    if (kind === "number" || kind === "optional_number") {
      if (String(node.value || "").trim() === "") return "";
      return Number(node.value);
    }
    return node.value;
  }

  function dirtyValues(nodes) {
    var values = {};
    var any = false;
    nodes.forEach(function (node) {
      if (!isDirty(node)) return;
      any = true;
      values[node.getAttribute("data-key")] = payloadValue(node);
    });
    return any ? values : null;
  }

  async function postCenter(center) {
    var res = await fetch("/api/v1/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ center: center }),
    });
    var data = await readJson(res);
    if (!res.ok || !data.ok) {
      say("centerHint", (data && data.error) || "保存失败");
      return;
    }
    renderCenter(data.center || {});
    var extra = "";
    if (data.restart_required && data.restart_required.length) {
      extra = " 监听地址、端口或数据目录已写入文件，重启 monitor-center 后才换。";
    }
    say("centerHint", "已保存。" + extra);
  }

  async function saveNodes(nodes) {
    var values = dirtyValues(nodes);
    if (!values) {
      say("centerHint", "没有修改");
      return;
    }
    await postCenter({ values: values });
  }

  function onCenterClick(ev) {
    var button = ev.target.closest ? ev.target.closest("button") : null;
    if (!button) return;
    var restore = button.getAttribute("data-restore");
    var saveGroup = button.getAttribute("data-save-group");
    var restoreGroup = button.getAttribute("data-restore-group");
    if (restore) {
      postCenter({ restore: [restore] });
      return;
    }
    if (restoreGroup) {
      if (!window.confirm("把这一组恢复成内置默认？")) return;
      postCenter({ restore_group: restoreGroup });
      return;
    }
    if (saveGroup) {
      var root = $("group-" + saveGroup);
      saveNodes(controlsIn(root));
    }
  }

  async function load() {
    var res = await fetch("/api/v1/settings");
    var data = await readJson(res);
    if (!res.ok || !data.ok) {
      say("tokenHint", (data && data.error) || "读取设置失败");
      return;
    }
    applyView(data.settings || {});
    renderCenter(data.center || null);
    say("tokenHint", "");
    say("pushHint", "");
  }

  async function saveToken() {
    var first = ($("mgmtToken").value || "").trim();
    var second = ($("mgmtTokenAgain").value || "").trim();
    if (!first) {
      say("tokenHint", "请填写新的管理 Token。留空不会修改。");
      return;
    }
    if (first !== second) {
      say("tokenHint", "两次输入不一致");
      return;
    }
    var res = await fetch("/api/v1/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ management_token: first }),
    });
    var data = await readJson(res);
    if (!res.ok || !data.ok) {
      say("tokenHint", (data && data.error) || "保存失败");
      return;
    }
    $("mgmtToken").value = "";
    $("mgmtTokenAgain").value = "";
    applyView(data.settings || {});
    if (data.center) renderCenter(data.center);
    if (window.MonitorAdmin) await window.MonitorAdmin.remember(first);
    say("tokenHint", "已保存。明文不会再显示。当前标签页已记住，可继续做写操作。");
  }

  async function clearToken() {
    if (!window.confirm("清空后，写操作会重新开放，直到再次设置。确定清空？")) return;
    var res = await fetch("/api/v1/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clear_management_token: true }),
    });
    var data = await readJson(res);
    if (!res.ok || !data.ok) {
      say("tokenHint", (data && data.error) || "清空失败");
      return;
    }
    applyView(data.settings || {});
    if (window.MonitorAdmin) await window.MonitorAdmin.remember("");
    say("tokenHint", "已清空。写操作暂时不再校验管理 Token。");
  }

  async function savePush() {
    var body = {
      push_enabled: !!($("pushEnabled") && $("pushEnabled").checked),
      push_channel: ($("pushChannel") && $("pushChannel").value) || "wecom",
    };
    var webhook = ($("pushWebhook").value || "").trim();
    if (webhook) body.push_webhook_url = webhook;
    var res = await fetch("/api/v1/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    var data = await readJson(res);
    if (!res.ok || !data.ok) {
      say("pushHint", (data && data.error) || "保存失败");
      return;
    }
    applyView(data.settings || {});
    say("pushHint", "推送设置已保存。地址不会完整显示。");
  }

  async function clearPush() {
    if (!window.confirm("清空后不再向外发送诊断报告。确定清空 Webhook？")) return;
    var res = await fetch("/api/v1/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clear_push_webhook: true, push_enabled: false }),
    });
    var data = await readJson(res);
    if (!res.ok || !data.ok) {
      say("pushHint", (data && data.error) || "清空失败");
      return;
    }
    applyView(data.settings || {});
    say("pushHint", "Webhook 已清空，推送已关闭。");
  }

  ["centerLead", "centerSecurity", "centerTail"].forEach(function (id) {
    var node = $(id);
    if (node) node.addEventListener("click", onCenterClick);
  });
  $("btnReload").addEventListener("click", load);
  $("btnSaveToken").addEventListener("click", saveToken);
  $("btnClearToken").addEventListener("click", clearToken);
  $("btnSavePush").addEventListener("click", savePush);
  $("btnClearPush").addEventListener("click", clearPush);
  $("pushChannel").addEventListener("change", refreshChannelHint);
  $("btnSaveAll").addEventListener("click", function () {
    saveNodes(allControls());
  });
  $("btnRestoreAll").addEventListener("click", function () {
    if (
      !window.confirm(
        "把 center.json 里的中心参数全部恢复为内置默认？不清管理 Token，也不清 Webhook。监听地址、端口、数据目录也会回到默认，重启后才换。"
      )
    ) {
      return;
    }
    postCenter({ restore_all: true });
  });
  load();
})();
