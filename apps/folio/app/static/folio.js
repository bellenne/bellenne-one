"use strict";

const NODE_LABELS = {
  start: "Начало", send: "Отправить без ожидания", ask_text: "Задать вопрос",
  ask_photo: "Запросить фото", choice: "Предложить выбор шаблона",
  condition: "Проверить условие", wait: "Ждать ответ", handoff: "Передать менеджеру",
  confirm: "Попросить подтверждение", retailcrm: "Создать сделку в RetailCRM",
  ready: "Проверить готовность", end: "Завершить"
};
const NODE_HELP = {
  start: "Точка входа в сценарий. Настраивать ничего не нужно.",
  send: "Отправляет текст и сразу идёт дальше. Ответ покупателя не ожидается и не сохраняется.",
  ask_text: "Отправляет вопрос, ждёт ответ покупателя и сохраняет его в бриф.",
  ask_photo: "Запрашивает и сохраняет фотографии покупателя.",
  choice: "Показывает разрешённые для SKU шаблоны и сохраняет выбор.",
  condition: "Выбирает дальнейший путь по уже собранному значению.",
  wait: "Останавливает сценарий до следующего ответа покупателя.",
  handoff: "Останавливает бота и переводит диалог менеджеру.",
  confirm: "Просит покупателя подтвердить собранный бриф.",
  retailcrm: "Создаёт заказ в RetailCRM по собранному брифу и продолжает сценарий после подтверждения API.",
  ready: "Проверяет обязательные данные перед готовностью брифа.",
  end: "Завершает сценарий без дополнительных действий."
};
const FIELD_LABELS = {
  photos: "Фотографии", background: "Пожелания к фону", caption: "Текст для коллажа",
  wishes: "Дополнительные пожелания", template_id: "Выбранный шаблон"
};
const PRODUCT_FIELDS = {
  portrait_background: ["photos", "background", "wishes"],
  collage: ["photos", "caption", "wishes"],
  template_art: ["photos", "template_id", "wishes"]
};
const TEXT_KINDS = new Set(["send", "ask_text", "ask_photo", "choice", "wait", "confirm"]);
const FIELD_KINDS = new Set(["ask_text", "ask_photo", "choice"]);
const TERMINAL_KINDS = new Set(["ready", "end", "handoff"]);
const SVG_NS = "http://www.w3.org/2000/svg";

function svgElement(name, attributes = {}) {
  const value = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, item]) => value.setAttribute(key, String(item)));
  return value;
}

