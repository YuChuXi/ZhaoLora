/**
 * nav.js —— 全站固定标签栏 + 右上角目录多选
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
    { path: '/tag-clean', label: '清洗' },
    { path: '/ready', label: '就绪' },
  ];

  // 裁剪/蒙版固定 main；就绪导出全部目录
  var HIDE_DIR = { '/crop': 1, '/paint': 1, '/ready': 1 };

  /**
   * @returns {string}
   */
  function currentPath() {
    var p = location.pathname.replace(/\/$/, '') || '/filter';
    if (p === '/') return '/filter';
    return p;
  }

  /**
   * 当前页对应的目录列表 API。
   * @param {string} cur
   * @returns {string}
   */
  function dirsApi(cur) {
    if (cur === '/dedupe') return '/api/dedupe/dirs';
    if (cur === '/aesthetic') return '/api/aesthetic/dirs';
    if (cur === '/tagging') return '/api/tagging/dirs';
    if (cur === '/tags' || cur === '/tag-clean') return '/api/tags/dirs';
    return '/api/dirs';
  }

  /**
   * 渲染顶部标签栏与目录选择器。
   */
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

    if (!showDir) return;

    DirPicker.bind(document.getElementById('app-dir'));
    DirPicker.load(dirsApi(cur)).catch(function (err) {
      console.error(err);
      DirPicker.fill([{ name: 'main', label: 'main', image_count: 0 }]);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render);
  } else {
    render();
  }
})();
