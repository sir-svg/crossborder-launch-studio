"use strict";

const main = document.querySelector("#page");
const titles = {
  launch: "上新任务", products: "商品资料", skills: "技能与模板", history: "历史与草稿",
  review: "审核队列", operations: "运行状态", team: "成员管理", audit: "操作记录", account: "我的账号",
};
const statuses = {queued: "待执行", running: "执行中", completed: "已完成", failed: "失败"};
const reviewLabels = {draft: "待送审", pending: "审核中", approved: "已通过", rejected: "已退回"};
let config;
let session;
let view = 0;
let events;
let noticeTimer;

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child != null) node.append(child);
  }
  return node;
}

function notify(message, error = false) {
  const node = document.querySelector("#notice");
  node.textContent = message;
  node.className = error ? "error" : "";
  node.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { node.hidden = true; }, 4800);
}

class RequestError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function httpRequest(path, method = "GET", body, headers = {}, signal) {
  const crossWorkspace = ["/auth/me", "/auth/login", "/auth/logout", "/auth/workspace"].includes(path);
  const workspaceId = session?.demo || crossWorkspace ? null : session?.workspace?.id;
  const response = await fetch("/api" + path, {
    method,
    signal,
    headers: {
      ...headers,
      ...(workspaceId ? {"X-Workspace-ID": workspaceId} : {}),
      ...(body === undefined ? {} : {"Content-Type": "application/json"}),
    },
    ...(body === undefined ? {} : {
      body: JSON.stringify(body),
    }),
  });
  if (!response.ok) {
    const result = await response.json().catch(() => null);
    if (response.status === 401 && session?.authenticated && !session.demo && !path.startsWith("/auth/")) {
      session = null;
      config = null;
      loginPage();
    }
    const fallback = {
      401: "登录已失效，请重新登录。",
      403: "当前账号没有执行此操作的权限。",
      409: "资料已被更新，请刷新页面后再试。",
      429: "尝试太频繁，请稍后再试。",
    };
    throw new RequestError(
      typeof result?.detail === "string" ? result.detail : fallback[response.status] || "操作未完成，请检查填写的内容或稍后重试。",
      response.status,
    );
  }
  return response;
}

async function api(path, method = "GET", body, headers = {}) {
  const response = await httpRequest(path, method, body, headers);
  return response.status === 204 ? null : await response.json();
}

function can(permission) {
  return session?.demo === true || session?.permissions?.includes(permission) === true;
}

function localTime(value) {
  if (!value) return "—";
  const stamp = new Date(value);
  return Number.isNaN(stamp.getTime()) ? "—" : stamp.toLocaleString("zh-CN");
}

function actorName(id) {
  if (!id) return "系统";
  if (id === session?.user?.id) return "我";
  return "成员 " + String(id).slice(0, 8);
}

function button(label, click, style = "secondary", type = "button") {
  return el("button", {type, class: style, ...(click ? {onclick: click} : {})}, label);
}

function action(label, callback, style) {
  return button(label, async (event) => {
    const node = event.currentTarget;
    node.disabled = true;
    try { await callback(); }
    catch (error) { notify(error.message, true); }
    finally { node.disabled = false; }
  }, style);
}

function formRun(form, callback) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try { await callback(new FormData(form)); }
    catch (error) { notify(error.message, true); }
    finally { submit.disabled = false; }
  });
}

function field(label, name, value = "", options = {}) {
  const {area = false, ...attrs} = options;
  const input = el(area ? "textarea" : "input", {name, ...attrs});
  input.value = value;
  return el("label", {}, label, input);
}

function select(label, name, options, selected) {
  const node = el("select", {name, required: "", "aria-label": label}, options.map(([value, text]) =>
    el("option", {value}, text)));
  if (selected) node.value = selected;
  return el("label", {}, label, node);
}

function head(title, description) {
  return el("div", {class: "page-head"}, el("div", {},
    el("h1", {}, title), el("p", {}, description)));
}

function panel(title, content, extra) {
  return el("section", {class: "panel"},
    el("div", {class: "panel-top"}, el("h2", {}, title), extra),
    el("div", {class: "block"}, content));
}

function badge(status) {
  return el("span", {class: "status " + status}, statuses[status] || "未知状态");
}

function reviewBadge(status) {
  return el("span", {class: "review-status " + status}, reviewLabels[status] || "待送审");
}

