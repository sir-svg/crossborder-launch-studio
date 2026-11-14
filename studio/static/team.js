"use strict";

const roleLabels = {owner: "所有者", editor: "编辑员", reviewer: "审核员", viewer: "查看者"};
const roleDescriptions = {
  owner: "管理工作区和成员，也可以编辑和审核。",
  editor: "编辑商品、运行任务、修改草稿并提交审核。",
  reviewer: "查看资料，对其他成员送审的版本作出决定。",
  viewer: "查看商品、任务和草稿。",
};

function applySession(result) {
  session = result;
  config = null;
}

function loginPage() {
  ++view;
  events?.abort();
  document.body.classList.add("signed-out");
  const form = el("form", {},
    field("邮箱", "email", "", {type: "email", autocomplete: "username", required: "", maxlength: 254}),
    field("密码", "password", "", {type: "password", autocomplete: "current-password", required: "", maxlength: 128}),
    button("登录工作台", null, "primary", "submit"));
  formRun(form, async (values) => {
    const result = await api("/auth/login", "POST", Object.fromEntries(values));
    applySession(result);
    document.querySelector("#notice").hidden = true;
    render();
  });
  main.replaceChildren(el("div", {class: "login-shell"},
    el("div", {class: "login-brand"},
      el("span", {class: "brand-mark", "aria-hidden": "true"}, "↗"),
      el("p", {}, "Launch Studio · 跨境商品上新")),
    el("h1", {}, "登录商品上新工作台"),
    el("p", {class: "subtle"}, "使用工作区所有者提供的账号登录。"),
    panel("账号登录", form)));
  form.querySelector("input").focus();
}

function workspaceHeader() {
  document.body.classList.remove("signed-out");
  const slot = document.querySelector("#workspace-tools");
  slot.replaceChildren();
  slot.hidden = session.demo;
  if (session.demo) return;
  const options = session.workspaces.map((space) => [space.id, space.name]);
  const switcher = select("切换工作区", "workspace", options, session.workspace.id);
  switcher.className = "workspace-switcher";
  const selectNode = switcher.querySelector("select");
  selectNode.addEventListener("change", async () => {
    selectNode.disabled = true;
    try {
      const result = await api("/auth/workspace", "POST", {workspace_id: selectNode.value});
      applySession(result);
      go(can("edit") ? "launch" : "history");
      notify("已切换到「" + result.workspace.name + "」。");
    } catch (error) {
      selectNode.value = session.workspace.id;
      notify(error.message, true);
    } finally {
      selectNode.disabled = false;
    }
  });
  slot.append(switcher, button(session.user.display_name, () => go("account"), "secondary account-link"));
}

function accountPage(token) {
  const user = session.user;
  const change = el("form", {},
    field("当前密码", "old_password", "", {type: "password", required: "", autocomplete: "current-password", maxlength: 128}),
    field("新密码（至少 12 位）", "new_password", "", {type: "password", required: "", autocomplete: "new-password", minlength: 12, maxlength: 128}),
    button("更新密码", null, "primary", "submit"));
  formRun(change, async (values) => {
    await api("/auth/password", "POST", Object.fromEntries(values));
    change.reset();
    notify("密码已更新。其他设备上的会话已退出。");
  });
  const controls = el("div", {class: "stack"}, el("dl", {class: "facts"},
    el("dt", {}, "姓名"), el("dd", {}, user.display_name),
    el("dt", {}, "邮箱"), el("dd", {}, user.email),
    el("dt", {}, "当前工作区"), el("dd", {}, session.workspace.name),
    el("dt", {}, "当前权限"), el("dd", {}, roleLabels[session.workspace.role])),
    action("退出登录", async () => {
      await api("/auth/logout", "POST");
      applySession(null);
      location.hash = "launch";
      loginPage();
      notify("已退出登录。");
    }));
  const settings = el("div", {class: "stack"}, panel("更改密码", change));
  if (can("manage")) {
    const create = el("form", {},
      field("工作区名称", "name", "", {required: "", minlength: 1, maxlength: 80}),
      button("创建并进入工作区", null, "secondary", "submit"));
    formRun(create, async (values) => {
      const result = await api("/auth/workspaces", "POST", Object.fromEntries(values));
      applySession(result);
      notify("已创建「" + result.workspace.name + "」。");
      go("products");
    });
    settings.append(panel("创建工作区", create));
  }
  if (token !== view) return;
  main.replaceChildren(head("我的账号", "查看当前身份、工作区和账号设置。"),
    el("div", {class: "split"}, panel("登录信息", controls), settings));
}

