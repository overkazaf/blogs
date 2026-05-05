// Reading progress indicator
window.addEventListener('scroll', () => {
    const article = document.querySelector('.post-content');
    if (!article) return;
    const rect = article.getBoundingClientRect();
    const total = article.scrollHeight - window.innerHeight;
    const progress = Math.min(100, Math.max(0, (-rect.top / total) * 100));
    article.style.setProperty('--scroll', progress + '%');
});

// Back-to-top button
(function() {
    var btn = document.createElement('button');
    btn.id = 'back-to-top';
    btn.innerHTML = '↑';
    btn.setAttribute('aria-label', 'Back to top');
    document.body.appendChild(btn);

    var style = document.createElement('style');
    style.textContent = `
        #back-to-top {
            position: fixed; bottom: 32px; right: 32px;
            width: 44px; height: 44px;
            border-radius: 50%; border: 1px solid var(--border);
            background: var(--entry); color: var(--primary);
            font-size: 20px; cursor: pointer;
            opacity: 0; transform: translateY(20px);
            transition: all 0.3s ease;
            z-index: 999; display: flex;
            align-items: center; justify-content: center;
            box-shadow: 0 4px 16px rgba(0,0,0,0.2);
        }
        #back-to-top.visible { opacity: 1; transform: translateY(0); }
        #back-to-top:hover { background: var(--primary); color: #fff; transform: translateY(-3px); }
    `;
    document.head.appendChild(style);

    window.addEventListener('scroll', function() {
        btn.classList.toggle('visible', window.scrollY > 400);
    });
    btn.addEventListener('click', function() {
        window.scrollTo({ top: 0, behavior: 'smooth' });
    });
})();

// Image lightbox (click to fullscreen)
document.addEventListener('DOMContentLoaded', function() {
    var overlay = document.createElement('div');
    overlay.id = 'lightbox-overlay';
    var style = document.createElement('style');
    style.textContent = `
        #lightbox-overlay {
            display: none; position: fixed; inset: 0;
            background: rgba(0,0,0,0.92); z-index: 10000;
            cursor: zoom-out; justify-content: center; align-items: center;
        }
        #lightbox-overlay.active { display: flex; }
        #lightbox-overlay img {
            max-width: 95vw; max-height: 92vh;
            object-fit: contain; border-radius: 8px;
            border: none; padding: 0; background: transparent;
            box-shadow: 0 8px 40px rgba(0,0,0,0.5);
        }
    `;
    document.head.appendChild(style);
    document.body.appendChild(overlay);

    document.querySelectorAll('.post-content img').forEach(function(img) {
        img.style.cursor = 'zoom-in';
        img.addEventListener('click', function(e) {
            e.stopPropagation();
            var clone = img.cloneNode();
            overlay.innerHTML = '';
            overlay.appendChild(clone);
            overlay.classList.add('active');
        });
    });

    overlay.addEventListener('click', function() {
        overlay.classList.remove('active');
    });

    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') overlay.classList.remove('active');
    });
});

// Estimated reading time enhancement (for long articles)
document.addEventListener('DOMContentLoaded', function() {
    var content = document.querySelector('.post-content');
    if (!content) return;
    var words = content.innerText.trim().split(/\s+/).length;
    var cjkChars = (content.innerText.match(/[一-鿿]/g) || []).length;
    var minutes = Math.ceil((words + cjkChars) / 400);
    var meta = document.querySelector('.post-meta');
    if (meta && minutes > 5) {
        var coffee = minutes > 30 ? '☕☕☕' : minutes > 15 ? '☕☕' : '☕';
        var span = document.createElement('span');
        span.textContent = ' · ' + coffee + ' ' + minutes + ' min deep read';
        span.style.color = 'var(--text-dim)';
        meta.appendChild(span);
    }
});
