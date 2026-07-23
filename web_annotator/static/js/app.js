/**
 * app.js -- toggle 增量更新 class，不重绘；批量操作才全量刷新
 */
(function() {
  'use strict';

  var _toastTimer = null;
  function toast(msg) {
    var el = document.getElementById('toast');
    if (!el) return;
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function() { el.classList.remove('show'); }, 2000);
  }

  function updateStats(st) {
    st = st || Store.stats();
    document.getElementById('stat-total').textContent    = st.total;
    document.getElementById('stat-selected').textContent = st.selected;
    document.getElementById('stat-trashed').textContent  = st.trashed;
    document.getElementById('btn-apply').disabled   = st.selNotTrashed === 0;
    document.getElementById('btn-restore').disabled = st.trashed === 0;
  }

  function refreshView() {
    var showTrash = document.getElementById('chk-show-trash').checked;
    Masonry.render(Store.getVisible(showTrash));
  }

  async function handleApplyDelete() {
    var st = Store.stats();
    if (st.selNotTrashed === 0) { toast('未选择图片'); return; }
    if (!confirm('将 ' + st.selNotTrashed + ' 张移入回收站？')) return;
    try {
      var res = await Store.applyDelete();
      refreshView();
      toast('已移入回收站 ' + res.moved + ' 张');
    } catch (e) { toast('删除失败'); }
  }

  async function handleRestore() {
    var st = Store.stats();
    if (st.trashed === 0) { toast('回收站为空'); return; }
    var restoreAll = confirm('恢复全部回收站图片？\n确定 = 全部 · 取消 = 仅选中项');
    var paths;
    if (restoreAll) {
      paths = [];
      var imgs = Store.getAll();
      for (var i = 0; i < imgs.length; i++) { if (imgs[i].trashed) paths.push(imgs[i].path); }
    } else {
      paths = [];
      var imgs2 = Store.getAll();
      for (var i = 0; i < imgs2.length; i++) { if (imgs2[i].trashed && imgs2[i].selected) paths.push(imgs2[i].path); }
      if (paths.length === 0) { toast('请先显示回收站并选中图片'); return; }
    }
    try {
      var res = await Store.restore(paths);
      refreshView();
      toast('已恢复 ' + res.restored + ' 张');
    } catch (e) { toast('恢复失败'); }
  }

  function onKeyDown(e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
    if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
      e.preventDefault(); Store.selectAll().then(refreshView);
    } else if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === 'A' || e.key === 'a')) {
      e.preventDefault(); Store.deselectAll().then(refreshView);
    } else if ((e.ctrlKey || e.metaKey) && e.key === 'i') {
      e.preventDefault(); Store.invert().then(refreshView);
    } else if ((e.ctrlKey || e.metaKey) && e.key === 'z') {
      e.preventDefault(); handleRestore();
    } else if (e.key === 'Delete') {
      e.preventDefault(); handleApplyDelete();
    }
  }

  function init() {
    Masonry.init('masonry');

    Store.onChange(function(st) {
      updateStats(st);
    });

    Store.onToggle(function(path, selected) {
      Masonry.setSelected(path, selected);
    });

    document.getElementById('btn-select-all').addEventListener('click', function() {
      Store.selectAll().then(refreshView);
    });
    document.getElementById('btn-deselect-all').addEventListener('click', function() {
      Store.deselectAll().then(refreshView);
    });
    document.getElementById('btn-invert').addEventListener('click', function() {
      Store.invert().then(refreshView);
    });
    document.getElementById('btn-apply').addEventListener('click', handleApplyDelete);
    document.getElementById('btn-restore').addEventListener('click', handleRestore);
    document.getElementById('chk-show-trash').addEventListener('change', refreshView);

    var sortSel = document.getElementById('sort-by');
    if (sortSel) {
      sortSel.value = Store.getSort();
      sortSel.addEventListener('change', function () {
        Store.setSort(sortSel.value);
        refreshView();
      });
    }

    DirPicker.onChange(function () {
      Store.load().then(function () {
        updateStats();
        refreshView();
      }).catch(function (e) { toast('加载失败: ' + e.message); });
    });

    var colsSlider = document.getElementById('cols-slider');
    var colsLabel = document.getElementById('cols-label');
    var _manualCols = 0;
    colsSlider.addEventListener('input', function() {
      var v = parseInt(this.value);
      _manualCols = v;
      if (v === 0) {
        colsLabel.textContent = '自动';
        _autoColumns();
      } else {
        colsLabel.textContent = v;
        Masonry.setColumns(v);
      }
    });
    window.addEventListener('resize', function() {
      if (_manualCols === 0) _autoColumns();
    });
    function _autoColumns() {
      var w = window.innerWidth;
      var c = w >= 1600 ? 6 : w >= 1280 ? 5 : w >= 960 ? 4 : w >= 640 ? 3 : 2;
      Masonry.setColumns(c);
    }
    _autoColumns();

    document.addEventListener('keydown', onKeyDown);

    Store.load().then(function() {
      updateStats();
      refreshView();
    }).catch(function(e) {
      toast('加载失败: ' + e.message);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