async function teamPage(token) {
  const members = await api("/members");
  if (token !== view) return;
  const roleOptions = Object.entries(roleLabels);
  const rows = members.map((member) => {
    const role = select("成员角色", "role", roleOptions, member.role);
    const selectNode = role.querySelector("select");
    selectNode.setAttribute("aria-label", member.display_name + "的角色");
    const save = action("保存角色", async () => {
      await api("/members/" + member.id, "PATCH", {role: selectNode.value});
      if (token === view) refreshIdentity("成员角色已更新。");
    });
    save.disabled = true;
    selectNode.addEventListener("change", () => { save.disabled = selectNode.value === member.role; });
    const remove = action("移出", async () => {
      if (!window.confirm("将「" + member.display_name + "」移出此工作区？")) return;
      await api("/members/" + member.id, "DELETE");
      if (token === view) refreshIdentity("成员已移出工作区。");
    });
    return el("div", {class: "member-row"},
      el("div", {},
        el("strong", {}, member.display_name + (member.id === session.user.id ? "（我）" : "")),
        el("p", {class: "subtle"}, member.email)),
      el("div", {class: "button-row"}, role, save, remove));
  });
  async function refreshIdentity(message) {
    applySession(await api("/auth/me"));
    notify(message);
    if (can("manage")) render();
    else go("history");
  }
  const invite = el("form", {},
    field("成员邮箱", "email", "", {type: "email", required: "", maxlength: 254}),
    select("角色", "role", roleOptions, "editor"),
    el("p", {class: "subtle"}, "已有账号可直接加入。创建新账号时，请填写姓名和初始密码。"),
    field("姓名（新账号）", "display_name", "", {maxlength: 80}),
    field("初始密码（新账号，至少 12 位）", "password", "", {type: "password", autocomplete: "new-password", minlength: 12, maxlength: 128}),
    button("添加成员", null, "primary", "submit"));
  formRun(invite, async (values) => {
    const data = Object.fromEntries(values);
    if (!data.display_name.trim()) delete data.display_name;
    if (!data.password) delete data.password;
    await api("/members", "POST", data);
    notify("成员已添加。");
    if (token === view) render();
  });
  main.replaceChildren(head("成员管理", "分别安排商品编辑、草稿审核和只读查看。"),
    el("div", {class: "split team-layout"},
      panel("添加成员", invite),
      el("div", {class: "stack"}, panel("当前成员", rows),
        panel("角色权限", el("dl", {class: "facts"}, roleOptions.flatMap(([key, label]) =>
          [el("dt", {}, label), el("dd", {}, roleDescriptions[key])]))))));
}

const auditLabels = {
  "workspace.create": "创建工作区", "member.add": "添加成员",
  "member.remove": "移出成员", "member.role_change": "更新成员角色",
  "auth.login": "登录工作台", "auth.logout": "退出工作台", "auth.password_change": "更新密码",
  "product.created": "创建商品", "product.updated": "修改商品资料", "reviews.imported": "导入评论",
  "task.submitted": "创建任务", "task.retried": "重试任务", "task.completed": "完成任务",
  "task.failed": "任务失败", "draft.updated": "保存草稿", "review.submitted": "提交审核",
  "review.approved": "审核通过", "review.rejected": "退回草稿", "draft.exported": "导出文案",
};

function auditDetails(details = {}) {
  if (!details || typeof details !== "object") return [];
  const labels = {
    version: "版本", revision: "修订", count: "数量", format: "格式", sku: "SKU", role: "角色",
    status: "状态", reason: "原因", market: "站点", decision: "结论",
  };
  return Object.entries(labels).flatMap(([key, label]) => {
    const value = details[key];
    return ["string", "number"].includes(typeof value)
      ? [label + " " + (roleLabels[value] || reviewLabels[value] || value)]
      : [];
  });
}

async function auditPage(token, offset = 0) {
  const limit = 30;
  const records = await api("/audit?limit=" + limit + "&offset=" + offset);
  if (token !== view) return;
  const entries = records.map((record) => el("li", {},
    el("div", {class: "audit-main"},
      el("strong", {}, auditLabels[record.action] || record.action),
      el("span", {class: "subtle"}, record.actor_name || actorName(record.actor_id)),
      el("time", {datetime: record.created_at}, localTime(record.created_at))),
    el("p", {class: "subtle"}, auditDetails(record.details).join(" · ")),
    record.resource_id ? el("p", {class: "audit-id"}, record.resource_id) : null));
  main.replaceChildren(head("操作记录", "追踪当前工作区里的商品改动、审核与导出。"),
    panel("工作区记录", el("div", {class: "stack"},
      entries.length ? el("ul", {class: "audit-list"}, entries) : el("p", {class: "empty"}, "此页没有记录。"),
      pagedControls(offset, entries.length, limit, (next) => auditPage(token, next).catch((error) => notify(error.message, true))))));
}

async function operationsPage(token) {
  const data = await api("/operations");
  if (token !== view) return;
  const metrics = (record, labels, open) =>
    el("div", {class: "metrics"}, Object.entries(labels).map(([key, label]) =>
      el("button", {type: "button", class: "metric", onclick: () => open(key)},
        el("span", {}, label), el("strong", {}, String(record[key] ?? 0)))));
  const worker = data.worker || {};
  const state = worker.healthy === true ? "正在运行" : worker.healthy === false ? "需要检查" : "等待状态";
  const help = worker.healthy === false ? el("p", {class: "note warning"}, "没有收到近期后台信号。请检查后台服务是否已启动。") : null;
  main.replaceChildren(head("运行状态", "查看当前工作区的任务积压、审核进度和后台状态。"),
    el("div", {class: "stack"},
      panel("后台任务", metrics(data.tasks, statuses, (status) => go("history/" + status)),
        action("刷新", () => operationsPage(token))),
      panel("草稿审核", metrics(data.reviews, reviewLabels, (status) => go("review/" + status))),
      panel("后台处理服务", el("div", {class: "stack"}, el("dl", {class: "facts"},
        el("dt", {}, "运行环境"), el("dd", {}, data.mode === "production" ? "生产配置" : "开发配置"),
        el("dt", {}, "运行状态"), el("dd", {}, state),
        el("dt", {}, "最近收到信号"), el("dd", {}, localTime(worker.last_seen)),
        el("dt", {}, "最早排队时间"), el("dd", {}, localTime(worker.oldest_queued_at)),
        el("dt", {}, "等待续跑的任务"), el("dd", {}, String(worker.expired_leases ?? 0))), help))));
}
