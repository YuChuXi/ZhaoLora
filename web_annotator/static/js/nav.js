/**
 * nav.js —— 全站固定标签栏 + 右上角目录下拉
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

  // 裁剪/蒙版固定 main；就绪导出全部目录
  var HIDE_DIR = { '/crop': 1, '/paint': 1, '/ready': 1 };

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
    var tabsHtml = TABS.map(function (t) {
      var cls = 'app-tab' + (cur === t.path ? ' active' : '');
      return '<a class="' + cls + '" href="' + t.path + '">' + t.label + '</a>';
    }).join('');

    var showDir = !HIDE_DIR[cur] && typeof DirPicker !== 'undefined';
    host.innerHTML = tabsHtml + (showDir
      ? '<div class="app-tabs-right"><div id="app-dir" aria-label="目录"></div></div>'
      : '');

    if (showDir) {
      DirPicker.bind(document.getElementById('app-dir'));
      var api =
        cur === '/dedupe' ? '/api/dedupe/dirs'
          : cur === '/aesthetic' ? '/api/aesthetic/dirs'
            : cur === '/tagging' ? '/api/tagging/dirs'
              : cur === '/tags' ? '/api/tags/dirs'
                : '/api/dirs';
      DirPicker.load(api).catch(function () {
        DirPicker.fill([{ name: 'main', label: 'main', image_count: 0 }]);
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render);
  } else {
    render();
  }
})();
