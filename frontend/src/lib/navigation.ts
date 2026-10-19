/** 侧栏、命令和退出共用设置页的未保存拦截，程序跳转不能绕过草稿提示。 */
export function requestNavigation(href: string, commit: () => void) {
  const event = new CustomEvent("chatgpt2api:before-navigate", {
    cancelable: true,
    detail: { href, commit },
  });
  if (!window.dispatchEvent(event)) return false;
  commit();
  return true;
}
