/**
 * app.js -- toggle 增量更新 class，不重绘；批量操作才全量刷新
 */
(function() {
  'use strict';

  function updateStats(st) {
    st = st || Store.stats();
    document.getElementById('stat-kept').textContent = st.total;
    document.getElementById('stat-selected').textContent = st.selected;
    document.getElementById('stat-trashed').textContent = st.trashed;
    document.getElementById('btn-apply').disabled = st.selNotTrashed === 0;
    document.getElementById('btn-restore').disabled = st.trashed === 0;
  }

  function refreshView() {
    if (typeof BooruFilter !== 'undefined' && BooruFilter.isRemote()) return;
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
    if (typeof BooruFilter !== 'undefined' && BooruFilter.isRemote()) return;
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

  // ---- 语义搜索排序（本地筛选，Qwen3-VL-Reranker-2B） ----
  var _searchBusy = false;

  /**
   * 依据当前排序方式显隐「搜索」按钮：仅当排序方式为「搜索」时显示。
   */
  function searchShowButton() {
    var sel = document.getElementById('sort-by');
    var btn = document.getElementById('btn-search-dlg');
    if (!btn) return;
    btn.hidden = !(sel && sel.value === 'search');
  }

  /**
   * 卡片装饰：搜索排序生效时在名称前标注相似度 S 前缀（参考标签检查页）。
   * @param {HTMLElement} card
   * @param {object} img
   */
  function searchDecorate(card, img) {
    if (Store.getSort() !== 'search') return;
    var sim = Store.getSearchSim(img.path);
    if (sim == null) return;
    var name = card.querySelector('.name');
    if (name) name.textContent = 'S' + sim.toFixed(3) + ' ' + name.textContent;
  }

  /**
   * 更新语义搜索进度条。
   * @param {Object} s - 任务快照
   */
  function searchProgress(s) {
    var bar = document.getElementById('search-progress');
    var running = s.status === 'running' || s.status === 'queued';
    if (bar) bar.hidden = !running;
    var fill = document.getElementById('search-progress-fill');
    if (fill) fill.classList.toggle('indeterminate', s.status === 'queued');
    if (fill && s.status === 'running') fill.style.width = Math.min(100, s.pct || 0) + '%';
    var pctEl = document.getElementById('search-progress-pct');
    if (pctEl) pctEl.textContent = s.status === 'queued' ? '…' : (Math.min(100, Math.round(s.pct || 0)) + '%');
    var statusEl = document.getElementById('search-status');
    if (statusEl && s.message) statusEl.textContent = s.message;
  }

  /**
   * 加载最近搜索历史并填充下拉框。
   */
  function searchLoadHistory() {
    API.request(API.apiPath('/tags/embedding/history')).then(function (res) {
      var sel = document.getElementById('search-history');
      if (!sel) return;
      var queries = (res && res.queries) || [];
      sel.innerHTML = '';
      for (var i = 0; i < queries.length; i++) {
        var opt = document.createElement('option');
        opt.value = queries[i].query;
        opt.textContent = queries[i].query;
        sel.appendChild(opt);
      }
      sel.hidden = queries.length === 0;
    }).catch(function () {
      var sel = document.getElementById('search-history');
      if (sel) sel.hidden = true;
    });
  }

  /**
   * 提交语义搜索排序：调用后端长任务，完成后按相似度重排本地列表。
   * @returns {Promise<void>}
   */
  async function searchSort() {
    var query = document.getElementById('search-query').value.trim();
    if (!query) { toast('请输入查询文本'); return; }
    if (_searchBusy) return;
    _searchBusy = true;
    var btn = document.getElementById('btn-search-sort');
    btn.disabled = true;
    document.getElementById('search-status').textContent = '提交中…';
    try {
      var res = await API.request(API.apiPath('/tags/embedding/query'), {
        method: 'POST',
        body: JSON.stringify({ query: query, dirs: DirPicker.getDirs() }),
      });
      if (!res.job_id) {
        document.getElementById('search-status').textContent = '查询失败';
        btn.disabled = false;
        _searchBusy = false;
        return;
      }
      API.watchJob(res.job_id, function (s) {
        searchProgress(s);
        if (s.status === 'done') {
          btn.disabled = false;
          _searchBusy = false;
          var items = (s.result && s.result.items) || [];
          if (items.length === 0) {
            document.getElementById('search-status').textContent = '无匹配结果';
            return;
          }
          var sel = document.getElementById('sort-by');
          if (sel) sel.value = 'search';
          Store.setSort('search');
          Store.setSearchResult(items);
          searchShowButton();
          refreshView();
          document.getElementById('search-status').textContent = '已按相似度排序 (' + items.length + ' 张)';
          toast('已按相似度排序 (' + items.length + ' 张)');
        } else if (s.status === 'error') {
          btn.disabled = false;
          _searchBusy = false;
          document.getElementById('search-status').textContent = '查询失败';
          toast('查询失败: ' + (s.message || ''));
        }
      });
    } catch (e) {
      document.getElementById('search-status').textContent = '查询失败';
      toast('查询失败: ' + String(e.message || e));
      btn.disabled = false;
      _searchBusy = false;
    }
  }

  /**
   * 打开语义搜索对话框并加载历史。
   */
  function searchOpen() {
    var dlg = document.getElementById('search-dlg');
    dlg.classList.add('open');
    dlg.setAttribute('aria-hidden', 'false');
    searchLoadHistory();
    document.getElementById('search-query').focus();
  }

  /** 关闭语义搜索对话框。 */
  function searchClose() {
    var dlg = document.getElementById('search-dlg');
    dlg.classList.remove('open');
    dlg.setAttribute('aria-hidden', 'true');
  }

  function init() {
    Masonry.init('masonry', { decorate: searchDecorate });

    var colsSlider = document.getElementById('cols-slider');
    var colsLabel = document.getElementById('cols-label');
    var COLS_KEY = 'zhao_filter_cols';
    var _manualCols = 0;

    function _autoColumns() {
      var w = window.innerWidth;
      var c = w >= 1600 ? 6 : w >= 1280 ? 5 : w >= 960 ? 4 : w >= 640 ? 3 : 2;
      if (typeof BooruFilter !== 'undefined' && BooruFilter.isRemote()) BooruFilter.render();
      else Masonry.setColumns(c);
    }

    function applyCols(v) {
      var keepPage = null;
      if (typeof BooruFilter !== 'undefined' && BooruFilter.isRemote()) {
        keepPage = BooruFilter.currentPage();
      }
      _manualCols = v;
      if (colsSlider) colsSlider.value = String(v);
      if (v === 0) {
        if (colsLabel) colsLabel.textContent = '自动';
        _autoColumns();
      } else {
        if (colsLabel) colsLabel.textContent = String(v);
        if (typeof BooruFilter !== 'undefined' && BooruFilter.isRemote()) BooruFilter.render();
        else if (typeof Masonry !== 'undefined') Masonry.setColumns(v);
      }
      if (keepPage != null && typeof BooruFilter !== 'undefined') {
        requestAnimationFrame(function () {
          BooruFilter.scrollToPage(keepPage);
        });
      }
    }

    var savedCols = 0;
    try { savedCols = parseInt(localStorage.getItem(COLS_KEY), 10); } catch (e) { savedCols = 0; }
    if (!(savedCols >= 0 && savedCols <= 16)) savedCols = 0;
    if (colsSlider) colsSlider.value = String(savedCols);
    _manualCols = savedCols;
    if (colsLabel) colsLabel.textContent = savedCols === 0 ? '自动' : String(savedCols);
    if (savedCols > 0) Masonry.setColumns(savedCols);
    else _autoColumns();

    if (colsSlider) {
      colsSlider.addEventListener('input', function() {
        var v = parseInt(this.value, 10);
        if (isNaN(v) || v < 0) v = 0;
        try { localStorage.setItem(COLS_KEY, String(v)); } catch (e) {}
        applyCols(v);
      });
    }
    window.addEventListener('resize', function() {
      if (_manualCols === 0) _autoColumns();
    });

    if (typeof BooruFilter !== 'undefined') BooruFilter.init();

    Store.onChange(function(st) {
      if (typeof BooruFilter !== 'undefined' && BooruFilter.isRemote()) return;
      updateStats(st);
    });

    Store.onToggle(function(path, selected) {
      if (typeof BooruFilter !== 'undefined' && BooruFilter.isRemote()) return;
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
        searchShowButton();
      });
    }
    searchShowButton();

    // ---- 语义搜索对话框事件 ----
    document.getElementById('btn-search-dlg').addEventListener('click', searchOpen);
    document.getElementById('btn-search-close').addEventListener('click', searchClose);
    document.getElementById('search-dlg').addEventListener('click', function (e) {
      if (e.target === this) searchClose();
    });
    document.getElementById('btn-search-sort').addEventListener('click', function () {
      searchSort().catch(function (e) { toast('查询失败: ' + String(e.message || e)); });
    });
    document.getElementById('search-query').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        searchSort().catch(function (e) { toast('查询失败: ' + String(e.message || e)); });
      }
    });
    document.getElementById('search-history').addEventListener('change', function () {
      var q = this.value;
      if (!q) return;
      document.getElementById('search-query').value = q;
      searchSort().catch(function (e) { toast('查询失败: ' + String(e.message || e)); });
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && document.getElementById('search-dlg').classList.contains('open')) {
        searchClose();
      }
    });

    DirPicker.onChange(function () {
      if (typeof BooruFilter !== 'undefined' && BooruFilter.isRemote()) {
        BooruFilter.refreshDiskCount().catch(function (e) {
          toast(String(e.message || e));
        });
        return;
      }
      Store.load().then(function () {
        updateStats();
        refreshView();
      }).catch(function (e) { toast('加载失败: ' + e.message); });
    });

    document.addEventListener('keydown', onKeyDown);

    Store.load().then(function () {
      updateStats();
      refreshView();
    }).catch(function (e) {
      toast('加载失败: ' + e.message);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
