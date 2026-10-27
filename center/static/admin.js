/* 本浏览器记住管理 Token，并在写接口上带 X-Management-Token。
   只存在当前标签页。Agent 上报不走这里。 */
(function () {
  if (window.__monitorAdminFetch) return;
  window.__monitorAdminFetch = true;

  var KEY = "monitorManagementToken";
  var origFetch = window.fetch.bind(window);
  var bar = null;
  var input = null;
  var state = null;

  function readToken() {
    try {
      return sessionStorage.getItem(KEY) || "";
    } catch (err) {
      return "";
    }
  }

  function writeToken(value) {
    try {
      if (value) sessionStorage.setItem(KEY, value);
      else sessionStorage.removeItem(KEY);
    } catch (err) {
      /* 隐私模式可能禁掉 sessionStorage，写操作会在 401 时再提示 */
    }
  }

  function pathOf(target) {
    var url = typeof target === "string" ? target : (target && target.url) || "";
    try {
      return new URL(url, window.location.origin).pathname;
    } catch (err) {
      return String(url || "");
    }
  }

  function shouldAttach(target, method) {
    var verb = String(method || "GET").toUpperCase();
    if (verb === "GET" || verb === "HEAD") return false;
    var path = pathOf(target);
    if (path === "/api/v1/metrics") return false;
    return path.indexOf("/api/") === 0;
  }

  window.fetch = function (target, init) {
    var next = init ? Object.assign({}, init) : {};
    var method = next.method || (target && target.method) || "GET";
    if (shouldAttach(target, method)) {
      var token = readToken();
      if (token) {
        var headers = new Headers(next.headers || (target && target.headers) || undefined);
        if (!headers.has("X-Management-Token")) {
          headers.set("X-Management-Token", token);
        }
        next.headers = headers;
        next.method = method;
      }
    }
    return origFetch(target, next).then(function (res) {
      if (res.status === 401 && shouldAttach(target, method)) {
        res.clone().json().then(function (data) {
          if (data && data.code === "management_token_required") {
            showBar("管理 Token 无效或未填写，写操作已被拒绝");
          }
        }).catch(function () {});
      }
      return res;
    });
  };

  function showBar(message) {
    if (!bar) return;
    bar.hidden = false;
    if (message && state) state.textContent = message;
  }

  function renderStatus(settings) {
    if (!bar) return;
    if (!settings || !settings.management_token_set) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    if (!state) return;
    if (readToken()) {
      state.textContent = "已填写。写操作会带上它，关闭标签页后需要再填。";
    } else {
      state.textContent = "已在中心启用。请填写后才能部署、改主机、生成报告或保存设置。";
    }
  }

  function loadStatus() {
    return origFetch("/api/v1/settings")
      .then(function (res) { return res.json(); })
      .then(function (data) {
        renderStatus((data && data.settings) || {});
        return data;
      })
      .catch(function () { return null; });
  }

  function mount() {
    bar = document.createElement("div");
    bar.className = "admin-token-bar";
    bar.id = "adminTokenBar";
    bar.hidden = true;
    bar.innerHTML =
      '<span class="admin-token-label">管理 Token</span>' +
      '<input id="adminTokenInput" type="password" autocomplete="off" placeholder="已启用时，写操作需要填写" />' +
      '<button type="button" class="btn btn-sm" id="adminTokenUse">本页使用</button>' +
      '<span class="muted" id="adminTokenState"></span>';
    var header = document.querySelector("header.top");
    if (header && header.parentNode) header.parentNode.insertBefore(bar, header.nextSibling);
    else document.body.insertBefore(bar, document.body.firstChild);
    input = document.getElementById("adminTokenInput");
    state = document.getElementById("adminTokenState");
    if (input && readToken()) input.value = readToken();
    var button = document.getElementById("adminTokenUse");
    if (button) {
      button.addEventListener("click", function () {
        writeToken(((input && input.value) || "").trim());
        loadStatus();
      });
    }
    if (input) {
      input.addEventListener("change", function () {
        writeToken((input.value || "").trim());
      });
    }
    loadStatus();
  }

  window.MonitorAdmin = {
    remember: function (value) {
      writeToken(value || "");
      if (input) input.value = value || "";
      return loadStatus();
    },
    refresh: loadStatus,
    read: readToken,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