function pagedControls(offset, count, limit, load) {
  return el("div", {class: "button-row pagination"},
    offset > 0 ? button("上一页", () => load(Math.max(0, offset - limit))) : null,
    count ? el("span", {class: "subtle"}, "第 " + (offset + 1) + "–" + (offset + count) + " 条") : null,
    count === limit ? button("下一页", () => load(offset + limit)) : null);
}

function go(route) {
  const target = "#" + route;
  if (location.hash === target) render();
  else location.hash = target;
}

function facts(product, snapshot = false) {
  return el("div", {class: "stack"},
    el("p", {class: "subtle"}, product.sku + " · " + product.source
      + (snapshot ? " · 本次任务使用的资料" : "")),
    el("dl", {class: "facts"}, product.facts.flatMap((item) =>
      [el("dt", {}, item.label), el("dd", {}, item.value || "待补充")])),
    el("p", {}, product.features.join(" · ") || "还没有商品特点。"),
    el("p", {class: "subtle"}, product.keywords.join(" / ") || "还没有关键词。"));
}

function timeline(taskEvents = []) {
  const completed = new Map();
  for (const event of taskEvents) {
    if (!event.step && event.status === "running") completed.clear();
    if (event.step) completed.set(event.step, event.status);
  }
  return el("ol", {class: "timeline"}, config.template.steps.map((step) => {
    const state = completed.get(step.key) || "waiting";
    return el("li", {}, el("span", {class: "dot " + state, "aria-label": statuses[state] || "未开始"},
      {completed: "✓", running: "…", failed: "!"}[state] || "·"),
    el("div", {}, el("div", {class: "step"}, step.label), el("p", {class: "subtle"}, step.description)));
  }));
}

async function launchPage(token, preferredId) {
  const products = await api("/products");
  if (preferredId && !products.some((item) => item.id === preferredId)) {
    products.unshift(await api("/products/" + preferredId));
  }
  if (token !== view) return;
  if (!can("edit") || products.length === 0) {
    const empty = el("div", {class: "empty"},
      el("p", {}, can("edit") ? "先添加一件商品，再用它的资料生成上架稿。" : "当前角色可以查看已经创建的任务。"),
      can("edit") ? button("添加第一件商品", () => go("products/new"), "primary")
        : button("查看历史任务", () => go("history")));
    main.replaceChildren(head("开始一项上新任务", "每个任务都会记录当时使用的商品资料。"),
      panel("任务设置", empty));
    return;
  }
  const markets = Object.entries(config.markets);
  const product = select("商品", "product_id", products.map((p) => [p.id, p.name + " · " + p.sku]), preferredId);
  const market = select("目标站点", "market", markets.map(([key, item]) => [key, item.label]));
  const language = select("文案语言", "language",
    markets.map(([, item]) => [item.language, item.language_name]));
  function syncLanguage() {
    language.querySelector("select").value = config.markets[market.querySelector("select").value].language;
  }
  market.addEventListener("change", syncLanguage);
  language.addEventListener("change", () => {
    const selected = language.querySelector("select").value;
    market.querySelector("select").value = markets.find(([, item]) => item.language === selected)[0];
  });
  const form = el("form", {}, product, el("div", {class: "row"}, market, language),
    config.model
      ? field("文案要求（可选）", "notes", "", {area: true, maxlength: "600", placeholder: "例如：语气简洁，突出桌面收纳"})
      : el("p", {class: "subtle"}, "当前使用样例草稿或资料整理模板。连接模型后可按自定义要求撰写。"),
    button("生成上架稿", null, "primary", "submit"));
  let lastForm;
  let requestId;
  formRun(form, async (values) => {
    const body = Object.fromEntries(values);
    const key = JSON.stringify(body);
    if (key !== lastForm) {
      lastForm = key;
      requestId = crypto.randomUUID();
    }
    const task = await api("/tasks", "POST", {...body, request_id: requestId});
    if (token === view) go("task/" + task.id);
  });
  const preview = el("div", {class: "stack"});
  function showProduct() {
    const selected = products.find((p) => p.id === product.querySelector("select").value);
    preview.replaceChildren(facts(selected), el("p", {class: "note"}, "生成后在这里核对评论依据、编辑草稿和下载。"));
  }
  product.addEventListener("change", showProduct);
  showProduct();
  main.replaceChildren(head("开始一项上新任务", "选好商品、站点和语言，生成可编辑的商品上架稿。"),
    el("div", {class: "split"},
      el("div", {class: "stack"},
        panel("任务设置", form, button("商品库", () => go("products"))),
        panel("处理步骤", timeline())),
      panel("商品资料预览", preview)));
}

