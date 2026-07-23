/**
 * similar_hover.js —— 悬停后在卡片旁浮出瀑布流，点击跳转
 */
var SimilarHover = (function () {
  'use strict';

  var HOVER_DELAY = 900;
  var HIDE_DELAY = 80; // 仅留给「卡片→浮层」的缝隙，离开后尽快关
  var LIMIT = 12;
  var GAP = 8;
  var PAD = 10;
  var INFO_H = 22;

  var _panel = null;
  var _anchorPath = '';
  var _anchorCard = null;
  var _hoverTimer = null;
  var _hideTimer = null;
  var _cache = {};
  var _reqSeq = 0;

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
    _panel.addEventListener('mouseenter', function () {
      clearTimeout(_hideTimer);
    });
    _panel.addEventListener('mouseleave', onLeavePanel);
    return _panel;
  }

  function scheduleHide() {
    clearTimeout(_hideTimer);
    _hideTimer = setTimeout(hide, HIDE_DELAY);
  }

  function onLeaveCard() {
    clearTimeout(_hoverTimer);
    // 浮层已开：短延迟方便移入；未开：立刻取消待显示
    if (_panel && _panel.classList.contains('show')) {
      scheduleHide();
      return;
    }
    hide();
  }

  function onLeavePanel(e) {
    clearTimeout(_hoverTimer);
    if (_anchorCard && e.relatedTarget && _anchorCard.contains(e.relatedTarget)) {
      scheduleHide();
      return;
    }
    hide();
  }

  function hide() {
    clearTimeout(_hoverTimer);
    clearTimeout(_hideTimer);
    _anchorPath = '';
    _anchorCard = null;
    if (_panel) _panel.classList.remove('show');
  }

  function panelMetrics() {
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    // 悬浮面板：约半屏宽，高度可滚
    var width = Math.min(560, Math.max(360, Math.floor(vw * 0.38)));
    var maxH = Math.min(640, Math.floor(vh * 0.78));
    var cols = width >= 480 ? 3 : 2;
    return { width: width, maxH: maxH, cols: cols };
  }

  function positionNear(card) {
    ensurePanel();
    var m = panelMetrics();
    var rect = card.getBoundingClientRect();
    var pw = m.width;
    var ph = Math.min(m.maxH, _panel.offsetHeight || 420);
    var gap = 12;
    var left = rect.right + gap;
    var top = rect.top;

    if (left + pw > window.innerWidth - 10) {
      left = rect.left - pw - gap;
    }
    if (left < 10) {
      left = Math.max(10, Math.round((window.innerWidth - pw) / 2));
      top = rect.bottom + gap;
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

  function isSelected(path) {
    var all = Store.getAll();
    for (var i = 0; i < all.length; i++) {
      if (all[i].path === path) return !!all[i].selected;
    }
    return false;
  }

  function isAlive(path) {
    var all = Store.getAll();
    for (var i = 0; i < all.length; i++) {
      if (all[i].path === path) return !all[i].trashed;
    }
    return false;
  }

  function filterAlive(list) {
    var out = [];
    for (var i = 0; i < list.length; i++) {
      if (isAlive(list[i].path)) out.push(list[i]);
    }
    return out;
  }

  function syncSelectedInPanel() {
    if (!_panel || !_panel.classList.contains('show')) return;
    var cards = _panel.querySelectorAll('.similar-float-card');
    for (var i = 0; i < cards.length; i++) {
      var p = cards[i].dataset.path;
      if (isSelected(p)) cards[i].classList.add('selected');
      else cards[i].classList.remove('selected');
    }
  }

  function layoutMasonry(list) {
    var body = document.getElementById('similar-float-body');
    var m = panelMetrics();
    body.innerHTML = '';

    if (!list.length) {
      body.innerHTML = '<div class="similar-float-empty">暂无相似图</div>';
      body.style.height = '80px';
      return;
    }

    list = filterAlive(list);
    if (!list.length) {
      body.innerHTML = '<div class="similar-float-empty">暂无相似图</div>';
      body.style.height = '80px';
      return;
    }

    var cols = m.cols;
    var innerW = m.width - 20; // panel padding
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
      try {
        var data = await API.similarImages(path, LIMIT);
        list = filterAlive(data.similar || []);
        _cache[path] = list;
      } catch (e) {
        if (_anchorPath === path && seq === _reqSeq) {
          body.innerHTML = '<div class="similar-float-empty">加载失败</div>';
        }
        return;
      }
    } else {
      list = filterAlive(list);
    }
    if (_anchorPath !== path || seq !== _reqSeq) return;

    layoutMasonry(list);
    positionNear(card);
    requestAnimationFrame(function () {
      if (_anchorPath !== path || seq !== _reqSeq) return;
      layoutMasonry(list);
      positionNear(card);
    });
  }

  function bindCard(card, path) {
    card.addEventListener('mouseenter', function () {
      clearTimeout(_hideTimer);
      clearTimeout(_hoverTimer);
      _hoverTimer = setTimeout(function () {
        showFor(path, card);
      }, HOVER_DELAY);
    });
    card.addEventListener('mouseleave', onLeaveCard);
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

  // 主列表变化：同步勾选；目录变化（删除/恢复）清缓存并刷新浮层
  if (typeof Store !== 'undefined') {
    var _catalogSig = '';
    function catalogSig() {
      var live = 0, trash = 0;
      var all = Store.getAll();
      for (var i = 0; i < all.length; i++) {
        if (all[i].trashed) trash++;
        else live++;
      }
      return live + ':' + trash;
    }
    function onStoreChange() {
      syncSelectedInPanel();
      var sig = catalogSig();
      if (sig === _catalogSig) return;
      _catalogSig = sig;
      _cache = {};
      if (!_panel || !_panel.classList.contains('show') || !_anchorPath) return;
      var keepPath = _anchorPath;
      // 等主列表 re-render 完成后再刷新浮层
      setTimeout(function () {
        var still = null;
        var all = Store.getAll();
        for (var i = 0; i < all.length; i++) {
          if (all[i].path === keepPath && !all[i].trashed) { still = all[i]; break; }
        }
        if (!still) { hide(); return; }
        var card = null;
        var root = document.getElementById('masonry');
        if (root) {
          var cards = root.querySelectorAll('.card');
          for (var j = 0; j < cards.length; j++) {
            if (cards[j].dataset.path === keepPath) { card = cards[j]; break; }
          }
        }
        if (!card) { hide(); return; }
        showFor(keepPath, card);
      }, 0);
    }
    _catalogSig = catalogSig();
    Store.onToggle(function () { syncSelectedInPanel(); });
    Store.onChange(onStoreChange);
  }

  return {
    bindCard: bindCard,
    hide: hide,
    clearCache: function () { _cache = {}; },
  };
})();
