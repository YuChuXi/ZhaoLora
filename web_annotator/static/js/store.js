/**
 * store.js -- toggle 时增量通知，不触发全量渲染；支持排序
 */
var Store = (function() {
  'use strict';

  var _images = [];
  var _listeners = [];
  var _toggleListeners = [];
  var _sortBy = localStorage.getItem('annotator_sort') || 'name';
  var _searchOrder = [];   // 语义搜索排序的有序路径列表（新的排序覆盖旧值）
  var _searchSim = {};     // 路径 -> 相似度，用于卡片标注 S 前缀

  function setSort(by) {
    _sortBy = by || 'name';
    localStorage.setItem('annotator_sort', _sortBy);
  }

  function getSort() { return _sortBy; }

  /**
   * 记录一次语义搜索排序结果。
   * @param {Array<{path: string, similarity: number}>} items 按相似度降序的结果
   */
  function setSearchResult(items) {
    _searchOrder = [];
    _searchSim = {};
    for (var i = 0; i < items.length; i++) {
      _searchOrder.push(items[i].path);
      _searchSim[items[i].path] = items[i].similarity;
    }
  }

  /** 清空语义搜索排序结果。 */
  function clearSearch() { setSearchResult([]); }

  /** @returns {boolean} 是否存在已生效的语义搜索排序 */
  function isSearchActive() { return _searchOrder.length > 0; }

  /**
   * 取某路径的相似度（不存在返回 undefined）。
   * @param {string} path
   * @returns {number|undefined}
   */
  function getSearchSim(path) { return _searchSim[path]; }

  function _sortList(list) {
    var out = list.slice();
    if (_sortBy === 'posted') {
      // 按发布时间新→旧（ISO8601 文本可直接比较）；无发布时间的置于末尾按名称排序
      out.sort(function (a, b) {
        var ta = a.posted_at || '', tb = b.posted_at || '';
        if (ta && tb && ta !== tb) return ta < tb ? 1 : -1;
        if (ta !== tb) return ta ? -1 : 1;
        return (a.stem || '').localeCompare(b.stem || '', 'en', { numeric: true });
      });
    } else if (_sortBy === 'aesthetic') {
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
    } else if (_sortBy === 'search') {
      // 按语义搜索结果顺序重排；未命中排序的图片置于末尾，按名称稳定排序
      var pos = {};
      for (var i = 0; i < _searchOrder.length; i++) pos[_searchOrder[i]] = i;
      out.sort(function (a, b) {
        var pa = (pos[a.path] !== undefined) ? pos[a.path] : Number.MAX_SAFE_INTEGER;
        var pb = (pos[b.path] !== undefined) ? pos[b.path] : Number.MAX_SAFE_INTEGER;
        if (pa !== pb) return pa - pb;
        return (a.stem || '').localeCompare(b.stem || '', 'en', { numeric: true });
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

  /**
   * 当前视图可见列表: 工作目录视图与回收站视图互斥.
   * @param {boolean} showTrash true = 仅回收站图片; false = 仅工作目录图片
   * @returns {Array} 排序后的可见图片列表
   */
  function getVisible(showTrash) {
    var result = [];
    for (var i = 0; i < _images.length; i++) {
      var img = _images[i];
      if (!!img.trashed !== showTrash) continue;
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

  /**
   * 全选当前视图的图片: 工作目录视图只选存活图片, 回收站视图只选回收站图片.
   * @param {boolean} showTrash 当前视图是否为回收站视图
   */
  async function selectAll(showTrash) {
    await API.selectAll(showTrash);
    for (var i = 0; i < _images.length; i++) {
      if (!!_images[i].trashed === showTrash) _images[i].selected = true;
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
    setSearchResult: setSearchResult, clearSearch: clearSearch,
    isSearchActive: isSearchActive, getSearchSim: getSearchSim,
  };
})();
