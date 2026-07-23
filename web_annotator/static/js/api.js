/**
 * api.js —— 后端 API 客户端
 */
var API = (function() {
  'use strict';

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
   * dirs 查询参数（逗号分隔）。
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

  function listDirs() { return request('/api/dirs'); }

  /**
   * @param {boolean} [showTrash]
   * @returns {Promise<object>}
   */
  function listImages(showTrash) {
    var url = '/api/images?' + dirsQuery();
    if (showTrash) url += '&show_trash=1';
    return request(url);
  }

  function thumbnailUrl(h) { return '/thumb/' + h + '.avif'; }
  function imageUrl(p) { return '/api/image?path=' + encodeURIComponent(p); }

  /**
   * @param {string} p
   * @param {number} [limit]
   * @returns {Promise<object>}
   */
  function similarImages(p, limit) {
    var url = '/api/images/similar?path=' + encodeURIComponent(p);
    if (limit) url += '&limit=' + limit;
    return request(url);
  }

  function toggle(p) {
    return request('/api/toggle', { method: 'POST', body: JSON.stringify({ path: p }) });
  }

  function selectAll() {
    return request('/api/select-all', {
      method: 'POST',
      body: JSON.stringify({ dirs: getDirs() }),
    });
  }

  function deselectAll() { return request('/api/deselect-all', { method: 'POST' }); }
  function applyDelete()  { return request('/api/apply', { method: 'POST' }); }
  function restore(paths) {
    return request('/api/restore', { method: 'POST', body: JSON.stringify({ paths: paths }) });
  }

  function getState() {
    return request('/api/state?' + dirsQuery());
  }

  return {
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
    selectAll: selectAll,
    deselectAll: deselectAll,
    applyDelete: applyDelete,
    restore: restore,
    getState: getState,
  };
})();
