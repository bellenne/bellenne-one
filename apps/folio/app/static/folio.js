"use strict";
document.addEventListener("click", (event) => {
  const remove = event.target.closest("[data-remove-node]");
  if (remove) remove.closest("[data-node]").remove();
  const up = event.target.closest("[data-move-up]");
  if (up) {
    const node = up.closest("[data-node]");
    if (node.previousElementSibling) node.parentNode.insertBefore(node, node.previousElementSibling);
  }
  if (event.target.closest("[data-add-node]")) {
    const template = document.getElementById("new-node");
    const fragment = template.content.cloneNode(true);
    fragment.querySelector('[name="node_id"]').value = "step_" + crypto.randomUUID().replaceAll("-", "");
    document.getElementById("nodes").appendChild(fragment);
  }
});
