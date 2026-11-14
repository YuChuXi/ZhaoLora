/**
 * masonry.js —— 多列瀑布流组件。
 *
 * 生命周期：mount 建立列并观察容器宽度，update/append 增量布局，unmount 释放。
 * 组件只持有布局状态（列数、列容器），不保存条目副本、不处理点击与选择。
 *
 * 卡片高度由已知宽高比、固定信息栏与间距在插入前算出，图片加载不改变任何已渲染
 * 卡片的尺寸与位置；列高只在追加与换列时读取一次真实值，不做二次测量与校正。
 */
var Masonry = (function () {
  'use strict';

  var GAP = 8;              // 列间距与卡片下间距，与 .masonry / .card 样式一致
  var CARD_BORDER = 4;      // .card 上下边框合计
  var INFO_H = 28;          // .card .info 固定高度
  var MIN_COL_W = 240;      // 自动列数下的最小列宽
  var MAX_COLUMNS = 16;     // 与列数滑杆上限一致

  var _root = null;
  var _opts = {};
  var _fixed = 0;           // 指定列数；0 表示按容器宽度自动
  var _columns = 0;         // 当前实际列数
  var _cols = [];
  var _observer = null;

  /**
   * 挂载瀑布流：建立列并观察容器宽度。
   *
   * Args:
   *   id (string): 容器元素 id。
   *   opts (object): 默认卡片的渲染参数与生命周期钩子。
   *     columns (number): 列数，0 表示自动。
   *     empty (string): 空列表文案，默认「没有图片」。
   *     isSelected(item) -> boolean: 卡片初始选中态，默认取 item.selected。
   *     thumb(item) -> string: 缩略图地址。
   *     decorate(card, item): 卡片构建后的页面装饰。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - app.js:init
   *   - caption.js:init
   *   - tag_check.js:init
   */
  function mount(id, opts) {
    var root = document.getElementById(id);
    if (!root) return;
    if (_root) unmount();
    _root = root;
    _opts = opts || {};
    _fixed = _clampFixed(_opts.columns);
    _columns = _fixed > 0 ? _fixed : _fitColumns();
    _buildColumns();
    if (typeof ResizeObserver !== 'undefined') {
      _observer = new ResizeObserver(_syncAutoColumns);
      _observer.observe(_root);
    }
  }

  /**
   * 卸载瀑布流：断开观察并移除全部列与空状态节点。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - masonry.js:mount（重复挂载）
   */
  function unmount() {
    if (_observer) {
      _observer.disconnect();
      _observer = null;
    }
    _removeColumns();
    _removeEmpty();
    _root = null;
    _opts = {};
    _fixed = 0;
    _columns = 0;
  }

  /**
   * 全量更新条目。
   *
   * Args:
   *   items (Array<object>): 条目列表，含 path / stem / width / height / thumbnail_hash。
   *   card (function(object): HTMLElement): 可选卡片构建器，省略时使用默认卡片。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - app.js:refreshView
   *   - caption.js:applyFilter
   *   - tag_check.js:runCheck
   *   - booru_filter.js:render
   */
  function update(items, card) {
    if (!_root) return;
    _removeEmpty();
    _removeColumns();
    if (!items.length) {
      _showEmpty(_opts.empty || '没有图片');
      return;
    }
    _buildColumns();
    var width = _colWidth();
    var heights = _zeroHeights();
    for (var i = 0; i < items.length; i++) {
      var el = _makeCard(items[i], card);
      var col = _shortest(heights);
      _cols[col].appendChild(el);
      heights[col] += _cardHeight(el, width);
    }
  }

  /**
   * 增量追加条目，已有卡片不重建。
   *
   * Args:
   *   items (Array<object>): 新条目列表。
   *   at (string): 'end' 追加到列尾，'start' 插入列首。
   *   card (function(object): HTMLElement): 可选卡片构建器。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - booru_filter.js:addPosts
   */
  function append(items, at, card) {
    if (!_root || !items.length) return;
    _removeEmpty();
    var toStart = at === 'start';
    var anchor = toStart ? _viewportAnchor() : null;
    var width = _colWidth();
    var heights = _zeroHeights();
    for (var h = 0; h < _cols.length; h++) heights[h] = _cols[h].offsetHeight;
    var fresh = [];
    for (var i = 0; i < items.length; i++) fresh.push(_makeCard(items[i], card));
    if (toStart) {
      for (var p = fresh.length - 1; p >= 0; p--) {
        var startCol = _shortest(heights);
        _cols[startCol].insertBefore(fresh[p], _cols[startCol].firstChild);
        heights[startCol] += _cardHeight(fresh[p], width);
      }
      _restoreAnchor(anchor);
      return;
    }
    for (var q = 0; q < fresh.length; q++) {
      var col = _shortest(heights);
      _cols[col].appendChild(fresh[q]);
      heights[col] += _cardHeight(fresh[q], width);
    }
  }

  /**
   * 设置列数并重新分配现有卡片。
   *
   * Args:
   *   n (number): 列数；0 表示按容器宽度自动。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - app.js:applyCols
   *   - tag_check.js:applyCols
   */
  function setColumns(n) {
    if (!_root) return;
    _fixed = _clampFixed(n);
    var target = _fixed > 0 ? _fixed : _fitColumns();
    if (target === _columns) return;
    _reflow(target);
  }

  /**
   * 把选中的路径投影到卡片样式，未列出的卡片取消选中。
   *
   * Args:
   *   paths (Array<string>): 选中的图片路径。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - app.js:Store.onToggle
   *   - caption.js:onSelectionChange
   */
  function setSelected(paths) {
    if (!_root) return;
    var selected = {};
    for (var i = 0; i < paths.length; i++) selected[paths[i]] = true;
    var cards = _root.querySelectorAll('.card[data-path]');
    for (var j = 0; j < cards.length; j++) {
      cards[j].classList.toggle('selected', !!selected[cards[j].dataset.path]);
    }
  }

  /**
   * 清空卡片并显示空状态文案。
   *
   * Args:
   *   text (string): 提示文案。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - booru_filter.js:render
   */
  function empty(text) {
    if (!_root) return;
    _removeColumns();
    _showEmpty(text);
  }

  /**
   * 滚动到指定路径的卡片并短暂高亮。
   *
   * Args:
   *   path (string): 图片相对路径。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - similar_hover.js:相似图点击
   */
  function jumpTo(path) {
    if (!_root) return;
    var cards = _root.querySelectorAll('.card[data-path]');
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].dataset.path !== path) continue;
      var card = cards[i];
      card.scrollIntoView({ block: 'center' });
      card.classList.add('jump-flash');
      setTimeout(function () { card.classList.remove('jump-flash'); }, 900);
      return;
    }
  }

  /**
   * 容器宽度变化时重算自动列数。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - ResizeObserver
   */
  function _syncAutoColumns() {
    if (!_root || _fixed > 0) return;
    var target = _fitColumns();
    if (target === _columns) return;
    _reflow(target);
  }

  /**
   * 按目标列数重排现有卡片，并保持视口锚点。
   *
   * Args:
   *   target (number): 目标列数。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - masonry.js:setColumns / _syncAutoColumns
   */
  function _reflow(target) {
    var cards = _root.querySelectorAll('.card');
    var anchor = _viewportAnchor();
    _columns = target;
    _buildColumns();
    var width = _colWidth();
    var heights = _zeroHeights();
    for (var i = 0; i < cards.length; i++) {
      var col = _shortest(heights);
      _cols[col].appendChild(cards[i]);
      heights[col] += _cardHeight(cards[i], width);
    }
    _restoreAnchor(anchor);
  }

  /**
   * 归一化列数：0 表示自动，正数截断到合法区间。
   *
   * Args:
   *   n (number): 请求列数。
   *
   * Returns:
   *   number: 指定列数；0 表示自动。
   *
   * Callers:
   *   - masonry.js:mount / setColumns
   */
  function _clampFixed(n) {
    var value = parseInt(n, 10);
    if (!(value > 0)) return 0;
    return Math.min(MAX_COLUMNS, value);
  }

  /**
   * 按容器宽度计算自动列数。
   *
   * Returns:
   *   number: 列数。
   *
   * Callers:
   *   - masonry.js:mount / setColumns / _syncAutoColumns
   */
  function _fitColumns() {
    if (!_root) return 1;
    var width = _root.clientWidth;
    if (!(width > 0)) return 1;
    return Math.max(1, Math.floor(width / MIN_COL_W));
  }

  /**
   * 建立列容器，保留容器内其它节点。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - masonry.js:mount / update / _reflow
   */
  function _buildColumns() {
    _removeColumns();
    for (var i = 0; i < _columns; i++) {
      var col = document.createElement('div');
      col.className = 'masonry-col';
      _root.appendChild(col);
      _cols.push(col);
    }
  }

  /**
   * 移除列容器。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - masonry.js:unmount / update / empty / _buildColumns
   */
  function _removeColumns() {
    for (var i = 0; i < _cols.length; i++) _cols[i].remove();
    _cols = [];
  }

  /**
   * 写入空状态节点。
   *
   * Args:
   *   text (string): 提示文案。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - masonry.js:update / empty
   */
  function _showEmpty(text) {
    var el = _root.querySelector('.masonry-empty');
    if (!el) {
      el = document.createElement('div');
      el.className = 'masonry-empty';
      _root.appendChild(el);
    }
    el.textContent = text;
  }

  /**
   * 移除空状态节点。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - masonry.js:unmount / update / append
   */
  function _removeEmpty() {
    if (!_root) return;
    var el = _root.querySelector('.masonry-empty');
    if (el) el.remove();
  }

  /**
   * 列宽，用于在插入前计算卡片高度。
   *
   * Returns:
   *   number: 列宽像素。
   *
   * Callers:
   *   - masonry.js:update / append / _reflow
   */
  function _colWidth() {
    return _cols.length ? _cols[0].getBoundingClientRect().width : 0;
  }

  /**
   * 卡片占用列高：边框、图片、信息栏与间距。
   *
   * Args:
   *   card (HTMLElement): 卡片元素。
   *   width (number): 列宽。
   *
   * Returns:
   *   number: 卡片总高。
   *
   * Callers:
   *   - masonry.js:update / append / _reflow
   */
  function _cardHeight(card, width) {
    var ratio = parseFloat(card.dataset.ratio);
    if (!(ratio > 0)) ratio = 1;
    return CARD_BORDER + Math.max(0, width - CARD_BORDER) * ratio + INFO_H + GAP;
  }

  /**
   * 返回列高数组。
   *
   * Returns:
   *   Array<number>: 长度等于列数。
   *
   * Callers:
   *   - masonry.js:update / append / _reflow
   */
  function _zeroHeights() {
    var heights = [];
    for (var i = 0; i < _columns; i++) heights.push(0);
    return heights;
  }

  /**
   * 返回当前最矮列的下标。
   *
   * Args:
   *   heights (Array<number>): 各列已占用高度。
   *
   * Returns:
   *   number: 列下标。
   *
   * Callers:
   *   - masonry.js:update / append / _reflow
   */
  function _shortest(heights) {
    var index = 0;
    for (var i = 1; i < heights.length; i++) {
      if (heights[i] < heights[index]) index = i;
    }
    return index;
  }

  /**
   * 构建卡片：有构建器时使用页面实现，否则使用默认卡片。
   *
   * Args:
   *   item (object): 条目。
   *   card (function(object): HTMLElement): 可选卡片构建器。
   *
   * Returns:
   *   HTMLElement: 卡片元素。
   *
   * Callers:
   *   - masonry.js:update / append
   */
  function _makeCard(item, card) {
    var el = card ? card(item) : _defaultCard(item);
    if (_opts.decorate) _opts.decorate(el, item);
    return el;
  }

  /**
   * 默认卡片：缩略图、名称与选中标记。图片以原始宽高预留空间。
   *
   * Args:
   *   item (object): 条目，含 path / stem / width / height / thumbnail_hash。
   *
   * Returns:
   *   HTMLElement: 卡片元素。
   *
   * Callers:
   *   - masonry.js:_makeCard
   */
  function _defaultCard(item) {
    var card = document.createElement('div');
    card.className = 'card';
    var selected = _opts.isSelected ? _opts.isSelected(item) : !!item.selected;
    if (selected) card.classList.add('selected');
    card.dataset.path = item.path;
    card.title = item.path;

    var hasSize = item.width > 0 && item.height > 0;
    card.dataset.ratio = String(hasSize ? item.height / item.width : 1);

    var img = document.createElement('img');
    img.alt = item.stem || '';
    img.loading = 'lazy';
    img.decoding = 'async';
    if (hasSize) {
      img.width = item.width;
      img.height = item.height;
    } else {
      img.style.aspectRatio = '1 / 1';
    }
    img.src = _opts.thumb ? _opts.thumb(item) : _thumbUrl(item);
    card.appendChild(img);

    var info = document.createElement('div');
    info.className = 'info';
    var name = document.createElement('span');
    name.className = 'name';
    name.textContent = item.stem || item.path;
    info.appendChild(name);
    card.appendChild(info);

    var overlay = document.createElement('div');
    overlay.className = 'overlay';
    overlay.textContent = '\u2713';
    card.appendChild(overlay);
    return card;
  }

  /**
   * 默认缩略图地址。
   *
   * Args:
   *   item (object): 条目。
   *
   * Returns:
   *   string: 图片地址。
   *
   * Callers:
   *   - masonry.js:_defaultCard
   */
  function _thumbUrl(item) {
    return item.thumbnail_hash ? API.thumbnailUrl(item.thumbnail_hash) : API.imageUrl(item.path);
  }

  /**
   * 记录视口顶部第一张可见卡片及其位置。
   *
   * Returns:
   *   ?{card: HTMLElement, top: number}: 锚点；无卡片时为 null。
   *
   * Callers:
   *   - masonry.js:append / _reflow / _restoreAnchor
   */
  function _viewportAnchor() {
    if (!_root) return null;
    var cards = _root.querySelectorAll('.card');
    for (var i = 0; i < cards.length; i++) {
      var rect = cards[i].getBoundingClientRect();
      if (rect.bottom > 0) return { card: cards[i], top: rect.top };
    }
    return null;
  }

  /**
   * 将锚点卡片恢复到记录时的视口位置。
   *
   * Args:
   *   anchor (?{card: HTMLElement, top: number}): 锚点。
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - masonry.js:append / _reflow
   */
  function _restoreAnchor(anchor) {
    if (!anchor || !anchor.card.isConnected) return;
    var shift = anchor.card.getBoundingClientRect().top - anchor.top;
    if (shift) window.scrollBy(0, shift);
  }

  return {
    mount: mount,
    unmount: unmount,
    update: update,
    append: append,
    setColumns: setColumns,
    setSelected: setSelected,
    empty: empty,
    jumpTo: jumpTo,
  };
})();
