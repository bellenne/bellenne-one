globalThis.parityExtract = ({marketplace}) => {
 const visible = e => !!e && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0 && getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
 const text = e => e?.innerText?.trim() || '';
 const unique = es => { const a = [...es].filter(visible); return a.length === 1 ? text(a[0]) : null; };
 const body = document.body?.innerText || '';
 const blocked = /доступ ограничен|подтвердите, что вы|проверяем браузер|проверка безопасности|access denied|captcha|доступ заблокирован|ограничен доступ/i.test(body.slice(0, 12000));
 const header = document.querySelector('header');
 let city = [...(header?.querySelectorAll('button, a') || [])].some(e => visible(e) && text(e) === 'Москва') ? 'Москва' : null;
 if (marketplace === 'wb' && text(header).split('\n').map(s=>s.trim()).includes('Москва')) city = 'Москва';
 let identity = null, benefit = null, regular = null, reference = null, box = null, benefitConfirmed = false;
 if (marketplace === 'wb') {
  const row = [...document.querySelectorAll('table tr')].find(e => visible(e) && /^Артикул\s/.test(text(e)));
  identity = text(row).match(/Артикул\s+(\d+)/)?.[1] || null;
  const boxes = [...document.querySelectorAll('[class*="priceBlockPriceWrap--"]')].filter(visible);
  if (boxes.length === 1) {
   box = boxes[0]; benefit = unique(box.querySelectorAll('[class*="priceBlockWalletPrice--"]'));
   regular = unique(box.querySelectorAll('[class*="priceBlockFinalPrice--"]'));
   reference = unique(box.querySelectorAll('[class*="priceBlockOldPrice--"]'));
   benefitConfirmed = !!benefit;
  }
 } else {
  const ids = [...document.querySelectorAll('button')].filter(e => visible(e) && /^Артикул:\s*\d+$/.test(text(e)));
  if (ids.length === 1) identity = text(ids[0]).match(/\d+/)[0];
  const boxes = [...document.querySelectorAll('[data-widget="webPrice"]')].filter(visible);
  if (boxes.length === 1) {
   box = boxes[0];
   const buttons = [...box.querySelectorAll('button')].filter(visible).filter(e => /С Ozon Картой|С банками/i.test(text(e)));
   if (buttons.length === 1) {
    const b = buttons[0];
    benefitConfirmed = /С Ozon Картой/i.test(text(b)) || (/С банками/i.test(text(b)) && !![...b.querySelectorAll('img')].find(i => /\/ozon-price-compact[^/]*\.png(?:\?|$)/.test(i.getAttribute('src') || '')));
    benefit = unique([...b.querySelectorAll('span')].filter(e => e.className.includes('tsHeadline') && text(e).includes('₽')));
   }
   regular = unique([...box.querySelectorAll('span')].filter(e => !e.closest('button') && e.className.includes('tsHeadline') && text(e).includes('₽')));
   const struck = [...box.querySelectorAll('span')].filter(e => !e.closest('button') && /^\s*[\d\s.,\u2009\u202f]+₽\s*$/.test(text(e)) && (getComputedStyle(e).textDecorationLine.includes('line-through') || (getComputedStyle(e,'::after').content !== 'none' && getComputedStyle(e,'::after').height === '1px')));
   reference = unique(struck);
  }
 }
 const scope = document.querySelector('main') || document.body;
 const unavailable = !benefit && !regular && [...(scope?.querySelectorAll('h1,h2,h3,p,button,span,div') || [])]
  .some(e => visible(e) && /^(?:нет в наличии|товар закончился|товар недоступен|нет в продаже)[.!]?$/i.test(text(e))
    && !e.closest('aside,[data-widget*="recommend"],[class*="recommend"]'));
 return {blocked, unavailable, city, identity, benefit, regular, reference, benefitConfirmed, hasBox:!!box};
};
