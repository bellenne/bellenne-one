importScripts('config.js','core.js');
let serial = Promise.resolve();
const queue = fn => { const result = serial.then(fn); serial = result.catch(() => {}); return result; };
const get = async () => (await chrome.storage.local.get(['clientId','run','outbox','progress']));
const save = value => chrome.storage.local.set(value);
// A vanished content-script response must not hold the serial queue forever.
const messageToTab = (id, message) => new Promise((resolve,reject) => {
  const timer = setTimeout(() => reject(new Error('Вкладка не ответила. Обновите страницу Parity; очередь сохранена.')),10000);
  chrome.tabs.sendMessage(id,message).then(resolve,reject).finally(() => clearTimeout(timer));
});
async function progress(stage, values={}) {
  let old = (await get()).progress || {};
  if (values.jobId && values.jobId !== old.jobId) old = {};
  await save({progress:{...old,...values,stage,updatedAt:Date.now(),
    ...(values.saved > (old.saved || 0) ? {lastSavedAt:Date.now()} : {})}});
}
const api = async (run, operation, body) => {
  if (run.origin) {
    const tab = await chrome.tabs.get(run.bridgeTab);
    if (!parityCore.bridge(tab.url) || new URL(tab.url).origin !== run.origin) {
      throw new Error('Откройте вкладку Parity, в которой запущено это задание.');
    }
  }
  const value = await messageToTab(run.bridgeTab, {type:'API',jobId:run.jobId,operation,body});
  if (!value?.ok) throw Object.assign(new Error(typeof value?.detail === 'string' ? value.detail : 'Вкладка Parity недоступна.'), {status:value?.status});
  return value.data;
};
async function cancelRead(run) {
  if (run?.cardTab) try { await messageToTab(run.cardTab,{type:'CANCEL'}); } catch {}
}
async function pump() {
  let {run,clientId,outbox} = await get();
  if (!run) return;
  try {
    if (outbox?.error_code === 'navigation' && run.navigated && run.cardTab
        && run.task?.id === outbox.task_id && run.task.token === outbox.token) {
      // READ may lose its content script during a redirect. Classify the final
      // tab before retrying; an unreadable destination cannot yield this price.
      try {
        const tab = await chrome.tabs.get(run.cardTab);
        if (tab.status === 'complete' && tab.url !== 'about:blank' && !parityCore.card(run.task,tab.url)) {
          outbox = {...outbox,error_code:'unavailable'};
          await save({outbox});
        }
      } catch {}
    }
    if (outbox) {
      if (outbox.error_code === 'navigation' && run.task?.id === outbox.task_id && run.task.token === outbox.token
          && (run.attempt || 1) < (run.task.max_attempts || 1)) {
        // Persist the budget and deadline before touching a tab. A worker restart
        // or a pause must not reset the attempts for this card.
        await cancelRead(run);
        const attempt = (run.attempt || 1) + 1;
        run = {...run,attempt,reading:false,navigated:false,sample:null,
          recreatePending:attempt >= 3,nextAt:Date.now()+run.task.interval_seconds*attempt*1000};
        await save({run,outbox:null});
        outbox = null;
      }
    }
    if (outbox) {
      await progress('sending');
      try {
        const result = await api(run,'receipt',{client_id:clientId,...outbox});
        await save({outbox:null,run:{...run,reading:false,nextAt:Date.now()+(run.task?.interval_seconds || 2)*1000,attentionShown:false,
          ...(result.accepted ? {attempt:1,sample:null} : {})}});
        run = (await get()).run;
        if (!result.accepted && run.cardTab) await chrome.tabs.update(run.cardTab,{active:true});
      } catch (error) {
        if (error.status === 409 || error.status === 422) { await save({outbox:null,run:{...run,reading:false}}); run = (await get()).run; }
        else throw error;
      }
    }
    const result = await api(run,'claim',{client_id:clientId});
    const retryNavigation = run.serverState === 'attention' && run.attentionCode === 'navigation' && result.state === 'running';
    run = {...run,serverState:result.state,attentionCode:result.attention_code,
      ...(retryNavigation ? {navigated:false,reading:false,attempt:1,sample:null,recreatePending:true,nextAt:0} : {})};
    await save({run});
    await progress(result.state,{jobId:run.jobId,state:result.state,total:result.total,done:result.done,
      saved:result.saved,skipped:result.skipped,current:result.current,message:result.message,attentionCode:result.attention_code,error:'',connectedAt:Date.now()});
    await chrome.action.setBadgeText({text:result.state === 'finished' ? '' : `${result.done}/${result.total}`});
    if (result.state === 'finished') { await cancelRead(run); await save({run:null,outbox:null}); return; }
    if (result.state !== 'running') {
      await cancelRead(run);
      if (result.state === 'attention' && run.cardTab && !run.attentionShown) {
        await chrome.tabs.update(run.cardTab,{active:true});
        run.attentionShown = true;
      }
      await save({run:{...run,reading:false}}); return;
    }
    const task = result.task;
    if (!task) throw new Error('Parity не вернул текущую карточку.');
    try {
      if (!parityCore.card(task)) throw new Error('Parity вернул неподдерживаемую ссылку карточки.');
      if (run.task?.id === task.id) {
        // Upgrade a persisted task from an older extension/server contract without
        // resetting its navigation state or retry budget.
        run = {...run,task}; await save({run});
      }
      if (Date.now() < (run.nextAt || 0)) {
        await progress((run.attempt || 1) > 1 ? 'retry' : 'interval',
          {attempt:run.attempt || 1,maxAttempts:task.max_attempts || 1});
        setTimeout(() => queue(pump), run.nextAt - Date.now()); return;
      }
      if (run.cardTab) {
        try { await chrome.tabs.get(run.cardTab); }
        catch {
          // Tab IDs are not durable across a browser restart. Keep the retry
          // budget, but create a new working tab instead of looping on a dead ID.
          run = {...run,cardTab:null,navigated:false,reading:false,sample:null};
          await save({run});
        }
      }
      if (run.recreatePending) {
        const oldTab = run.cardTab;
        // Forget the old ID first; its onRemoved event cannot clear the new tab.
        run = {...run,cardTab:null,recreatePending:false,reading:false,navigated:false};
        await save({run});
        if (oldTab) try { await chrome.tabs.remove(oldTab); } catch {}
      }
      if (run.task?.id !== task.id || !run.cardTab) {
        await cancelRead(run);
        const newTask = run.task?.id !== task.id;
        if (newTask) run = {...run,attempt:1,sample:null,nextAt:0};
        if (run.cardTab) {
          try { await chrome.tabs.get(run.cardTab); } catch { run.cardTab = null; }
        }
        if (!run.cardTab) {
          run.cardTab = (await chrome.tabs.create({url:'about:blank',active:false})).id;
          await save({run:{...run,task,reading:false,navigated:false,attentionShown:false}});
          setTimeout(() => queue(pump), 100);
          return;
        }
        run = {...run,task,reading:false,navigated:false,attentionShown:false};
        await save({run});
      }
      if (!run.navigated) {
        await progress('navigation',{attempt:run.attempt || 1,maxAttempts:task.max_attempts || 1});
        await chrome.tabs.update(run.cardTab,{url:task.url});
        await save({run:{...run,navigated:true,navigationAt:Date.now()}});
        setTimeout(() => queue(pump),task.timeout_seconds*1000);
        return;
      }
      const tab = await chrome.tabs.get(run.cardTab);
      let ready = tab.status === 'complete';
      if (!ready && parityCore.card(task,tab.url)) {
        try { ready = (await messageToTab(run.cardTab,{type:'READY'}))?.ready === true; } catch {}
      }
      // A hanging analytics/image request must not prevent reading a ready DOM.
      if (!ready) {
        await progress('navigation');
        if (Date.now() - run.navigationAt > task.timeout_seconds * 1000) {
          await save({outbox:{task_id:task.id,token:task.token,error_code:'navigation'}});
          return pump();
        }
        return;
      }
      if (!parityCore.card(task,tab.url)) {
        // An external redirect is unreadable. Skip it without an account/region gate.
        await save({outbox:{task_id:task.id,token:task.token,error_code:'unavailable'}}); return;
      }
      if (run.reading && Date.now() - run.reading > task.timeout_seconds * 2000) {
        await cancelRead(run);
        let code = 'navigation';
        try {
          const sample = await messageToTab(run.cardTab,{type:'SAMPLE',task});
          // Never automatically reload a visible challenge or
          // an unsupported price layout just because the page timer was throttled.
          code = sample.error_code || parityCore.observation(sample.observation,task) || 'navigation';
          if (!sample.error_code && !parityCore.observation(sample.observation,task)) {
            // Even after a throttled timer's deadline, a readable DOM can recover
            // with two fresh stable samples instead of unnecessarily reloading.
            await new Promise(resolve => setTimeout(resolve,1000));
            const second = await messageToTab(run.cardTab,{type:'SAMPLE',task});
            if (!second.error_code && !parityCore.observation(second.observation,task)
                && JSON.stringify(sample.observation) === JSON.stringify(second.observation)) {
              await save({outbox:{task_id:task.id,token:task.token,observation:second.observation,observed_at:new Date().toISOString()}});
              return pump();
            }
            code = second.error_code || parityCore.observation(second.observation,task) || 'navigation';
          }
        } catch {}
        await save({outbox:{task_id:task.id,token:task.token,error_code:code}});
        return pump();
      }
      if (!run.reading) {
        try { await messageToTab(run.cardTab,{type:'READ',task}); }
        catch {
          await save({outbox:{task_id:task.id,token:task.token,error_code:'navigation'}});
          return pump();
        }
        await save({run:{...run,reading:Date.now()}});
      } else {
        // Chrome may throttle timers in a background Ozon tab. An incoming message
        // takes an immediate DOM sample without waiting for the page's timer chain.
        try {
          const sample = await messageToTab(run.cardTab,{type:'SAMPLE',task});
          const data = sample.observation;
          const code = sample.error_code || parityCore.observation(data,task);
          const key = !code || ['unavailable','blocked'].includes(code) ? JSON.stringify(data) : null;
          if (key && run.sample?.key === key && Date.now()-run.sample.at >= 1000) {
            await save({outbox:code ? {task_id:task.id,token:task.token,error_code:code} :
              {task_id:task.id,token:task.token,observation:data,observed_at:new Date().toISOString()}});
            return pump();
          }
          await save({run:{...run,sample:key ? {key,at:Date.now()} : null}});
        } catch {}
      }
      await progress('reading');
    } catch {
      // A local failure of this assigned card must not become a manual stop or
      // erase an unacknowledged server result. Persist its error before advancing.
      await cancelRead(run);
      await save({run:{...run,reading:false},outbox:{task_id:task.id,token:task.token,error_code:'internal'}});
      setTimeout(() => queue(pump),0);
    }
  } catch (error) {
    // No moving to another product after loss of server/session/tab.
    await cancelRead(run);
    await chrome.action.setBadgeText({text:'!'});
    if (run) await save({run:{...run,reading:false}});
    const stage = error.status === 409 ? 'profile' : [401,403].includes(error.status) ? 'auth' : 'connection';
    await progress(stage,{jobId:run.jobId,error:stage === 'connection' ? 'Переподключаемся к Parity автоматически; очередь и полученные цены сохранены.' : error.message});
    return {error:error.message || 'Нет связи с Parity. Обновите страницу.'};
  }
}
chrome.runtime.onMessage.addListener((message,sender,respond) => {
  if (sender.id !== chrome.runtime.id) return;
  const popup = sender.url === chrome.runtime.getURL('popup.html') && (sender.frameId === undefined || sender.frameId === 0);
  if (popup && message.type === 'STATUS') {
    get().then(value=>respond({progress:value.progress,active:!!value.run,jobId:value.run?.jobId})); return true;
  }
  if (!popup && sender.frameId !== 0) return;
  queue(async () => {
    let {clientId,run} = await get();
    if (!clientId) { clientId = crypto.randomUUID(); await save({clientId}); }
    if (popup) {
      if (message.type === 'CONTROL' && run && ['pause','resume','skip'].includes(message.action)) {
        await cancelRead(run);
        await api(run,'control',{client_id:clientId,action:message.action});
        await save({outbox:null,run:{...run,reading:false}});
        await pump(); return {ok:true};
      }
      if (message.type === 'OPEN') {
        if (message.target === 'card' && run?.cardTab) {
          await chrome.tabs.update(run.cardTab,{active:true});
        } else {
          let origin = run?.origin || PARITY_ORIGINS[0];
          if (run && !run.origin) {
            try {
              const tab = await chrome.tabs.get(run.bridgeTab);
              if (parityCore.bridge(tab.url)) origin = new URL(tab.url).origin;
            } catch {}
          }
          const url = `${origin}${PARITY_PREFIX}${run ? `/jobs/${run.jobId}` : '/integrations'}`;
          if (run?.bridgeTab) {
            try { await chrome.tabs.update(run.bridgeTab,{active:true,url}); return {ok:true}; } catch {}
          }
          await chrome.tabs.create({url});
        }
        return {ok:true};
      }
      return {ok:false};
    }
    if (['BRIDGE','RESET_PROFILE'].includes(message.type)) {
      if (!parityCore.bridge(sender.url)) throw new Error('Неподдерживаемая вкладка Parity.');
      const origin = new URL(sender.url).origin;
      // Job IDs and receipt tokens belong to one installation. Opening another
      // allowed Parity host must not redirect an active run or its saved outbox.
      if (run && !run.origin) {
        try {
          const tab = await chrome.tabs.get(run.bridgeTab);
          if (parityCore.bridge(tab.url)) run = {...run,origin:new URL(tab.url).origin};
        } catch {}
      }
      if (run?.origin && run.origin !== origin) throw new Error('В этом браузере идёт задание другой установки Parity. Завершите или остановите его в исходной вкладке.');
      if (message.type === 'RESET_PROFILE') {
        if (run) {
          const state = await api({...run,bridgeTab:sender.tab.id},'claim',{client_id:clientId});
          if (state.state !== 'finished') throw new Error('Сначала остановите текущее задание.');
        }
        await save({clientId:crypto.randomUUID(),run:null,outbox:null});
        return {ok:true};
      }
      if (Number.isSafeInteger(message.jobId) && message.jobId > 0) {
        if (run && run.jobId !== message.jobId) {
          const state = await api({...run,bridgeTab:sender.tab.id},'claim',{client_id:clientId});
          if (state.state !== 'finished') throw new Error('В этом браузере уже идёт другое задание. Остановите или завершите его.');
        }
        await save({run:{...(run?.jobId === message.jobId ? run : {}),jobId:message.jobId,bridgeTab:sender.tab.id,origin}});
      } else if (run) await save({run:{...run,bridgeTab:sender.tab.id,origin}});
      return await pump() || {ok:true};
    }
    if (message.type === 'OBSERVATION') {
      if (!run || sender.tab.id !== run.cardTab || !parityCore.card(run.task,sender.url) || run.task.id !== message.taskId || run.task.token !== message.token) return {ok:false};
      const outbox = {task_id:message.taskId,token:message.token};
      if (message.observation) { outbox.observation = message.observation; outbox.observed_at = message.observedAt; }
      else outbox.error_code = message.error_code;
      await save({outbox}); await pump(); return {ok:true};
    }
    return {ok:false};
  }).then(respond,error => respond({error:error.message}));
  return true;
});
chrome.tabs.onUpdated.addListener((id,change) => {
  if (change.status === 'complete') get().then(({run}) => {
    if (id === run?.cardTab || id === run?.bridgeTab) queue(pump);
  });
});
chrome.tabs.onRemoved.addListener(id => queue(async () => {
  const {run} = await get();
  if (run?.cardTab === id) await save({run:{...run,cardTab:null,reading:false}});
}));
chrome.alarms.onAlarm.addListener(() => queue(pump));
chrome.runtime.onStartup.addListener(() => queue(async () => {
  const {run} = await get();
  if (run) await save({run:{...run,cardTab:null,reading:false}});
  return pump();
}));
(async () => {
  if (!await chrome.alarms.get('parity-recovery')) await chrome.alarms.create('parity-recovery',{periodInMinutes:0.5});
  await queue(pump);
})().catch(() => {});
