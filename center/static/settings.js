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

  async function load() {
    var res = await fetch("/api/v1/settings");
    var data = await readJson(res);
    if (!res.ok || !data.ok) {
      say("tokenHint", (data && data.error) || "读取设置失败");
      return;
    }
    applyView(data.settings || {});
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

  $("btnReload").addEventListener("click", load);
  $("btnSaveToken").addEventListener("click", saveToken);
  $("btnClearToken").addEventListener("click", clearToken);
  $("btnSavePush").addEventListener("click", savePush);
  $("btnClearPush").addEventListener("click", clearPush);
  $("pushChannel").addEventListener("change", refreshChannelHint);
  load();
})();