function initializeBuilder(root) {
  const graph = JSON.parse(root.querySelector("[data-graph-source]").textContent);
  const form = root.querySelector("[data-graph-form]");
  const hidden = root.querySelector("[data-graph-json]");
  const canvas = root.querySelector("[data-graph-canvas]");
  const viewport = root.querySelector("[data-viewport]");
  const edgeLayer = root.querySelector("[data-edges]");
  const nodeLayer = root.querySelector("[data-nodes]");
  const inspector = root.querySelector("[data-inspector]");
  const emptyInspector = root.querySelector("[data-inspector-empty]");
  const stateLabel = root.querySelector("[data-save-state]");
  const publish = document.querySelector("[data-publish]");
  const productType = root.dataset.productType;
  let selected = null;
  let dirty = false;
  let connecting = false;
  let connectionSource = null;
  let view = { x: 0, y: 0, scale: 1 };
  let drag = null;

  graph.nodes.forEach((node, index) => {
    node.title ||= NODE_LABELS[node.kind] || node.id;
    node.position ||= { x: 80 + (index % 4) * 280, y: 100 + Math.floor(index / 4) * 160 };
  });

  function markDirty() {
    dirty = true;
    stateLabel.textContent = "Есть несохранённые изменения";
    if (publish) publish.disabled = true;
    syncGraph();
  }

  function syncGraph() {
    hidden.value = JSON.stringify(graph);
  }

  function nodeById(id) {
    return graph.nodes.find((node) => node.id === id);
  }

  function setView() {
    viewport.setAttribute("transform", `translate(${view.x} ${view.y}) scale(${view.scale})`);
  }

  function edgePath(source, target) {
    const sx = source.position.x + 210;
    const sy = source.position.y + 42;
    const tx = target.position.x;
    const ty = target.position.y + 42;
    const bend = Math.max(70, Math.abs(tx - sx) / 2);
    return `M${sx},${sy} C${sx + bend},${sy} ${tx - bend},${ty} ${tx},${ty}`;
  }

  function renderEdges() {
    edgeLayer.replaceChildren();
    graph.nodes.forEach((source) => {
      [["next", "Далее"], ["otherwise", "Иначе"], ["error", "Ошибка"]].forEach(([field, label]) => {
        const target = nodeById(source[field]);
        if (!target) return;
        const group = svgElement("g", { class: `folio-edge folio-edge-${field}` });
        group.appendChild(svgElement("path", { d: edgePath(source, target), "marker-end": "url(#folio-arrow)" }));
        const text = svgElement("text", {
          x: (source.position.x + target.position.x + 210) / 2,
          y: (source.position.y + target.position.y) / 2 + 30
        });
        text.textContent = label;
        group.appendChild(text);
        edgeLayer.appendChild(group);
      });
    });
  }

  function chooseNode(id) {
    if (connecting) {
      if (!connectionSource) {
        connectionSource = id;
        root.querySelector("[data-connect-help]").textContent = "Теперь выберите целевой шаг.";
      } else if (connectionSource !== id) {
        nodeById(connectionSource).next = id;
        connecting = false;
        connectionSource = null;
        root.querySelector("[data-connect]").classList.remove("button-primary");
        root.querySelector("[data-connect-help]").textContent = "Связь создана. Ветви «Иначе» и «Ошибка» задаются в инспекторе.";
        markDirty();
      }
    }
    selected = id;
    render();
  }

  function renderNodes() {
    nodeLayer.replaceChildren();
    graph.nodes.forEach((node) => {
      const group = svgElement("g", {
        class: `folio-graph-node${selected === node.id ? " is-selected" : ""}${connectionSource === node.id ? " is-connecting" : ""}`,
        transform: `translate(${node.position.x} ${node.position.y})`, tabindex: "0", role: "button",
        "aria-label": `${node.title}, ${NODE_LABELS[node.kind] || node.kind}`
      });
      group.dataset.nodeId = node.id;
      group.appendChild(svgElement("rect", { width: 210, height: 84, rx: 12 }));
      const title = svgElement("text", { x: 16, y: 30, class: "folio-node-title" });
      title.textContent = node.title;
      const type = svgElement("text", { x: 16, y: 58, class: "folio-node-kind" });
      type.textContent = NODE_LABELS[node.kind] || node.kind;
      group.append(title, type);
      group.addEventListener("pointerdown", (event) => {
        event.stopPropagation();
        if (connecting) {
          chooseNode(node.id);
          return;
        }
        selected = node.id;
        nodeLayer.querySelectorAll("[data-node-id]").forEach((item) => {
          item.classList.toggle("is-selected", item.dataset.nodeId === selected);
        });
        renderInspector();
        renderStepList();
        drag = { node, startX: event.clientX, startY: event.clientY, x: node.position.x, y: node.position.y };
        canvas.setPointerCapture(event.pointerId);
      });
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") chooseNode(node.id);
      });
      nodeLayer.appendChild(group);
    });
  }

  function targetOptions(select, current) {
    select.replaceChildren(new Option("Без перехода", ""));
    graph.nodes.filter((node) => node.id !== selected).forEach((node) => {
      select.add(new Option(node.title, node.id, false, node.id === current));
    });
    select.value = current || "";
  }

  function renderInspector() {
    const node = nodeById(selected);
    inspector.hidden = !node;
    emptyInspector.hidden = Boolean(node);
    if (!node) return;
    inspector.querySelector("[data-node-kind-label]").textContent = NODE_LABELS[node.kind] || node.kind;
    inspector.querySelector("[data-node-kind-help]").textContent = NODE_HELP[node.kind] || "";
    const fieldSelect = inspector.querySelector('[data-field="field"]');
    if (fieldSelect) {
      const fields = (PRODUCT_FIELDS[productType] || []).filter((field) =>
        node.kind === "ask_text" ? !["photos", "template_id"].includes(field) : true
      );
      fieldSelect.replaceChildren(new Option("Выберите данные", ""));
      fields.forEach((field) => fieldSelect.add(new Option(FIELD_LABELS[field], field)));
    }
    inspector.querySelectorAll("[data-field]").forEach((control) => {
      const key = control.dataset.field;
      if (["next", "otherwise", "error"].includes(key)) targetOptions(control, node[key]);
      else if (key === "required") control.checked = Boolean(node[key]);
      else if (key === "choices") control.value = (node[key] || []).join("\n");
      else control.value = node[key] ?? "";
    });
    inspector.querySelectorAll("[data-for]").forEach((element) => {
      const kind = node.kind;
      const rule = element.dataset.for;
      const visible = (rule === "text" && TEXT_KINDS.has(kind)) ||
        (rule === "field" && ["ask_text", "condition"].includes(kind)) ||
        (rule === "required" && kind === "ask_text") ||
        (rule === "photo" && kind === "ask_photo") ||
        (rule === "accept" && ["ask_photo", "confirm"].includes(kind)) ||
        (rule === "max_length" && kind === "ask_text") ||
        (rule === "condition" && kind === "condition") ||
        (rule === "retailcrm" && kind === "retailcrm") ||
        (rule === "next" && !TERMINAL_KINDS.has(kind)) ||
        (rule === "error" && ["ask_text", "ask_photo", "choice", "confirm", "retailcrm"].includes(kind));
      element.hidden = !visible;
    });
    const textLabel = inspector.querySelector("[data-text-label]");
    if (textLabel) textLabel.textContent = ["ask_text", "ask_photo", "choice", "confirm", "wait"].includes(node.kind) ? "Вопрос покупателю" : "Сообщение покупателю";
    const fieldLabel = inspector.querySelector("[data-field-label]");
    if (fieldLabel) fieldLabel.textContent = node.kind === "condition" ? "Какие собранные данные проверить" : "Что сохранить в бриф";
    const conditionHelp = inspector.querySelector("[data-condition-field-help]");
    if (conditionHelp) conditionHelp.hidden = node.kind !== "condition";
    const errorLabel = inspector.querySelector("[data-error-label]");
    if (errorLabel) errorLabel.textContent = node.kind === "retailcrm" ? "Если RetailCRM отклонила создание" : "Если ответ не подходит";
  }

  function renderStepList() {
    const list = root.querySelector("[data-step-list]");
    list.replaceChildren();
    graph.nodes.forEach((node, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `folio-step-link${selected === node.id ? " is-selected" : ""}`;
      button.textContent = `${index + 1}. ${node.title} · ${NODE_LABELS[node.kind]}`;
      button.addEventListener("click", () => chooseNode(node.id));
      list.appendChild(button);
    });
  }

  function render() {
    renderEdges();
    renderNodes();
    renderInspector();
    renderStepList();
    syncGraph();
    setView();
  }

  function defaultNode(kind) {
    const id = `step_${crypto.randomUUID().replaceAll("-", "")}`;
    const node = { id, kind, title: NODE_LABELS[kind], position: { x: 120, y: 120 } };
    if (!TERMINAL_KINDS.has(kind)) node.next = "";
    if (TEXT_KINDS.has(kind)) node.text = "";
    if (FIELD_KINDS.has(kind)) {
      const textField = (PRODUCT_FIELDS[productType] || []).find((field) => !["photos", "template_id"].includes(field)) || "wishes";
      Object.assign(node, {
        field: kind === "ask_photo" ? "photos" : kind === "choice" ? "template_id" : textField,
        required: false
      });
    }
    if (kind === "ask_photo") Object.assign(node, { min: null, max: null, accept: "" });
    if (kind === "ask_text") node.max_length = null;
    if (kind === "condition") Object.assign(node, { field: "", equals: "", otherwise: "" });
    if (kind === "confirm") node.accept = "";
    if (kind === "retailcrm") Object.assign(node, {
      comment: "Заказ Ozon: {posting}\nТовар: {product_name}\nSKU: {sku}\nКоличество: {quantity}\n\n{summary}",
      status: "", order_type: "", order_method: "", error: ""
    });
    return node;
  }

  function validateGraph() {
    const problems = [];
    const ids = new Set(graph.nodes.map((node) => node.id));
    const reaches = (fromId, targetId, seen = new Set()) => {
      if (fromId === targetId) return true;
      if (seen.has(fromId)) return false;
      seen.add(fromId);
      const source = nodeById(fromId);
      return source ? [source.next, source.otherwise, source.error].filter(Boolean)
        .some((nextId) => reaches(nextId, targetId, new Set(seen))) : false;
    };
    const add = (node, message) => problems.push({ node, message });
    const starts = graph.nodes.filter((node) => node.kind === "start");
    if (starts.length !== 1) add(starts[0] || graph.nodes[0], "На схеме должен быть ровно один старт.");
    if (!graph.nodes.some((node) => node.kind === "ready")) add(graph.nodes[0], "Добавьте шаг проверки брифа.");
    graph.nodes.forEach((node) => {
      if (TEXT_KINDS.has(node.kind) && !(node.text || "").trim()) add(node, "Заполните текст шага.");
      if (!TERMINAL_KINDS.has(node.kind) && !node.next) add(node, "Укажите следующий шаг.");
      ["next", "otherwise", "error"].forEach((key) => {
        if (node[key] && !ids.has(node[key])) add(node, `Переход «${key}» ведёт на отсутствующий шаг.`);
      });
      if (node.kind === "ask_photo" && (!node.min || !node.max || node.min > node.max)) add(node, "Проверьте минимум и максимум фотографий.");
      if (node.kind === "ask_photo" && productType === "collage" && node.max > 8) add(node, "Для коллажа разрешено не более 8 фотографий.");
      if (node.kind === "confirm" && !(node.accept || "").trim()) add(node, "Укажите ответ, подтверждающий бриф.");
      if (node.kind === "condition" && !node.field) add(node, "Выберите собранные данные для проверки.");
      if (node.kind === "condition" && node.field && !graph.nodes.some((candidate) => FIELD_KINDS.has(candidate.kind) && candidate.field === node.field && reaches(candidate.id, node.id))) add(node, `До этого условия ни один шаг «Задать вопрос» не сохраняет «${FIELD_LABELS[node.field] || node.field}».`);
      if (node.kind === "condition" && !node.otherwise) add(node, "Выберите переход «Если условие не выполнено».");
      if (node.kind === "retailcrm" && !(node.comment || "").trim()) add(node, "Заполните комментарий для сделки RetailCRM.");
      if (node.kind === "retailcrm" && !node.error) add(node, "Выберите переход на случай отказа RetailCRM.");
    });
    const results = root.querySelector("[data-validation-results]");
    results.replaceChildren();
    if (!problems.length) {
      const message = document.createElement("p");
      message.className = "status status-success";
      message.textContent = "Схема прошла предварительную проверку. Сохраните её перед публикацией.";
      results.appendChild(message);
      if (publish) publish.disabled = dirty;
    } else {
      problems.forEach((problem) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "folio-problem";
        button.textContent = `${problem.node?.title || "Схема"}: ${problem.message}`;
        if (problem.node) button.addEventListener("click", () => chooseNode(problem.node.id));
        results.appendChild(button);
      });
      if (publish) publish.disabled = true;
    }
    return problems.length === 0;
  }

  function addPaletteButton(container, kind) {
    const label = NODE_LABELS[kind];
    const button = document.createElement("button");
    button.type = "button";
    button.className = "folio-palette-item";
    button.textContent = label;
    button.addEventListener("click", () => {
      const node = defaultNode(kind);
      graph.nodes.push(node);
      selected = node.id;
      markDirty();
      render();
    });
    container.appendChild(button);
  }
  const palette = root.querySelector("[data-node-palette]");
  const advancedPalette = root.querySelector("[data-node-palette-advanced]");
  ["send", "ask_text", "ask_photo", ...(productType === "template_art" ? ["choice"] : []), "retailcrm", "handoff", "ready", "end"]
    .forEach((kind) => addPaletteButton(palette, kind));
  ["condition", "wait", "confirm"].forEach((kind) => addPaletteButton(advancedPalette, kind));

  inspector.addEventListener("input", (event) => {
    const node = nodeById(selected);
    const control = event.target.closest("[data-field]");
    if (!node || !control) return;
    const key = control.dataset.field;
    if (key === "required") node[key] = control.checked;
    else if (key === "choices") node[key] = control.value.split("\n").map((item) => item.trim()).filter(Boolean);
    else if (["min", "max", "max_length"].includes(key)) node[key] = control.value ? Number(control.value) : null;
    else node[key] = control.value;
    markDirty();
    renderEdges();
    renderNodes();
    renderStepList();
  });

  root.querySelector("[data-delete-node]").addEventListener("click", () => {
    if (!selected) return;
    graph.nodes = graph.nodes.filter((node) => node.id !== selected);
    graph.nodes.forEach((node) => ["next", "otherwise", "error"].forEach((key) => {
      if (node[key] === selected) node[key] = "";
    }));
    selected = null;
    markDirty();
    render();
  });

  root.querySelector("[data-connect]").addEventListener("click", (event) => {
    connecting = !connecting;
    connectionSource = null;
    event.currentTarget.classList.toggle("button-primary", connecting);
    root.querySelector("[data-connect-help]").textContent = connecting ? "Выберите исходный шаг." : "Выберите исходный шаг, затем целевой.";
    render();
  });

  canvas.addEventListener("pointerdown", (event) => {
    if (event.target.closest("[data-node-id]")) return;
    drag = { pan: true, startX: event.clientX, startY: event.clientY, x: view.x, y: view.y };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!drag) return;
    if (drag.pan) {
      view.x = drag.x + event.clientX - drag.startX;
      view.y = drag.y + event.clientY - drag.startY;
      setView();
    } else {
      drag.node.position.x = drag.x + (event.clientX - drag.startX) / view.scale;
      drag.node.position.y = drag.y + (event.clientY - drag.startY) / view.scale;
      renderEdges();
      renderNodes();
      markDirty();
    }
  });
  canvas.addEventListener("pointerup", () => { drag = null; });
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    view.scale = Math.min(1.8, Math.max(0.45, view.scale * (event.deltaY > 0 ? 0.9 : 1.1)));
    setView();
  }, { passive: false });

  root.querySelector("[data-fit]").addEventListener("click", () => {
    if (!graph.nodes.length) return;
    const xs = graph.nodes.map((node) => node.position.x);
    const ys = graph.nodes.map((node) => node.position.y);
    const width = Math.max(...xs) - Math.min(...xs) + 260;
    const height = Math.max(...ys) - Math.min(...ys) + 140;
    view.scale = Math.min(1, canvas.clientWidth / width, canvas.clientHeight / height);
    view.x = 40 - Math.min(...xs) * view.scale;
    view.y = 40 - Math.min(...ys) * view.scale;
    setView();
  });
  root.querySelector("[data-validate]").addEventListener("click", validateGraph);
  form.addEventListener("submit", () => { syncGraph(); dirty = false; });
  window.addEventListener("beforeunload", (event) => {
    if (!dirty) return;
    event.preventDefault();
  });
  render();
}

