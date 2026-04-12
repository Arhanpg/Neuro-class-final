// ── Dark/light mode toggle ───────────────────────────────────────────────
(function () {
  const root   = document.documentElement;
  const toggle = document.querySelector('[data-theme-toggle]');
  let theme    = localStorage.getItem('nc-theme') ||
                 (matchMedia('(prefers-color-scheme:dark)').matches ? 'dark' : 'light');

  root.setAttribute('data-theme', theme);

  if (toggle) {
    toggle.addEventListener('click', () => {
      theme = theme === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', theme);
      try { localStorage.setItem('nc-theme', theme); } catch(e) {}
    });
  }
})();

// ── Flash auto-dismiss ───────────────────────────────────────────────────
document.querySelectorAll('.flash').forEach(el => {
  setTimeout(() => el.style.opacity = '0', 4000);
  setTimeout(() => el.remove(), 4500);
});
