/**
 * nav.js —— 全站固定标签栏
 */
(function () {
  'use strict';

  var TABS = [
    { path: '/filter', label: '筛选' },
    { path: '/dedupe', label: '去重' },
    { path: '/crop', label: '裁剪' },
    { path: '/paint', label: '蒙版' },
    { path: '/aesthetic', label: '美学' },
    { path: '/tagging', label: '打标' },
    { path: '/tags', label: '标签' },
    { path: '/ready', label: '就绪' },
  ];

  function currentPath() {
    var p = location.pathname.replace(/\/$/, '') || '/filter';
    if (p === '/') return '/filter';
    return p;
  }

  function render() {
    var host = document.getElementById('app-tabs');
    if (!host) {
      host = document.createElement('nav');
      host.id = 'app-tabs';
      host.className = 'app-tabs';
      document.body.insertBefore(host, document.body.firstChild);
    }
    var cur = currentPath();
    host.innerHTML = TABS.map(function (t) {
      var cls = 'app-tab' + (cur === t.path ? ' active' : '');
      return '<a class="' + cls + '" href="' + t.path + '">' + t.label + '</a>';
    }).join('');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render);
  } else {
    render();
  }
})();
