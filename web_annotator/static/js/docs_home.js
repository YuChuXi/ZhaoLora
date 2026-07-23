/**
 * docs_home.js —— 首页文档：侧栏导航 + 渲染 /api/docs/{slug}
 */
(function () {
  'use strict';

  var FALLBACK_NAV = [
    { slug: '', label: '概览' },
    { slug: 'layout', label: '目录约定' },
    { slug: 'aesthetic', label: '美学' },
    { slug: 'filter', label: '筛选' },
    { slug: 'dedupe', label: '去重' },
    { slug: 'crop', label: '裁剪' },
    { slug: 'paint', label: '蒙版' },
    { slug: 'tags', label: '标签' },
    { slug: 'tag-clean', label: '清洗' },
    { slug: 'ready', label: '就绪' },
  ];

  function currentSlug() {
    var q = new URLSearchParams(location.search).get('doc');
    if (q == null || q === 'readme' || q === 'index') return '';
    return String(q).trim();
  }

  function setSlug(slug, replace) {
    var url = new URL(location.href);
    if (!slug) url.searchParams.delete('doc');
    else url.searchParams.set('doc', slug);
    if (replace) history.replaceState(null, '', url);
    else history.pushState(null, '', url);
  }

  function renderNav(nav, active) {
    var host = document.getElementById('docs-nav');
    host.innerHTML = '';
    nav.forEach(function (item) {
      var a = document.createElement('a');
      a.href = item.slug ? ('/?doc=' + encodeURIComponent(item.slug)) : '/';
      a.textContent = item.label;
      if ((item.slug || '') === (active || '')) a.className = 'active';
      a.addEventListener('click', function (e) {
        e.preventDefault();
        setSlug(item.slug || '', false);
        loadDoc(item.slug || '');
      });
      host.appendChild(a);
    });
  }

  function updateRawLink(slug) {
    var el = document.getElementById('docs-raw-link');
    var name = slug || 'README';
    var file = slug ? (slug + '.md') : 'README.md';
    el.innerHTML = '源文件 <a href="/docs/' + encodeURIComponent(name === 'README' ? 'readme' : slug) + '.md">' + file + '</a>';
  }

  async function loadDoc(slug) {
    var article = document.getElementById('docs-article');
    article.innerHTML = '<p class="docs-empty">加载中…</p>';
    var key = slug || 'readme';
    var res = await fetch('/api/docs/' + encodeURIComponent(key));
    if (!res.ok) {
      article.innerHTML = '<p class="docs-empty">文档加载失败：' + res.status + '</p>';
      return;
    }
    var data = await res.json();
    article.innerHTML = data.html || '';
    renderNav(window.__DOCS_NAV || FALLBACK_NAV, data.slug || '');
    updateRawLink(data.slug || '');
    document.title = (data.file || '文档').replace(/\.md$/, '') + ' · ZhaoLora 标注工具';

    article.querySelectorAll('a[href^="?doc="]').forEach(function (a) {
      a.addEventListener('click', function (e) {
        e.preventDefault();
        var u = new URL(a.getAttribute('href'), location.origin + '/');
        var s = u.searchParams.get('doc') || '';
        var hash = u.hash || '';
        setSlug(s, false);
        loadDoc(s).then(function () {
          if (hash) {
            var id = decodeURIComponent(hash.slice(1));
            var target = document.getElementById(id);
            if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }
        });
      });
    });
  }

  async function init() {
    var nav = FALLBACK_NAV;
    var res = await fetch('/api/docs');
    if (res.ok) {
      var data = await res.json();
      if (data.nav && data.nav.length) nav = data.nav;
    }
    window.__DOCS_NAV = nav;
    renderNav(nav, currentSlug());
    await loadDoc(currentSlug());
  }

  window.addEventListener('popstate', function () {
    loadDoc(currentSlug());
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      init().catch(function (e) {
        document.getElementById('docs-article').textContent = String(e.message || e);
      });
    });
  } else {
    init().catch(function (e) {
      document.getElementById('docs-article').textContent = String(e.message || e);
    });
  }
})();