function reviewBox(review) {
  return el("details", {},
    el("summary", {}, review.id + " · " + review.rating + "/5 · " + review.source),
    el("p", {}, review.text));
}

function reviewEvidence(task) {
  const reviews = task.snapshot.reviews;
  const findings = task.insights?.findings || [];
  return el("div", {class: "stack"},
    el("p", {class: "subtle"}, "本次使用 " + reviews.length + " 条评论，共 "
      + task.snapshot.review_count + " 条。"),
    reviews.length < 3 ? el("p", {class: "note warning"}, "样本较少，观点仅供参考。") : null,
    findings.length ? findings.map((finding) => el("div", {class: "note"},
      el("strong", {}, {positive: "好评", negative: "待改进", selling_point: "卖点参考"}[finding.kind]),
      el("p", {}, finding.text), el("small", {}, finding.review_ids.join(" · ")),
      ...(finding.quotes || []).map((item) => el("blockquote", {class: "review-quote"},
        el("p", {}, item.quote), el("cite", {}, item.review_id)))))
      : el("p", {class: "subtle"}, "当前没有归纳结果。"),
    reviews.map(reviewBox));
}

function listingSection(title, content) {
  return el("div", {class: "listing-section"}, el("h3", {}, title), content);
}

function exportLink(path, label) {
  return el("a", {class: "download", href: "/api" + path, onclick: async (event) => {
    const link = event.currentTarget;
    event.preventDefault();
    if (link.getAttribute("aria-busy")) return;
    link.setAttribute("aria-busy", "true");
    try {
      const response = await httpRequest(path);
      const original = response.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1];
      const objectUrl = URL.createObjectURL(await response.blob());
      const download = el("a", {href: objectUrl, download: original || "listing"});
      document.body.append(download);
      download.click();
      download.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch (error) {
      notify(error.message, true);
    } finally {
      link.removeAttribute("aria-busy");
    }
  }}, label);
}

function reviewControls(task, version, token) {
  const path = "/tasks/" + task.id;
  const state = version.review_status || "draft";
  const info = el("div", {class: "review-card stack"},
    el("div", {class: "button-row"}, el("h3", {}, "版本审核"), reviewBadge(state)),
    version.submitted_at ? el("p", {class: "subtle"},
      actorName(version.submitted_by) + " · 送审于 " + localTime(version.submitted_at)) : null,
    version.submit_note ? el("div", {}, el("p", {class: "subtle"}, "送审说明"),
      el("p", {class: "review-note"}, version.submit_note)) : null,
    version.decided_by ? el("p", {class: "subtle"},
      actorName(version.decided_by) + " · 审核于 " + localTime(version.decided_at)) : null,
    version.review_note ? el("div", {}, el("p", {class: "subtle"}, "审核意见"),
      el("p", {class: "review-note"}, version.review_note)) : null);
  const ownsVersion = [version.author_id, version.submitted_by].includes(session.user.id);
  const hasErrors = version.checks.some((item) => item.level === "error");
  if (state === "approved") return info;
  if (state === "pending" && can("review") && ownsVersion) {
    info.append(el("p", {class: "subtle"}, "需要由其他拥有审核权限的成员处理。"));
  }
  if (state === "pending" && can("review") && !ownsVersion) {
    const noteField = field("审核意见", "note", "", {
      area: true, maxlength: 600, placeholder: "通过时可选，退回时请写明需要修改的内容",
    });
    const noteInput = noteField.querySelector("textarea");
    const decide = async (decision) => {
      const note = noteInput.value.trim();
      if (decision === "rejected" && !note) {
        noteInput.focus();
        throw new Error("退回前请填写审核意见。");
      }
      await api(path + "/review", "POST", {version: version.version, decision, note});
      notify(decision === "approved" ? "审核已通过，可以导出此版本。" : "已退回此版本。");
      if (token === view) render("draft");
    };
    const pass = action("审核通过", () => decide("approved"), "primary");
    pass.disabled = hasErrors;
    info.append(noteField, el("div", {class: "button-row"}, pass,
      action("退回修改", () => decide("rejected"))));
  }
  if ((state === "draft" || state === "rejected") && can("edit")) {
    if (hasErrors) {
      info.append(el("p", {class: "note warning"}, "请先修正检查结果中的错误，再提交审核。"));
      return info;
    }
    if (state === "rejected") {
      info.append(el("p", {class: "subtle"}, "根据审核意见编辑并保存新版本后，再提交审核。"));
      return info;
    }
    const form = el("form", {},
      field("送审说明（可选）", "note", "", {area: true, maxlength: 600}),
      button("提交审核", null, "primary", "submit"));
    formRun(form, async (values) => {
      await api(path + "/submit-review", "POST", {version: version.version, note: values.get("note").trim()});
      notify("当前版本已送审。");
      if (token === view) render("draft");
    });
    info.append(form);
  }
  return info;
}

