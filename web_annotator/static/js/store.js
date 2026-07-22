/**
 * store.js -- toggle 时增量通知，不触发全量渲染
 */
var Store = (function() {
  'use strict';

  var _images = [];
  var _listeners = [];
  var _toggleListeners = [];

  async function load() {
    _images = await API.listImages(true);  // 始终加载含回收站的数据，前端本地过滤
    _notify();
  }

  function getAll() { return _images; }

  function getVisible(showTrash) {
    var result = [];
    for (var i = 0; i < _images.length; i++) {
      var img = _images[i];
      if (img.trashed && !showTrash) continue;
      result.push(img);
    }
    return result;
  }

  function _findByPath(path) {
    for (var i = 0; i < _images.length; i++) {
      if (_images[i].path === path) return _images[i];
    }
    return null;
  }

  async function toggle(path) {
    var res = await API.toggle(path);
    var img = _findByPath(path);
    if (img) img.selected = res.selected;
    _notify();
    // 增量通知: 只更新被点的卡片
    for (var i = 0; i < _toggleListeners.length; i++) {
      _toggleListeners[i](path, res.selected);
    }
  }

  async function selectAll() {
    await API.selectAll();
    for (var i = 0; i < _images.length; i++) {
      if (!_images[i].trashed) _images[i].selected = true;
    }
    _notify();
  }

  async function deselectAll() {
    await API.deselectAll();
    for (var i = 0; i < _images.length; i++) _images[i].selected = false;
    _notify();
  }

  async function invert() {
    for (var i = 0; i < _images.length; i++) {
      var res = await API.toggle(_images[i].path);
      _images[i].selected = res.selected;
    }
    _notify();
  }

  async function applyDelete() {
    var res = await API.applyDelete();
    await load();
    return res;
  }

  async function restore(paths) {
    var res = await API.restore(paths);
    await load();
    return res;
  }

  function stats() {
    var total = _images.length;
    var selected = 0, trashed = 0, selNotTrashed = 0;
    for (var i = 0; i < _images.length; i++) {
      var img = _images[i];
      if (img.selected) selected++;
      if (img.trashed) trashed++;
      if (img.selected && !img.trashed) selNotTrashed++;
    }
    return { total: total, selected: selected, trashed: trashed, selNotTrashed: selNotTrashed };
  }

  function onChange(fn) { _listeners.push(fn); }
  function onToggle(fn) { _toggleListeners.push(fn); }

  function _notify() {
    var st = stats();
    for (var i = 0; i < _listeners.length; i++) _listeners[i](st);
  }

  return {
    load: load, getAll: getAll, getVisible: getVisible,
    toggle: toggle, selectAll: selectAll, deselectAll: deselectAll,
    invert: invert, applyDelete: applyDelete, restore: restore,
    stats: stats, onChange: onChange, onToggle: onToggle,
  };
})();
