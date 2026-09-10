/**
 * api.js —— 数据集绑定与请求客户端。
 *
 * 引用：nav.js 启动时 bindDataset；各业务页通过 API.apiPath / watchJob / 封装方法访问后端。
 */
var API = (function () {
  'use strict';

  var _dataset = null;
  var _bound = false;

  /**
   * 从路径解析数据集名。引用：boot / nav.js。
   * @returns {string|null}
   */
  function parseDatasetFromPath() {
    var m = location.pathname.match(/^\/d\/([^/]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }

  /**
   * 绑定当前页面数据集，整个文档生命周期仅一次。
   * @param {string} name
   * 引用：nav.js boot。
   */
  function bindDataset(name) {
    if (_bound) {
      throw new Error('数据集已绑定，同一页面内不得更换');
    }
    if (!name) {
      throw new Error('数据集名为空');
    }
    _dataset = name;
    _bound = true;
  }

  /**
   * @returns {string}
   */
  function dataset() {
    if (!_bound || !_dataset) {
      throw new Error('未绑定数据集');
    }
    return _dataset;
  }

  /**
   * 构造数据集作用域 API 路径。
   * @param {string} suffix 如 '/dirs' 或 '/aesthetic/start'
   * @returns {string}
   * 引用：本模块各请求；各页本地 fetch。
   */
  function apiPath(suffix) {
    if (!suffix) suffix = '';
    if (suffix.charAt(0) !== '/') suffix = '/' + suffix;
    return '/api/d/' + encodeURIComponent(dataset()) + suffix;
  }

  /**
   * 当前已选子目录列表。
   * @returns {string[]}
   */
  function getDirs() {
    if (typeof DirPicker !== 'undefined' && DirPicker.getDirs) {
      return DirPicker.getDirs();
    }
    return ['main'];
  }

  /**
   * @returns {string}
   */
  function dirsQuery() {
    return 'dirs=' + encodeURIComponent(getDirs().join(','));
  }

  /**
   * @param {string[]} names
   */
  function setDirs(names) {
    if (typeof DirPicker !== 'undefined') DirPicker.save(names || ['main']);
  }

  /**
   * @param {string} path
   * @param {{method?: string, body?: string}} [opts]
   * @returns {Promise<object>}
   */
  async function request(path, opts) {
    opts = opts || {};
    var res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      method: opts.method || 'GET',
      body: opts.body || undefined,
    });
    if (!res.ok) {
      var err = await res.text();
      throw new Error(res.status + ': ' + err);
    }
    return res.json();
  }

  function listDirs() { return request(apiPath('/dirs')); }

  /**
   * @param {boolean} [showTrash]
   * @returns {Promise<object>}
   */
  function listImages(showTrash) {
    var url = apiPath('/images') + '?' + dirsQuery();
    if (showTrash) url += '&show_trash=1';
    return request(url);
  }

  /**
   * @param {string} h
   * @returns {string}
   */
  function thumbnailUrl(h) {
    return '/d/' + encodeURIComponent(dataset()) + '/thumb/' + h + '.avif';
  }

  /**
   * @param {string} p
   * @returns {string}
   */
  function imageUrl(p) {
    return apiPath('/image') + '?path=' + encodeURIComponent(p);
  }

  /**
   * @param {string} p
   * @param {number} [limit]
   * @returns {Promise<object>}
   */
  function similarImages(p, limit) {
    var url = apiPath('/images/similar') + '?path=' + encodeURIComponent(p);
    if (limit) url += '&limit=' + limit;
    return request(url);
  }

  function toggle(p) {
    return request(apiPath('/toggle'), { method: 'POST', body: JSON.stringify({ path: p }) });
  }
  function selectRange(paths) {
    return request(apiPath('/select-range'), { method: 'POST', body: JSON.stringify({ paths: paths }) });
  }

  function selectAll(showTrash) {
    return request(apiPath('/select-all'), {
      method: 'POST',
      body: JSON.stringify({ dirs: getDirs(), show_trash: !!showTrash }),
    });
  }

  function deselectAll() {
    return request(apiPath('/deselect-all'), { method: 'POST' });
  }

  function applyDelete() {
    return request(apiPath('/apply'), { method: 'POST' });
  }

  function restore(paths) {
    return request(apiPath('/restore'), { method: 'POST', body: JSON.stringify({ paths: paths }) });
  }

  function getState() {
    return request(apiPath('/state') + '?' + dirsQuery());
  }

  /**
   * 订阅长任务 Server-Sent Events，断连自动重连。
   * @param {string} jobId
   * @param {(ev: object) => void} onEvent
   * @returns {{ close: function }}
   * 引用：index.js、scoring.js、booru_filter.js。
   */
  function watchJob(jobId, onEvent) {
    var closed = false;
    var retry = 0;
    var es = null;

    function connect() {
      if (closed) return;
      es = new EventSource('/api/jobs/' + encodeURIComponent(jobId) + '/events');
      es.onmessage = function (msg) {
        retry = 0;
        var data = JSON.parse(msg.data);
        onEvent(data);
        if (data.status === 'done' || data.status === 'error' || data.status === 'cancelled') {
          es.close();
          closed = true;
        }
      };
      es.onerror = function () {
        es.close();
        if (closed) return;
        var delay = Math.min(1000 * Math.pow(2, retry), 10000);
        retry++;
        setTimeout(connect, delay);
      };
    }

    connect();
    return {
      close: function () { closed = true; if (es) es.close(); },
    };
  }

  /**
   * 查询指定类型的活跃长任务（页面刷新后重连用）。
   * @param {string} kind 任务类型: archive / aesthetic / quality / online_batch
   * @param {string} [dataset] 数据集名, 省略则不限
   * @returns {Promise<object|null>} JobSnapshot 或 null
   * 引用：index.js、scoring.js、booru_filter.js。
   */
  async function findActiveJob(kind, dataset) {
    var qs = 'kind=' + encodeURIComponent(kind);
    if (dataset) qs += '&dataset=' + encodeURIComponent(dataset);
    var res = await fetch('/api/jobs/active?' + qs);
    if (!res.ok) return null;
    var data = await res.json();
    if (!data.job_id || data.status === 'idle') return null;
    return data;
  }

  /**
   * 请求取消长任务。
   * @param {string} jobId
   * @returns {Promise<object>}
   */
  function cancelJob(jobId) {
    return request('/api/jobs/' + encodeURIComponent(jobId) + '/cancel', { method: 'POST' });
  }

  return {
    parseDatasetFromPath: parseDatasetFromPath,
    bindDataset: bindDataset,
    dataset: dataset,
    apiPath: apiPath,
    getDirs: getDirs,
    setDirs: setDirs,
    getDir: function () { return getDirs()[0] || 'main'; },
    setDir: function (d) { setDirs([d || 'main']); },
    listDirs: listDirs,
    listImages: listImages,
    thumbnailUrl: thumbnailUrl,
    imageUrl: imageUrl,
    similarImages: similarImages,
    toggle: toggle,
    selectRange: selectRange,
    selectAll: selectAll,
    deselectAll: deselectAll,
    applyDelete: applyDelete,
    restore: restore,
    getState: getState,
    watchJob: watchJob,
    findActiveJob: findActiveJob,
    cancelJob: cancelJob,
    request: request,
  };
})();

(function () {
  var name = API.parseDatasetFromPath();
  if (name) API.bindDataset(name);
})();
