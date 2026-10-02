(() => {
  const prefix = document.querySelector('[data-prefix]')?.dataset.prefix || '';
  let dirty = false;
  document.querySelectorAll('[data-credential-form]').forEach(form => {
    form.addEventListener('input', () => { dirty = true; form.querySelector('[data-unsaved]').hidden = false; });
    form.addEventListener('submit', () => { dirty = false; });
  });
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  // Optional empty filter values must not become numeric zero on the server.
  document.querySelectorAll('form[method="get"]').forEach(form => form.addEventListener('submit', () => {
    form.querySelectorAll('input').forEach(input => { if (!input.value) input.disabled = true; });
  }));
  const job = document.querySelector('[data-job-id]');
  const phases = {waiting_browser:'Ожидаем подключения расширения',running:'Сбор в вашем браузере',paused:'Сбор приостановлен',attention:'Ожидаем вашего действия в рабочей вкладке',storefront_wb:'Просмотр карточек Wildberries',storefront_ozon:'Просмотр карточек Ozon',catalog_wb:'Импорт каталога Wildberries',catalog_ozon:'Импорт каталога Ozon',queued:'Задание в очереди',starting:'Начало задания',sync_wb:'Получение товаров и цен Wildberries',sync_ozon:'Получение товаров и цен Ozon',probe_wb:'Проверка прав Wildberries',probe_ozon:'Проверка прав Ozon',comparing:'Сопоставление товаров и сравнение цен',complete:'Задание завершено',interrupted:'Задание прервано'};
  if (job) job.querySelector('[data-job-phase]').textContent = phases[job.querySelector('[data-job-phase]').textContent] || 'Обработка задания';
  async function poll() {
    try {
      const response = await fetch(`${prefix}/api/jobs/${job.dataset.jobId}`, {headers:{Accept:'application/json'}});
      if (!response.ok) throw new Error('poll');
      const value = await response.json();
      job.querySelector('[data-job-status]').textContent = {running:'В работе',success:'Успешно',partial:'Частично',failed:'Ошибка',cancelled:'Остановлено'}[value.status] || value.status;
      job.querySelector('[data-job-phase]').textContent = phases[value.phase] || value.phase;
      job.querySelector('[data-job-counts]').textContent = `Товаров получено: ${value.products_received} · обновлено: ${value.products_updated} · снимков: ${value.prices_updated}`;
      job.querySelector('[data-job-error]').textContent = value.error_summary || '';
      job.querySelector('[data-job-poll-error]').textContent = '';
      if (value.browser && document.querySelector('[data-browser-job]')) {
        const browser = value.browser;
        if (!value.finished_at) job.querySelector('[data-job-status]').textContent = {paused:'Пауза',attention:'Нужно ваше действие',waiting_browser:'Ожидаем расширение',running:'В работе'}[browser.state] || browser.state;
        document.querySelector('[data-browser-progress]').textContent = `Обработано ${browser.done} из ${browser.total}`;
        document.querySelector('[data-browser-message]').textContent = browser.message || '';
        document.querySelector('[data-browser-current]').textContent = browser.current ? `${browser.current.marketplace === 'wb' ? 'Wildberries' : 'Ozon'} · ${browser.current.article}` : '';
        document.querySelector('[data-browser-control="pause"]').disabled = browser.state !== 'running';
        document.querySelector('[data-browser-control="resume"]').disabled = !['paused','attention','waiting_browser'].includes(browser.state);
        document.querySelector('[data-browser-control="skip"]').disabled = browser.state !== 'attention';
      }
      if (value.finished_at) { window.location.reload(); return; }
    } catch {
      job.querySelector('[data-job-poll-error]').textContent = 'Не удалось обновить состояние. Обновите страницу или проверьте соединение.';
    }
    window.setTimeout(poll, 3000);
  }
  if (job?.dataset.jobRunning === 'true') window.setTimeout(poll, 1000);
  document.querySelectorAll('[data-product-search]').forEach(input => {
    const marketplace = input.dataset.productSearch;
    const options = document.querySelector(`[data-product-options="${marketplace}"]`);
    const count = document.querySelector(`[data-product-count="${marketplace}"]`);
    let timer, controller;
    input.addEventListener('input', () => {
      clearTimeout(timer); controller?.abort(); options.replaceChildren(new Option('Выберите товар', ''));
      timer = window.setTimeout(async () => {
        controller = new AbortController();
        try {
          const response = await fetch(`${prefix}/api/products?marketplace=${marketplace}&q=${encodeURIComponent(input.value)}`, {signal:controller.signal});
          if (!response.ok) throw new Error('search');
          const result = await response.json();
          options.replaceChildren(new Option('Выберите товар', ''), ...result.items.map(p => new Option(`${p.article} · SKU ${p.sku || '—'} · ID ${p.external_id} · ${p.name}`, p.id)));
          count.textContent = `Найдено ${result.total}. Показано ${result.items.length}. ${result.total > 50 ? 'Уточните артикул или SKU.' : ''}`;
        } catch (error) { if (error.name !== 'AbortError') count.textContent = 'Не удалось получить товары. Повторите поиск.'; }
      }, 250);
    });
  });
  const svg = document.querySelector('[data-history-chart]');
  if (!svg) return;
  const data = JSON.parse(document.getElementById('history-data').textContent);
  const selector = document.querySelector('[data-chart-type]');
  const message = document.querySelector('[data-chart-message]');
  const ns = 'http://www.w3.org/2000/svg';
  const element = (name, attrs, text) => {
    const node = document.createElementNS(ns, name);
    Object.entries(attrs).forEach(([key,value]) => node.setAttribute(key,value));
    if (text !== undefined) node.textContent = text;
    svg.append(node); return node;
  };
  const amount = (value,currency) => value === null || value === undefined ? 'Недоступно' : new Intl.NumberFormat('ru-RU',{style:'currency',currency:currency || 'RUB',maximumFractionDigits:2}).format(Number(value));
  function draw() {
    svg.replaceChildren();
    const key = selector.value;
    const available = data.filter(d => d[key] !== null && d.currency);
    const currencies = [...new Set(available.map(d => d.currency))];
    if (!available.length || currencies.length > 1) {
      message.textContent = currencies.length > 1 ? 'В истории разные валюты. Общая шкала не применяется; точные цены доступны в таблице.' : 'Этот тип цены недоступен за выбранный период.';
      return;
    }
    message.textContent = 'Отсутствующие цены показаны разрывами. Наведите на точку или выберите её клавишей Tab.';
    const times = data.map(d => Date.parse(d.captured_at));
    const start = Math.min(...times), end = Math.max(...times);
    const low = Math.min(...available.map(d => Number(d[key]))), high = Math.max(...available.map(d => Number(d[key])));
    const ylow = low === high ? Math.max(0, low - 1) : low;
    const yhigh = low === high ? high + 1 : high;
    const x = t => 100 + ((t-start)/(end-start || 1))*770;
    const y = p => 250 - ((p-ylow)/(yhigh-ylow))*210;
    for(let i=0;i<=4;i++) {
      const value = ylow + (yhigh-ylow)*i/4;
      element('line',{x1:100,x2:870,y1:y(value),y2:y(value),class:'chart-grid-line'});
      element('text',{x:94,y:y(value)+4,'text-anchor':'end',class:'chart-axis-label'},new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(value));
    }
    element('text',{x:100,y:24,class:'chart-axis-label'},currencies[0]);
    element('text',{x:100,y:280,class:'chart-axis-label'},new Date(start).toLocaleDateString('ru-RU'));
    element('text',{x:870,y:280,'text-anchor':'end',class:'chart-axis-label'},new Date(end).toLocaleDateString('ru-RU'));
    for (const marketplace of ['wb','ozon']) {
      let path='', segment=false;
      const series = data.filter(d=>d.marketplace===marketplace);
      for (const point of series) {
        if (point[key] === null || !point.currency) { segment=false; continue; }
        const px=x(Date.parse(point.captured_at)), py=y(Number(point[key]));
        path += `${segment?'L':'M'}${px},${py} `; segment=true;
      }
      element('path',{d:path,class:`history-line history-${marketplace}`});
      for (const point of series) {
        if (point[key] === null || !point.currency) continue;
        const counterpart = data.find(d => d.marketplace !== marketplace && d.job_id === point.job_id)
          || data.filter(d => d.marketplace !== marketplace && d.captured_at <= point.captured_at).at(-1);
        const wb = marketplace === 'wb' ? point : counterpart;
        const ozon = marketplace === 'ozon' ? point : counterpart;
        const diff = wb?.[key] != null && ozon?.[key] != null && wb.currency === ozon.currency ? Number(ozon[key])-Number(wb[key]) : null;
        const description = `${new Date(point.captured_at).toLocaleString('ru-RU')} · WB: ${amount(wb?.[key],wb?.currency)} · Ozon: ${amount(ozon?.[key],ozon?.currency)} · Разница Ozon − WB: ${amount(diff,point.currency)}`;
        const dot = element('circle',{cx:x(Date.parse(point.captured_at)),cy:y(Number(point[key])),r:4,tabindex:0,class:`history-dot history-${marketplace}`,'aria-label':description});
        const title=document.createElementNS(ns,'title'); title.textContent=description; dot.append(title);
        for (const event of ['focus','pointerenter']) dot.addEventListener(event,()=>{message.textContent=description;});
      }
    }
  }
  selector.addEventListener('change',draw); draw();
})();
