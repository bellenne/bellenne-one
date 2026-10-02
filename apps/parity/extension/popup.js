(() => {
  const $ = id => document.getElementById(id);
  const labels = {running:'Сбор идёт',navigation:'Загрузка карточки',reading:'Чтение цены',sending:'Сохранение цены',interval:'Пауза между карточками',retry:'Автоматический повтор',paused:'Сбор на паузе',attention:'Нужно ваше действие',waiting_browser:'Ожидаем браузер',finished:'Сбор завершён',connection:'Нет связи с Parity',profile:'Задание в другом профиле браузера',auth:'Войдите в Parity'};
  let busy = false;
  async function refresh() {
    try {
      const value = await chrome.runtime.sendMessage({type:'STATUS'});
      const p = value?.progress;
      $('state').textContent = p ? labels[p.stage] || 'Сбор идёт' : 'Нет активного сбора. Запустите его в Parity.';
      if (p && ['retry','navigation','reading'].includes(p.stage) && p.attempt > 1) $('state').textContent += ` · попытка ${p.attempt} из ${p.maxAttempts}`;
      $('job').hidden = !p;
      if (!p) return;
      $('counts').textContent = Number.isSafeInteger(p.total) ? `Обработано ${p.done || 0} из ${p.total}` : 'Прогресс пока недоступен';
      $('progress').max = p.total || 1; $('progress').value = p.done || 0;
      $('results').textContent = `Цен сохранено: ${p.saved || 0} · пропущено: ${p.skipped || 0}`;
      $('current').textContent = p.current ? `${p.current.marketplace === 'wb' ? 'Wildberries' : 'Ozon'} · ${p.current.article}` : '';
      $('activity').textContent = p.lastSavedAt ? `Последняя цена: ${new Date(p.lastSavedAt).toLocaleTimeString('ru-RU')}` : '';
      $('message').hidden = !p.message;
      $('message').textContent = p.attentionCode === 'navigation' ? 'Карточка не загрузилась или не ответила вовремя. Нажмите «Продолжить» для повторной загрузки или «Пропустить карточку».' : p.message || '';
      const blocked = ['connection','profile','auth'].includes(p.stage);
      const stale = !blocked && value.active && p.connectedAt && Date.now()-p.connectedAt > 60000;
      if (stale) $('state').textContent = 'Связь давно не обновлялась';
      $('error').hidden = !(p.error || stale); $('error').textContent = p.error || (stale ? 'Откройте задание в Parity и обновите страницу. Очередь сохранена.' : '');
      $('pause').disabled = busy || !value.active || p.state !== 'running' || blocked;
      $('resume').disabled = busy || !value.active || !['attention','paused'].includes(p.state) || blocked;
      $('skip').disabled = busy || !value.active || p.state !== 'attention' || blocked;
      $('card').disabled = busy || !value.active || !p.current;
      const jobId = value.jobId || p.jobId;
      $('open').textContent = value.active && Number.isSafeInteger(jobId) ? `Открыть задание #${jobId}` : 'Открыть Parity';
    } catch { $('state').textContent = 'Расширение недоступно. Откройте окно снова.'; }
  }
  async function send(message) {
    if (busy) return;
    busy = true; await refresh();
    try {
      const value = await chrome.runtime.sendMessage(message);
      if (!value?.ok) throw new Error(value?.error || 'Действие не выполнено. Обновите вкладку Parity.');
      await refresh();
    } catch (error) { $('error').hidden = false; $('error').textContent = error.message; }
    finally { busy = false; }
  }
  for (const action of ['pause','resume','skip']) $(action).onclick = () => send({type:'CONTROL',action});
  $('card').onclick = () => send({type:'OPEN',target:'card'});
  $('open').onclick = () => send({type:'OPEN',target:'parity'});
  refresh(); setInterval(refresh,1000);
})();