document.querySelectorAll("[data-scenario-builder]").forEach(initializeBuilder);

document.querySelectorAll("[data-datetime-form]").forEach((form) => {
  const localInput = form.querySelector("[data-datetime-local]");
  const isoInput = form.querySelector("[data-datetime-iso]");
  if (!localInput || !isoInput) return;
  const current = localInput.dataset.iso;
  if (current) {
    const date = new Date(current);
    if (!Number.isNaN(date.getTime())) {
      const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
      localInput.value = local.toISOString().slice(0, 16);
    }
  }
  const sync = () => {
    const date = localInput.value ? new Date(localInput.value) : null;
    isoInput.value = date && !Number.isNaN(date.getTime()) ? date.toISOString() : "";
  };
  localInput.addEventListener("change", sync);
  form.addEventListener("submit", sync);
});

document.querySelectorAll("[data-mapping-form]").forEach((form) => {
  const scenario = form.querySelector("[data-scenario-select]");
  const templates = form.querySelector("[data-template-selection]");
  if (!scenario || !templates) return;
  const update = () => {
    const selected = scenario.selectedOptions[0];
    templates.hidden = selected?.dataset.productType !== "template_art";
    if (templates.hidden) templates.querySelectorAll('input[type="checkbox"]').forEach((input) => { input.checked = false; });
  };
  scenario.addEventListener("change", update);
  update();
});

document.querySelectorAll("[data-open-drawer]").forEach((button) => {
  button.addEventListener("click", () => document.querySelector("[data-order-drawer]")?.showModal());
});
document.querySelectorAll("[data-close-drawer]").forEach((button) => {
  button.addEventListener("click", () => button.closest("dialog").close());
});
document.querySelectorAll("[data-order-drawer]").forEach((drawer) => {
  drawer.addEventListener("click", (event) => {
    if (event.target === drawer) drawer.close();
  });
});

document.querySelectorAll("[data-test-messages]").forEach((messages) => {
  messages.scrollTop = messages.scrollHeight;
});
