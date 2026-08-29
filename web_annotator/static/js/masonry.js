/**
 * masonry.js -- JS 绝对定位瀑布流，横排不乱 + 无空隙
 * 离屏渲染跳过与图片懒加载全部交给浏览器自动管理:
 * 卡片逐张设 content-visibility:auto 与精确占位尺寸, 浏览器自动跳过离屏渲染;
 * 缩略图用原生 loading=lazy, 浏览器自动按视口距离决定加载与解码时机。
 * 列高以探针测得的固定附加高度纯计算得出, 不逐卡强制回流。
 */
var Masonry = (function() {
  'use strict';

  var _container = null;
  var _columns = 5;

  // 可选配置: 非筛选页可通过 init 第二参数注入自定义数据源与交互
  var _config = {
    getData: null,   // function() → 图片数组; 用于 setColumns 触发的重绘
    onClick: null,   // function(img, card, event); 覆盖默认 Store.toggle
    decorate: null,  // function(card, img); 创建卡片后追加自定义样式/属性
    imageSrc: null,  // function(img) → 缩略图 URL; 省略则用 API.thumbnailUrl
  };

  var _index = {};          // path → {img, x, y, w, h}
  var _extraH = 0;          // 卡片中图片占位之外的固定高度(info 区 + 边框)
  var _lastClickedPath = null;

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
      _config.imageSrc = config.imageSrc || null;
    }
  }

  function setColumns(n) { _columns = n; renderAll(); }

  /**
   * 图片的占位高度.
   * @param {Object} img - 图片元数据, 含 width/height
   * @param {number} colW - 列宽
   * @returns {number} 按列宽等比缩放的占位高度
   */
  function _placeholderH(img, colW) {
    return Math.max(1, Math.round(colW * (img.height || 1) / (img.width || 1)));
  }

  /**
   * 用探针卡片测量图片占位之外的固定附加高度(info 区 + 边框).
   * @param {Object} img - 探针使用的图片元数据
   * @param {number} colW - 列宽
   * @returns {number} 卡片总高与图片占位高之差
   */
  function _measureExtra(img, colW) {
    var probe = _createCard(img, colW);
    // 强制渲染探针: content-visibility:auto 的离屏元素以占位尺寸代替真实内容尺寸
    probe.style.contentVisibility = 'visible';
    _container.appendChild(probe);
    var extra = probe.offsetHeight - _placeholderH(img, colW);
    _container.removeChild(probe);
    return extra;
  }

  function render(images) {
    if (!_container) return;
    _container.innerHTML = '';
    _index = {};
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

    _extraH = _measureExtra(images[0], cw);
    var heights = new Array(cols).fill(0);

    for (var i = 0; i < images.length; i++) {
      var img = images[i];
      var card = _createCard(img, cw);
      var col = _shortestCol(heights);
      var x = pad + col * (cw + gap);
      var y = heights[col];
      var h = _placeholderH(img, cw) + _extraH;
      card.style.position = 'absolute';
      card.style.left = x + 'px';
      card.style.top = y + 'px';
      card.style.width = cw + 'px';
      // 离屏卡片跳过渲染; 占位尺寸用精确估算值, 避免滚动条跳动
      card.style.contentVisibility = 'auto';
      card.style.containIntrinsicSize = 'auto ' + h + 'px';
      _container.appendChild(card);
      _index[img.path] = { img: img, x: x, y: y, w: cw, h: h };
      heights[col] += h + gap;
    }
    _container.style.height = Math.max.apply(null, heights) + 'px';
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
        var itA = _index[_lastClickedPath];
        var itB = _index[img.path];
        if (itA && itB && itA !== itB) {
          // 布局坐标下与 [yLo, yHi] 有交集的卡片即选中
          var yLo = Math.min(itA.y, itB.y);
          var yHi = Math.max(itA.y + itA.h, itB.y + itB.h);
          var paths = [];
          for (var key in _index) {
            var it = _index[key];
            if (it.y + it.h > yLo && it.y < yHi) paths.push(key);
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

    var imgEl = document.createElement('img');
    imgEl.alt = img.stem;
    imgEl.loading = 'lazy';
    imgEl.src = _config.imageSrc ? _config.imageSrc(img) : API.thumbnailUrl(img.thumbnail_hash);
    imgEl.dataset.full = API.imageUrl(img.path);
    // 占位高度，避免未加载时卡片塌缩导致整页被当成「可见」
    imgEl.style.height = _placeholderH(img, colW) + 'px';
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

    if (_config.decorate) _config.decorate(card, img);

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

  /** 滚动到指定图片并闪烁 */
  function jumpTo(path) {
    var card = _findCard(path);
    if (!card) return false;
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
