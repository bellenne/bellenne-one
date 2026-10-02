/* Fetches stay in the authenticated Parity tab. No cookies or credentials are
 * copied to extension storage, and marketplace scripts cannot invoke this API. */
(() => {
  if (!parityCore.bridge(location.href) || window.top !== window) return;
  const panel = document.querySelector('[data-browser-job]');
  const status = document.querySelector('[data-extension-status]');
  const csrf = () => document.querySelector('meta[name="parity-csrf"]')?.content;
  const announce = async () => {
    try {
      const value = await chrome.runtime.sendMessage({type:'BRIDGE', jobId: panel ? Number(panel.dataset.browserJob) : null});
      if (status) status.textContent = value?.error || 'Расширение подключено. Можно запускать сбор цен.';
      const reset = document.querySelector('[data-browser-reset]');
      if (reset) reset.disabled = reset.dataset.resetAllowed !== 'true' || !value?.ok;
    } catch { if (status) status.textContent = 'Расширение отключено. Проверьте его в настройках браузера и обновите страницу.'; }
  };
  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (sender.id !== chrome.runtime.id || message.type !== 'API') return;
    if (!Number.isSafeInteger(message.jobId) || message.jobId < 1 || !['claim','receipt','control'].includes(message.operation) || !csrf()) {
      respond({ok:false,status:403,detail:'Обновите страницу Parity и войдите в аккаунт.'}); return;
    }
    (async () => {
      try {
        const response = await fetch(`${PARITY_PREFIX}/api/browser/${message.jobId}/${message.operation}`, {
          method:'POST', credentials:'same-origin', redirect:'error', headers:{'Content-Type':'application/json',Accept:'application/json'},
          body:JSON.stringify({...message.body,csrf_token:csrf()}), signal:AbortSignal.timeout(10000)
        });
        const data = await response.json();
        respond({ok:response.ok,status:response.status,data,detail:response.ok ? '' : data.detail});
      } catch { respond({ok:false,status:0,detail:'Нет связи с Parity. Оставьте вкладку открытой; результат будет повторно передан после восстановления связи.'}); }
    })();
    return true;
  });
  document.querySelector('[data-browser-reset]')?.addEventListener('click', async () => {
    const value = await chrome.runtime.sendMessage({type:'RESET_PROFILE'});
    if (status) status.textContent = value.error || 'Контекст браузера сброшен. Следующая проверка использует новую историю. Выберите Москву и нужные аккаунты перед запуском.';
  });
  announce();
  // Tab timers advance the ordinary collection; alarms recover a suspended MV3
  // worker. Throttling background tabs slows the queue but cannot lose its cursor.
  setInterval(announce, 10000);
})();
