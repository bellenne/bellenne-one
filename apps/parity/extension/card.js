(() => {
  let generation = 0;
  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (sender.id !== chrome.runtime.id) return;
    if (message.type === 'READY') { respond({ready:true}); return; }
    if (message.type === 'SAMPLE' && parityCore.card(message.task,location.href)) {
      try { respond({observation:parityExtract({marketplace:message.task.marketplace})}); }
      catch { respond({error_code:'layout'}); }
      return;
    }
    if (message.type === 'CANCEL') { generation++; respond({ok:true}); return; }
    if (message.type !== 'READ' || !parityCore.card(message.task, location.href)) return;
    const task = message.task, round = ++generation;
    respond({ok:true}); // No long-lived message promise in the service worker.
    (async () => {
      let previous = '', last = 'navigation';
      const deadline = Date.now() + task.timeout_seconds * 1000;
      while (round === generation && Date.now() < deadline) {
        const data = parityExtract({marketplace:task.marketplace});
        last = parityCore.observation(data, task);
        const key = JSON.stringify(data);
        if (previous === key && ['unavailable','blocked'].includes(last)) {
          await chrome.runtime.sendMessage({type:'OBSERVATION',taskId:task.id,token:task.token,error_code:last}); return;
        }
        if (!last && previous === key) {
          await chrome.runtime.sendMessage({type:'OBSERVATION',taskId:task.id,token:task.token,observation:data,observedAt:new Date().toISOString()}); return;
        }
        // Ordinary "checking browser" may finish on its own. A challenge is
        // never interacted with here; its error is skipped by the server.
        previous = key;
        await new Promise(resolve => setTimeout(resolve,1000));
      }
      if (round === generation) await chrome.runtime.sendMessage({type:'OBSERVATION',taskId:task.id,token:task.token,error_code:last || 'layout'});
    })().catch(async () => {
      if (round === generation) try {
        await chrome.runtime.sendMessage({type:'OBSERVATION',taskId:task.id,token:task.token,error_code:'layout'});
      } catch {}
    });
  });
})();
