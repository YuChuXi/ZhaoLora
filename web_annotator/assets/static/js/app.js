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

  var _visiblePaths = [];
  var _lastPath = null;

  function refreshView() {
    if (typeof BooruFilter !== 'undefined' && BooruFilter.isRemote()) return;
    var items = Store.getVisible(showTrashChecked());
    _visiblePaths = items.map(function (it) { return it.path; });
    Masonry.update(items);
  }

  /**
   * 本地卡片单击：Shift 连选闭区间，否则切换单张。
   *
   * Args:
   *   path (string): 图片路径。
   *   rangeSelect (boolean): 是否按住 Shift 连选。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - app.js:瀑布流 click 委托
   */
  function toggleLocal(path, rangeSelect) {
    if (rangeSelect && _lastPath) {
      var from = _visiblePaths.indexOf(_lastPath);
      var to = _visiblePaths.indexOf(path);
      if (from >= 0 && to >= 0) {
        Store.selectRange(_visiblePaths.slice(Math.min(from, to), Math.max(from, to) + 1))
          .catch(function (e) { toast(String(e.message || e)); });
        _lastPath = path;
        return;
      }
    }
    _lastPath = path;
    Store.toggle(path).catch(function (e) { toast(String(e.message || e)); });
  }

  /**
   * 卡片预览：远程打开帖子预览，本地打开颜色相似图。
   *
   * Args:
   *   card (HTMLElement): 卡片元素。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - app.js:瀑布流 contextmenu 委托 / UI.bindLongPress
   */
  function openCardPreview(card) {
    if (typeof BooruFilter !== 'undefined' && BooruFilter.isRemote()) {
      BooruFilter.previewCard(card);
      return;
    }
    if (!card.dataset.path || typeof SimilarHover === 'undefined') return;
    SimilarHover.showFor(card.dataset.path, card);
  }

  function showTrashChecked() {
    return document.getElementById('chk-show-trash').checked;
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
    var imgs = Store.getAll();
    var paths = [];
    for (var i = 0; i < imgs.length; i++) { if (imgs[i].trashed && imgs[i].selected) paths.push(imgs[i].path); }
    if (paths.length === 0) { toast('回收站视图中未选中图片'); return; }
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
      e.preventDefault(); Store.selectAll(showTrashChecked()).then(refreshView);
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
    if (name) name.textContent = _searchPrefix + sim.toFixed(3) + ' ' + name.textContent;
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

  // 查询模式: semantic=自然语言描述(重排序模型), tag=标签(标签评分器)
  var _searchMode = 'semantic';
  var _searchSuggest = null;
  var _searchPrefix = 'S';   // 卡片分值前缀: S=相似度, T=标签评分

  // WD14 标签类别 → 中文说明
  var _TAG_CATEGORY_NAMES = { general: '通用', character: '角色', rating: '分级' };

  /**
   * 按当前片段取标签评分器可评分的标签候选(WD14 标签表前缀匹配)。
   *
   * Args:
   *   word {string}: 正在输入的标签片段。
   *
   * Returns:
   *   {Promise<Array<{name: string, note: string, count: number}>>}
   *
   * Callers:
   *   - app.js:init 中 UI.bindTagSuggest 的 fetchTags
   */
  function searchFetchTags(word) {
    if (_searchMode !== 'tag') return Promise.resolve([]);
    var q = word.replace(/_/g, ' ').trim();
    if (!q) return Promise.resolve([]);
    return API.request(API.apiPath('/scoring/tags/suggest')
      + '?q=' + encodeURIComponent(q) + '&limit=12')
      .then(function (data) {
        return (data && data.tags ? data.tags : []).map(function (tag) {
          return {
            name: tag.name,
            note: _TAG_CATEGORY_NAMES[tag.category] || tag.category,
            count: 0,
          };
        });
      });
  }

  /**
   * 依查询模式切换查询框的标签与占位提示, 并关闭残留的补全下拉。
   *
   * Callers:
   *   - app.js:init
   */
  function searchApplyMode() {
    var tagMode = _searchMode === 'tag';
    document.getElementById('search-query-label').textContent = tagMode ? '标签' : '查询文本';
    document.getElementById('search-query').placeholder = tagMode
      ? '标签，逗号分隔，如 1girl, solo'
      : '查询文本，回车或点击排序';
    document.getElementById('search-history-row').hidden = tagMode;
    document.getElementById('btn-search-sort').textContent =
      tagMode ? '按标签评分排序' : '按相似度排序';
    if (_searchSuggest) _searchSuggest.close();
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
   * 应用一次排序结果: 写入 Store 的顺序并重排瀑布流。
   *
   * Args:
   *   items {Array<object>}: 后端返回的排序项(含 score 或 similarity)。
   *   tagMode {boolean}: 是否为标签评分排序。
   *   missing {number}: 缺 WD14 缓存、未参与排序的图片数。
   *
   * Callers:
   *   - app.js:searchSort
   */
  function searchApply(items, tagMode, missing) {
    // 语义结果用 similarity, 标签评分结果用 score, 统一为 Store 的 similarity 字段
    var ranked = items.map(function (item) {
      return {
        path: item.path,
        similarity: (typeof item.score === 'number') ? item.score : item.similarity,
      };
    });
    _searchPrefix = tagMode ? 'T' : 'S';
    var sel = document.getElementById('sort-by');
    if (sel) sel.value = 'search';
    Store.setSort('search');
    Store.setSearchResult(ranked);
    searchShowButton();
    refreshView();
    var text = tagMode
      ? '已按标签评分排序 (' + items.length + ' 张'
        + (missing ? '，缺缓存 ' + missing + ' 张' : '') + ')'
      : '已按相似度排序 (' + items.length + ' 张)';
    document.getElementById('search-status').textContent = text;
    toast(text);
  }

  /**
   * 提交搜索排序：语义模式走重排序模型，标签模式走标签评分器。
   * @returns {Promise<void>}
   */
  async function searchSort() {
    var tagMode = _searchMode === 'tag';
    var query = document.getElementById('search-query').value.trim().replace(/[\s,]+$/, '');
    if (!query) { toast(tagMode ? '请输入标签' : '请输入查询文本'); return; }
    if (_searchBusy) return;
    _searchBusy = true;
    var btn = document.getElementById('btn-search-sort');
    btn.disabled = true;
    document.getElementById('search-status').textContent = '提交中…';
    try {
      if (tagMode) {
        // 标签排序只读已有评分缓存, 同步返回, 不占用长任务队列
        var tagRes = await API.request(API.apiPath('/scoring/tag-query'), {
          method: 'POST',
          body: JSON.stringify({
            query: query.replace(/_/g, ' '),
            dirs: DirPicker.getDirs(),
          }),
        });
        btn.disabled = false;
        _searchBusy = false;
        var tagItems = (tagRes && tagRes.items) || [];
        if (!tagItems.length) {
          document.getElementById('search-status').textContent =
            '没有可用的标签分值，请先在评分页建立 WD14 缓存';
          return;
        }
        searchApply(tagItems, true, (tagRes && tagRes.missing) || 0);
        return;
      }
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
          searchApply(items, false, 0);
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
   * 打开搜索对话框并加载历史。
   */
  function searchOpen() {
    UI.openModal('search-dlg');
    searchLoadHistory();
    document.getElementById('search-query').focus();
  }

  /** 关闭语义搜索对话框。 */
  function searchClose() {
    UI.closeModal('search-dlg');
  }

  /**
   * 加载数据集评分项并追加到排序下拉, 同时暴露评分项元数据供卡片渲染。
   *
   * Returns:
   *   {Promise<void>}
   *
   * Callers:
   *   - app.js:init
   */
  async function loadScoreOptions() {
    var sel = document.getElementById('sort-by');
    if (!sel) return;
    var res = await API.request(API.apiPath('/scoring/items'));
    var items = res.items || [];
    window.ScoringMeta = { order: items };
    items.forEach(function (item) {
      var opt = document.createElement('option');
      opt.value = 'score:' + item.key;
      opt.textContent = item.label;
      sel.appendChild(opt);
    });
  }

  async function init() {
    await AppSettings.ready();
    await loadScoreOptions();
    Masonry.mount('masonry', { decorate: searchDecorate });

    var masonryRoot = document.getElementById('masonry');
    masonryRoot.addEventListener('click', function (e) {
      var card = e.target.closest('.card');
      if (!card) return;
      if (typeof BooruFilter !== 'undefined' && BooruFilter.isRemote()) {
        BooruFilter.clickCard(card);
        return;
      }
      if (card.dataset.path) toggleLocal(card.dataset.path, e.shiftKey);
    });
    masonryRoot.addEventListener('contextmenu', function (e) {
      var card = e.target.closest('.card');
      if (!card) return;
      e.preventDefault();
      openCardPreview(card);
    });
    UI.bindLongPress(masonryRoot, openCardPreview);
    _searchSuggest = UI.bindTagSuggest(document.getElementById('search-query'), {
      separator: 'comma',
      fetchTags: searchFetchTags,
    });
    var searchModeSel = document.getElementById('search-mode');
    if (searchModeSel) {
      searchModeSel.addEventListener('change', function () {
        _searchMode = searchModeSel.value === 'tag' ? 'tag' : 'semantic';
        searchApplyMode();
      });
      searchApplyMode();
    }

    var colsSlider = document.getElementById('cols-slider');
    var colsLabel = document.getElementById('cols-label');

    function applyCols(v) {
      var keepPage = null;
      if (typeof BooruFilter !== 'undefined' && BooruFilter.isRemote()) {
        keepPage = BooruFilter.currentPage();
      }
      if (colsSlider) colsSlider.value = String(v);
      if (colsLabel) colsLabel.textContent = v === 0 ? '自动' : String(v);
      Masonry.setColumns(v);
      if (keepPage != null) {
        requestAnimationFrame(function () {
          BooruFilter.scrollToPage(keepPage);
          BooruFilter.syncLoadTrigger();
        });
      }
    }

    var savedCols = (AppSettings.get('filter') || {}).cols;
    if (!(savedCols >= 0 && savedCols <= 16)) savedCols = 0;
    applyCols(savedCols);

    if (colsSlider) {
      colsSlider.addEventListener('input', function() {
        var v = parseInt(this.value, 10);
        if (isNaN(v) || v < 0) v = 0;
        var f = AppSettings.get('filter') || {};
        f.cols = v;
        AppSettings.set('filter', f);
        applyCols(v);
      });
    }
    window.addEventListener('resize', function() {
      if (typeof BooruFilter === 'undefined' || !BooruFilter.isRemote()) return;
      requestAnimationFrame(function () { BooruFilter.syncLoadTrigger(); });
    });

    if (typeof BooruFilter !== 'undefined') BooruFilter.init({ onLocal: refreshView });

    Store.onChange(function(st) {
      if (typeof BooruFilter !== 'undefined' && BooruFilter.isRemote()) return;
      updateStats(st);
    });

    Store.onToggle(function() {
      if (typeof BooruFilter !== 'undefined' && BooruFilter.isRemote()) return;
      Masonry.setSelected(Store.selectedPaths());
    });

    document.getElementById('btn-select-all').addEventListener('click', function() {
      Store.selectAll(showTrashChecked()).then(refreshView);
    });
    document.getElementById('btn-deselect-all').addEventListener('click', function() {
      Store.deselectAll().then(refreshView);
    });
    document.getElementById('btn-invert').addEventListener('click', function() {
      Store.invert().then(refreshView);
    });
    document.getElementById('btn-apply').addEventListener('click', handleApplyDelete);
    document.getElementById('btn-restore').addEventListener('click', handleRestore);
    var trashChk = document.getElementById('chk-show-trash');
    trashChk.checked = !!((AppSettings.get('filter') || {}).show_trash);
    trashChk.addEventListener('change', function () {
      var f = AppSettings.get('filter') || {};
      f.show_trash = trashChk.checked;
      AppSettings.set('filter', f);
      refreshView();
    });

    var sortSel = document.getElementById('sort-by');
    if (sortSel) {
      var savedSort = AppSettings.get('sort');
      if (savedSort) Store.setSort(savedSort);
      sortSel.value = Store.getSort();
      if (sortSel.selectedIndex < 0) {
        Store.setSort('name');
        sortSel.value = 'name';
      }
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
    UI.bindModal('search-dlg');
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

  function start() {
    init().catch(function (e) {
      toast('初始化失败: ' + String(e.message || e));
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
