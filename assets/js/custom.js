// ====== Reading progress indicator ======
window.addEventListener('scroll', () => {
    const article = document.querySelector('.post-content');
    if (!article) return;
    const rect = article.getBoundingClientRect();
    const total = article.scrollHeight - window.innerHeight;
    const progress = Math.min(100, Math.max(0, (-rect.top / total) * 100));
    article.style.setProperty('--scroll', progress + '%');
});

// ====== Back-to-top button ======
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

// ====== Image lightbox ======
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
            border: none !important; padding: 0 !important;
            background: transparent !important;
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

// ====== TOC active heading tracking ======
document.addEventListener('DOMContentLoaded', function() {
    var tocLinks = document.querySelectorAll('.toc a');
    if (!tocLinks.length) return;

    var headings = [];
    tocLinks.forEach(function(link) {
        var id = decodeURIComponent(link.getAttribute('href').replace('#', ''));
        var el = document.getElementById(id);
        if (el) headings.push({ el: el, link: link });
    });

    if (!headings.length) return;

    var ticking = false;
    function updateActive() {
        var scrollPos = window.scrollY + 120;
        var current = headings[0];
        for (var i = 0; i < headings.length; i++) {
            if (headings[i].el.offsetTop <= scrollPos) {
                current = headings[i];
            }
        }
        tocLinks.forEach(function(l) { l.classList.remove('toc-active'); });
        if (current) {
            current.link.classList.add('toc-active');
            // scroll TOC sidebar to keep active item visible
            var toc = current.link.closest('.toc');
            if (toc && toc.style.position === 'fixed') {
                var linkTop = current.link.offsetTop;
                var tocHeight = toc.clientHeight;
                if (linkTop > toc.scrollTop + tocHeight - 60 || linkTop < toc.scrollTop + 40) {
                    toc.scrollTo({ top: linkTop - tocHeight / 3, behavior: 'smooth' });
                }
            }
        }
        ticking = false;
    }

    window.addEventListener('scroll', function() {
        if (!ticking) { requestAnimationFrame(updateActive); ticking = true; }
    });
    updateActive();
});

// ====== Deep read coffee indicator ======
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

// ====== Heading anchor links (click to copy) ======
document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('.post-content h2[id], .post-content h3[id], .post-content h4[id]').forEach(function(h) {
        h.style.position = 'relative';
        h.style.cursor = 'pointer';
        var anchor = document.createElement('span');
        anchor.className = 'heading-anchor';
        anchor.textContent = '#';
        anchor.style.cssText = 'position:absolute;left:-1.2em;color:var(--text-dim);opacity:0;transition:opacity 0.2s;font-weight:400;';
        h.prepend(anchor);
        h.addEventListener('mouseenter', function() { anchor.style.opacity = '0.6'; });
        h.addEventListener('mouseleave', function() { anchor.style.opacity = '0'; });
        h.addEventListener('click', function() {
            var url = location.origin + location.pathname + '#' + h.id;
            navigator.clipboard.writeText(url).then(function() {
                anchor.textContent = '✓';
                anchor.style.color = 'var(--accent-green)';
                anchor.style.opacity = '1';
                setTimeout(function() {
                    anchor.textContent = '#';
                    anchor.style.color = 'var(--text-dim)';
                    anchor.style.opacity = '0';
                }, 1500);
            });
        });
    });
});

// ====== External link indicator ======
document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('.post-content a[href^="http"]').forEach(function(a) {
        if (a.hostname !== location.hostname) {
            a.setAttribute('target', '_blank');
            a.setAttribute('rel', 'noopener noreferrer');
            if (!a.querySelector('img') && !a.textContent.includes('↗')) {
                a.insertAdjacentHTML('afterend', '<sup style="font-size:0.7em;color:var(--text-dim);margin-left:1px;">↗</sup>');
            }
        }
    });
});

// ====== Table of contents reading progress mini-bar ======
document.addEventListener('DOMContentLoaded', function() {
    var toc = document.querySelector('.toc');
    if (!toc) return;
    var bar = document.createElement('div');
    bar.id = 'toc-progress';
    bar.style.cssText = 'height:2px;background:linear-gradient(90deg,var(--primary),var(--accent-purple));width:0%;transition:width 0.3s;border-radius:2px;margin-top:8px;';
    toc.appendChild(bar);

    window.addEventListener('scroll', function() {
        var article = document.querySelector('.post-content');
        if (!article) return;
        var rect = article.getBoundingClientRect();
        var total = article.scrollHeight - window.innerHeight;
        var pct = Math.min(100, Math.max(0, (-rect.top / total) * 100));
        bar.style.width = pct + '%';
    });
});
