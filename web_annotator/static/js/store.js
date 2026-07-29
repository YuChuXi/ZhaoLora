/**
 * store.js -- toggle 时增量通知，不触发全量渲染；支持排序
 */
var Store = (function() {
  'use strict';

  var _images = [];
  var _listeners = [];
  var _toggleListeners = [];
  var _sortBy = localStorage.getItem('annotator_sort') || 'name';

  function setSort(by) {
    _sortBy = by || 'name';
    localStorage.setItem('annotator_sort', _sortBy);
  }

  function getSort() { return _sortBy; }

  function _sortList(list) {
    var out = list.slice();
    if (_sortBy === 'aesthetic') {
      out.sort(function (a, b) {
        var da = (typeof a.aesthetic === 'number') ? a.aesthetic : -1;
        var db = (typeof b.aesthetic === 'number') ? b.aesthetic : -1;
        if (db !== da) return db - da;
        return (a.stem || '').localeCompare(b.stem || '', 'en');
      });
    } else if (_sortBy === 'quality') {
      out.sort(function (a, b) {
        var da = (typeof a.quality === 'number') ? a.quality : -1;
        var db = (typeof b.quality === 'number') ? b.quality : -1;
        if (db !== da) return db - da;
        return (a.stem || '').localeCompare(b.stem || '', 'en');
      });
    } else if (_sortBy === 'pixels') {
      out.sort(function (a, b) {
        var pa = (a.width || 0) * (a.height || 0);
        var pb = (b.width || 0) * (b.height || 0);
        if (pb !== pa) return pb - pa;
        return (a.stem || '').localeCompare(b.stem || '', 'en');
      });
    } else {
      out.sort(function (a, b) {
        return (a.stem || '').localeCompare(b.stem || '', 'en', { numeric: true });
      });
    }
    return out;
  }

  async function load() {
    _images = await API.listImages(true);
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
    return _sortList(result);
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
    for (var i = 0; i < _toggleListeners.length; i++) {
      _toggleListeners[i](path, res.selected);
    }
  }

  /**
   * 批量选中路径范围（Shift+点击）。
   * @param {string[]} paths
   */
  async function selectRange(paths) {
    await API.selectRange(paths);
    var pathSet = {};
    for (var i = 0; i < paths.length; i++) pathSet[paths[i]] = true;
    for (var j = 0; j < _images.length; j++) {
      if (pathSet[_images[j].path]) _images[j].selected = true;
    }
    _notify();
    for (var k = 0; k < paths.length; k++) {
      for (var m = 0; m < _toggleListeners.length; m++) {
        _toggleListeners[m](paths[k], true);
      }
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

  /**
   * 统计当前列表：已收录不含回收站。
   * @returns {{total: number, selected: number, trashed: number, selNotTrashed: number}}
   */
  function stats() {
    var total = 0;
    var selected = 0;
    var trashed = 0;
    var selNotTrashed = 0;
    for (var i = 0; i < _images.length; i++) {
      var img = _images[i];
      if (img.trashed) {
        trashed++;
        if (img.selected) selected++;
        continue;
      }
      total++;
      if (img.selected) {
        selected++;
        selNotTrashed++;
      }
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
    toggle: toggle, selectRange: selectRange, selectAll: selectAll, deselectAll: deselectAll,
    invert: invert, applyDelete: applyDelete, restore: restore,
    stats: stats, onChange: onChange, onToggle: onToggle,
    setSort: setSort, getSort: getSort,
  };
})();
