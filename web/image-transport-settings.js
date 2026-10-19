(() => {
  'use strict';

  if (window.__chatgpt2apiImageTransportSettingsInitialized) return;
  window.__chatgpt2apiImageTransportSettingsInitialized = true;

  const events = {
    request: 'chatgpt2api:image-transport:request',
    state: 'chatgpt2api:image-transport:state',
    change: 'chatgpt2api:image-transport:change',
  };
  let state = { mode: '', loading: true, saving: false, error: false };

  function render() {
    const selector = document.getElementById('image-transport-mode');
    const status = document.getElementById('image-transport-status');
    if (!selector || !status) return;
    selector.value = state.mode;
    selector.disabled = state.loading || state.saving || state.error || !state.mode;
    const message = state.loading
      ? '正在加载生图模式…'
      : state.error
        ? '读取配置失败，请刷新页面后重试。'
        : state.saving
          ? '正在保存系统配置…'
          : '更改后点击页面底部的「保存」生效。';
    if (status.textContent !== message) status.textContent = message;
  }

  function mount() {
    if (location.pathname.replace(/\/+$/, '') !== '/settings') return;
    if (document.getElementById('image-transport-settings')) return;
    const anchor = Array.from(document.querySelectorAll('label'))
      .find(element => element.textContent.trim() === '图片轮询超时');
    const field = anchor?.parentElement;
    if (!field?.parentElement) return;

    const wrapper = document.createElement('div');
    wrapper.id = 'image-transport-settings';
    wrapper.className = 'space-y-2';
    const label = document.createElement('label');
    label.htmlFor = 'image-transport-mode';
    label.className = 'text-sm text-stone-700';
    label.textContent = '生图模式';
    const selector = document.createElement('select');
    selector.id = 'image-transport-mode';
    selector.className = 'h-10 w-full rounded-xl border border-stone-200 bg-white px-3 text-sm text-stone-700 disabled:opacity-50';
    selector.setAttribute('aria-describedby', 'image-transport-description image-transport-status');
    for (const [value, text] of [['voice', '语音模式'], ['conversation', '普通会话模式']]) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = text;
      selector.append(option);
    }
    selector.addEventListener('change', () => {
      if (selector.disabled || !['voice', 'conversation'].includes(selector.value)) return;
      window.dispatchEvent(new CustomEvent(events.change, { detail: { mode: selector.value } }));
    });
    const description = document.createElement('p');
    description.id = 'image-transport-description';
    description.className = 'text-xs leading-5 text-stone-500';
    description.textContent = '对所有新生图请求生效，支持文生图和图生图；在途请求保持原模式。';
    const status = document.createElement('p');
    status.id = 'image-transport-status';
    status.className = 'text-xs text-stone-500';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    wrapper.append(label, selector, description, status);
    field.parentElement.insertBefore(wrapper, field);
    render();
    // Next 切页可能先于或晚于 store 初始化；主动请求状态避免显示过期模式。
    window.dispatchEvent(new CustomEvent(events.request));
  }

  window.addEventListener(events.state, event => {
    const next = event.detail;
    if (!next || typeof next !== 'object') return;
    state = {
      mode: ['voice', 'conversation'].includes(next.mode) ? next.mode : '',
      loading: next.loading === true,
      saving: next.saving === true,
      error: next.error === true,
    };
    render();
  });
  // 仓库只有 Next 构建产物；独立控件通过窄事件桥接复用原保存按钮，不读取完整配置或凭据。
  new MutationObserver(mount).observe(document.body, { childList: true, subtree: true });
  mount();
})();
