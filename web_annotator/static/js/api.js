/**
 * api.js —— 后端 API 客户端
 */
var API = (function() {
  'use strict';

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

  function listImages(showTrash) {
    var url = '/api/images';
    if (showTrash) url += '?show_trash=1';
    return request(url);
  }
  function thumbnailUrl(h) { return '/thumb/' + h + '.avif'; }
  function imageUrl(p) { return '/api/image?path=' + encodeURIComponent(p); }
  function similarImages(p, limit) {
    var url = '/api/images/similar?path=' + encodeURIComponent(p);
    if (limit) url += '&limit=' + limit;
    return request(url);
  }
  function toggle(p)      { return request('/api/toggle', { method: 'POST', body: JSON.stringify({ path: p }) }); }
  function selectAll()    { return request('/api/select-all', { method: 'POST' }); }
  function deselectAll()  { return request('/api/deselect-all', { method: 'POST' }); }
  function applyDelete()  { return request('/api/apply', { method: 'POST' }); }
  function restore(paths) { return request('/api/restore', { method: 'POST', body: JSON.stringify({ paths: paths }) }); }
  function getState()     { return request('/api/state'); }

  return {
    listImages: listImages, thumbnailUrl: thumbnailUrl, imageUrl: imageUrl,
    similarImages: similarImages,
    toggle: toggle, selectAll: selectAll, deselectAll: deselectAll,
    applyDelete: applyDelete, restore: restore, getState: getState,
  };
})();
