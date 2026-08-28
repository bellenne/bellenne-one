const matchMode = document.querySelector("[data-match-mode]");
const categoryField = document.querySelector("[data-category-value-field]");
const categoryValue = document.querySelector("[data-category-value]");
const textField = document.querySelector("[data-text-value-field]");
const textValue = document.querySelector("[data-text-value]");

function syncMatchFields() {
  if (!matchMode) return;
  const isCategory = matchMode.value === "category";
  const isArticle = matchMode.value === "article";
  if (categoryField) categoryField.hidden = !isCategory;
  if (categoryValue) categoryValue.disabled = !isCategory;
  if (textField) textField.hidden = !isArticle;
  if (textValue) {
    textValue.disabled = !isArticle;
    if (!isArticle) textValue.value = "";
  }
}

matchMode?.addEventListener("change", syncMatchFields);
syncMatchFields();

const marketplaceSelect = document.querySelector("[data-marketplace-select]");
const credentialPanels = document.querySelectorAll("[data-echo-credentials]");

function syncCredentialPanels() {
  credentialPanels.forEach((panel) => {
    panel.hidden = panel.dataset.echoCredentials !== marketplaceSelect?.value;
  });
}

marketplaceSelect?.addEventListener("change", syncCredentialPanels);
syncCredentialPanels();

const queueRefresh = document.querySelector("[data-queue-auto-refresh]");
if (queueRefresh) {
  window.setTimeout(() => {
    window.location.assign(queueRefresh.dataset.refreshUrl);
  }, 5000);
}
