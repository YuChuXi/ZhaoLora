/**
 * similar_hover.js —— 悬停卡片旁显示色调相似图瀑布流，点击跳转
 */
var SimilarHover = (function () {
  'use strict';

  var HOVER_DELAY = 900;
  var LIMIT = 12;
  var GAP = 8;
  var PAD = 10;
  var INFO_H = 22;

  var _panel = null;
  var _anchorPath = '';
  var _anchorCard = null;
  var _hoverTimer = null;
  var _cache = {};
  var _reqSeq = 0;
  var _catalogSig = '';

  /**
   * 创建并返回悬浮面板 DOM（单例）。
   * @returns {HTMLElement}
   */
  function ensurePanel() {
    if (_panel) return _panel;
    _panel = document.createElement('div');
    _panel.className = 'similar-float';
    _panel.innerHTML =
      '<div class="similar-float-head">' +
        '<span>色调相似</span>' +
        '<button type="button" class="similar-float-close" id="similar-float-close">×</button>' +
      '</div>' +
      '<div class="similar-float-body" id="similar-float-body"></div>';
    document.body.appendChild(_panel);

    document.getElementById('similar-float-close').addEventListener('click', function (e) {
      e.stopPropagation();
      hide();
    });
    _panel.addEventListener('mouseleave', onLeavePanel);
    return _panel;
  }

  /**
   * 判断事件目标是否仍在锚定卡片或悬浮面板内。
   * @param {EventTarget|null} target
   * @returns {boolean}
   */
  function isInsideHoverZone(target) {
    if (!target || target.nodeType !== 1) return false;
    if (_panel && _panel.contains(target)) return true;
    if (_anchorCard && _anchorCard.contains(target)) return true;
    return false;
  }

  /**
   * 鼠标离开主列表卡片：若未进入悬浮面板则立即关闭。
   * @param {MouseEvent} e
   */
  function onLeaveCard(e) {
    clearTimeout(_hoverTimer);
    if (isInsideHoverZone(e.relatedTarget)) return;
    hide();
  }

  /**
   * 鼠标离开悬浮面板：若未回到锚定卡片则立即关闭。
   * @param {MouseEvent} e
   */
  function onLeavePanel(e) {
    clearTimeout(_hoverTimer);
    if (isInsideHoverZone(e.relatedTarget)) return;
    hide();
  }

  /**
   * 关闭悬浮面板并清除锚定状态。
   */
  function hide() {
    clearTimeout(_hoverTimer);
    _hoverTimer = null;
    _anchorPath = '';
    _anchorCard = null;
    if (_panel) _panel.classList.remove('show');
  }

  /**
   * 按视口计算面板宽度、最大高度与列数。
   * @returns {{width: number, maxH: number, cols: number}}
   */
  function panelMetrics() {
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var width = Math.min(560, Math.max(360, Math.floor(vw * 0.38)));
    var maxH = Math.min(640, Math.floor(vh * 0.78));
    var cols = width >= 480 ? 3 : 2;
    return { width: width, maxH: maxH, cols: cols };
  }

  /**
   * 将面板贴靠到卡片边缘（间距为 0，便于 mouseleave.relatedTarget 正确进入面板）。
   * @param {HTMLElement} card
   */
  function positionNear(card) {
    ensurePanel();
    var m = panelMetrics();
    var rect = card.getBoundingClientRect();
    var pw = m.width;
    var ph = Math.min(m.maxH, _panel.offsetHeight || 420);
    var left = rect.right;
    var top = rect.top;

    if (left + pw > window.innerWidth - 10) {
      left = rect.left - pw;
    }
    if (left < 10) {
      left = Math.max(10, Math.round((window.innerWidth - pw) / 2));
      top = rect.bottom;
    }
    if (top + ph > window.innerHeight - 10) {
      top = Math.max(10, window.innerHeight - ph - 10);
    }
    if (top < 10) top = 10;

    _panel.style.width = pw + 'px';
    _panel.style.maxHeight = m.maxH + 'px';
    _panel.style.left = Math.round(left) + 'px';
    _panel.style.top = Math.round(top) + 'px';
  }

  /**
   * @param {string} path
   * @returns {boolean}
   */
  function isSelected(path) {
    var all = Store.getAll();
    for (var i = 0; i < all.length; i++) {
      if (all[i].path === path) return !!all[i].selected;
    }
    return false;
  }

  /**
   * @param {string} path
   * @returns {boolean}
   */
  function isAlive(path) {
    var all = Store.getAll();
    for (var i = 0; i < all.length; i++) {
      if (all[i].path === path) return !all[i].trashed;
    }
    return false;
  }

  /**
   * 过滤已删除项。
   * @param {Array<{path: string}>} list
   * @returns {Array<{path: string}>}
   */
  function filterAlive(list) {
    var out = [];
    for (var i = 0; i < list.length; i++) {
      if (isAlive(list[i].path)) out.push(list[i]);
    }
    return out;
  }

  /**
   * 同步悬浮面板内卡片的选中态。
   */
  function syncSelectedInPanel() {
    if (!_panel || !_panel.classList.contains('show')) return;
    var cards = _panel.querySelectorAll('.similar-float-card');
    for (var i = 0; i < cards.length; i++) {
      var p = cards[i].dataset.path;
      if (isSelected(p)) cards[i].classList.add('selected');
      else cards[i].classList.remove('selected');
    }
  }

  /**
   * 在面板内按瀑布流排布相似图列表。
   * @param {Array<object>} list
   */
  function layoutMasonry(list) {
    var body = document.getElementById('similar-float-body');
    var m = panelMetrics();
    list = filterAlive(list);

    if (!list.length) {
      body.innerHTML = '<div class="similar-float-empty">暂无相似图</div>';
      body.style.height = '80px';
      return;
    }

    body.innerHTML = '';
    var cols = m.cols;
    var innerW = m.width - 20;
    var cw = (innerW - PAD * 2 - GAP * (cols - 1)) / cols;
    var heights = [];
    for (var c0 = 0; c0 < cols; c0++) heights.push(0);

    for (var i = 0; i < list.length; i++) {
      (function (item) {
        var iw = item.width > 0 ? item.width : 1;
        var ih0 = item.height > 0 ? item.height : 1;
        var imgH = cw * ih0 / iw;
        var cardH = imgH + INFO_H;
        var col = 0;
        for (var c = 1; c < cols; c++) {
          if (heights[c] < heights[col]) col = c;
        }

        var card = document.createElement('div');
        card.className = 'similar-float-card' + (isSelected(item.path) ? ' selected' : '');
        card.dataset.path = item.path;
        card.title = item.path;
        card.style.left = (PAD + col * (cw + GAP)) + 'px';
        card.style.top = (PAD + heights[col]) + 'px';
        card.style.width = cw + 'px';

        var im = document.createElement('img');
        im.src = API.thumbnailUrl(item.thumbnail_hash);
        im.alt = item.stem || '';
        im.loading = 'lazy';

        var info = document.createElement('div');
        info.className = 'similar-float-info';
        var res = (item.width && item.height) ? (item.width + '×' + item.height + ' ') : '';
        info.textContent = res + (item.stem || item.path);

        var mark = document.createElement('div');
        mark.className = 'similar-float-mark';
        mark.textContent = '\u2713';

        card.appendChild(im);
        card.appendChild(info);
        card.appendChild(mark);
        card.addEventListener('click', function (e) {
          e.preventDefault();
          e.stopPropagation();
          hide();
          Masonry.jumpTo(item.path);
        });
        body.appendChild(card);
        heights[col] += cardH + GAP;
      })(list[i]);
    }

    var totalH = Math.max.apply(null, heights) + PAD * 2;
    var viewH = Math.min(totalH, m.maxH - 44);
    body.style.height = Math.max(120, viewH) + 'px';
  }

  /**
   * 为指定卡片拉取并展示相似图。
   * @param {string} path
   * @param {HTMLElement} card
   * @returns {Promise<void>}
   */
  async function showFor(path, card) {
    ensurePanel();
    _anchorPath = path;
    _anchorCard = card;
    var body = document.getElementById('similar-float-body');
    body.innerHTML = '<div class="similar-float-empty">加载中…</div>';
    body.style.height = '80px';
    _panel.classList.add('show');
    positionNear(card);

    var seq = ++_reqSeq;
    var list = _cache[path];
    if (!list) {
      var data = await API.similarImages(path, LIMIT);
      list = filterAlive(data.similar || []);
      _cache[path] = list;
    } else {
      list = filterAlive(list);
    }
    if (_anchorPath !== path || seq !== _reqSeq) return;

    layoutMasonry(list);
    positionNear(card);
  }

  /**
   * 绑定主列表卡片的悬停显示逻辑。
   * @param {HTMLElement} card
   * @param {string} path
   */
  function bindCard(card, path) {
    card.addEventListener('mouseenter', function () {
      clearTimeout(_hoverTimer);
      if (_anchorPath && _anchorPath !== path) hide();
      _hoverTimer = setTimeout(function () {
        showFor(path, card).catch(function (err) {
          if (_anchorPath === path) hide();
          console.error(err);
        });
      }, HOVER_DELAY);
    });
    card.addEventListener('mouseleave', onLeaveCard);
  }

  /**
   * 目录存活签名（未删除数:已删除数），用于判断是否需要清缓存。
   * @returns {string}
   */
  function catalogSig() {
    var live = 0;
    var trash = 0;
    var all = Store.getAll();
    for (var i = 0; i < all.length; i++) {
      if (all[i].trashed) trash++;
      else live++;
    }
    return live + ':' + trash;
  }

  /**
   * 在主列表中查找仍存在的卡片 DOM。
   * @param {string} path
   * @returns {HTMLElement|null}
   */
  function findMainCard(path) {
    var root = document.getElementById('masonry');
    if (!root) return null;
    var cards = root.querySelectorAll('.card');
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].dataset.path === path) return cards[i];
    }
    return null;
  }

  /**
   * Store 变更：同步选中态；目录变化时清缓存并刷新当前浮层。
   */
  function onStoreChange() {
    syncSelectedInPanel();
    var sig = catalogSig();
    if (sig === _catalogSig) return;
    _catalogSig = sig;
    _cache = {};
    if (!_panel || !_panel.classList.contains('show') || !_anchorPath) return;

    var keepPath = _anchorPath;
    queueMicrotask(function () {
      var still = null;
      var all = Store.getAll();
      for (var i = 0; i < all.length; i++) {
        if (all[i].path === keepPath && !all[i].trashed) {
          still = all[i];
          break;
        }
      }
      if (!still) {
        hide();
        return;
      }
      var card = findMainCard(keepPath);
      if (!card) {
        hide();
        return;
      }
      showFor(keepPath, card).catch(function (err) {
        hide();
        console.error(err);
      });
    });
  }

  window.addEventListener('scroll', function (e) {
    if (!_panel || !_panel.classList.contains('show')) return;
    if (_panel.contains(e.target)) return;
    hide();
  }, true);

  window.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') hide();
  });

  window.addEventListener('resize', function () {
    if (_panel && _panel.classList.contains('show')) hide();
  });

  _catalogSig = catalogSig();
  Store.onToggle(function () { syncSelectedInPanel(); });
  Store.onChange(onStoreChange);

  return {
    bindCard: bindCard,
    hide: hide,
    clearCache: function () { _cache = {}; },
  };
})();
