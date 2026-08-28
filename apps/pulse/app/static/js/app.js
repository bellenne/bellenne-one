const svgNS = "http://www.w3.org/2000/svg";

function svgElement(name, attrs = {}) {
  const node = document.createElementNS(svgNS, name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}

function compactNumber(value) {
  if (value >= 1000000) return `${(value / 1000000).toFixed(1)}M`;
  if (value >= 1000) return `${Math.round(value / 1000)}K`;
  return Math.round(value).toString();
}

function chartNumber(value, format) {
  const number = Number(value || 0);
  if (format === "percent") {
    const percent = number * 100;
    return `${percent.toFixed(Math.abs(percent) >= 10 ? 1 : 2)}%`;
  }
  const compact = compactNumber(number);
  return format === "money" ? `${compact} ₽` : compact;
}

function renderLineChart(container) {
  const data = JSON.parse(container.dataset.chart || "[]");
  const actualKey = container.dataset.actual;
  const planKey = container.dataset.plan;
  const tone = container.dataset.tone || "revenue";
  const format = container.dataset.format || "number";
  const width = 800;
  const height = 260;
  const padding = { left: 64, right: 20, top: 20, bottom: 40 };
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, class: "line-chart", role: "img", "aria-label": "Динамика факта и плана" });
  const values = data.flatMap((row) => [Number(row[actualKey] || 0), Number(row[planKey] || 0)]);
  const maxValue = Math.max(...values, format === "percent" ? 0.01 : 1);
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const xFor = (index) => padding.left + (data.length <= 1 ? chartWidth / 2 : (index / (data.length - 1)) * chartWidth);
  const yFor = (value) => padding.top + chartHeight - (Number(value || 0) / maxValue) * chartHeight;

  for (let index = 0; index <= 4; index += 1) {
    const y = padding.top + (index / 4) * chartHeight;
    svg.appendChild(svgElement("line", { x1: padding.left, y1: y, x2: width - padding.right, y2: y, class: "chart-grid-line" }));
    const label = svgElement("text", { x: padding.left - 8, y: y + 4, "text-anchor": "end", class: "chart-axis-label" });
    label.textContent = chartNumber(maxValue * (1 - index / 4), format);
    svg.appendChild(label);
  }

  if (!data.length) {
    const empty = svgElement("text", { x: width / 2, y: height / 2, "text-anchor": "middle", class: "chart-axis-label" });
    empty.textContent = "Нет данных за выбранный период";
    svg.appendChild(empty);
    container.replaceChildren(svg);
    return;
  }

  const actualPoints = data.map((row, index) => `${xFor(index)},${yFor(row[actualKey])}`).join(" ");
  const planPoints = data.map((row, index) => `${xFor(index)},${yFor(row[planKey])}`).join(" ");
  const areaPoints = `${xFor(0)},${padding.top + chartHeight} ${actualPoints} ${xFor(data.length - 1)},${padding.top + chartHeight}`;
  svg.appendChild(svgElement("polygon", { points: areaPoints, class: `chart-area chart-area-${tone}` }));
  svg.appendChild(svgElement("polyline", { points: planPoints, class: "chart-plan", pathLength: "1" }));
  svg.appendChild(svgElement("polyline", { points: actualPoints, class: `chart-glow chart-glow-${tone}`, pathLength: "1" }));
  svg.appendChild(svgElement("polyline", { points: actualPoints, class: `chart-fact chart-fact-${tone}`, pathLength: "1" }));
  svg.appendChild(svgElement("polyline", { points: actualPoints, class: `chart-runner chart-runner-${tone}`, pathLength: "1" }));

  data.forEach((row, index) => {
    if (index % Math.max(Math.ceil(data.length / 7), 1) === 0 || index === data.length - 1) {
      const label = svgElement("text", { x: xFor(index), y: height - 12, "text-anchor": "middle", class: "chart-axis-label" });
      label.textContent = row.date_label;
      svg.appendChild(label);
    }
    const dot = svgElement("circle", { cx: xFor(index), cy: yFor(row[actualKey]), r: index === data.length - 1 ? 4 : 3, class: `chart-dot chart-dot-${tone}${index === data.length - 1 ? " chart-live-point" : ""}`, tabindex: "0" });
    const title = svgElement("title");
    title.textContent = `${row.date_label}: ${chartNumber(row[actualKey], format)}`;
    dot.appendChild(title);
    svg.appendChild(dot);
  });
  container.replaceChildren(svg);
}

