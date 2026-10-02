/* Shared validation; no arbitrary pages or server endpoints. */
globalThis.parityCore = {
  bridge(url) {
    try { const u = new URL(url); return PARITY_ORIGINS.includes(u.origin) && u.pathname.startsWith(`${PARITY_PREFIX}/`); } catch { return false; }
  },
  card(task, url) {
    try {
      // Chrome omits URLs outside host permissions. An explicit missing tab URL
      // must not fall back to the assigned card and trigger endless READ retries.
      if (arguments.length < 2) url = task.url;
      const u = new URL(url);
      return /^[1-9][0-9]{0,19}$/.test(task.identifier) && u.protocol === 'https:' &&
        (task.marketplace === 'wb' ? ['wildberries.ru','www.wildberries.ru'].includes(u.hostname) :
          task.marketplace === 'ozon' && ['ozon.ru','www.ozon.ru'].includes(u.hostname));
    } catch { return false; }
  },
  observation(data, task) {
    if (data.blocked) return 'blocked';
    if (data.unavailable) return 'unavailable';
    if (data.identity !== task.identifier) return 'identity';
    if (!data.hasBox) return 'layout';
    if (!data.benefitConfirmed || !data.benefit) return 'benefit_missing';
    if (![data.benefit, data.regular, data.reference].every(v => v === null || /^\d+(?:[.,]\d{1,2})?₽$/.test(v.replace(/[\s\u00a0\u2009\u202f]/g, '')))) return 'layout';
    return null;
  }
};
