/**
 * masonry.js -- JS 绝对定位瀑布流，横排不乱 + 无空隙
 * 缩略图用 IntersectionObserver 懒加载（原生 loading=lazy 在快网下预取范围过大）
 */
var Masonry = (function() {
  'use strict';

  var _container = null;
  var _columns = 5;
  var _io = null;
  var _LAZY_MARGIN = '400px 0px';

  // 可选配置: 非筛选页可通过 init 第二参数注入自定义数据源与交互
  var _config = {
    getData: null,   // function() → 图片数组; 用于 setColumns 触发的重绘
    onClick: null,   // function(img, card, event); 覆盖默认 Store.toggle
    decorate: null,  // function(card, img); 创建卡片后追加自定义样式/属性
  };

  /**
   * 初始化瀑布流容器.
   * @param {string} containerId
   * @param {{getData?: function, onClick?: function, decorate?: function}} [config]
   *   - getData: setColumns 重绘时的数据源; 省略则用 Store.getVisible
   *   - onClick: 卡片点击回调; 省略则调用 Store.toggle
   *   - decorate: 卡片创建后追加样式/属性的回调
   */
  function init(containerId, config) {
    _container = document.getElementById(containerId);
    if (config) {
      _config.getData = config.getData || null;
      _config.onClick = config.onClick || null;
      _config.decorate = config.decorate || null;
    }
  }

  function setColumns(n) { _columns = n; renderAll(); }

  function _stopLazy() {
    if (_io) {
      _io.disconnect();
      _io = null;
    }
  }

  function _loadImg(imgEl) {
    var url = imgEl.dataset.src;
    if (!url) return;
    imgEl.src = url;
    delete imgEl.dataset.src;
    if (_io) _io.unobserve(imgEl);
  }

  function _startLazy() {
    _stopLazy();
    if (!_container) return;
    _io = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (!entries[i].isIntersecting) continue;
        _loadImg(entries[i].target);
      }
    }, { root: null, rootMargin: _LAZY_MARGIN, threshold: 0 });

    var imgs = _container.querySelectorAll('img[data-src]');
    for (var j = 0; j < imgs.length; j++) _io.observe(imgs[j]);
  }

  var _lastClickedPath = null;

  function render(images) {
    if (!_container) return;
    _stopLazy();
    _container.innerHTML = '';
    if (images.length === 0) {
      _container.innerHTML =
        '<div style="padding:60px;text-align:center;color:var(--text-dim);">没有图片</div>';
      return;
    }

    var gap = 8, pad = 8;
    var cols = Math.max(1, _columns);
    var avail = Math.max(0, _container.clientWidth - pad * 2);
    var cw = (avail - gap * (cols - 1)) / cols;
    if (!(cw > 0)) cw = 1;
    var heights = new Array(cols).fill(0);

    for (var i = 0; i < images.length; i++) {
      var img = images[i];
      var card = _createCard(img, cw);
      var col = _shortestCol(heights);
      card.style.position = 'absolute';
      card.style.left = (pad + col * (cw + gap)) + 'px';
      card.style.width = cw + 'px';
      _container.appendChild(card);
      // 用真实渲染高度累加列高, 而非预估, 保证竖直空隙恒定 = gap
      var rh = card.offsetHeight;
      card.style.top = heights[col] + 'px';
      heights[col] += rh + gap;
    }
    _container.style.height = Math.max.apply(null, heights) + 'px';
    _startLazy();
  }

  function _shortestCol(h) {
    var min = 0;
    for (var i = 1; i < h.length; i++) { if (h[i] < h[min]) min = i; }
    return min;
  }

  function _createCard(img, colW) {
    var card = document.createElement('div');
    card.className = 'card';
    if (img.selected) card.classList.add('selected');
    if (img.trashed)  card.classList.add('trashed');
    card.dataset.path = img.path;
    card.addEventListener('click', function(e) {
      if (_config.onClick) {
        _config.onClick(img, card, e);
        _lastClickedPath = img.path;
        return;
      }
      if (e.shiftKey && _lastClickedPath) {
        var lastCard = _container.querySelector('.card[data-path="' + _lastClickedPath.replace(/"/g, '\\"') + '"]');
        if (lastCard && card !== lastCard) {
          var yA = parseFloat(lastCard.style.top) || 0;
          var hA = lastCard.offsetHeight || 0;
          var yB = parseFloat(card.style.top) || 0;
          var hB = card.offsetHeight || 0;
          var yLo = Math.min(yA, yB);
          var yHi = Math.max(yA + hA, yB + hB);
          var allCards = _container.querySelectorAll('.card');
          var paths = [];
          for (var ci = 0; ci < allCards.length; ci++) {
            var c = allCards[ci];
            var cy = parseFloat(c.style.top) || 0;
            var ch = c.offsetHeight || 0;
            // 卡片与 [yLo, yHi] 有交集即选中
            if (cy + ch > yLo && cy < yHi) paths.push(c.dataset.path);
          }
          if (paths.length) Store.selectRange(paths);
        } else {
          Store.toggle(img.path);
        }
      } else {
        Store.toggle(img.path);
      }
      _lastClickedPath = img.path;
    });
    if (typeof SimilarHover !== 'undefined') SimilarHover.bindCard(card, img.path);
    if (_config.decorate) _config.decorate(card, img);

    var imgEl = document.createElement('img');
    imgEl.alt = img.stem;
    imgEl.dataset.src = API.thumbnailUrl(img.thumbnail_hash);
    imgEl.dataset.full = API.imageUrl(img.path);
    // 占位高度，避免未加载时卡片塌缩导致整页被当成「可见」
    var ph = Math.max(1, Math.round(colW * (img.height || 1) / (img.width || 1)));
    imgEl.style.height = ph + 'px';
    imgEl.addEventListener('load', function () {
      imgEl.style.height = '';
    });
    imgEl.addEventListener('error', function () {
      if (imgEl.dataset.fallback) return;
      imgEl.dataset.fallback = '1';
      imgEl.src = imgEl.dataset.full || API.imageUrl(img.path);
    });
    card.appendChild(imgEl);

    var info = document.createElement('div');
    info.className = 'info';
    var name = document.createElement('span');
    name.className = 'name';
    var res = (img.width && img.height) ? (img.width + '×' + img.height + ' ') : '';
    var aes = (typeof img.aesthetic === 'number')
      ? ('A' + img.aesthetic.toFixed(2) + ' ')
      : '';
    var qual = (typeof img.quality === 'number')
      ? ('Q' + img.quality.toFixed(2) + ' ')
      : '';
    name.textContent = aes + qual + res + img.stem;
    info.appendChild(name);
    card.appendChild(info);

    var overlay = document.createElement('div');
    overlay.className = 'overlay';
    overlay.textContent = '\u2713';
    card.appendChild(overlay);

    return card;
  }

  /** 增量更新选中态 */
  function setSelected(path, selected) {
    if (!_container) return;
    var card = _findCard(path);
    if (!card) return;
    if (selected) card.classList.add('selected');
    else card.classList.remove('selected');
  }

  function _findCard(path) {
    if (!_container) return null;
    var cards = _container.querySelectorAll('.card');
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].dataset.path === path) return cards[i];
    }
    return null;
  }

  function jumpTo(path) {
    var card = _findCard(path);
    if (!card) return false;
    var imgEl = card.querySelector('img[data-src]');
    if (imgEl) _loadImg(imgEl);
    card.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
    card.classList.add('jump-flash');
    setTimeout(function () { card.classList.remove('jump-flash'); }, 1200);
    return true;
  }

  function renderAll() {
    if (!_container || _container.children.length === 0) return;
    if (_config.getData) {
      render(_config.getData());
      return;
    }
    // 默认: 从 Store 取可见列表
    var showTrash = document.getElementById('chk-show-trash');
    var visible = Store.getVisible(showTrash ? showTrash.checked : false);
    render(visible);
  }

  return {
    init: init, render: render, setSelected: setSelected,
    setColumns: setColumns, jumpTo: jumpTo,
  };
})();
