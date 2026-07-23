/**
 * masonry.js -- JS 绝对定位瀑布流，横排不乱 + 无空隙
 */
var Masonry = (function() {
  'use strict';

  var _container = null;
  var _columns = 5;

  function init(containerId) {
    _container = document.getElementById(containerId);
  }

  function setColumns(n) { _columns = n; renderAll(); }

  function render(images) {
    if (!_container) return;
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
      var card = _createCard(img);
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
  }

  function _shortestCol(h) {
    var min = 0;
    for (var i = 1; i < h.length; i++) { if (h[i] < h[min]) min = i; }
    return min;
  }

  function _createCard(img) {
    var card = document.createElement('div');
    card.className = 'card';
    if (img.selected) card.classList.add('selected');
    if (img.trashed)  card.classList.add('trashed');
    card.dataset.path = img.path;
    card.addEventListener('click', function() { Store.toggle(img.path); });
    if (typeof SimilarHover !== 'undefined') SimilarHover.bindCard(card, img.path);

    var imgEl = document.createElement('img');
    imgEl.src = API.thumbnailUrl(img.thumbnail_hash);
    imgEl.loading = 'lazy';
    imgEl.alt = img.stem;
    imgEl.addEventListener('error', function () {
      if (imgEl.dataset.fallback) return;
      imgEl.dataset.fallback = '1';
      imgEl.src = API.imageUrl(img.path);
    });
    card.appendChild(imgEl);

    var info = document.createElement('div');
    info.className = 'info';
    var name = document.createElement('span');
    name.className = 'name';
    var res = (img.width && img.height) ? (img.width + '×' + img.height + ' ') : '';
    name.textContent = res + img.stem;
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