function evidenceDetails(items) {
  if (!Array.isArray(items) || !items.length) return null;
  return el("details", {class: "evidence-details"}, el("summary", {}, "核对资料 · " + items.length + " 项"),
    el("ul", {}, items.map((item) => el("li", {},
      el("strong", {}, item.label), el("p", {}, item.value), el("small", {}, item.source || item.id)))));
}

function listing(task, token) {
  const version = task.versions[0];
  const draft = version.draft;
  const path = "/tasks/" + task.id;
  const state = session.demo
    ? version.approved_at ? "已确认" : "待确认"
    : reviewLabels[version.review_status] || "待送审";
  const mode = {model: "模型生成", sample: "样例草稿", template: "资料整理模板"}[task.mode];
  const status = el("p", {class: "subtle"}, "版本 " + version.version + " · " + state + " · " + (mode || "已保存草稿"));
  const sources = version.version === task.run_metadata?.field_sources_version
    ? task.run_metadata.field_sources || {} : {};
  const content = el("div", {},
    listingSection("标题", [el("p", {}, draft.title), evidenceDetails(sources.title)]),
    listingSection("五点描述", el("ol", {}, draft.bullets.map((line, index) =>
      el("li", {}, line, evidenceDetails(sources["bullet_" + (index + 1)]))))),
    listingSection("商品描述", [el("p", {}, draft.description), evidenceDetails(sources.description)]),
    listingSection("搜索词", [el("p", {}, draft.search_terms), evidenceDetails(sources.search_terms)]));
  const controls = el("div", {class: "button-row"});
  const history = task.versions.length > 1 ? el("div", {class: "stack"},
    task.versions.slice(1).map((old) => el("details", {},
      el("summary", {}, "版本 " + old.version + " · " + (session.demo
        ? old.approved_at ? "曾确认" : "未确认"
        : reviewLabels[old.review_status] || "历史草稿")),
      old.review_note ? el("p", {class: "review-note"}, old.review_note) : null,
      listingSection("标题", el("p", {}, old.draft.title)),
      listingSection("五点描述", el("ol", {}, old.draft.bullets.map((line) => el("li", {}, line)))),
      listingSection("商品描述", el("p", {}, old.draft.description)),
      listingSection("搜索词", el("p", {}, old.draft.search_terms))))) : null;
  const review = session.demo ? null : reviewControls(task, version, token);
  if (can("edit")) controls.append(button("编辑草稿", () => {
    const form = el("form", {},
      field("标题", "title", draft.title, {maxlength: "600"}),
      draft.bullets.map((line, i) => field("五点 " + (i + 1), "bullet" + i, line, {area: true, maxlength: "600"})),
      field("商品描述", "description", draft.description, {area: true, rows: "5", maxlength: "4500"}),
      field("搜索词", "search_terms", draft.search_terms, {maxlength: "600"}),
      !session.demo && version.review_status === "pending"
        ? el("p", {class: "note warning"}, "保存后，审核对象会变成旧版本。请重新送审新版本。") : null,
      el("div", {class: "button-row"}, button("保存新版本", null, "primary", "submit"),
        button("取消", () => render("draft"))));
    formRun(form, async (values) => {
      const edited = Object.fromEntries([...values].filter(([name]) => !name.startsWith("bullet")));
      edited.bullets = draft.bullets.map((_, i) => values.get("bullet" + i));
      await api(path + "/draft", "PUT", {base_version: version.version, draft: edited});
      notify("新版本已保存，文案已重新检查。");
      if (token === view) render("draft");
    });
    content.replaceChildren(form);
    controls.hidden = true;
    if (review) review.hidden = true;
    form.querySelector("input").focus();
  }));
  if (version.approved_at && (session.demo || version.review_status === "approved")) {
    for (const [kind, label] of [["csv", "下载 CSV"], ["md", "下载 Markdown"]]) {
      controls.append(exportLink(path + "/export/" + kind + "?version=" + version.version, label));
    }
  } else if (session.demo) {
    const approve = action("确认当前版本", async () => {
      await api(path + "/approve", "POST", {version: version.version});
      notify("版本已确认，可以导出。");
      if (token === view) render("draft");
    }, "primary");
    approve.disabled = version.checks.some((item) => item.level === "error");
    controls.append(approve);
  }
  return el("div", {class: "stack"}, status, content, controls, review, history);
}

