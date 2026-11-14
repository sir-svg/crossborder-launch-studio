"use strict";

const main = document.querySelector("#page");
const titles = {launch: "上新任务", products: "商品资料", skills: "技能与模板", history: "历史与草稿"};
const statuses = {queued: "待执行", running: "执行中", completed: "已完成", failed: "失败"};
let config;
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

async function api(path, method = "GET", body) {
  const response = await fetch("/api" + path, {
    method,
    ...(body === undefined ? {} : {
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    }),
  });
  const result = await response.json();
  if (!response.ok) {
    throw new Error(typeof result.detail === "string" ? result.detail : "请检查填写的内容。");
  }
  return result;
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
  return el("span", {class: "status " + status}, statuses[status]);
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
    if (event.step) completed.set(event.step, event.status);
  }
  return el("ol", {class: "timeline"}, config.template.steps.map((step) => {
    const state = completed.get(step.key) || "waiting";
    return el("li", {}, el("span", {class: "dot " + state, "aria-label": statuses[state] || "未开始"},
      {completed: "✓", running: "…", failed: "!"}[state] || "·"),
    el("div", {}, el("div", {class: "step"}, step.label), el("p", {class: "subtle"}, step.description)));
  }));
}

async function launchPage(token) {
  const products = await api("/products");
  if (token !== view) return;
  const markets = Object.entries(config.markets);
  const product = select("商品", "product_id", products.map((p) => [p.id, p.name + " · " + p.sku]));
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
      el("div", {class: "stack"}, panel("任务设置", form), panel("处理步骤", timeline())),
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
      el("p", {}, finding.text), el("small", {}, finding.review_ids.join(" · "))))
      : el("p", {class: "subtle"}, "当前没有归纳结果。"),
    reviews.map(reviewBox));
}

function listingSection(title, content) {
  return el("div", {class: "listing-section"}, el("h3", {}, title), content);
}

function listing(task, token) {
  const version = task.versions[0];
  const draft = version.draft;
  const path = "/tasks/" + task.id;
  const state = version.approved_at ? "已确认" : "待确认";
  const mode = {model: "模型生成", sample: "样例草稿", template: "资料整理模板"}[task.mode];
  const status = el("p", {class: "subtle"}, "版本 " + version.version + " · " + state + " · " + mode);
  const content = el("div", {},
    listingSection("标题", el("p", {}, draft.title)),
    listingSection("五点描述", el("ol", {}, draft.bullets.map((line) => el("li", {}, line)))),
    listingSection("商品描述", el("p", {}, draft.description)),
    listingSection("搜索词", el("p", {}, draft.search_terms)));
  const controls = el("div", {class: "button-row"});
  const history = task.versions.length > 1 ? el("div", {class: "stack"},
    task.versions.slice(1).map((old) => el("details", {},
      el("summary", {}, "版本 " + old.version + " · " + (old.approved_at ? "曾确认" : "未确认")),
      listingSection("标题", el("p", {}, old.draft.title)),
      listingSection("五点描述", el("ol", {}, old.draft.bullets.map((line) => el("li", {}, line)))),
      listingSection("商品描述", el("p", {}, old.draft.description)),
      listingSection("搜索词", el("p", {}, old.draft.search_terms))))) : null;
  controls.append(button("编辑草稿", () => {
    const form = el("form", {},
      field("标题", "title", draft.title, {maxlength: "600"}),
      draft.bullets.map((line, i) => field("五点 " + (i + 1), "bullet" + i, line, {area: true, maxlength: "600"})),
      field("商品描述", "description", draft.description, {area: true, rows: "5", maxlength: "4500"}),
      field("搜索词", "search_terms", draft.search_terms, {maxlength: "600"}),
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
    form.querySelector("input").focus();
  }));
  if (version.approved_at) {
    for (const [kind, label] of [["csv", "下载 CSV"], ["md", "下载 Markdown"]]) {
      controls.append(el("a", {class: "download",
        href: "/api" + path + "/export/" + kind + "?version=" + version.version}, label));
    }
  } else {
    const approve = action("确认当前版本", async () => {
      await api(path + "/approve", "POST", {version: version.version});
      notify("版本已确认，可以导出。");
      if (token === view) render("draft");
    }, "primary");
    approve.disabled = version.checks.some((item) => item.level === "error");
    controls.append(approve);
  }
  return el("div", {class: "stack"}, status, content, controls, history);
}

async function taskPage(taskId, token, selectedTab) {
  const task = await api("/tasks/" + taskId);
  if (token !== view) return;
  const version = task.versions[0];
  const left = el("div", {class: "stack"}, facts(task.snapshot, true),
    el("p", {class: "subtle"}, config.markets[task.input.market].label + " · " + task.input.language),
    task.input.notes ? el("p", {}, task.input.notes) : null,
    task.retry_of ? button("查看原任务", () => go("task/" + task.retry_of)) : null);
  const progress = el("div", {class: "stack"}, timeline(task.events));
  if (task.error) {
    const requestId = crypto.randomUUID();
    progress.append(el("p", {class: "note error"}, task.error), action("重试这项任务", async () => {
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
        version.checks.length ? version.checks.map((item) => el("p", {class: "note " + item.level},
          item.field + " · " + item.text)) : el("p", {}, "本次检查未发现问题。")));
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
  if (task.status === "queued" || task.status === "running") {
    const cursor = task.events.at(-1)?.seq || 0;
    const source = new EventSource("/api/tasks/" + task.id + "/events?since=" + cursor);
    events = source;
    source.onmessage = (event) => {
      const update = JSON.parse(event.data);
      task.events.push(update);
      progress.replaceChildren(timeline(task.events));
      if (!update.step) {
        status.className = "status " + update.status;
        status.textContent = statuses[update.status];
      }
    };
    source.addEventListener("end", () => {
      source.close();
      if (token === view) render("draft");
    });
  }
}

function lines(text) {
  return text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

async function productsPage(token, productId) {
  const products = await api("/products");
  const selected = productId || products[0]?.id;
  const product = selected && selected !== "new" ? await api("/products/" + selected) : null;
  if (token !== view) return;
  const choose = el("div", {class: "stack"},
    button("＋ 新建商品", () => go("products/new")),
    products.map((item) => button(item.sku + " · " + item.name, () => go("products/" + item.id),
      item.id === selected ? "primary" : "secondary")));
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
    const result = await api("/products" + (product ? "/" + product.id : ""), product ? "PUT" : "POST", data);
    notify("商品资料已保存。");
    if (token === view) go("products/" + result.id);
  });
  const editor = el("div", {class: "stack"}, panel(product ? product.name : "新商品", form));
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
      el("div", {class: "stack"}, upload, product.reviews.map(reviewBox))));
  }
  main.replaceChildren(head("商品资料", "维护上架稿所依据的规格、特点、关键词和买家评论。"),
    el("div", {class: "split"}, panel("商品列表", choose), editor));
}

