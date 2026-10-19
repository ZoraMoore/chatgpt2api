// 清理任务独立于弹窗的显示状态；关闭或恢复页面只读取同一个任务，不重复提交。
export function createCleanupController({dialog, request, onChange}) {
  const storageKey = 'account-image-cleanup-task';
  const names = {queued:'等待中',scanning:'正在预览',ready:'等待确认',deleting:'正在清理',verifying:'正在复核',running:'正在清理',completed:'已完成',failed:'失败',partial:'部分失败'};
  const state = {id:'',refs:[],all:false,job:null,starting:false,submitting:false,error:'',expired:false};
  let timer, epoch = 0, pollSequence = 0, executePending = false;
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
    if (saved && /^[a-f0-9]{32}$/.test(saved.id)) {
      state.id = saved.id;
      state.refs = Array.isArray(saved.refs) ? saved.refs.filter(id => /^[a-f0-9]{64}$/.test(id)) : [];
      state.all = saved.all === true;
    }
  } catch (_) { /* 会话存储不可用时仍可使用当前弹窗，不扩大任务范围。 */ }
  const panel = document.createElement('section'); panel.id = 'account-cleanup-panel'; panel.hidden = true;
  panel.innerHTML = '<p id="cleanup-summary" role="status" aria-live="polite"></p><div class="cleanup-table-wrap"><table class="cleanup-table"><thead><tr><th>账号</th><th>状态</th><th>预览图片</th><th>确认移出</th><th>剩余</th><th>说明</th></tr></thead><tbody id="cleanup-results"></tbody></table></div><p id="cleanup-error" role="alert"></p><label id="cleanup-confirm-label"><input type="checkbox" id="cleanup-confirm"> 我确认将本次预览的图片移入最近删除</label><div class="account-op-actions"><button type="button" class="account-op-button" id="cleanup-execute">确认清理预览图片</button><button type="button" class="account-op-button" id="cleanup-poll">继续读取进度</button><button type="button" class="account-op-button" id="cleanup-repreview">重新预览</button></div><p class="account-op-status">仅清理本次预览快照，预览后新增图片会保留。关闭弹窗后任务继续执行，可点击「查看清理任务」重新打开。</p>';
  dialog.append(panel);
  const $ = id => panel.querySelector('#' + id);
  function persist() {
    try {
      if (state.id) sessionStorage.setItem(storageKey, JSON.stringify({id:state.id,refs:state.refs,all:state.all}));
      else sessionStorage.removeItem(storageKey);
    } catch (_) { /* 隐私模式不保证刷新后恢复，当前任务仍可继续。 */ }
    onChange();
  }
  const isActive = () => state.starting || state.submitting || (!!state.id && !state.job) || ['scanning','running'].includes(state.job?.status);
  function render() {
    const job = state.job;
    const accounts = Array.isArray(job?.accounts) ? job.accounts : [];
    const total = accounts.reduce((sum,a) => sum + Number(a.total || 0), 0);
    const verified = accounts.reduce((sum,a) => sum + Number(a.verified || 0), 0);
    const failed = accounts.filter(a => a.phase === 'failed').length;
    let summary = '正在读取原任务进度…';
    if (state.starting) summary = '正在提交预览，后端将逐个扫描所选账号…';
    else if (state.expired) summary = '任务已过期或服务已重启，请重新预览后再确认。';
    else if (!state.id && state.error) summary = '预览未完成，请查看说明后重试。';
    else if (job?.status === 'scanning') summary = `正在预览 ${accounts.length} 个账号，已发现 ${total} 张图片。`;
    else if (job?.status === 'ready') summary = `预览完成：${accounts.length} 个账号，共 ${total} 张图片。` + (total ? '请核对后确认清理。' : '没有需要清理的图片。');
    else if (state.submitting || job?.status === 'running') summary = `正在清理 ${accounts.length} 个账号，已确认移出 ${verified} / ${total} 张图片。`;
    else if (job?.status === 'completed') summary = `清理完成：${accounts.length} 个账号，已确认移出 ${verified} 张图片。`;
    else if (job?.status === 'failed') summary = `预览失败：${failed} 个账号未完成，请查看说明并重新选择账号。`;
    else if (job?.status === 'partial') summary = `清理部分完成：已确认移出 ${verified} 张图片，${failed} 个账号未完成。`;
    $('cleanup-summary').textContent = summary;
    $('cleanup-error').textContent = state.error;
    const body = $('cleanup-results'); body.replaceChildren();
    for (const account of accounts) {
      const row = document.createElement('tr');
      for (const value of [account.label,names[account.phase] || account.phase,account.total,account.verified,account.remaining ?? '未确认',account.error || (account.sweep ? `第 ${account.sweep} 轮复核` : '')]) {
        const cell = document.createElement('td'); cell.textContent = String(value ?? ''); row.append(cell);
      }
      body.append(row);
    }
    const ready = !!state.id && job?.status === 'ready' && total > 0 && !state.starting && !state.submitting && !state.expired && !state.error;
    $('cleanup-confirm-label').hidden = job?.status !== 'ready' || total === 0;
    $('cleanup-confirm').disabled = !ready;
    if (!ready) $('cleanup-confirm').checked = false;
    $('cleanup-execute').disabled = !ready || !$('cleanup-confirm').checked;
    $('cleanup-poll').disabled = !state.id || state.starting || executePending;
    $('cleanup-repreview').disabled = isActive() || (!state.refs.length && !state.all);
    onChange();
  }
  function show() {
    dialog.querySelector('[data-account-list]').hidden = true;
    dialog.querySelector('.account-op-actions').hidden = true;
    dialog.querySelector('.account-op-status').hidden = true;
    dialog.querySelector('#account-operations-title').textContent = '图像空间清理';
    panel.hidden = false; render();
    if (!dialog.open) dialog.showModal();
  }
  function hide() {
    panel.hidden = true;
    dialog.querySelector('[data-account-list]').hidden = false;
    dialog.querySelector('.account-op-actions').hidden = false;
    dialog.querySelector('.account-op-status').hidden = false;
    dialog.querySelector('#account-operations-title').textContent = '账号操作';
  }
  async function poll() {
    clearTimeout(timer);
    if (!state.id || executePending) return;
    const id = state.id, current = epoch, sequence = ++pollSequence;
    try {
      const job = await request('/jobs/' + id);
      if (current !== epoch || id !== state.id || sequence !== pollSequence) return;
      if (!job || !Array.isArray(job.accounts)) throw new Error('任务进度格式异常，请继续读取');
      state.job = job; state.error = ''; state.expired = false; state.submitting = false;
      render();
      if (['scanning','running'].includes(job.status)) timer = setTimeout(poll, 1500);
    } catch (error) {
      if (current !== epoch || id !== state.id || sequence !== pollSequence) return;
      state.error = error.message; state.submitting = false;
      if (error.status === 404) { state.id = ''; state.job = null; state.expired = true; persist(); }
      render();
    }
  }
  async function start(refs, all = false) {
    if (isActive()) { show(); return; }
    state.refs = [...new Set(refs)].filter(id => /^[a-f0-9]{64}$/.test(id)); state.all = all;
    if ((!all && (!state.refs.length || state.refs.length > 1000))) throw new Error('请选择 1 至 1000 个账号，或使用全选账号');
    clearTimeout(timer); epoch += 1;
    state.id = ''; state.job = null; state.error = ''; state.expired = false; state.starting = true;
    $('cleanup-confirm').checked = false;
    persist(); show();
    try {
      const result = await request('/preview', all ? {all_accounts:true} : {account_ids:state.refs});
      if (!result || !/^[a-f0-9]{32}$/.test(result.id)) throw new Error('服务没有返回有效任务编号，请稍后重新预览');
      state.id = result.id; persist();
    } catch (error) { state.error = error.message; }
    finally { state.starting = false; render(); }
    if (state.id) await poll();
  }
  async function execute() {
    if ($('cleanup-execute').disabled || !state.id || state.job?.status !== 'ready') return;
    const id = state.id;
    state.submitting = true; executePending = true; state.error = ''; $('cleanup-confirm').checked = false; render();
    try { await request('/jobs/' + id + '/execute', {confirm:true}); }
    catch (error) { state.error = error.message; }
    finally { executePending = false; }
    // 提交响应丢失也只查原任务，不自动重复提交删除。
    await poll();
  }
  $('cleanup-confirm').onchange = render;
  $('cleanup-execute').onclick = execute;
  $('cleanup-poll').onclick = poll;
  $('cleanup-repreview').onclick = () => start(state.refs, state.all).catch(error => {state.error=error.message;render();});
  render();
  return {start,show,hide,poll,isActive,hasTask:()=>!!state.id || state.starting || state.expired || !!state.job};
}