function updatePlanMode() {
  const selected = document.querySelector("input[name='mode']:checked");
  if (!selected) return;
  document.querySelectorAll("[data-plan-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.planPanel !== selected.value;
  });
}

function updateAllocationTotal() {
  const fields = [...document.querySelectorAll("[data-allocation]")];
  const totalNode = document.querySelector("[data-allocation-total]");
  if (!totalNode) return;
  const total = fields.reduce((sum, field) => sum + Number(field.value || 0), 0);
  totalNode.textContent = `Итого: ${total.toFixed(2)}%`;
  totalNode.classList.toggle("is-invalid", Math.abs(total - 100) > 0.01);
}

function normalizeNumberInput(input) {
  if (!input.value || input.step !== "0.01") return;
  const value = Number(input.value);
  if (!Number.isFinite(value)) return;
  input.value = value.toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1");
}

document.querySelectorAll("[data-chart]").forEach(renderLineChart);
document.querySelectorAll("input[name='mode']").forEach((input) => input.addEventListener("change", updatePlanMode));
document.querySelectorAll("[data-allocation]").forEach((input) => input.addEventListener("input", updateAllocationTotal));
document.querySelectorAll("input[type='number']").forEach((input) => input.addEventListener("blur", () => normalizeNumberInput(input)));
updatePlanMode();
updateAllocationTotal();

document.querySelectorAll("[data-category-merge-form]").forEach((form) => {
  const target = form.querySelector("[data-merge-target]");
  const sources = [...form.querySelectorAll("[data-merge-source]")];
  const submit = form.querySelector("[data-merge-submit]");
  const update = () => {
    sources.forEach((source) => {
      const isTarget = source.value === target?.value;
      source.disabled = isTarget;
      if (isTarget) source.checked = false;
    });
    if (submit) submit.disabled = !sources.some((source) => source.checked && !source.disabled);
  };
  target?.addEventListener("change", update);
  sources.forEach((source) => source.addEventListener("change", update));
  form.addEventListener("submit", (event) => {
    if (!sources.some((source) => source.checked && !source.disabled)) event.preventDefault();
  });
  update();
});

document.querySelectorAll("[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});

document.querySelector("[data-sidebar-toggle]")?.addEventListener("click", () => {
  document.querySelector(".sidebar")?.classList.toggle("is-open");
});

document.querySelectorAll("[data-marketplace-credentials]").forEach((panel) => {
  const select = document.querySelector("[data-marketplace-select]");
  const update = () => { panel.hidden = panel.dataset.marketplaceCredentials !== select?.value; };
  select?.addEventListener("change", update);
  update();
});

const userMenu = document.querySelector("[data-user-menu]");
const userMenuTrigger = userMenu?.querySelector("[data-user-menu-trigger]");
const userMenuPanel = userMenu?.querySelector("[data-user-menu-panel]");

function closeUserMenu() {
  if (!userMenuTrigger || !userMenuPanel) return;
  userMenuTrigger.setAttribute("aria-expanded", "false");
  userMenuPanel.hidden = true;
}

userMenuTrigger?.addEventListener("click", () => {
  const willOpen = userMenuPanel?.hidden;
  if (!userMenuPanel) return;
  userMenuPanel.hidden = !willOpen;
  userMenuTrigger.setAttribute("aria-expanded", willOpen ? "true" : "false");
});

document.addEventListener("click", (event) => {
  if (userMenu && !userMenu.contains(event.target)) closeUserMenu();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeUserMenu();
    userMenuTrigger?.focus();
  }
});