function skillsPage() {
  const skills = config.template.steps.slice(1);
  main.replaceChildren(head("技能与模板", "同一套流程可以换商品、站点和语言重复使用。"),
    panel("商品上新模板 · v" + config.template.version,
      el("div", {class: "stack"}, timeline(), button("使用此模板", () => go("launch"), "primary"))),
    el("div", {class: "skill-grid"}, skills.map((skill) => panel(skill.label, el("p", {}, skill.description)))));
}

async function historyPage(token) {
  const tasks = await api("/tasks");
  if (token !== view) return;
  const rows = tasks.map((task) => el("div", {class: "list-item"},
    el("div", {}, el("strong", {}, task.product_name), el("p", {class: "subtle"},
      task.sku + " · " + config.markets[task.input.market].label + " · "
      + new Date(task.created_at).toLocaleString("zh-CN"))),
    el("div", {class: "button-row"}, badge(task.status), button("打开", () => go("task/" + task.id)))));
  main.replaceChildren(head("历史与草稿", "打开任务可查看执行状态、商品资料和历次保存的草稿。"),
    panel("最近任务", rows.length ? rows : el("div", {class: "empty"}, el("p", {}, "还没有上新任务。"),
      button("创建第一项任务", () => go("launch"), "primary"))));
}

async function render(tab) {
  const token = ++view;
  if (events) events.close();
  const [name, id] = location.hash.slice(1).split("/");
  const page = name === "task" ? "launch" : Object.hasOwn(titles, name) ? name : "launch";
  for (const node of document.querySelectorAll("[data-page]")) {
    if (node.dataset.page === page) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  }
  document.querySelector("#breadcrumb").textContent = titles[page];
  main.replaceChildren(el("p", {class: "loading"}, "加载中…"));
  try {
    config ??= await api("/config");
    if (token !== view) return;
    document.querySelector("#mode").textContent = config.model ? config.model : "本地演示模式";
    if (name === "task" && id) await taskPage(id, token, tab);
    else if (page === "history") await historyPage(token);
    else if (page === "products") await productsPage(token, id);
    else if (page === "skills") skillsPage();
    else await launchPage(token);
  } catch (error) {
    if (token === view) main.replaceChildren(panel("页面加载失败", el("div", {class: "stack"},
      el("p", {}, error.message), button("重新加载", () => render(tab)))));
  }
}

window.addEventListener("hashchange", () => render());
document.querySelectorAll("[data-page]").forEach((node) => {
  node.addEventListener("click", () => go(node.dataset.page));
});
document.querySelector("#new-task").addEventListener("click", () => go("launch"));
render();