function runDetails(task) {
  if (session.demo && !task.run_metadata) return null;
  const run = task.run_metadata || {};
  const reported = (run.calls || []).flatMap((call) =>
    typeof call.usage?.total_tokens === "number" ? [call.usage.total_tokens] : []);
  const data = [
    ["发起人", task.submitted_by ? actorName(task.submitted_by) : null],
    ["执行次数", task.attempt == null ? null : String(task.attempt)],
    ["模型", typeof run.model === "string" ? run.model : null],
    ["模型用量", reported.length ? reported.reduce((sum, next) => sum + next, 0) + " tokens" : null],
    ["检查规则", typeof run.ruleset === "string" ? run.ruleset : null],
  ].filter(([, value]) => value !== null);
  return data.length ? el("dl", {class: "facts run-facts"}, data.flatMap(([label, value]) =>
    [el("dt", {}, label), el("dd", {}, value)])) : null;
}

function eventFrames(buffer, handle) {
  const pieces = buffer.split(/\r?\n\r?\n/);
  for (const piece of pieces.slice(0, -1)) {
    const lines = piece.split(/\r?\n/);
    const name = lines.find((line) => line.startsWith("event:"))?.slice(6).trim() || "message";
    const data = lines.filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, "")).join("\n");
    if (data) handle(name, JSON.parse(data));
  }
  return pieces.at(-1);
}

async function streamTask(id, cursor, token, connection, update) {
  const controller = new AbortController();
  events = controller;
  let backoff = 700;
  while (!controller.signal.aborted && token === view) {
    try {
      const response = await httpRequest("/tasks/" + id + "/events?since=" + cursor,
        "GET", undefined, {"Accept": "text/event-stream"}, controller.signal);
      connection.textContent = "实时接收进度";
      backoff = 700;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let ended = false;
      try {
        while (!ended) {
          const chunk = await reader.read();
          if (chunk.done || token !== view) break;
          buffer = eventFrames(buffer + decoder.decode(chunk.value, {stream: true}), (name, data) => {
            if (name === "end") {
              ended = true;
            } else if (name === "auth" || name === "session") {
              throw new RequestError(data.detail || "会话状态有变化，请刷新页面。", 409);
            } else if (name === "message" && data.seq > cursor) {
              cursor = data.seq;
              update(data);
            }
          });
        }
      } finally {
        await reader.cancel().catch(() => {});
      }
      if (ended && token === view) {
        render("draft");
        return;
      }
    } catch (error) {
      if (controller.signal.aborted || token !== view) return;
      if ([401, 403, 404, 409].includes(error.status)) {
        connection.replaceChildren(error.message, " ", button("重新加载", () => location.reload()));
        return;
      }
    }
    connection.textContent = "连接中断，正在重新连接。";
    await new Promise((resolve) => setTimeout(resolve, backoff));
    backoff = Math.min(backoff * 2, 10000);
  }
}

