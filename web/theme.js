// Theme: follows the system until you choose otherwise; choosing what the system already shows goes back to following it.
// Loaded in <head> by the app and by the landing page, so the first paint is already in the right colours.
(() => {
  const root = document.documentElement, KEY = 'theme';
  const light = matchMedia('(prefers-color-scheme: light)'), still = matchMedia('(prefers-reduced-motion: reduce)');
  const system = () => (light.matches ? 'light' : 'dark');
  const chosen = () => { try { const v = localStorage.getItem(KEY); return v === 'light' || v === 'dark' ? v : null; } catch { return null; } };
  function apply() {
    const theme = chosen() || system();
    root.dataset.theme = theme;
    const bg = getComputedStyle(root).getPropertyValue('--bg').trim();
    if (bg) document.querySelectorAll('meta[name="theme-color"]').forEach(m => { m.content = bg; });
    document.querySelectorAll('[data-theme-toggle]').forEach(b => b.setAttribute('aria-pressed', String(theme === 'dark')));
  }
  function toggle(x, y) {
    const next = root.dataset.theme === 'light' ? 'dark' : 'light';
    try { next === system() ? localStorage.removeItem(KEY) : localStorage.setItem(KEY, next); } catch {}
    if (!document.startViewTransition || still.matches) return apply();
    // The new colours spread from the button, like ink from a pressed seal.
    root.style.setProperty('--turn-x', `${x}px`);
    root.style.setProperty('--turn-y', `${y}px`);
    root.classList.add('theme-turn');
    document.startViewTransition(apply).finished.finally(() => root.classList.remove('theme-turn'));
  }
  apply();
  light.addEventListener('change', apply);
  addEventListener('storage', e => { if (e.key === KEY || e.key === null) apply(); });
  document.addEventListener('DOMContentLoaded', apply);
  addEventListener('load', apply);
  document.addEventListener('click', e => {
    const b = e.target.closest?.('[data-theme-toggle]');
    if (!b) return;
    const r = b.getBoundingClientRect();
    toggle(r.left + r.width / 2, r.top + r.height / 2);
  });
  window.ulakTheme = {toggle, apply};
})();
