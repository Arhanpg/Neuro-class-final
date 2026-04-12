// NeuroClass — global JS helpers

// Auto-dismiss flash messages after 5 seconds
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.flash').forEach(el => {
    setTimeout(() => el.remove(), 5000);
  });

  // Auto-uppercase classroom code input
  const codeInput = document.getElementById('code');
  if (codeInput) {
    codeInput.addEventListener('input', e => {
      const pos = e.target.selectionStart;
      e.target.value = e.target.value.toUpperCase();
      e.target.setSelectionRange(pos, pos);
    });
  }
});
