(() => {
  'use strict';
  if (window.__accountOperationsMounted) return;
  window.__accountOperationsMounted = true;
  const prefix = 'chatgpt2api:account-operations:';
  const state = { items: [], selected: [], busy: false, loaded: false };
  let cleanup, cleanupPromise, restored = false, deepLinkHandled = false;
  let dialog, status, actionArea, selectedRefs = [], cachedKey = '';
  const formatTime = value => {
    if (!value) return '未知';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    if (!Number.isFinite(date.getTime())) return '未知';
    return new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit',
      hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false}).format(date);
  };
  const text = (node, value) => { if (node.textContent !== value) node.textContent = value; };
  const rows = () => selectedRefs.map(id => state.items.find(item => item.id === id)).filter(Boolean);
  function quotaDescription(item) {
    if (!item.quota_state) return '';
    const stages = {cooldown:'冷却等待',awaiting_check:'到期待复查',probe_ready:'等待业务验证',probing:'业务验证中'};
    let value = '图像限流 · ' + (stages[item.quota_state] || '等待恢复') + '\n原因：' + (item.quota_reason || '上游明确报告图像限额') +
      '\n预计恢复（北京时间）：' + formatTime(item.quota_restore_at);
    if (item.status === '禁用') value += '\n账号已人工禁用，不会自动启用。';
    else if (item.quota_state === 'probe_ready') value += '\n额度查询已通过，等待下一次正常生图请求验证。';
    else if (item.quota_state === 'probing') value += '\n仅当前业务请求可验证恢复，其余请求使用其他账号。';
    else if (item.quota_state === 'awaiting_check') value += '\n到期不代表已恢复，后台复查后仍需业务验证。';
    return value;
  }
  function message(value) { text(status, value); }
  async function key() {
    if (cachedKey) return cachedKey;
    cachedKey = await new Promise(resolve => {
      let done = false;
      const finish = value => { if (!done) { done = true; resolve(typeof value === 'string' ? value : ''); } };
      const request = indexedDB.open('chatgpt2api');
      request.onupgradeneeded = () => request.transaction.abort();
      request.onerror = request.onblocked = () => finish('');
      request.onsuccess = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains('auth')) { db.close(); finish(''); return; }
        const transaction = db.transaction('auth', 'readonly');
        const read = transaction.objectStore('auth').get('chatgpt2api_auth_key');
        read.onsuccess = () => finish(read.result);
        read.onerror = () => finish('');
        transaction.oncomplete = () => db.close();
      };
      setTimeout(() => finish(''), 1500);
    });
    if (!cachedKey) {
      try { cachedKey = JSON.parse(localStorage.getItem('chatgpt2api/auth/chatgpt2api_auth_key') || '""'); } catch (_) { cachedKey = ''; }
    }
    return cachedKey;
  }
  async function api(path, body, scope = 'operations') {
    const credential = await key();
    if (!credential) throw new Error('请先登录管理面板');
    const response = await fetch('/api/accounts/' + scope + path, {
      method: body === undefined ? 'GET' : 'POST', headers: {'Authorization':'Bearer ' + credential, 'Content-Type':'application/json'},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    });
    const value = await response.json();
    if (!response.ok) {
      if (response.status === 401) cachedKey = '';
      const error = new Error(value.detail?.error || '请求失败，请重新登录或稍后重试'); error.status = response.status; throw error;
    }
    return value;
  }
  function hasStoredCleanup() {
    try { return !!sessionStorage.getItem('account-image-cleanup-task'); } catch (_) { return false; }
  }
  function taskButton() {
    const node = document.getElementById('account-cleanup-task');
    if (node) node.hidden = !(cleanup?.hasTask() || hasStoredCleanup());
  }
  async function ensureCleanup() {
    if (!cleanupPromise) cleanupPromise = import('/account-image-cleanup.js?v=f58a63366325').then(module => {
      cleanup = module.createCleanupController({dialog, request:(path,body)=>api(path,body,'image-space'), onChange:taskButton});
      return cleanup;
    }).catch(error=>{cleanupPromise=null;throw error;});
    return cleanupPromise;
  }
  async function startCleanup() {
    restored = true;
    try {
      const controller = await ensureCleanup();
      const supported = state.items.filter(item=>item.supported).map(item=>item.id);
      const selected = new Set(selectedRefs);
      const all = supported.length > 0 && supported.length === selected.size && supported.every(id=>selected.has(id));
      await controller.start([...selectedRefs],all);
    } catch(error) { message(error.message); }
  }
  function busy(value) {
    state.busy = value;
    for (const button of actionArea.querySelectorAll('button')) button.disabled = value || button.dataset.unavailable === 'true';
  }
  function button(label, handler, disabled = false) {
    const node = document.createElement('button');
    node.type = 'button'; node.className = 'account-op-button'; node.textContent = label;
    node.disabled = disabled; node.dataset.unavailable = String(disabled); node.onclick = handler; return node;
  }
  async function preferences(fields) {
    if (fields.image_cleanup_enabled === true && !window.confirm('对所选账号开启每天自动清理：仅将 7 天前的图片移入最近删除，首次执行在 24 小时后。是否开启？')) return;
    busy(true);
    try {
      const result = await api('/preferences', {account_ids:selectedRefs, ...fields});
      state.items = result.items;
      renderDialog();
      message('设置已保存，账号级开关重启后仍然有效。');
      window.dispatchEvent(new CustomEvent(prefix + 'reload'));
    } catch (error) { message(error.message); }
    finally { busy(false); }
  }
  async function refresh() {
    busy(true); message('正在通过 Refresh Token 刷新所选账号，请稍候…');
    try {
      const job = await api('/refresh', {account_ids:selectedRefs});
      const deadline = Date.now() + 20 * 60 * 1000;
      let result;
      do {
        await new Promise(resolve => setTimeout(resolve, 1000));
        result = await api('/jobs/' + job.id);
      } while (result.status === 'running' && Date.now() < deadline);
      if (result.status !== 'completed') throw new Error(result.error || '任务仍在处理中，请稍后刷新账号列表查看结果');
      state.items = (await api('')).items;
      renderDialog();
      const success = result.results.filter(item => item.status === 'success').length;
      message('刷新完成：成功 ' + success + ' 个，未完成 ' + (result.results.length - success) + ' 个。' +
        (result.results.some(item => item.code === 'missing_refresh_token') ? '缺少 Refresh Token 的账号需要重新导入完整凭据。' : '') +
        (result.results.some(item => item.code === 'refresh_failed') ? '部分凭据被上游拒绝或网络失败，请查看账号日志。' : ''));
      window.dispatchEvent(new CustomEvent(prefix + 'reload'));
    } catch (error) { message(error.message); }
    finally { busy(false); }
  }
  function renderDialog() {
    const selected = rows();
    const list = dialog.querySelector('[data-account-list]'); list.replaceChildren();
    for (const item of selected) {
      const section = document.createElement('section'); section.className = 'account-op-summary';
      const label = document.createElement('strong'); label.textContent = item.label;
      const expiry = document.createElement('div'); expiry.textContent = 'Access Token 到期：' + formatTime(item.expires_at) + '（北京时间）';
      const refreshState = document.createElement('div'); refreshState.textContent = 'Refresh Token：' + (item.has_refresh_token ? '已保存' : '缺失') + ' · 自动刷新：' + (item.auto_refresh_token ? '开启' : '关闭');
      const cleanup = document.createElement('div'); cleanup.textContent = '自动清理：' + (item.image_cleanup_enabled ? '开启 · 下次 ' + formatTime(item.cleanup_next_at) : '关闭');
      section.append(label, expiry, refreshState, cleanup);
      if (item.quota_state) {
        const quota = document.createElement('div'); quota.className = 'account-quota-limit';
        quota.textContent = quotaDescription(item); section.append(quota);
      }
      if (item.cleanup_last_status) {
        const last = document.createElement('div');
        const names = {running:'执行中',success:'成功',error:'失败',skipped:'已跳过'};
        last.textContent = '上次清理：' + (names[item.cleanup_last_status] || '未确认') + ' · ' + formatTime(item.cleanup_last_at) + (item.cleanup_last_error ? ' · ' + item.cleanup_last_error : '');
        section.append(last);
      }
      list.append(section);
    }
    actionArea.replaceChildren();
    const available = selected.length > 0 && selected.every(item => item.supported);
    actionArea.append(
      button('清理图像空间', startCleanup, !available),
      button('手动刷新 Access Token', refresh, !available || selected.length > 20 || selected.every(item => !item.has_refresh_token)),
      button('开启每日自动清理（保留 7 天）', () => preferences({image_cleanup_enabled:true}), !available || selected.length > 20),
      button('关闭每日自动清理', () => preferences({image_cleanup_enabled:false}), !available || selected.length > 20),
      button('开启到期自动刷新', () => preferences({auto_refresh_token:true}), !available || selected.length > 20),
      button('关闭到期自动刷新', () => preferences({auto_refresh_token:false}), !available || selected.length > 20),
    );
  }
  function open(refs) {
    if (state.busy) { dialog.showModal(); return; }
    if (cleanup?.isActive()) { cleanup.show(); return; }
    cleanup?.hide();
    selectedRefs = [...new Set(refs)].filter(id => /^[a-f0-9]{64}$/.test(id));
    if (!selectedRefs.length) return;
    renderDialog(); message('手动清理需先预览并确认。自动清理每天执行一次，只处理 7 天前的图片；到期自动刷新提前 24 小时触发。');
    dialog.showModal();
  }
  function initDialog() {
    if (dialog) return;
    const style = document.createElement('style');
    style.textContent = '.account-op-dialog{position:fixed;inset:0;margin:auto;height:fit-content;width:min(860px,94vw);max-height:88vh;overflow:auto;border:1px solid #e7e5e4;border-radius:16px;padding:24px;background:#fff;color:#292524;box-shadow:0 20px 65px #0003}.account-op-dialog::backdrop{background:#1c191780}.account-op-heading{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}.account-op-heading h2{font-size:20px;font-weight:600}.account-op-summary{padding:12px 0;border-bottom:1px solid #e7e5e4;font-size:13px;line-height:1.8;overflow-wrap:anywhere}.account-op-summary strong{font-size:14px}.account-op-actions{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}.account-op-button{border:1px solid #d6d3d1;border-radius:8px;background:#fff;padding:7px 10px;font-size:12px;line-height:1.4;cursor:pointer;color:#44403c}.account-op-button:hover{background:#f5f5f4}.account-op-button:disabled{opacity:.45;cursor:not-allowed}.account-op-status{font-size:12px;line-height:1.8;color:#78716c;white-space:pre-wrap}.account-token-expiry{white-space:pre-line;font-size:11px;line-height:1.7;color:#78716c;margin-top:6px}.cleanup-table-wrap{overflow:auto;max-height:44vh;margin:14px 0}.cleanup-table{width:100%;min-width:640px;border-collapse:collapse;text-align:left;font-size:12px}.cleanup-table th,.cleanup-table td{padding:9px;border-bottom:1px solid #e7e5e4}.cleanup-table td:first-child{max-width:240px;overflow-wrap:anywhere}.cleanup-table td:last-child{max-width:220px;overflow-wrap:anywhere}#cleanup-summary{font-size:14px;font-weight:500;line-height:1.7}#cleanup-error{color:#b45309;font-size:12px;line-height:1.8}#cleanup-confirm-label{font-size:13px;line-height:1.8;display:block;margin-top:12px}#cleanup-confirm-label[hidden]{display:none}#cleanup-confirm{margin-right:6px}#cleanup-execute:not(:disabled){background:#b91c1c;color:white;border-color:#b91c1c}[data-account-list]{max-height:38vh;overflow:auto}.account-quota-limit{margin-top:8px;border-left:2px solid #d97706;padding-left:8px;font-size:12px;line-height:1.7;white-space:pre-line;overflow-wrap:anywhere;color:#92400e}.account-op-button:focus-visible{outline:2px solid #a16207;outline-offset:3px}';
    document.head.append(style);
    dialog = document.createElement('dialog'); dialog.className = 'account-op-dialog'; dialog.id = 'account-operations-dialog';
    dialog.setAttribute('aria-labelledby','account-operations-title');
    dialog.innerHTML = '<div class="account-op-heading"><h2 id="account-operations-title">账号操作</h2><button type="button" class="account-op-button" aria-label="关闭账号操作">关闭</button></div><div data-account-list></div><div class="account-op-actions"></div><p class="account-op-status" role="status" aria-live="polite"></p>';
    dialog.querySelector('[aria-label="关闭账号操作"]').onclick = () => dialog.close();
    actionArea = dialog.querySelector('.account-op-actions'); status = dialog.querySelector('.account-op-status');
    document.body.append(dialog);
  }
  function mount() {
    if (location.pathname.replace(/\/$/, '') !== '/accounts') return;
    initDialog();
    document.getElementById('image-space-entry')?.remove();
    for (const row of document.querySelectorAll('tr[data-account-ref]')) {
      const id = row.getAttribute('data-account-ref');
      const item = state.items.find(item => item.id === id);
      if (!item) continue;
      if (!row.querySelector('[data-account-operation]')) {
        const action = button('操作', () => open([id])); action.setAttribute('data-account-operation',id);
        row.lastElementChild.append(action);
      }
      const info = row.children[5];
      if (info) {
        let expiry = info.querySelector('.account-token-expiry');
        if (!expiry) { expiry = document.createElement('div'); expiry.className = 'account-token-expiry'; info.append(expiry); }
        text(expiry, 'Token 到期：' + formatTime(item.expires_at) + '（北京）\n' +
          (item.has_refresh_token ? 'RT 已保存' : 'RT 缺失') + ' · 自动刷新' + (item.auto_refresh_token ? '开' : '关') + ' · 自动清理' + (item.image_cleanup_enabled ? '开' : '关'));
        let quota = info.querySelector('.account-quota-limit');
        if (item.quota_state) {
          if (!quota) { quota = document.createElement('div'); quota.className = 'account-quota-limit'; info.append(quota); }
          text(quota, quotaDescription(item));
        } else if (quota) quota.remove();
      }
    }
    const heading = Array.from(document.querySelectorAll('h1')).find(el => el.textContent === '号池管理');
    const toolbar = heading?.parentElement?.parentElement?.lastElementChild;
    if (toolbar && !document.getElementById('account-batch-operations')) {
      const entry = button('批量操作', () => open(state.selected)); entry.id = 'account-batch-operations'; toolbar.append(entry);
    }
    if (toolbar && !document.getElementById('account-select-all')) {
      const select = button('全选账号', () => {
        const available = state.items.filter(item=>item.supported);
        const all = available.length > 0 && available.every(item=>state.selected.includes(item.id));
        window.dispatchEvent(new CustomEvent(prefix+'select-all',{detail:{all:!all}}));
      });
      select.id = 'account-select-all'; select.title = '跨分页选择全部可清理的 ChatGPT 账号'; toolbar.append(select);
      const task = button('查看清理任务', async () => {
        try { const controller = await ensureCleanup();controller.show();await controller.poll(); }
        catch(error){message(error.message);}
      });task.id='account-cleanup-task';task.hidden=true;toolbar.append(task);
    }
    const selectAll = document.getElementById('account-select-all');
    if (selectAll) {
      const available = state.items.filter(item=>item.supported);
      text(selectAll,available.length>0&&available.every(item=>state.selected.includes(item.id))?'取消全选':'全选账号');
      selectAll.disabled = available.length === 0;
    }
    taskButton();
    if (state.loaded && !restored && hasStoredCleanup()) {
      restored=true;ensureCleanup().then(controller=>controller.poll()).catch(()=>{});
    }
    if (state.loaded && !deepLinkHandled) {
      deepLinkHandled=true;const url=new URL(location.href),raw=url.searchParams.get('image_space_accounts');
      if(raw){
        url.searchParams.delete('image_space_accounts');history.replaceState(history.state,'',url.pathname+url.search+url.hash);
        const requested=new Set(raw.split(','));const refs=state.items.filter(item=>item.supported&&requested.has(item.id)).map(item=>item.id);
        if(refs.length){open(refs);startCleanup();}
      }
    }
    const batch = document.getElementById('account-batch-operations');
    if (batch) { text(batch, '批量操作（已选 ' + state.selected.length + '）'); batch.disabled = !state.selected.length; }
  }
  window.addEventListener(prefix + 'state', event => {
    if (!event.detail || !Array.isArray(event.detail.items) || !Array.isArray(event.detail.selected)) return;
    state.items = event.detail.items; state.selected = event.detail.selected; state.loaded = event.detail.loading !== true; mount();
  });
  new MutationObserver(mount).observe(document.body, {childList:true,subtree:true});
  mount(); window.dispatchEvent(new CustomEvent(prefix + 'request'));
})();
