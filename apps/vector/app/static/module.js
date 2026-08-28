document.querySelector(".menu-button")?.addEventListener("click", () => {
  document.body.classList.toggle("menu-open");
});

document.querySelector(".reveal-token")?.addEventListener("click", (event) => {
  const input = document.querySelector("#api-token");
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  event.currentTarget.textContent = show ? "Скрыть" : "Показать";
});

document.querySelector("[data-refresh]")?.addEventListener("click", () => {
  window.location.reload();
});

const integerFormatter = new Intl.NumberFormat("ru-RU", {
  maximumFractionDigits: 0,
});

document.querySelectorAll("[data-funnel-card]").forEach((card) => {
  const tabs = card.querySelectorAll("[data-funnel-label]");
  const current = card.querySelector("[data-funnel-current]");

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((item) => {
        const selected = item === tab;
        item.classList.toggle("active", selected);
        item.setAttribute("aria-selected", selected ? "true" : "false");
      });

      current.textContent = tab.dataset.funnelLabel;
      ["views", "clicks", "atbs", "orders"].forEach((metric) => {
        const target = card.querySelector(`[data-funnel-value="${metric}"]`);
        target.textContent = integerFormatter.format(
          Number(tab.dataset[`funnel${metric[0].toUpperCase()}${metric.slice(1)}`]),
        );
      });
      ["ctr", "cartCr", "orderCr"].forEach((metric) => {
        const target = card.querySelector(`[data-funnel-rate="${metric}"]`);
        target.textContent = `${(Number(tab.dataset[`funnel${metric[0].toUpperCase()}${metric.slice(1)}`]) * 100).toFixed(2)}%`;
      });
    });
  });
});
