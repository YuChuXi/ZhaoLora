/**
 * nav.js —— 全站标签栏 + 目录多选（业务页须已加载 api.js）。
 *
 * 首页仅渲染返回入口；业务页路径为 /d/{dataset}/…。
 */
(function () {
  'use strict';

  var PAGE_TABS = [
    { page: 'scoring', label: '评分' },
    { page: 'filter', label: '筛选' },
    { page: 'dedupe', label: '去重' },
    { page: 'crop', label: '裁剪' },
    { page: 'paint', label: '蒙版' },
    { page: 'tags', label: '标签' },
    { page: 'tag-check', label: '标签检查' },
    { page: 'ready', label: '就绪' },
  ];

  var HIDE_DIR = { filter: 0, crop: 1, paint: 1, ready: 1, scoring: 0, dedupe: 0, tags: 0, 'tag-check': 0 };

  /**
   * @returns {{dataset: string|null, page: string}}
   */
  function parseRoute() {
    var m = location.pathname.match(/^\/d\/([^/]+)(?:\/([^/]*))?$/);
    if (!m) {
      return { dataset: null, page: '' };
    }
    return {
      dataset: decodeURIComponent(m[1]),
      page: m[2] || 'filter',
    };
  }

  /**
   * @param {string} page
   * @returns {string}
   */
  function dirsApi(page) {
    if (page === 'dedupe') return API.apiPath('/dedupe/dirs');
    if (page === 'scoring') return API.apiPath('/scoring/dirs');
    if (page === 'tags' || page === 'tag-check') return API.apiPath('/tags/dirs');
    return API.apiPath('/dirs');
  }

  /**
   * 渲染顶部标签栏。
   */
  function render() {
    var host = document.getElementById('app-tabs');
    if (!host) {
      host = document.createElement('nav');
      host.id = 'app-tabs';
      host.className = 'app-tabs';
      document.body.insertBefore(host, document.body.firstChild);
    }

    var route = parseRoute();
    if (!route.dataset) {
      host.innerHTML = '<a class="app-tab active" href="/">首页</a>';
      return;
    }

    var base = '/d/' + encodeURIComponent(route.dataset);
    var tabsHtml =
      '<a class="app-tab" href="/">首页</a>' +
      PAGE_TABS.map(function (t) {
        var cls = 'app-tab' + (route.page === t.page ? ' active' : '');
        return '<a class="' + cls + '" href="' + base + '/' + t.page + '">' + t.label + '</a>';
      }).join('');

    var showDir = !HIDE_DIR[route.page] && typeof DirPicker !== 'undefined';
    host.innerHTML = tabsHtml + (showDir
      ? '<div class="app-tabs-right"><div id="app-dir" aria-label="目录"></div></div>'
      : '');

    if (!showDir) return;

    DirPicker.bind(document.getElementById('app-dir'));
    DirPicker.load(dirsApi(route.page)).catch(function (err) {
      console.error(err);
      var el = document.getElementById('app-dir');
      if (el) el.textContent = '目录加载失败';
    });
  }

  /**
   * 校验并绑定数据集后渲染导航。
   * 引用：各业务页脚本加载后自动执行。
   */
  async function boot() {
    var route = parseRoute();
    if (!route.dataset) {
      render();
      return;
    }
    if (typeof API === 'undefined') {
      document.body.innerHTML = '<p style="padding:24px">缺少 api.js</p>';
      return;
    }
    var data = await fetch('/api/datasets').then(function (r) {
      if (!r.ok) throw new Error('无法读取数据集列表');
      return r.json();
    });
    var ok = (data.datasets || []).some(function (d) { return d.name === route.dataset; });
    if (!ok) {
      document.body.innerHTML =
        '<p style="padding:24px">未知数据集：' +
        route.dataset +
        ' · <a href="/">返回首页</a></p>';
      return;
    }
    if (typeof DirPicker !== 'undefined' && DirPicker.setStorageKey) {
      DirPicker.setStorageKey('annotator_work_dirs:' + route.dataset);
    }
    render();
    document.dispatchEvent(new CustomEvent('annotator:ready', { detail: { dataset: route.dataset, page: route.page } }));
  }

  if (typeof API !== 'undefined' && typeof DirPicker !== 'undefined') {
    var _ds = API.parseDatasetFromPath();
    if (_ds) DirPicker.setStorageKey('annotator_work_dirs:' + _ds);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      boot().catch(function (e) {
        document.body.innerHTML = '<p style="padding:24px">' + String(e.message || e) + '</p>';
      });
    });
  } else {
    boot().catch(function (e) {
      document.body.innerHTML = '<p style="padding:24px">' + String(e.message || e) + '</p>';
    });
  }
})();
