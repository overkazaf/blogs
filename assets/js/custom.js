// Reading progress indicator
window.addEventListener('scroll', () => {
    const article = document.querySelector('.post-content');
    if (!article) return;
    const rect = article.getBoundingClientRect();
    const total = article.scrollHeight - window.innerHeight;
    const progress = Math.min(100, Math.max(0, (-rect.top / total) * 100));
    document.querySelector('.post-content')?.style.setProperty('--scroll', progress + '%');
});
