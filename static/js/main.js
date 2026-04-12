// NeuroClass — Main JS

// ─── Theme Toggle ─────────────────────────────────────────────────────────
(function() {
    const toggle = document.querySelector('[data-theme-toggle]');
    const html = document.documentElement;
    let theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';

    function applyTheme(t) {
        html.setAttribute('data-theme', t);
        if (toggle) {
            toggle.innerHTML = t === 'dark'
                ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>'
                : '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
            toggle.setAttribute('aria-label', 'Switch to ' + (t === 'dark' ? 'light' : 'dark') + ' mode');
        }
    }

    applyTheme(theme);

    if (toggle) {
        toggle.addEventListener('click', () => {
            theme = theme === 'dark' ? 'light' : 'dark';
            applyTheme(theme);
        });
    }
})();

// ─── Copy Code ────────────────────────────────────────────────────────────
function copyCode(code) {
    if (navigator.clipboard) {
        navigator.clipboard.writeText(code).then(() => showToast('Code copied: ' + code));
    } else {
        const el = document.createElement('textarea');
        el.value = code;
        document.body.appendChild(el);
        el.select();
        document.execCommand('copy');
        document.body.removeChild(el);
        showToast('Code copied: ' + code);
    }
}

// ─── Toast ────────────────────────────────────────────────────────────────
function showToast(msg) {
    const toast = document.createElement('div');
    toast.textContent = msg;
    toast.style.cssText = [
        'position:fixed', 'bottom:24px', 'right:24px', 'z-index:9999',
        'background:var(--color-primary)', 'color:white',
        'padding:10px 20px', 'border-radius:8px',
        'font-size:14px', 'font-family:var(--font-body)',
        'box-shadow:var(--shadow-lg)',
        'animation:fadeInUp 0.2s ease',
        'pointer-events:none'
    ].join(';');
    document.head.insertAdjacentHTML('beforeend',
        '<style>@keyframes fadeInUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}</style>'
    );
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 2500);
}

// ─── Password Toggle ──────────────────────────────────────────────────────
function togglePassword(id) {
    const input = document.getElementById(id);
    if (input) input.type = input.type === 'password' ? 'text' : 'password';
}

// ─── Auto-dismiss Flashes ─────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    setTimeout(() => {
        document.querySelectorAll('.flash').forEach(el => {
            el.style.transition = 'opacity 0.4s';
            el.style.opacity = '0';
            setTimeout(() => el.remove(), 400);
        });
    }, 4000);
});
