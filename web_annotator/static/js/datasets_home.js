/**
 * datasets_home.js —— 首页列出已配置数据集。
 *
 * 引用：templates/home.html。
 */
(function () {
  'use strict';

  async function load() {
    var host = document.getElementById('ds-list');
    var res = await fetch('/api/datasets');
    if (!res.ok) {
      host.innerHTML = '<li class="home-empty">无法读取数据集列表</li>';
      return;
    }
    var data = await res.json();
    var items = data.datasets || [];
    if (!items.length) {
      host.innerHTML = '<li class="home-empty">未配置数据集。启动时传入 --data-dir。</li>';
      return;
    }
    host.innerHTML = items.map(function (d) {
      var href = '/d/' + encodeURIComponent(d.name) + '/filter';
      return (
        '<li><a href="' + href + '">' +
        d.name +
        '<span class="path">' + d.root + '</span>' +
        '</a></li>'
      );
    }).join('');
  }

  load().catch(function (e) {
    var host = document.getElementById('ds-list');
    host.innerHTML = '<li class="home-empty">' + String(e.message || e) + '</li>';
  });
})();