async function taskPage(taskId, token, selectedTab) {
  const task = await api("/tasks/" + taskId);
  if (token !== view) return;
  const version = task.versions[0];
  const left = el("div", {class: "stack"}, facts(task.snapshot, true),
    el("p", {class: "subtle"}, config.markets[task.input.market].label + " · " + task.input.language),
    task.input.notes ? el("p", {}, task.input.notes) : null,
    task.retry_of ? button("查看原任务", () => go("task/" + task.retry_of)) : null);
  const steps = el("div", {}, timeline(task.events));
  const connection = el("p", {class: "subtle", "aria-live": "polite"}, "正在连接任务进度…");
  const active = task.status === "queued" || task.status === "running";
  const progress = el("div", {class: "stack"}, steps, runDetails(task), active ? connection : null);
  if (task.error) {
    const requestId = crypto.randomUUID();
    progress.append(el("p", {class: "note error"}, task.error));
    if (can("edit")) progress.append(action("重试这项任务", async () => {
      const next = await api("/tasks/" + task.id + "/retry", "POST", {request_id: requestId});
      if (token === view) go("task/" + next.id);
    }));
  }
  const body = el("div", {class: "block"});
  const tabs = el("div", {class: "tabs", "aria-label": "任务结果"});
  const options = [["draft", "上架草稿"], ["facts", "商品事实"], ["reviews", "评论依据"], ["checks", "检查结果"]];
  function display(key) {
    for (const tab of tabs.children) tab.setAttribute("aria-pressed", String(tab.dataset.tab === key));
    if (key === "facts") body.replaceChildren(facts(task.snapshot, true));
    else if (key === "reviews") body.replaceChildren(reviewEvidence(task));
    else if (key === "checks" && version) {
      body.replaceChildren(el("div", {class: "stack"},
        el("p", {class: "subtle"}, "版本 " + version.version + " · 工作台规则检查"),
        version.checks.length ? version.checks.map((item) => el("div", {class: "note " + item.level},
          el("p", {}, item.field + " · " + item.text), evidenceDetails(item.evidence)))
          : el("p", {}, "本次检查未发现问题。")));
    } else if (version) body.replaceChildren(listing(task, token));
    else body.replaceChildren(el("div", {class: "empty"}, el("h3", {}, "草稿尚未生成"),
      el("p", {}, task.error || "执行完成后，草稿和检查结果会保存在这里。")));
  }
  for (const [key, label] of options) {
    const tab = button(label, () => display(key), "tab");
    tab.dataset.tab = key;
    tabs.append(tab);
  }
  display(selectedTab || (version ? "draft" : "facts"));
  const status = badge(task.status);
  main.replaceChildren(head(task.snapshot.name, "上新任务 · " + task.created_at.slice(0, 10)),
    el("div", {class: "split"},
      el("div", {class: "stack"}, panel("任务资料", left), panel("处理记录", progress, status)),
      el("section", {class: "panel"}, el("div", {class: "panel-top"}, el("h2", {}, "上新结果")), tabs, body)));
  if (active) {
    const cursor = task.events.at(-1)?.seq || 0;
    streamTask(task.id, cursor, token, connection, (update) => {
      task.events.push(update);
      steps.replaceChildren(timeline(task.events));
      if (!update.step) {
        status.className = "status " + update.status;
        status.textContent = statuses[update.status];
      }
    });
  }
}

