/**
 * booru_filter.js —— 筛选页挂载 Gelbooru / Danbooru
 * 搜索 tag → 瀑布流缩略图；选中即下载到当前目录（g{id}.png / d{id}.png）
 */
var BooruFilter = (function () {
  'use strict';

  var STATE_KEY = 'zhao_booru_filter_v1';
  var PROXY_KEY = 'zhao_booru_proxy_media';

  var _posts = [];
  var _page = 1;
  var _pageLo = 1;
  var _pageHi = 1;
  var _source = 'gelbooru';
  var _loading = false;
  var _hasMore = false;
  var _hasPrev = false;
  var _dupSkip = 0;
  var _selected = {};
  var _status = {};
  var _io = null;
  var _pageIo = null;
  var _toastTimer = null;
  var _colLo = null;
  var _colHi = null;
  var _minY = 0;
  var _cw = 0;
  var _gap = 8;
  var _pad = 8;
  var _pendingScrollY = null;
  var _scrollSaveTimer = null;
  var _scrollRaf = null;
  var _EX_KEYS = ['score', 'width', 'height', 'id'];

  function toast(msg) {
    var el = document.getElementById('toast');
    if (!el) return;
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () { el.classList.remove('show'); }, 2200);
  }

  function destDir() {
    if (typeof DirPicker !== 'undefined' && DirPicker.getDirs) {
      var ds = DirPicker.getDirs();
      if (ds.length) return ds[0];
    }
    return 'main';
  }

  function mode() {
    var el = document.getElementById('filter-mode');
    return el ? el.value : 'local';
  }

  function isRemote() {
    return mode() === 'gelbooru' || mode() === 'danbooru';
  }

  function isProxyMedia() {
    var el = document.getElementById('booru-proxy');
    return !!(el && el.checked);
  }

  function loadProxyPref() {
    var el = document.getElementById('booru-proxy');
    if (!el) return;
    el.checked = localStorage.getItem(PROXY_KEY) === '1';
  }

  function saveProxyPref() {
    var el = document.getElementById('booru-proxy');
    if (!el) return;
    localStorage.setItem(PROXY_KEY, el.checked ? '1' : '0');
  }

  function proxyApiUrl(kind, post) {
    return API.apiPath('/booru/' + kind) +
      '?source=' + encodeURIComponent(post.source) +
      '&id=' + encodeURIComponent(post.id);
  }

  function thumbUrl(post) {
    if (isProxyMedia()) return proxyApiUrl('thumb', post);
    return post.preview_url || post.sample_url || proxyApiUrl('thumb', post);
  }

  function sampleUrl(post) {
    if (isProxyMedia()) return proxyApiUrl('sample', post);
    return post.sample_url || post.file_url || post.preview_url || proxyApiUrl('sample', post);
  }

  function saveUiState() {
    var payload = {
      mode: mode(),
      tags: (document.getElementById('booru-tags') || {}).value || '',
      extra: (document.getElementById('booru-extra') || {}).value || '',
      order: (document.getElementById('booru-order') || {}).value || '',
      startPage: startPageFromInput(),
      source: _source,
      page: _page,
      pageLo: _pageLo,
      pageHi: _pageHi,
      scrollY: isRemote() ? window.scrollY : 0,
    };
    try {
      sessionStorage.setItem(STATE_KEY, JSON.stringify(payload));
    } catch (e) { /* ignore quota */ }
  }

  function loadUiState() {
    try {
      var raw = sessionStorage.getItem(STATE_KEY);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  }

  function startPageFromInput() {
    var el = document.getElementById('booru-start-page');
    var n = el ? parseInt(el.value, 10) : 1;
    if (!n || n < 1) n = 1;
    if (n > 10000) n = 10000;
    if (el) el.value = String(n);
    return n;
  }

  function applyForm(st) {
    if (!st) return;
    var tags = document.getElementById('booru-tags');
    var extra = document.getElementById('booru-extra');
    var order = document.getElementById('booru-order');
    var start = document.getElementById('booru-start-page');
    if (tags && st.tags != null) tags.value = st.tags;
    if (extra && st.extra != null) extra.value = st.extra;
    if (order && st.order != null) order.value = st.order;
    var pg = parseInt(st.page != null ? st.page : st.startPage, 10);
    if (!(pg > 0)) pg = 1;
    if (start) start.value = String(pg);
    // 仅单页会话可还原绝对滚动；多页跨页高度刷新后对不上
    var lo = parseInt(st.pageLo, 10);
    var hi = parseInt(st.pageHi, 10);
    if (typeof st.scrollY === 'number' && st.scrollY > 0 && lo === pg && hi === pg) {
      _pendingScrollY = st.scrollY;
    } else {
      _pendingScrollY = null;
    }
    syncExtraBtn();
  }

  function resetLayout() {
    _colLo = null;
    _colHi = null;
    _minY = 0;
  }

  function countExtra(f) {
    var n = 0;
    if (f.rating) n += 1;
    for (var i = 0; i < _EX_KEYS.length; i++) {
      var k = _EX_KEYS[i];
      if (f[k + 'Lo'] || f[k + 'Hi']) n += 1;
    }
    if (f.dateLo || f.dateHi) n += 1;
    if (f.user) n += 1;
    var more = f.more;
    if (Array.isArray(more)) n += more.length;
    else if (more) n += String(more).split(/\s+/).filter(Boolean).length;
    return n;
  }

  function syncExtraBtn() {
    var btn = document.getElementById('btn-booru-extra');
    var el = document.getElementById('booru-extra');
    if (!btn || !el) return;
    var raw = el.value.trim();
    var n = countExtra(parseExtra(raw));
    btn.classList.toggle('has-extra', n > 0);
    btn.title = n > 0 ? raw : '评级、分数、分辨率等';
    var badge = btn.querySelector('.booru-extra-badge');
    if (badge) badge.textContent = n > 0 ? String(n) : '';
  }

  function parseExtra(raw) {
    var out = { rating: '', user: '', more: [], dateLo: '', dateHi: '' };
    for (var i = 0; i < _EX_KEYS.length; i++) {
      out[_EX_KEYS[i] + 'Lo'] = '';
      out[_EX_KEYS[i] + 'Hi'] = '';
    }
    var tokens = String(raw || '').split(/\s+/).filter(Boolean);
    for (var j = 0; j < tokens.length; j++) {
      var t = tokens[j], m;
      if ((m = /^rating:([gsqe])$/i.exec(t))) out.rating = m[1].toLowerCase();
      else if ((m = /^(score|width|height|id):(>=|>|<=|<)?(-?\d+)$/i.exec(t))) {
        var k = m[1].toLowerCase(), op = m[2] || '=', v = m[3];
        if (op === '<=' || op === '<') out[k + 'Hi'] = v;
        else if (op === '=') { out[k + 'Lo'] = v; out[k + 'Hi'] = v; }
        else out[k + 'Lo'] = v;
      } else if ((m = /^date:(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/i.exec(t))) {
        out.dateLo = m[1];
        out.dateHi = m[2];
      } else if ((m = /^date:(>=|>|<=|<)?(\d{4}-\d{2}-\d{2})$/i.exec(t))) {
        var dop = m[1] || '=', dv = m[2];
        if (dop === '<=' || dop === '<') out.dateHi = dv;
        else if (dop === '=') { out.dateLo = dv; out.dateHi = dv; }
        else out.dateLo = dv;
      } else if ((m = /^user:(.+)$/i.exec(t))) out.user = m[1];
      else out.more.push(t);
    }
    return out;
  }

  function buildExtra(f) {
    var parts = [];
    if (f.rating) parts.push('rating:' + f.rating);
    for (var i = 0; i < _EX_KEYS.length; i++) {
      var k = _EX_KEYS[i], lo = f[k + 'Lo'] || '', hi = f[k + 'Hi'] || '';
      if (lo && hi && lo === hi) parts.push(k + ':' + lo);
      else {
        if (lo) parts.push(k + ':>=' + lo);
        if (hi) parts.push(k + ':<=' + hi);
      }
    }
    var dLo = f.dateLo || '', dHi = f.dateHi || '';
    if (dLo && dHi && dLo === dHi) parts.push('date:' + dLo);
    else {
      if (dLo) parts.push('date:>=' + dLo);
      if (dHi) parts.push('date:<=' + dHi);
    }
    if (f.user) parts.push('user:' + f.user);
    if (f.more) parts.push(f.more);
    return parts.join(' ').trim();
  }

  function readExtraDlg() {
    var f = parseExtra('');
    var on = document.querySelector('#booru-extra-dlg .booru-rate-chip.on');
    f.rating = on ? (on.dataset.rate || '') : '';
    f.more = '';
    var fields = document.querySelectorAll('#booru-extra-dlg [data-ex]');
    for (var i = 0; i < fields.length; i++) {
      var el = fields[i], key = el.dataset.ex, raw = el.value.trim();
      if (key === 'user' || key === 'more' || key === 'dateLo' || key === 'dateHi') {
        f[key] = raw;
        continue;
      }
      if (!raw) { f[key] = ''; continue; }
      var n = parseInt(raw, 10);
      f[key] = isNaN(n) ? '' : String(n);
    }
    return f;
  }

  function fillExtraDlg() {
    var f = parseExtra((document.getElementById('booru-extra') || {}).value || '');
    var chips = document.querySelectorAll('#booru-extra-dlg .booru-rate-chip');
    for (var i = 0; i < chips.length; i++) {
      chips[i].classList.toggle('on', (chips[i].dataset.rate || '') === f.rating);
    }
    var fields = document.querySelectorAll('#booru-extra-dlg [data-ex]');
    for (var j = 0; j < fields.length; j++) {
      var el = fields[j], key = el.dataset.ex;
      el.value = key === 'more' ? (f.more || []).join(' ') : (f[key] || '');
    }
  }

  function setExtraDlg(show) {
    if (show) {
      syncCheatLink();
      fillExtraDlg();
    }
    var dlg = document.getElementById('booru-extra-dlg');
    if (!dlg) return;
    dlg.classList.toggle('show', !!show);
    dlg.setAttribute('aria-hidden', show ? 'false' : 'true');
  }

  function clearExtraDlg() {
    var chips = document.querySelectorAll('#booru-extra-dlg .booru-rate-chip');
    for (var i = 0; i < chips.length; i++) {
      chips[i].classList.toggle('on', !chips[i].dataset.rate);
    }
    var fields = document.querySelectorAll('#booru-extra-dlg [data-ex]');
    for (var j = 0; j < fields.length; j++) fields[j].value = '';
  }

  function applyExtraDlg() {
    var el = document.getElementById('booru-extra');
    if (el) el.value = buildExtra(readExtraDlg());
    syncExtraBtn();
    setExtraDlg(false);
    saveUiState();
  }

  function updatePageStatus() {
    var start = document.getElementById('booru-start-page');
    var prev = document.getElementById('btn-booru-page-prev');
    var next = document.getElementById('btn-booru-page-next');
    var remote = isRemote();
    if (start && remote && _page >= 1 && document.activeElement !== start) {
      start.value = String(_page);
    }
    if (prev) {
      prev.disabled = !remote || _loading || !_hasPrev;
      prev.title = _loading ? '加载中…' : (_hasPrev ? '上一页' : '没有上一页');
    }
    if (next) {
      next.disabled = !remote || _loading || !_hasMore;
      next.title = _loading ? '加载中…' : (_hasMore ? '下一页' : '没有下一页');
    }
  }

  /** 视口靠上卡片 → 当前页 */
  function syncPageFromScroll() {
    if (!isRemote() || !_posts.length) return;
    var start = document.getElementById('booru-start-page');
    if (start && document.activeElement === start) return;
    var cards = document.querySelectorAll('#masonry .card[data-page]');
    if (!cards.length) return;
    var anchor = window.innerHeight * 0.28;
    var best = null;
    var bestDist = Infinity;
    for (var i = 0; i < cards.length; i++) {
      var r = cards[i].getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) continue;
      var d = Math.abs((r.top + r.bottom) / 2 - anchor);
      if (d < bestDist) {
        bestDist = d;
        best = cards[i];
      }
    }
    if (!best) return;
    var p = parseInt(best.dataset.page, 10);
    if (!(p > 0) || p === _page) return;
    _page = p;
    if (start) start.value = String(p);
  }

  /**
   * 滚动到指定页的第一张卡片。引用：app.js 更改列数后恢复页位。
   * @param {number} page
   */
  function scrollToPage(page) {
    var p = parseInt(page, 10);
    if (!(p > 0)) return;
    _page = p;
    var startEl = document.getElementById('booru-start-page');
    if (startEl) startEl.value = String(p);
    var cards = document.querySelectorAll('#masonry .card[data-page="' + p + '"]');
    if (!cards.length) return;
    var y = cards[0].getBoundingClientRect().top + window.scrollY - 80;
    window.scrollTo(0, Math.max(0, y));
  }

  /**
   * @returns {number}
   */
  function currentPage() {
    var start = document.getElementById('booru-start-page');
    if (start) {
      var v = parseInt(start.value, 10);
      if (v > 0) return v;
    }
    return _page > 0 ? _page : 1;
  }

  function stopPageObserve() {
    if (_pageIo) {
      _pageIo.disconnect();
      _pageIo = null;
    }
  }

  function ensureTrigger(container, id, topPx) {
    var el = document.getElementById(id);
    if (!el || el.parentNode !== container) {
      if (el) el.remove();
      el = document.createElement('div');
      el.id = id;
      el.setAttribute('aria-hidden', 'true');
      el.style.cssText =
        'position:absolute;left:0;width:1px;height:1px;margin:0;padding:0;' +
        'pointer-events:none;visibility:hidden;';
      container.appendChild(el);
    }
    el.style.top = Math.max(0, topPx) + 'px';
    return el;
  }

  function syncLoadTrigger() {
    stopPageObserve();
    var container = document.getElementById('masonry');
    var downOld = document.getElementById('booru-load-trigger-down');
    var upOld = document.getElementById('booru-load-trigger-up');
    if (!container || !isRemote() || !_posts.length || !_colHi || !_colHi.length) {
      if (downOld) downOld.remove();
      if (upOld) upOld.remove();
      return;
    }

    var margin = '280px 0px';
    _pageIo = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (!entries[i].isIntersecting) continue;
        if (_loading || !_posts.length) return;
        var id = entries[i].target.id;
        if (id === 'booru-load-trigger-down' && _hasMore) loadMore(false);
        if (id === 'booru-load-trigger-up' && _hasPrev) loadMore(true);
      }
    }, { root: null, rootMargin: margin, threshold: 0 });

    if (_hasMore) {
      var short = Math.min.apply(null, _colHi) - _minY;
      var downEl = ensureTrigger(container, 'booru-load-trigger-down', short);
      _pageIo.observe(downEl);
      requestAnimationFrame(function () {
        if (!_hasMore || _loading || !downEl.isConnected) return;
        if (downEl.getBoundingClientRect().top < window.innerHeight + 280) loadMore(false);
      });
    } else if (downOld) {
      downOld.remove();
    }

    if (_hasPrev) {
      var upEl = ensureTrigger(container, 'booru-load-trigger-up', 0);
      _pageIo.observe(upEl);
      requestAnimationFrame(function () {
        if (!_hasPrev || _loading || !upEl.isConnected) return;
        var r = upEl.getBoundingClientRect();
        if (r.top < window.innerHeight + 280 && r.bottom > -280) loadMore(true);
      });
    } else if (upOld) {
      upOld.remove();
    }
  }

  function filterNewPosts(list) {
    var seen = {};
    for (var i = 0; i < _posts.length; i++) seen[_posts[i].key] = true;
    var fresh = [];
    var dup = 0;
    for (var j = 0; j < list.length; j++) {
      var p = list[j];
      if (seen[p.key]) {
        dup += 1;
        continue;
      }
      seen[p.key] = true;
      fresh.push(p);
    }
    return { fresh: fresh, dup: dup };
  }

  function markDownloaded(list) {
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      if (p.downloaded) {
        _selected[p.key] = true;
        _status[p.key] = 'done';
      }
    }
  }

  /**
   * 同步本地/远程工具栏显隐与统计段标签。
   * 段位：1 总计 · 2 已选/已保存 · 6 回收站（仅本地）。
   * 引用：mode 切换、init。
   */
  function syncToolbar() {
    var remote = isRemote();
    var localBar = document.getElementById('toolbar-local');
    var booruBar = document.getElementById('toolbar-booru');
    if (localBar) localBar.classList.toggle('is-off', remote);
    if (booruBar) booruBar.classList.toggle('is-off', !remote);
    var selLabel = document.getElementById('stat-selected-label');
    if (selLabel) selLabel.textContent = remote ? '已保存' : '已选';
    var trashedWrap = document.getElementById('stat-trashed-wrap');
    if (trashedWrap) trashedWrap.hidden = remote;
    var hint = document.getElementById('filter-hint');
    if (hint) {
      hint.textContent = remote
        ? '左键选中写入目录 · 右键大图 · 上滑上一页 · 下滑下一页'
        : '点击选中 · 右键相似 · Delete 删除';
    }
    if (!remote) {
      stopPageObserve();
      _hasMore = false;
      _hasPrev = false;
      updatePageStatus();
    }
    updateStats();
  }

  /**
   * 刷新筛选统计。远程「已保存」= 结果集中已写入工作目录的条目数。
   * 引用：render、下载/移除完成、syncToolbar。
   */
  function updateStats() {
    var total = _posts.length;
    var done = 0;
    _posts.forEach(function (p) {
      if (p.downloaded || _status[p.key] === 'done') done += 1;
    });
    var t = document.getElementById('stat-total');
    var s = document.getElementById('stat-selected');
    if (t) t.textContent = String(total);
    if (s) s.textContent = String(isRemote() ? done : Object.keys(_selected).length);
  }

  function cardLabel(post) {
    var st = _status[post.key] || '';
    var stLabel = st === 'downloading' ? '保存中…'
      : (st === 'done' || post.downloaded) ? '已保存'
      : st === 'error' ? '失败'
      : '未保存';
    var res = (post.width && post.height) ? (post.width + '×' + post.height + ' ') : '';
    var stem = post.stem || ((post.source === 'danbooru' ? 'd' : 'g') + post.id);
    return stLabel + ' · ' + res + stem;
  }

  function findCard(key) {
    var container = document.getElementById('masonry');
    if (!container) return null;
    var cards = container.querySelectorAll('.card');
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].dataset.key === key) return cards[i];
    }
    return null;
  }

  function postByKey(key) {
    for (var i = 0; i < _posts.length; i++) {
      if (_posts[i].key === key) return _posts[i];
    }
    return null;
  }

  function updateCard(key) {
    var card = findCard(key);
    if (!card) return;
    card.classList.toggle('selected', !!_selected[key]);
    var name = card.querySelector('.info .name');
    var post = postByKey(key);
    if (name && post) name.textContent = cardLabel(post);
    updateStats();
  }

  function stopLazy() {
    if (_io) {
      _io.disconnect();
      _io = null;
    }
  }

  function ensureLazy(container) {
    if (_io) return;
    _io = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (!entries[i].isIntersecting) continue;
        var img = entries[i].target;
        var url = img.dataset.src;
        if (!url) continue;
        img.src = url;
        delete img.dataset.src;
        _io.unobserve(img);
      }
    }, { root: null, rootMargin: '400px 0px', threshold: 0 });
  }

  function observeNew(container, root) {
    ensureLazy(container);
    var imgs = (root || container).querySelectorAll('img[data-src]');
    for (var j = 0; j < imgs.length; j++) _io.observe(imgs[j]);
  }

  function resolveCols() {
    var colsSlider = document.getElementById('cols-slider');
    if (colsSlider) {
      var v = parseInt(colsSlider.value, 10);
      if (v > 0) return v;
    }
    var w = window.innerWidth;
    return w >= 1600 ? 6 : w >= 1280 ? 5 : w >= 960 ? 4 : w >= 640 ? 3 : 2;
  }

  function createCard(post, cw) {
    var card = document.createElement('div');
    card.className = 'card';
    if (_selected[post.key]) card.classList.add('selected');
    card.dataset.key = post.key;
    if (post.page) card.dataset.page = String(post.page);
    card.addEventListener('click', onCardClick.bind(null, post));
    card.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      e.stopPropagation();
      showPreview(post);
    });

    var imgEl = document.createElement('img');
    imgEl.alt = post.id;
    imgEl.dataset.src = thumbUrl(post);
    imgEl.style.width = '100%';
    imgEl.style.height = 'auto';
    imgEl.style.aspectRatio = (post.width || 1) + ' / ' + (post.height || 1);
    imgEl.style.background = '#111';
    card.appendChild(imgEl);

    var info = document.createElement('div');
    info.className = 'info';
    var name = document.createElement('span');
    name.className = 'name';
    name.textContent = cardLabel(post);
    info.appendChild(name);
    card.appendChild(info);

    var overlay = document.createElement('div');
    overlay.className = 'overlay';
    overlay.textContent = '\u2713';
    card.appendChild(overlay);
    return card;
  }

  var _previewPost = null;

  function postPageUrl(post) {
    if (post.source === 'danbooru') {
      return 'https://danbooru.donmai.us/posts/' + post.id;
    }
    return 'https://gelbooru.com/index.php?page=post&s=view&id=' + post.id;
  }

  function parseTags(raw) {
    if (!raw) return [];
    return String(raw).replace(/,/g, ' ').split(/\s+/).filter(Boolean);
  }

  function hidePreview() {
    _previewPost = null;
    var box = document.getElementById('booru-preview');
    var img = document.getElementById('booru-preview-img');
    if (box) box.classList.remove('show');
    if (img) {
      img.removeAttribute('src');
      img.alt = '';
    }
  }

  function showPreview(post) {
    _previewPost = post;
    var box = document.getElementById('booru-preview');
    var img = document.getElementById('booru-preview-img');
    var meta = document.getElementById('booru-preview-meta');
    var tagsEl = document.getElementById('booru-preview-tags');
    var open = document.getElementById('booru-preview-open');
    var dl = document.getElementById('booru-preview-dl');
    if (!box || !img || !meta || !tagsEl) return;

    var sample = sampleUrl(post);
    img.alt = post.stem || post.id;
    img.src = sample;

    var res = (post.width && post.height) ? (post.width + '×' + post.height) : '-';
    var st = _status[post.key] === 'done' || post.downloaded ? '已保存' : '未保存';
    meta.innerHTML =
      '<div><b>' + (post.source === 'danbooru' ? 'Danbooru' : 'Gelbooru') + '</b> · ' +
      (post.stem || post.id) + '</div>' +
      '<div>分辨率 ' + res + ' · 分数 ' + (post.score != null ? post.score : '-') +
      ' · rating ' + (post.rating || '-') + '</div>' +
      '<div>状态 <b>' + st + '</b></div>';

    if (open) open.href = postPageUrl(post);
    if (dl) dl.textContent = (post.downloaded || _status[post.key] === 'done') ? '移除' : '下载';

    tagsEl.innerHTML = '';
    var tags = parseTags(post.tags);
    if (!tags.length) {
      tagsEl.textContent = '无标签';
    } else {
      for (var i = 0; i < tags.length; i++) {
        (function (tag) {
          var chip = document.createElement('button');
          chip.type = 'button';
          chip.className = 'booru-tag-chip';
          chip.textContent = tag;
          chip.title = '点击搜索此标签';
          chip.addEventListener('click', function () {
            var input = document.getElementById('booru-tags');
            if (input) input.value = tag;
            hidePreview();
            search(true).catch(function (e) { toast(String(e.message || e)); });
          });
          tagsEl.appendChild(chip);
        })(tags[i]);
      }
    }

    box.classList.add('show');
  }

  function togglePreviewDownload() {
    if (!_previewPost) return;
    var post = _previewPost;
    hidePreview();
    onCardClick(post);
  }

  function itemH(post) {
    return _cw * (post.height || 1) / (post.width || 1) + 22;
  }

  function pickCol(up) {
    var arr = up ? _colLo : _colHi;
    var c = 0;
    for (var i = 1; i < arr.length; i++) {
      if (up ? arr[i] > arr[c] : arr[i] < arr[c]) c = i;
    }
    return c;
  }

  function place(card, post, up) {
    var ih = itemH(post);
    var c = pickCol(up);
    var y;
    if (up) {
      _colLo[c] -= ih + _gap;
      y = _colLo[c];
    } else {
      y = _colHi[c];
      _colHi[c] = y + ih + _gap;
    }
    card.dataset.worldY = String(y);
    card.style.position = 'absolute';
    card.style.left = (_pad + c * (_cw + _gap)) + 'px';
    card.style.top = (y - _minY) + 'px';
    card.style.width = _cw + 'px';
  }

  function syncOrigin() {
    var container = document.getElementById('masonry');
    if (!container || !_colLo) return;
    var lo = Math.min.apply(null, _colLo);
    var hi = Math.max.apply(null, _colHi);
    if (lo < _minY) {
      var delta = _minY - lo;
      _minY = lo;
      var cards = container.querySelectorAll('.card');
      for (var i = 0; i < cards.length; i++) {
        var w = parseFloat(cards[i].dataset.worldY);
        if (!isNaN(w)) cards[i].style.top = (w - _minY) + 'px';
      }
      if (delta) window.scrollBy(0, delta);
    }
    container.style.height = Math.max(0, hi - _minY) + 'px';
  }

  function addPosts(fresh, up) {
    var container = document.getElementById('masonry');
    if (!container || !fresh.length || !_colHi) {
      render();
      return;
    }
    var frag = document.createDocumentFragment();
    if (up) {
      for (var i = fresh.length - 1; i >= 0; i--) {
        var card = createCard(fresh[i], _cw);
        place(card, fresh[i], true);
        frag.appendChild(card);
      }
      container.appendChild(frag);
      syncOrigin();
    } else {
      for (var j = 0; j < fresh.length; j++) {
        var c2 = createCard(fresh[j], _cw);
        place(c2, fresh[j], false);
        frag.appendChild(c2);
      }
      container.appendChild(frag);
      container.style.height = (Math.max.apply(null, _colHi) - _minY) + 'px';
    }
    observeNew(container);
    updateStats();
    syncLoadTrigger();
    syncPageFromScroll();
  }

  function render() {
    var container = document.getElementById('masonry');
    if (!container) return;

    if (!_posts.length) {
      stopLazy();
      container.innerHTML =
        '<div style="padding:60px;text-align:center;color:var(--text-dim);">无结果，输入 tag 后搜索</div>';
      container.style.height = '';
      resetLayout();
      updateStats();
      syncLoadTrigger();
      return;
    }

    var cols = resolveCols();
    _gap = 8;
    _pad = 8;
    var avail = Math.max(0, container.clientWidth - _pad * 2);
    _cw = (avail - _gap * (cols - 1)) / cols;
    if (!(_cw > 0)) _cw = 1;

    stopLazy();
    container.innerHTML = '';
    _colLo = new Array(cols).fill(0);
    _colHi = new Array(cols).fill(0);
    _minY = 0;

    for (var j = 0; j < _posts.length; j++) {
      var card = createCard(_posts[j], _cw);
      place(card, _posts[j], false);
      container.appendChild(card);
    }
    container.style.height = (Math.max.apply(null, _colHi) - _minY) + 'px';
    observeNew(container);
    updateStats();
    syncLoadTrigger();
    syncPageFromScroll();
    if (_pendingScrollY != null) {
      var y = _pendingScrollY;
      _pendingScrollY = null;
      requestAnimationFrame(function () {
        window.scrollTo(0, y);
        syncPageFromScroll();
      });
    }
  }

  async function downloadOne(post) {
    _status[post.key] = 'downloading';
    updateCard(post.key);
    var res = await fetch(API.apiPath('/booru/download'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: post.source,
        id: String(post.id),
        dir: destDir(),
        overwrite: false,
      }),
    });
    if (!res.ok) {
      _status[post.key] = 'error';
      updateCard(post.key);
      var t = await res.text();
      throw new Error(t || res.statusText);
    }
    var data = await res.json();
    _status[post.key] = 'done';
    post.downloaded = true;
    if (data.stem) post.stem = data.stem;
    updateCard(post.key);
    return data;
  }

  async function undownloadOne(post) {
    var res = await fetch(API.apiPath('/booru/undownload'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: post.source,
        id: String(post.id),
        dir: destDir(),
      }),
    });
    if (!res.ok) {
      var t = await res.text();
      throw new Error(t || res.statusText);
    }
    delete _status[post.key];
    post.downloaded = false;
    updateCard(post.key);
    return res.json();
  }

  function onCardClick(post) {
    var isOn = !!_selected[post.key] || post.downloaded || _status[post.key] === 'done';
    if (isOn) {
      if (_status[post.key] === 'downloading') {
        toast('正在保存，稍后再移除');
        return;
      }
      delete _selected[post.key];
      updateCard(post.key);
      undownloadOne(post).then(function (data) {
        toast(data.count ? ('已移除 ' + (post.stem || post.id)) : ('目录无文件 ' + (post.stem || post.id)));
      }).catch(function (e) {
          _selected[post.key] = true;
        if (post.downloaded) _status[post.key] = 'done';
        updateCard(post.key);
        toast('移除失败: ' + (e.message || e));
      });
      return;
    }
    _selected[post.key] = true;
    updateCard(post.key);
    downloadOne(post).then(function (data) {
      toast(data.skipped ? ('已存在 ' + (data.stem || post.id)) : ('已保存 ' + (data.stem || post.id) + '.png'));
    }).catch(function (e) {
      delete _selected[post.key];
      updateCard(post.key);
      toast('保存失败: ' + (e.message || e));
    });
  }

  function syncCheatLink() {
    var a = document.getElementById('booru-cheat');
    if (!a) return;
    a.href = mode() === 'danbooru'
      ? 'https://danbooru.donmai.us/wiki_pages/help:cheatsheet'
      : 'https://gelbooru.com/index.php?page=help&topic=cheatsheet';
  }

  async function search(dir) {
    if (_loading) return;
    var isReset = dir === true || dir === 'reset';
    var isUp = dir === 'up';
    var input = document.getElementById('booru-tags');
    var extraEl = document.getElementById('booru-extra');
    var orderEl = document.getElementById('booru-order');
    var tags = input ? input.value.trim() : '';
    var extra = extraEl ? extraEl.value.trim() : '';
    var order = orderEl ? orderEl.value : '';
    _source = mode();
    if (isReset) {
      var start = startPageFromInput();
      _pageLo = start;
      _pageHi = start;
      _page = start;
      _posts = [];
      _selected = {};
      _status = {};
      _hasMore = false;
      _hasPrev = start > 1;
      _dupSkip = 0;
      stopPageObserve();
    }
    var fetchPage = isUp ? _pageLo : _pageHi;
    _page = fetchPage;
    saveUiState();
    _loading = true;
    updatePageStatus();
    var btn = document.getElementById('btn-booru-search');
    if (btn) btn.disabled = true;
    var autoNext = false;
    var autoPrev = false;
    try {
      var res = await fetch(API.apiPath('/booru/search'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source: _source,
          tags: tags,
          extra: extra,
          order: order,
          page: fetchPage,
          limit: 40,
          dir: destDir(),
        }),
      });
      if (!res.ok) {
        var err = await res.text();
        throw new Error(err || res.statusText);
      }
      var data = await res.json();
      var list = data.posts || [];
      for (var pi = 0; pi < list.length; pi++) list[pi].page = fetchPage;
      if (!isUp) {
        _hasMore = !!data.has_more;
      }

      var fresh = list;
      var dup = 0;
      if (isReset) {
        _posts = list;
        markDownloaded(list);
        render();
        _dupSkip = 0;
        _hasPrev = _pageLo > 1;
        if (!list.length && _hasMore) { _dupSkip = 1; autoNext = true; }
      } else if (isUp && !list.length) {
        _hasPrev = false;
        _pageLo += 1;
        _page = _pageLo;
      } else if (!isReset) {
        var filtered = filterNewPosts(list);
        fresh = filtered.fresh;
        dup = filtered.dup;
        markDownloaded(fresh);
        if (fresh.length) {
          _posts = isUp ? fresh.concat(_posts) : _posts.concat(fresh);
          addPosts(fresh, isUp);
          _dupSkip = 0;
          if (isUp) _hasPrev = _pageLo > 1;
        } else if (_dupSkip < 8 && (isUp ? _pageLo > 1 : _hasMore)) {
          _dupSkip += 1;
          if (isUp) autoPrev = true; else autoNext = true;
        } else if (isUp) {
          _hasPrev = _pageLo > 1;
        }
      }

      if (!autoNext && !autoPrev && (isReset || fresh.length || dup)) {
        var head = isReset
          ? ((_source === 'danbooru' ? 'Danbooru' : 'Gelbooru') + ' · +')
          : (isUp ? '↑+' : '+');
        toast(head + fresh.length + (dup ? ' · 跳过重复 ' + dup : ''));
      }
    } finally {
      _loading = false;
      if (btn) btn.disabled = false;
      updatePageStatus();
      if (!autoNext && !autoPrev) syncLoadTrigger();
    }
    if (autoNext) {
      _pageHi += 1;
      _page = _pageHi;
      return search('down');
    }
    if (autoPrev) {
      _pageLo -= 1;
      _page = _pageLo;
      return search('up');
    }
  }

  function loadMore(up) {
    if (_loading) return;
    if (up) {
      if (!_hasPrev || _pageLo <= 1) return;
      _pageLo -= 1;
      _page = _pageLo;
    } else {
      if (!_hasMore) return;
      _pageHi += 1;
      _page = _pageHi;
    }
    search(up ? 'up' : 'down').catch(function (e) {
      if (up) { _pageLo += 1; _page = _pageLo; }
      else { _pageHi -= 1; _page = _pageHi; }
      toast(String(e.message || e));
      updatePageStatus();
      syncLoadTrigger();
    });
  }

  function enterRemote(opts) {
    opts = opts || {};
    syncToolbar();
    syncCheatLink();
    _source = mode();
    _posts = [];
    var start = startPageFromInput();
    _page = start;
    _pageLo = start;
    _pageHi = start;
    _selected = {};
    _status = {};
    _hasMore = false;
    _hasPrev = start > 1;
    resetLayout();
    stopLazy();
    stopPageObserve();
    render();
    updatePageStatus();
    if (opts.autoSearch !== false) {
      search(true).catch(function (e) { toast(String(e.message || e)); });
    }
  }

  function leaveRemote() {
    hidePreview();
    syncToolbar();
    stopLazy();
    stopPageObserve();
    saveUiState();
  }

  function init() {
    var modeEl = document.getElementById('filter-mode');
    if (!modeEl) return;

    loadProxyPref();

    var saved = loadUiState();
    if (saved && (saved.mode === 'gelbooru' || saved.mode === 'danbooru' || saved.mode === 'local')) {
      modeEl.value = saved.mode;
      applyForm(saved);
    }

    syncToolbar();
    syncCheatLink();

    if (isRemote()) {
      enterRemote({ autoSearch: true });
    }

    modeEl.addEventListener('change', function () {
      syncCheatLink();
      if (isRemote()) {
        enterRemote({ autoSearch: true });
      } else {
        leaveRemote();
        if (typeof Store !== 'undefined') {
          Store.load().then(function () {
            if (typeof Masonry !== 'undefined') {
              var showTrash = document.getElementById('chk-show-trash');
              Masonry.render(Store.getVisible(showTrash ? showTrash.checked : false));
            }
          });
        }
      }
      saveUiState();
    });

    var proxyEl = document.getElementById('booru-proxy');
    if (proxyEl) {
      proxyEl.addEventListener('change', function () {
        saveProxyPref();
        if (!isRemote() || !_posts.length) return;
        var imgs = document.querySelectorAll('#masonry .card img');
        for (var i = 0; i < imgs.length; i++) {
          var card = imgs[i].closest('.card');
          var post = card && postByKey(card.dataset.key);
          if (!post) continue;
          var url = thumbUrl(post);
          if (imgs[i].dataset.src) imgs[i].dataset.src = url;
          else imgs[i].src = url;
        }
        if (_previewPost) showPreview(_previewPost);
        toast(isProxyMedia() ? '已开代理图（经后端）' : '已关代理图（直链）');
      });
    }

    var searchBtn = document.getElementById('btn-booru-search');
    if (searchBtn) {
      searchBtn.addEventListener('click', function () {
        search(true).catch(function (e) { toast(String(e.message || e)); });
      });
    }
    function onSearchKey(e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        search(true).catch(function (err) { toast(String(err.message || err)); });
      }
    }
    var tagsInput = document.getElementById('booru-tags');
    if (tagsInput) {
      tagsInput.addEventListener('keydown', onSearchKey);
      tagsInput.addEventListener('change', saveUiState);
    }
    var extraBtn = document.getElementById('btn-booru-extra');
    if (extraBtn) extraBtn.addEventListener('click', function () { setExtraDlg(true); });
    var extraDlg = document.getElementById('booru-extra-dlg');
    if (extraDlg) {
      extraDlg.addEventListener('click', function (e) {
        if (e.target === extraDlg) { setExtraDlg(false); return; }
        var chip = e.target.closest('.booru-rate-chip');
        if (chip) {
          var all = extraDlg.querySelectorAll('.booru-rate-chip');
          for (var j = 0; j < all.length; j++) all[j].classList.remove('on');
          chip.classList.add('on');
          return;
        }
        var act = e.target.closest('[data-ex-act]');
        if (!act) return;
        if (act.dataset.exAct === 'clear') clearExtraDlg();
        else if (act.dataset.exAct === 'cancel') setExtraDlg(false);
        else if (act.dataset.exAct === 'apply') applyExtraDlg();
      });
    }
    syncExtraBtn();
    var orderEl = document.getElementById('booru-order');
    if (orderEl) orderEl.addEventListener('change', saveUiState);
    var startEl = document.getElementById('booru-start-page');
    if (startEl) {
      startEl.addEventListener('change', saveUiState);
      startEl.addEventListener('keydown', onSearchKey);
    }
    var prevBtn = document.getElementById('btn-booru-page-prev');
    if (prevBtn) prevBtn.addEventListener('click', function () { loadMore(true); });
    var nextBtn = document.getElementById('btn-booru-page-next');
    if (nextBtn) nextBtn.addEventListener('click', function () { loadMore(false); });

    var preview = document.getElementById('booru-preview');
    if (preview) {
      preview.addEventListener('click', function (e) {
        if (e.target === preview) hidePreview();
      });
    }
    var previewClose = document.getElementById('booru-preview-close');
    if (previewClose) previewClose.addEventListener('click', hidePreview);
    var previewDl = document.getElementById('booru-preview-dl');
    if (previewDl) previewDl.addEventListener('click', togglePreviewDownload);

    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      var extraDlg = document.getElementById('booru-extra-dlg');
      if (extraDlg && extraDlg.classList.contains('show')) {
        setExtraDlg(false);
        return;
      }
      if (document.getElementById('booru-preview') &&
          document.getElementById('booru-preview').classList.contains('show')) {
        hidePreview();
      }
    });

    window.addEventListener('scroll', function () {
      if (!isRemote()) return;
      if (!_scrollRaf) {
        _scrollRaf = requestAnimationFrame(function () {
          _scrollRaf = null;
          syncPageFromScroll();
        });
      }
      clearTimeout(_scrollSaveTimer);
      _scrollSaveTimer = setTimeout(saveUiState, 200);
    }, { passive: true });
  }

  return {
    init: init,
    isRemote: isRemote,
    render: render,
    mode: mode,
    currentPage: currentPage,
    scrollToPage: scrollToPage,
  };
})();
