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

  function init(containerId) {
    _container = document.getElementById(containerId);
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
    var cw = Math.max(100, (_container.clientWidth - pad * 2 - gap * (_columns - 1)) / _columns);
    var heights = new Array(_columns).fill(0);

    for (var i = 0; i < images.length; i++) {
      var img = images[i];
      var card = _createCard(img, cw);
      var ih = cw * (img.height || 1) / (img.width || 1) + 22; // 22px for info bar
      var col = _shortestCol(heights);
      card.style.position = 'absolute';
      card.style.left = (col * (cw + gap)) + 'px';
      card.style.top = heights[col] + 'px';
      card.style.width = cw + 'px';
      heights[col] += ih + gap;
      _container.appendChild(card);
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
    card.addEventListener('click', function() { Store.toggle(img.path); });
    if (typeof SimilarHover !== 'undefined') SimilarHover.bindCard(card, img.path);

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
    var score = (typeof img.aesthetic === 'number')
      ? (img.aesthetic.toFixed(2) + ' ')
      : '';
    name.textContent = score + res + img.stem;
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
    if (_container && _container.children.length > 0) {
      // re-render from Store
      var showTrash = document.getElementById('chk-show-trash');
      var visible = Store.getVisible(showTrash ? showTrash.checked : false);
      render(visible);
    }
  }

  return {
    init: init, render: render, setSelected: setSelected,
    setColumns: setColumns, jumpTo: jumpTo,
  };
})();