function lines(text) {
  return text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

async function productsPage(token, productId, offset = 0) {
  const limit = 40;
  const products = await api("/products?limit=" + limit + "&offset=" + offset);
  const selected = productId || products[0]?.id;
  const product = selected && selected !== "new" ? await api("/products/" + selected) : null;
  if (token !== view) return;
  const choose = el("div", {class: "stack"},
    can("edit") ? button("＋ 新建商品", () => go("products/new")) : null,
    products.map((item) => button(item.sku + " · " + item.name, () => go("products/" + item.id),
      item.id === selected ? "primary" : "secondary")),
    !products.length ? el("p", {class: "subtle"}, "这一页没有商品。") : null,
    pagedControls(offset, products.length, limit, (next) => productsPage(token, null, next)
      .catch((error) => notify(error.message, true))));
  if (!can("edit")) {
    const data = product
      ? el("div", {class: "stack"}, panel(product.name, facts(product)),
        panel("评论 · " + product.reviews.length, el("div", {class: "stack"}, product.reviews.map(reviewBox))),
        productRevisions(product))
      : panel("商品资料", el("p", {class: "empty"}, "还没有可查看的商品。"));
    main.replaceChildren(head("商品资料", "查看上架稿所依据的规格、关键词和买家评论。"),
      el("div", {class: "split"}, panel("商品列表", choose), data));
    return;
  }
  const form = el("form", {},
    el("div", {class: "row"},
      field("SKU", "sku", product?.sku, {required: "", maxlength: "32", pattern: "[A-Za-z0-9][A-Za-z0-9_\\-]{0,31}"}),
      field("商品名称", "name", product?.name, {required: "", maxlength: "160"})),
    field("资料来源", "source", product?.source || "卖家录入", {required: "", maxlength: "160"}),
    field("规格（每行填 规格名=规格值）", "facts",
      product?.facts.map((item) => item.label + "=" + item.value).join("\n"), {area: true, rows: "5", maxlength: "10000"}),
    field("商品特点（每行一项）", "features", product?.features.join("\n"), {area: true, maxlength: "3000"}),
    field("关键词（每行一项）", "keywords", product?.keywords.join("\n"), {area: true, maxlength: "5000"}),
    button("保存商品资料", null, "primary", "submit"));
  formRun(form, async (values) => {
    const data = Object.fromEntries(values);
    data.facts = lines(data.facts).map((line) => {
      const separator = line.indexOf("=");
      if (separator < 1) throw new Error("每行规格请用「规格名=规格值」填写。");
      return {label: line.slice(0, separator), value: line.slice(separator + 1)};
    });
    data.features = lines(data.features);
    data.keywords = lines(data.keywords);
    const headers = product?.revision != null ? {"If-Match": String(product.revision)} : {};
    const result = await api("/products" + (product ? "/" + product.id : ""),
      product ? "PUT" : "POST", data, headers);
    notify("商品资料已保存。");
    if (token === view) go("products/" + result.id);
  });
  const editor = el("div", {class: "stack"}, panel(product ? product.name : "新商品", form,
    product ? button("用这件商品生成", () => go("launch/" + product.id)) : null));
  if (product) {
    const file = el("input", {type: "file", name: "csv", accept: ".csv,text/csv", required: ""});
    const upload = el("form", {},
      el("p", {class: "subtle"}, "上传 UTF-8 CSV，表头为 id,rating,text,source。相同编号会更新对应评论。"),
      el("label", {}, "评论 CSV 文件", file), button("导入评论", null, "secondary", "submit"));
    formRun(upload, async () => {
      const chosen = file.files[0];
      if (chosen.size > 500000) throw new Error("请选择小于 500 KB 的 CSV 文件。");
      await api("/products/" + product.id + "/reviews", "POST", {csv_text: await chosen.text()});
      notify("评论已导入。");
      if (token === view) render();
    });
    editor.append(panel("评论 · " + product.reviews.length,
      el("div", {class: "stack"}, upload, product.reviews.map(reviewBox))), productRevisions(product));
  }
  main.replaceChildren(head("商品资料", "维护上架稿所依据的规格、特点、关键词和买家评论。"),
    el("div", {class: "split"}, panel("商品列表", choose), editor));
}

function productRevisions(product) {
  if (product.revision == null) return el("div", {hidden: ""});
  const entries = el("div", {class: "stack"}, el("p", {class: "subtle"}, "当前为修订 " + product.revision + "。"));
  const load = action("读取修订记录", async () => {
    const result = await api("/products/" + product.id + "/revisions");
    entries.replaceChildren(...(result.length ? result.map((item) => el("details", {},
      el("summary", {}, "修订 " + item.revision + " · " + actorName(item.actor_id) + " · " + localTime(item.created_at)),
      item.data ? el("div", {class: "revision-facts"}, facts({sku: product.sku, ...item.data})) : null))
      : [el("p", {class: "subtle"}, "还没有保存的修订记录。")]));
    load.hidden = true;
  });
  return panel("资料修订", entries, load);
}

function skillsPage() {
  const skills = config.template.steps.slice(1);
  main.replaceChildren(head("技能与模板", "同一套流程可以换商品、站点和语言重复使用。"),
    panel("商品上新模板 · v" + config.template.version,
      el("div", {class: "stack"}, timeline(), can("edit") ? button("使用此模板", () => go("launch"), "primary") : null)),
    el("div", {class: "skill-grid"}, skills.map((skill) => panel(skill.label, el("p", {}, skill.description)))));
}

async function historyPage(token, filter, reviewQueue = false, offset = 0) {
  const limit = 30;
  const choices = reviewQueue ? reviewLabels : statuses;
  const selected = filter && Object.hasOwn(choices, filter) ? filter : "";
  const params = new URLSearchParams({limit, offset});
  if (selected) params.set(reviewQueue ? "review_status" : "status", selected);
  const tasks = await api("/tasks?" + params);
  if (token !== view) return;
  const target = reviewQueue ? "review" : "history";
  const filterBar = el("div", {class: "tabs history-filters", "aria-label": "筛选记录"},
    [["", "全部"], ...Object.entries(choices)].map(([key, label]) => {
      const node = button(label, () => go(target + "/" + key), "tab");
      node.setAttribute("aria-pressed", String(key === selected));
      return node;
    }));
  const rows = tasks.map((task) => el("div", {class: "list-item"},
    el("div", {}, el("strong", {}, task.product_name), el("p", {class: "subtle"},
      task.sku + " · " + (config.markets[task.input.market]?.label || task.input.market) + " · "
      + localTime(task.created_at))),
    el("div", {class: "button-row"}, badge(task.status),
      task.review_status && !session.demo ? reviewBadge(task.review_status) : null,
      button("打开", () => go("task/" + task.id)))));
  const empty = el("div", {class: "empty"}, el("p", {}, selected ? "没有符合此筛选条件的任务。" : "没有可查看的任务。"),
    !selected && !reviewQueue && can("edit") ? button("创建第一项任务", () => go("launch"), "primary") : null);
  main.replaceChildren(
    head(reviewQueue ? "审核队列" : "历史与草稿", reviewQueue
      ? "打开待审版本，核对商品事实、文案和检查结果后作出决定。"
      : "打开任务可查看执行状态、商品资料和历次保存的草稿。"),
    panel(reviewQueue ? "草稿审核" : "任务列表", el("div", {class: "stack"}, filterBar,
      rows.length ? el("div", {}, rows) : empty,
      pagedControls(offset, rows.length, limit, (next) => historyPage(token, selected, reviewQueue, next)
        .catch((error) => notify(error.message, true))))));
}

async function render(tab) {
  const token = ++view;
  events?.abort();
  const [name, id] = location.hash.slice(1).split("/");
  main.replaceChildren(el("p", {class: "loading"}, "加载中…"));
  try {
    session ??= await api("/auth/me");
    if (token !== view) return;
    config ??= await api("/config");
    if (token !== view) return;
    workspaceHeader();
    let page = name === "task" ? (can("edit") ? "launch" : "history")
      : Object.hasOwn(titles, name) ? name : (can("edit") ? "launch" : "history");
    const hidden = {
      launch: !can("edit"), review: session.demo, operations: session.demo,
      team: session.demo || !can("manage"), audit: session.demo || !can("manage"),
      account: session.demo,
    };
    if (hidden[page]) page = "history";
    for (const node of document.querySelectorAll("[data-page]")) {
      node.hidden = Boolean(hidden[node.dataset.page]);
      if (node.dataset.page === page) node.setAttribute("aria-current", "page");
      else node.removeAttribute("aria-current");
    }
    document.querySelector("#breadcrumb").textContent = titles[page];
    document.querySelector("#new-task").hidden = !can("edit");
    document.querySelector("#mode").textContent = session.demo ? config.model || "本地演示模式"
      : roleLabels[session.workspace.role] + " · " + (config.model || "资料整理模式");
    if (name === "task" && id) await taskPage(id, token, tab);
    else if (page === "history") await historyPage(token, id);
    else if (page === "review") await historyPage(token, id === undefined ? "pending" : id, true);
    else if (page === "products") await productsPage(token, id);
    else if (page === "skills") skillsPage();
    else if (page === "operations") await operationsPage(token);
    else if (page === "team") await teamPage(token);
    else if (page === "audit") await auditPage(token);
    else if (page === "account") accountPage(token);
    else await launchPage(token, id);
  } catch (error) {
    if (token !== view) return;
    if (error.status === 401) {
      applySession(null);
      loginPage();
      return;
    }
    main.replaceChildren(panel("页面加载失败", el("div", {class: "stack"},
      el("p", {}, error.message), button("重新加载", () => render(tab)))));
  }
}

window.addEventListener("hashchange", () => render());
document.querySelectorAll("[data-page]").forEach((node) => {
  node.addEventListener("click", () => go(node.dataset.page));
});
document.querySelector("#new-task").addEventListener("click", () => go("launch"));
render();
