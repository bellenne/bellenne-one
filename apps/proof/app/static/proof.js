(() => {
  const root = document.body;
  if (!root.hasAttribute("data-proof-live") || document.visibilityState === "hidden") return;
  const prefix = window.location.pathname.startsWith("/proof") ? "/proof" : "";
  let knownVersion = null;
  const check = async () => {
    if (document.visibilityState === "hidden") return;
    try {
      const response = await fetch(`${prefix}/api/ui/state`, {headers: {Accept: "application/json"}, cache: "no-store"});
      if (!response.ok) return;
      const state = await response.json();
      if (knownVersion !== null && state.version !== knownVersion) window.location.reload();
      knownVersion = state.version;
    } catch (_) {
      // The next polling cycle retries without interrupting the operational UI.
    }
  };
  check();
  window.setInterval(check, 5000);
})();
