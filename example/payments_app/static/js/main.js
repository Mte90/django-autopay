document.addEventListener('DOMContentLoaded', () => {
    // Add fade-in effect to cards
    document.querySelectorAll('.card').forEach((card, index) => {
        card.style.opacity = '0';
        card.classList.add('fade-in');
        card.style.animationDelay = `${index * 0.1}s`;
    });

    // Simple form validation feedback
    const forms = document.querySelectorAll('form');
    forms.forEach(form => {
        form.addEventListener('submit', (e) => {
            const btn = form.querySelector('button[type="submit"]');
            if (btn) {
                btn.disabled = true;
                btn.innerText = 'Processing...';
            }
        });
    });
});
