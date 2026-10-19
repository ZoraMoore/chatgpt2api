(() => {
  'use strict';
  if (window.__accountPoolLayoutMounted) return;
  window.__accountPoolLayoutMounted = true;

  const stylesheet = document.createElement('link');
  stylesheet.rel = 'stylesheet';
  stylesheet.href = '/account-pool-layout.css?v=20261019-1';
  document.head.append(stylesheet);

  const icons = {
    image: ['M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z', 'm3 16 6-6 12 11', 'm13 15 3-3 5 5', 'M8 7h.01'],
    database: ['M21 5c0 2.2-4 4-9 4S3 7.2 3 5s4-4 9-4 9 1.8 9 4Z', 'M3 5v14c0 2.2 4 4 9 4s9-1.8 9-4V5', 'M3 12c0 2.2 4 4 9 4s9-1.8 9-4'],
    logs: ['M14 2H5a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V9Z', 'M14 2v7h7', 'M7 13h10', 'M7 17h10'],
    code: ['m8 5-7 7 7 7', 'm16 5 7 7-7 7', 'm14 2-4 20'],
    settings: ['M9 3h6l1 4 4 1 2 5-3 3v4l-5 2-3-3-4 1-3-5 2-4-1-4 4-1Z', 'M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z'],
  };

  function icon(paths) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.7');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.classList.add('pool-nav-icon');
    for (const d of paths) {
      const path = document.createElementNS(svg.namespaceURI, 'path');
      path.setAttribute('d', d);
      svg.append(path);
    }
    return svg;
  }

  const add = (node, name) => node?.classList.add(name);
  let pending = false;

  function decorate() {
    pending = false;
    const active = location.pathname.replace(/\/$/, '') === '/accounts';
    // 样式仅由路由开关控制；保留 React 节点和账号扩展依赖的父子关系。
    if (!active) {
      document.body.removeAttribute('data-account-pool-layout');
      for (const link of document.querySelectorAll('.pool-sidebar a[aria-current="page"]')) {
        link.removeAttribute('aria-current');
      }
      // Next 会复用顶栏；离开时移除装饰，避免失去页面 CSS 后 SVG 使用默认尺寸。
      for (const svg of document.querySelectorAll('.pool-sidebar .pool-nav-icon')) svg.remove();
      return;
    }
    const heading = Array.from(document.querySelectorAll('h1')).find(node => node.textContent === '号池管理');
    if (!heading) return;
    const titleSection = heading.closest('section');
    const shell = titleSection?.parentElement;
    if (!shell) return;
    document.body.setAttribute('data-account-pool-layout', '');
    add(shell, 'pool-shell');
    add(shell.closest('main'), 'pool-main');
    add(titleSection, 'pool-heading');
    add(titleSection.lastElementChild, 'pool-page-actions');
    const header = shell.querySelector('header');
    add(header, 'pool-header');
    add(header?.querySelector('a'), 'pool-brand');
    const nav = header?.querySelector('nav');
    add(nav, 'pool-sidebar');
    nav?.setAttribute('aria-label', '主导航');
    const navIcons = {'/image': 'image', '/accounts': 'database', '/images': 'image', '/logs': 'logs', '/debug': 'code', '/settings': 'settings'};
    for (const link of nav?.querySelectorAll('a') || []) {
      const path = new URL(link.href, location.origin).pathname.replace(/\/$/, '');
      if (!link.querySelector('.pool-nav-icon')) link.prepend(icon(icons[navIcons[path]] || icons.image));
      if (path === '/accounts') link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    for (const button of header?.querySelectorAll('button') || []) {
      if (button.textContent.trim() === '打开导航') add(button, 'pool-menu-toggle');
    }
    const sections = Array.from(shell.children).filter(node => node.tagName === 'SECTION');
    const overview = sections.find(node => node !== titleSection && !node.querySelector('h2'));
    add(overview, 'pool-overview');
    const stats = overview?.firstElementChild;
    add(stats, 'pool-stats');
    Array.from(stats?.children || []).forEach((node, index) => {
      add(node, 'pool-stat');
      node.dataset.poolStat = String(index);
    });
    add(overview?.lastElementChild, 'pool-models');
    const list = sections.find(node => node.querySelector('h2'));
    add(list, 'pool-list');
    add(list?.firstElementChild, 'pool-list-heading');
    add(list?.firstElementChild?.lastElementChild, 'pool-filters');
    const table = list?.querySelector('table');
    if (table) {
      add(table, 'pool-table');
      add(table.parentElement, 'pool-table-scroll');
      // 使表格横向滚动区域可用键盘访问，不拦截原有选择、编辑和删除事件。
      table.parentElement.tabIndex = 0;
      table.parentElement.setAttribute('role', 'region');
      table.parentElement.setAttribute('aria-label', '账号列表，可横向滚动');
      const content = table.parentElement.parentElement;
      add(content?.parentElement, 'pool-table-card');
      add(content?.firstElementChild, 'pool-bulk-actions');
      add(content?.lastElementChild, 'pool-pagination');
    }
    for (const button of titleSection.lastElementChild?.querySelectorAll('button') || []) {
      const label = button.textContent.trim();
      if (label === '一键刷新所有账号信息和额度') {
        add(button, 'pool-refresh-all');
        button.setAttribute('aria-label', label);
        button.title = label;
      }
      if (label === '导入') add(button, 'pool-import');
    }
  }

  function schedule() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(decorate);
  }
  // 不观察属性，避免页面标记触发自身；内容更新只在下一帧合并处理一次。
  new MutationObserver(schedule).observe(document.body, {childList: true, subtree: true});
  window.addEventListener('popstate', schedule);
  window.addEventListener('chatgpt2api:account-operations:state', schedule);
  decorate();
})();
