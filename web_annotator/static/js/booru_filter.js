/**
 * booru_filter.js —— 筛选页挂载 Gelbooru / Danbooru
 * 搜索 tag → 瀑布流缩略图；选中即下载到当前目录（g{id}.png / d{id}.png）
 */
var BooruFilter = (function () {
  'use strict';

  var STATE_KEY = 'zhao_booru_filter_v1';

  /** 读取输入控件值; 不存在时返回空串. 引用: _SOURCES.buildRequest. */
  function _val(id) {
    return (document.getElementById(id) || {}).value || '';
  }

  /** Gelbooru/Danbooru 共用: 从标签工具栏构建请求参数. 引用: _SOURCES. */
  function _booruRequest() {
    return { tags: _val('booru-tags').trim(), extra: _val('booru-extra').trim(), order: _val('booru-order') };
  }

  /**
   * 远程来源注册表: 每个来源声明自己的工具栏 / 参数构建 / 媒体 URL /
   * 原站地址与预览渲染。主逻辑只查询本表, 新增来源只需在此加一项并提供
   * 对应工具栏 HTML, 无需改动业务主干。
   * 引用: isRemote / cur / thumbUrl / sampleUrl / cardLabel / postPageUrl /
   *       showPreview / syncToolbar / saveUiState / applyForm / init。
   */
  var _SOURCES = {
    local: { remote: false },
    gelbooru: {
      remote: true,
      toolbarId: 'toolbar-booru',
      searchBtn: 'btn-booru-search',
      batchBtn: 'btn-booru-batch',
      batchDialog: true,
      stemPrefix: 'g',
      nameLabel: 'Gelbooru',
      stateKeys: ['booru-tags', 'booru-extra', 'booru-order'],
      buildRequest: _booruRequest,
      thumb: function (post) {
        return API.apiPath('/booru/thumb')
          + '?id=' + encodeURIComponent(post.id)
          + '&url=' + encodeURIComponent(post.preview_url || post.sample_url || '');
      },
      sample: function (post) {
        return API.apiPath('/booru/sample')
          + '?id=' + encodeURIComponent(post.id)
          + '&url=' + encodeURIComponent(post.sample_url || post.file_url || post.preview_url || '');
      },
      pageUrl: function (post) {
        return 'https://gelbooru.com/index.php?page=post&s=view&id=' + post.id;
      },
    },
    danbooru: {
      remote: true,
      toolbarId: 'toolbar-booru',
      searchBtn: 'btn-booru-search',
      batchBtn: 'btn-booru-batch',
      batchDialog: true,
      stemPrefix: 'd',
      nameLabel: 'Danbooru',
      stateKeys: ['booru-tags', 'booru-extra', 'booru-order'],
      buildRequest: _booruRequest,
      thumb: function (post) { return post.preview_url || post.sample_url || ''; },
      sample: function (post) { return post.sample_url || post.file_url || post.preview_url || ''; },
      pageUrl: function (post) { return 'https://danbooru.donmai.us/posts/' + post.id; },
    },
    bilibili: {
      remote: true,
      toolbarId: 'toolbar-bilibili',
      searchBtn: 'btn-bilibili-search',
      batchBtn: 'btn-bilibili-batch',
      batchDialog: false,
      enterIds: ['bilibili-uid', 'bilibili-keyword'],
      stemPrefix: 'bi',
      nameLabel: 'Bilibili',
      requireTags: true,
      hintText: '输入 B站 UID 搜索 · 左键写入目录 · 右键大图',
      stateKeys: ['bilibili-uid', 'bilibili-type', 'bilibili-keyword', 'bilibili-time-from', 'bilibili-time-to'],
      buildRequest: function () {
        var parts = [];
        var type = _val('bilibili-type');
        if (type) parts.push('type:' + type);
        var kw = _val('bilibili-keyword');
        if (kw.trim()) parts.push('keyword:' + kw.trim());
        var from = _val('bilibili-time-from');
        if (from) parts.push('after:' + from);
        var to = _val('bilibili-time-to');
        if (to) parts.push('before:' + to);
        return { tags: _val('bilibili-uid').trim(), extra: parts.join(' '), order: '' };
      },
      thumb: function (post) {
        return API.apiPath('/booru/image') + '?url=' + encodeURIComponent(post.preview_url || post.sample_url || '');
      },
      sample: function (post) {
        return API.apiPath('/booru/image') + '?url=' + encodeURIComponent(post.sample_url || post.file_url || post.preview_url || '');
      },
      pageUrl: function (post) {
        var i = post.id.lastIndexOf('_');
        return 'https://t.bilibili.com/' + (i > 0 ? post.id.slice(0, i) : post.id);
      },
      renderPreviewTags: function (el, post) {
        var para = document.createElement('div');
        para.className = 'bili-preview-text';
        para.textContent = post.tags || '(无正文)';
        el.appendChild(para);
      },
    },
  };

  /** 当前来源定义. 引用: 各来源相关函数. */
  function cur() {
    return _SOURCES[mode()] || _SOURCES.local;
  }

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
  var _scrollSaveTimer = null;
  var _scrollRaf = null;
  var _EX_KEYS = ['score', 'width', 'height', 'id'];
  var _batchRunning = false;
  var _batchJobId = null;
  var _batchWatch = null;
  var _batchParamTimer = null;

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
    return !!(cur() && cur().remote);
  }

  /** 媒体 URL 依来源查表. 引用: 卡片/预览. */
  function thumbUrl(post) {
    var d = _SOURCES[post.source];
    return d && d.thumb ? d.thumb(post) : (post.preview_url || post.sample_url || '');
  }

  function sampleUrl(post) {
    var d = _SOURCES[post.source];
    return d && d.sample ? d.sample(post) : (post.sample_url || post.file_url || post.preview_url || '');
  }

  function saveUiState() {
    var payload = {
      mode: mode(),
      startPage: startPageFromInput(),
      source: _source,
      page: _page,
    };
    for (var skey in _SOURCES) {
      var sdef = _SOURCES[skey];
      if (!sdef.stateKeys) continue;
      for (var si = 0; si < sdef.stateKeys.length; si++) {
        var cid = sdef.stateKeys[si];
        payload[cid] = (document.getElementById(cid) || {}).value || '';
      }
    }
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
    for (var akey in _SOURCES) {
      var adef = _SOURCES[akey];
      if (!adef.stateKeys) continue;
      for (var ai = 0; ai < adef.stateKeys.length; ai++) {
        var cid = adef.stateKeys[ai];
        var ael = document.getElementById(cid);
        if (ael && st[cid] != null) ael.value = st[cid];
      }
    }
    var start = document.getElementById('booru-start-page');
    var pg = parseInt(st.page != null ? st.page : st.startPage, 10);
    if (!(pg > 0)) pg = 1;
    if (start) start.value = String(pg);
    syncExtraBtn();
  }

  function resetLayout() {
    _colLo = null;
    _colHi = null;
    _minY = 0;
  }

  /**
   * 统计已生效的附加条件条数（每项上下限分别计；精确相等计 1）。
   * 引用：syncExtraBtn。
   * @param {object} f
   * @returns {number}
   */
  function countExtra(f) {
    var n = 0;
    if (f.rating) n += 1;
    for (var i = 0; i < _EX_KEYS.length; i++) {
      var k = _EX_KEYS[i];
      var lo = f[k + 'Lo'] || '';
      var hi = f[k + 'Hi'] || '';
      if (lo && hi && lo === hi) n += 1;
      else {
        if (lo) n += 1;
        if (hi) n += 1;
      }
    }
    var dLo = f.dateLo || '';
    var dHi = f.dateHi || '';
    if (dLo && dHi && dLo === dHi) n += 1;
    else {
      if (dLo) n += 1;
      if (dHi) n += 1;
    }
    if (f.user) n += 1;
    var more = f.more;
    if (Array.isArray(more)) n += more.length;
    else if (more) n += String(more).split(/\s+/).filter(Boolean).length;
    return n;
  }

  /**
   * 附加条件可读摘要。引用：syncExtraBtn 的 title。
   * @param {object} f
   * @returns {string}
   */
  function summarizeExtra(f) {
    var bits = [];
    if (f.rating) bits.push('评级:' + f.rating);
    for (var i = 0; i < _EX_KEYS.length; i++) {
      var k = _EX_KEYS[i];
      var lo = f[k + 'Lo'] || '';
      var hi = f[k + 'Hi'] || '';
      if (lo && hi && lo === hi) bits.push(k + '=' + lo);
      else if (lo && hi) bits.push(k + ' ' + lo + '…' + hi);
      else if (lo) bits.push(k + '≥' + lo);
      else if (hi) bits.push(k + '≤' + hi);
    }
    var dLo = f.dateLo || '';
    var dHi = f.dateHi || '';
    if (dLo && dHi && dLo === dHi) bits.push('日期=' + dLo);
    else if (dLo && dHi) bits.push('日期 ' + dLo + '…' + dHi);
    else if (dLo) bits.push('日期≥' + dLo);
    else if (dHi) bits.push('日期≤' + dHi);
    if (f.user) bits.push('用户:' + f.user);
    var more = f.more;
    if (Array.isArray(more) && more.length) bits.push(more.join(' '));
    else if (more && String(more).trim()) bits.push(String(more).trim());
    return bits.join(' · ');
  }

  function syncExtraBtn() {
    var btn = document.getElementById('btn-booru-extra');
    var el = document.getElementById('booru-extra');
    if (!btn || !el) return;
    var f = parseExtra(el.value.trim());
    var n = countExtra(f);
    var summary = summarizeExtra(f);
    btn.classList.toggle('has-extra', n > 0);
    btn.title = n > 0 ? summary : '评级、分数、分辨率、日期、用户等';
    var badge = btn.querySelector('.booru-extra-badge');
    if (badge) badge.textContent = n > 0 ? String(n) : '';
  }

  function parseExtra(raw) {
    var out = { rating: '', user: '', more: [], dateLo: '', dateHi: '' };
    for (var i = 0; i < _EX_KEYS.length; i++) {
      out[_EX_KEYS[i] + 'Lo'] = '';
      out[_EX_KEYS[i] + 'Hi'] = '';
    }
    var dropDate = mode() === 'gelbooru';
    var tokens = String(raw || '').split(/\s+/).filter(Boolean);
    for (var j = 0; j < tokens.length; j++) {
      var t = tokens[j], m;
      if ((m = /^rating:([gsqe])$/i.exec(t))) out.rating = m[1].toLowerCase();
      else if ((m = /^(score|width|height|id):(>=|>|<=|<)?(-?\d+)$/i.exec(t))) {
        var k = m[1].toLowerCase(), op = m[2] || '=', v = m[3];
        if (op === '<=' || op === '<') out[k + 'Hi'] = v;
        else if (op === '=') { out[k + 'Lo'] = v; out[k + 'Hi'] = v; }
        else out[k + 'Lo'] = v;
      } else if (/^date:/i.test(t)) {
        if (dropDate) continue;
        if ((m = /^date:(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/i.exec(t))) {
          out.dateLo = m[1];
          out.dateHi = m[2];
        } else if ((m = /^date:(>=|>|<=|<)?(\d{4}-\d{2}-\d{2})$/i.exec(t))) {
          var dop = m[1] || '=', dv = m[2];
          if (dop === '<=' || dop === '<') out.dateHi = dv;
          else if (dop === '=') { out.dateLo = dv; out.dateHi = dv; }
          else out.dateLo = dv;
        } else out.more.push(t);
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
    if (mode() !== 'gelbooru') {
      var dLo = f.dateLo || '', dHi = f.dateHi || '';
      if (dLo && dHi && dLo === dHi) parts.push('date:' + dLo);
      else {
        if (dLo) parts.push('date:>=' + dLo);
        if (dHi) parts.push('date:<=' + dHi);
      }
    }
    if (f.user) parts.push('user:' + f.user);
    if (Array.isArray(f.more) && f.more.length) parts.push(f.more.join(' '));
    else if (f.more && String(f.more).trim()) parts.push(String(f.more).trim());
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
   * 远程「已收录」= 工作目录磁盘张数。
   * 引用：syncToolbar、目录切换、下载/移除。
   * @returns {Promise<void>}
   */
  async function refreshDiskCount() {
    if (!isRemote()) return;
    var keptEl = document.getElementById('stat-kept');
    var res = await fetch(API.apiPath('/dirs'));
    if (!res.ok) throw new Error('无法读取目录统计');
    var data = await res.json();
    var name = destDir();
    var hit = null;
    var dirs = data.dirs || [];
    for (var i = 0; i < dirs.length; i++) {
      if (dirs[i].name === name) {
        hit = dirs[i];
        break;
      }
    }
    if (keptEl) keptEl.textContent = String(hit && hit.image_count != null ? hit.image_count : 0);
  }

  /**
   * 远程「已加载」= 当前搜索结果条数。
   * 引用：render、addPosts。
   */
  function paintRemoteCounts() {
    if (!isRemote()) return;
    var loadedEl = document.getElementById('stat-loaded');
    if (loadedEl) loadedEl.textContent = String(_posts.length);
  }

  /**
   * 同步本地/远程工具栏。
   * 本地：已收录 · 已选择 · 回收站
   * 远程：已收录 · 已加载
   * 引用：mode 切换、init。
   */
  function syncToolbar() {
    var remote = isRemote();
    var curDef = cur();
    var localBar = document.getElementById('toolbar-local');
    var pageWrap = document.getElementById('remote-page-wrap');
    var loadedWrap = document.getElementById('stat-loaded-wrap');
    var selWrap = document.getElementById('stat-selected-wrap');
    var trashWrap = document.getElementById('stat-trashed-wrap');
    if (localBar) localBar.classList.toggle('is-off', remote);
    // 依注册表切换各来源工具栏: 仅当前远程来源显示。
    // gelbooru/danbooru 共用 toolbar-booru, 按 toolbarId 而非对象引用比较, 避免共用工具栏被覆盖.
    for (var skey in _SOURCES) {
      var sdef = _SOURCES[skey];
      if (!sdef.toolbarId) continue;
      var bar = document.getElementById(sdef.toolbarId);
      if (bar) bar.classList.toggle('is-off', !(remote && sdef.toolbarId === curDef.toolbarId));
    }
    if (pageWrap) pageWrap.classList.toggle('is-off', !remote);
    if (loadedWrap) loadedWrap.hidden = !remote;
    if (selWrap) selWrap.hidden = remote;
    if (trashWrap) trashWrap.hidden = remote;
    var hint = document.getElementById('filter-hint');
    if (hint) {
      hint.textContent = !remote
        ? '点击选中 · 右键相似 · Delete 删除'
        : (curDef.hintText || '左键写入目录 · 右键大图 · 上滑上一页 · 下滑下一页');
    }
    if (!remote) {
      stopPageObserve();
      _hasMore = false;
      _hasPrev = false;
      updatePageStatus();
      return;
    }
    refreshDiskCount().catch(function (e) { toast(String(e.message || e)); });
  }

  function cardLabel(post) {
    var st = _status[post.key] || '';
    var stLabel = st === 'downloading' ? '保存中…'
      : (st === 'done' || post.downloaded) ? '已保存'
      : st === 'error' ? '失败'
      : '未保存';
    var rs = resText(post);
    var stem = post.stem || (((_SOURCES[post.source] || {}).stemPrefix || 'g') + post.id);
    return stLabel + ' · ' + (rs ? rs + ' ' : '') + stem;
  }

  /** 帖子是否有真实宽高. 引用: cardLabel / createCard / showPreview. */
  function hasSize(post) {
    return post.width > 0 && post.height > 0;
  }

  /** 分辨率文本; 无真实宽高时返回空串. 引用: cardLabel / showPreview. */
  function resText(post) {
    return hasSize(post) ? (post.width + '×' + post.height) : '';
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
    // 宽高已知时按真实比例占位, 避免加载前塌缩与加载后拉伸
    if (hasSize(post)) {
      imgEl.style.aspectRatio = post.width + ' / ' + post.height;
    }
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
    var d = _SOURCES[post.source];
    return d && d.pageUrl ? d.pageUrl(post) : '#';
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

    var res = resText(post) || '-';
    var st = _status[post.key] === 'done' || post.downloaded ? '已保存' : '未保存';
    var srcName = ((_SOURCES[post.source] || {}).nameLabel) || post.source;
    meta.innerHTML =
      '<div><b>' + srcName + '</b> · ' +
      (post.stem || post.id) + '</div>' +
      '<div>分辨率 ' + res + ' · 分数 ' + (post.score != null ? post.score : '-') +
      ' · rating ' + (post.rating || '-') + '</div>' +
      '<div>状态 <b>' + st + '</b></div>';

    if (open) open.href = postPageUrl(post);
    if (dl) dl.textContent = (post.downloaded || _status[post.key] === 'done') ? '移除' : '下载';

    tagsEl.innerHTML = '';
    var rpt = (_SOURCES[post.source] || {}).renderPreviewTags;
    if (rpt) {
      rpt(tagsEl, post);
    } else {
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
    }

    box.classList.add('show');
  }

  function togglePreviewDownload() {
    if (!_previewPost) return;
    var post = _previewPost;
    hidePreview();
    onCardClick(post);
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
    var container = document.getElementById('masonry');
    var c = pickCol(up);
    card.style.position = 'absolute';
    card.style.left = (_pad + c * (_cw + _gap)) + 'px';
    card.style.width = _cw + 'px';
    container.appendChild(card);
    // 用真实渲染高度累加列高, 而非预估, 保证竖直空隙恒定 = _gap
    var rh = card.offsetHeight;
    var y = up ? (_colLo[c] - rh - _gap) : _colHi[c];
    card.style.top = (y - _minY) + 'px';
    card.dataset.worldY = String(y);
    if (up) _colLo[c] = y;
    else _colHi[c] = y + rh + _gap;
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
    if (up) {
      for (var i = fresh.length - 1; i >= 0; i--) {
        place(createCard(fresh[i], _cw), fresh[i], true);
      }
      syncOrigin();
    } else {
      for (var j = 0; j < fresh.length; j++) {
        place(createCard(fresh[j], _cw), fresh[j], false);
      }
      container.style.height = (Math.max.apply(null, _colHi) - _minY) + 'px';
    }
    observeNew(container);
    paintRemoteCounts();
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
      paintRemoteCounts();
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
      place(createCard(_posts[j], _cw), _posts[j], false);
    }
    container.style.height = (Math.max.apply(null, _colHi) - _minY) + 'px';
    observeNew(container);
    paintRemoteCounts();
    syncLoadTrigger();
    syncPageFromScroll();
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
      var t = await res.text();
      var err = new Error(t || res.statusText);
      err.status = res.status;
      err.retryAfter = parseInt(res.headers.get('Retry-After') || '', 10) || 0;
      _status[post.key] = 'error';
      updateCard(post.key);
      throw err;
    }
    var data = await res.json();
    _status[post.key] = 'done';
    post.downloaded = true;
    if (data.stem) post.stem = data.stem;
    updateCard(post.key);
    await refreshDiskCount();
    return data;
  }

  function setBatchDlg(show) {
    var dlg = document.getElementById('booru-batch-dlg');
    if (!dlg) return;
    dlg.classList.toggle('show', !!show);
    dlg.setAttribute('aria-hidden', show ? 'false' : 'true');
    if (!show && !_batchRunning) setBatchProgress(null);
    syncBatchCloseBtn();
    syncBatchStopBtn();
  }

  /** 空闲显示「关闭」，运行中显示「隐藏」。引用：setBatchDlg、applyBatchSnapshot。 */
  function syncBatchCloseBtn() {
    var btn = document.getElementById('booru-batch-close');
    if (!btn) return;
    btn.textContent = _batchRunning ? '隐藏' : '关闭';
  }

  function syncBatchStopBtn() {
    var btn = document.getElementById('booru-batch-stop');
    if (!btn) return;
    btn.hidden = !_batchRunning;
  }

  function syncBatchStartBtn() {
    var startBtn = document.getElementById('booru-batch-start');
    if (!startBtn) return;
    startBtn.disabled = !!_batchRunning;
    startBtn.textContent = _batchRunning ? '运行中' : '开始';
  }

  /**
   * 同步对话框内与顶栏两处批量进度。
   * @param {number|null} done 已完成数；null 表示隐藏顶栏并重置对话框进度
   * @param {number} [limit=0] 目标数量
   * @param {string} [note=''] 附加说明（如 429 退避、已完成）
   * 引用：applyBatchSnapshot。
   */
  function setBatchProgress(done, limit, note) {
    var dlgWrap = document.getElementById('booru-batch-progress');
    var dlgFill = document.getElementById('booru-batch-fill');
    var dlgPct = document.getElementById('booru-batch-pct');
    var dlgText = document.getElementById('booru-batch-progress-text');
    var bar = document.getElementById('booru-batch-bar');
    var barFill = document.getElementById('booru-batch-bar-fill');
    var barPct = document.getElementById('booru-batch-bar-pct');
    var barText = document.getElementById('booru-batch-bar-text');

    function paint(fill, pctEl, textEl, wrap, hideWrap) {
      if (done == null) {
        if (wrap && hideWrap) wrap.hidden = true;
        if (fill) {
          fill.style.width = '0%';
          fill.classList.remove('indeterminate');
        }
        if (pctEl) pctEl.textContent = '0%';
        if (textEl) textEl.textContent = '-';
        return;
      }
      if (wrap) wrap.hidden = false;
      var total = limit > 0 ? limit : 0;
      var pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
      if (fill) {
        fill.classList.toggle('indeterminate', total <= 0 && done === 0);
        fill.style.width = (total > 0 ? pct : 32) + '%';
      }
      if (pctEl) pctEl.textContent = total > 0 ? (pct + '%') : '…';
      var line = total > 0 ? ('已完成 ' + done + '/' + total) : '准备中…';
      if (note) line += ' · ' + note;
      if (textEl) textEl.textContent = line;
    }

    paint(dlgFill, dlgPct, dlgText, dlgWrap, true);
    paint(barFill, barPct, barText, bar, true);
  }

  /**
   * 应用服务端 JobSnapshot 到进度 UI。
   * @param {object} s
   * 引用：watchBatch、resumeBatch。
   */
  function applyBatchSnapshot(s) {
    var running = s.status === 'running' || s.status === 'queued';
    _batchRunning = running;
    syncBatchCloseBtn();
    syncBatchStopBtn();
    syncBatchStartBtn();
    var done = s.done || 0;
    var total = s.total || 0;
    var note = s.message || '';
    if (s.status === 'queued') {
      setBatchProgress(0, total, note || '等待执行');
      return;
    }
    if (running || s.status === 'done' || s.status === 'cancelled' || s.status === 'error') {
      setBatchProgress(done, total, note);
    }
    if (!running && s.status !== 'queued') {
      updatePageStatus();
    }
  }

  function stopBatchWatch() {
    if (_batchWatch) {
      _batchWatch.close();
      _batchWatch = null;
    }
  }

  /**
   * 订阅批量任务 SSE。
   * @param {string} jobId
   * 引用：runBatch、resumeBatch。
   */
  function watchBatch(jobId) {
    stopBatchWatch();
    _batchJobId = jobId;
    _batchRunning = true;
    syncBatchCloseBtn();
    syncBatchStopBtn();
    syncBatchStartBtn();
    bindBatchParamInputs(true);
    _batchWatch = API.watchJob(jobId, function (s) {
      applyBatchSnapshot(s);
      if (s.status === 'done') {
        toast('批量完成 ' + (s.done || 0) + (s.message ? ' · ' + s.message : ''));
        finishBatchUi();
      } else if (s.status === 'cancelled') {
        toast('批量已取消 · 完成 ' + (s.done || 0));
        finishBatchUi();
      } else if (s.status === 'error') {
        toast('批量失败' + (s.message ? ' · ' + s.message : ''));
        finishBatchUi();
      }
    });
  }

  function finishBatchUi() {
    stopBatchWatch();
    _batchRunning = false;
    _batchJobId = null;
    bindBatchParamInputs(false);
    syncBatchCloseBtn();
    syncBatchStopBtn();
    syncBatchStartBtn();
    setBatchProgress(null);
    updatePageStatus();
  }

  async function stopBatch() {
    if (!_batchJobId) {
      finishBatchUi();
      return;
    }
    await API.cancelJob(_batchJobId);
    toast('正在停止…');
  }

  /**
   * 从对话框读取并校正批量参数。
   * @returns {{limit:number, conn:number, rateRps:number}}
   * 引用：runBatch、pushBatchParams。
   */
  function readBatchParams() {
    var limitEl = document.getElementById('booru-batch-limit');
    var connEl = document.getElementById('booru-batch-conn');
    var rateEl = document.getElementById('booru-batch-rate');
    var limit = limitEl ? parseInt(limitEl.value, 10) : 50;
    var conn = connEl ? parseInt(connEl.value, 10) : 2;
    var rateRps = rateEl ? parseFloat(rateEl.value) : 0;
    if (!(limit > 0)) limit = 50;
    if (limit > 5000) limit = 5000;
    if (!(conn > 0)) conn = 1;
    if (conn > 8) conn = 8;
    if (!(rateRps > 0)) rateRps = 0;
    if (rateRps > 60) rateRps = 60;
    if (limitEl) limitEl.value = String(limit);
    if (connEl) connEl.value = String(conn);
    if (rateEl) rateEl.value = String(rateRps);
    return { limit: limit, conn: conn, rateRps: rateRps };
  }

  function pushBatchParams() {
    if (!_batchRunning || !_batchJobId) return;
    var p = readBatchParams();
    API.request(API.apiPath('/booru/batch/params'), {
      method: 'POST',
      body: JSON.stringify({
        limit: p.limit,
        conn: p.conn,
        rate_rps: p.rateRps,
      }),
    }).catch(function () {});
  }

  function onBatchParamInput() {
    if (!_batchRunning) return;
    clearTimeout(_batchParamTimer);
    _batchParamTimer = setTimeout(pushBatchParams, 200);
  }

  function bindBatchParamInputs(on) {
    var paramIds = ['booru-batch-limit', 'booru-batch-conn', 'booru-batch-rate'];
    for (var i = 0; i < paramIds.length; i++) {
      var el = document.getElementById(paramIds[i]);
      if (!el) continue;
      el.removeEventListener('input', onBatchParamInput);
      el.removeEventListener('change', onBatchParamInput);
      if (on) {
        el.addEventListener('input', onBatchParamInput);
        el.addEventListener('change', onBatchParamInput);
      }
    }
  }

  /**
   * 提交服务端批量下载并监听进度。
   * 引用：工具栏「批量下载」。
   */
  async function runBatch() {
    if (_batchRunning) {
      toast('批量下载进行中');
      return;
    }
    if (!isRemote()) {
      toast('请先切换到图站模式');
      return;
    }
    var params = readBatchParams();
    var req = cur().buildRequest();
    var tags = req.tags, extra = req.extra || '', order = req.order || '';
    if (cur().requireTags && !tags) {
      toast('请先输入 ' + cur().nameLabel + ' 的查询条件');
      return;
    }
    var body = {
      source: mode(),
      tags: tags,
      extra: extra,
      order: order,
      dir: destDir(),
      page: _pageLo > 0 ? _pageLo : 1,
      limit: params.limit,
      conn: params.conn,
      rate_rps: params.rateRps,
      anima_format: !!document.getElementById('booru-batch-anima')?.checked,
      include_meta_tags: !!document.getElementById('booru-batch-meta-tags')?.checked,
      keep_underscores: !!document.getElementById('booru-batch-keep-underscores')?.checked,
    };
    setBatchProgress(0, params.limit, '提交中…');
    _batchRunning = true;
    syncBatchCloseBtn();
    syncBatchStopBtn();
    syncBatchStartBtn();
    var data = await API.request(API.apiPath('/booru/batch/start'), {
      method: 'POST',
      body: JSON.stringify(body),
    });
    toast('已提交批量下载');
    watchBatch(data.job_id);
  }

  /**
   * 从 .danbooru_meta 批量重生成当前目录所有旁路 .txt。
   * 引用：批量下载对话框「重生成 Caption」按钮。
   */
  async function regenerateCaptions() {
    var body = {
      dir: destDir(),
      anima_format: !!document.getElementById('booru-batch-anima')?.checked,
      include_meta_tags: !!document.getElementById('booru-batch-meta-tags')?.checked,
      keep_underscores: !!document.getElementById('booru-batch-keep-underscores')?.checked,
    };
    var data = await API.request(API.apiPath('/booru/regenerate-captions'), {
      method: 'POST',
      body: JSON.stringify(body),
    });
    toast('已重生成 ' + (data.count || 0) + ' 个 caption');
  }

  /**
   * 根据文件 ID 重新拉取 booru 元数据（长任务，SSE 进度）。
   * 引用：批量下载对话框「刷新元数据」按钮。
   */
  async function refreshMetadata() {
    var body = { dir: destDir() };
    setBatchDlg(true);
    setBatchProgress(0, 0, '提交中…');
    _batchRunning = true;
    syncBatchCloseBtn();
    syncBatchStopBtn();
    syncBatchStartBtn();
    var submitted = await API.request(API.apiPath('/booru/refresh-metadata'), {
      method: 'POST',
      body: JSON.stringify(body),
    });
    watchBatch(submitted.job_id);
  }

  /**
   * 刷新后查询并重连进行中的批量任务。
   * 引用：init。
   */
  async function resumeBatch() {
    var data = await API.request(API.apiPath('/booru/batch/active'));
    if (!data || !data.job_id) return;
    if (data.status) applyBatchSnapshot(data.status);
    watchBatch(data.job_id);
    toast('已恢复批量下载进度');
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
    await refreshDiskCount();
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
    var dateRow = document.getElementById('booru-extra-date-row');
    if (dateRow) dateRow.hidden = mode() === 'gelbooru';
    // 去掉已保存的 date:（Gelbooru 无效）
    if (mode() === 'gelbooru') {
      var el = document.getElementById('booru-extra');
      if (el && el.value) {
        var next = buildExtra(parseExtra(el.value));
        if (next !== el.value) el.value = next;
      }
      syncExtraBtn();
    }
  }

  async function search(dir) {
    if (_loading) return;
    var isReset = dir === true || dir === 'reset';
    var isUp = dir === 'up';
    var req = cur().buildRequest();
    var tags = req.tags, extra = req.extra || '', order = req.order || '';
    if (cur().requireTags && !tags) {
      toast('请先输入 ' + cur().nameLabel + ' 的查询条件');
      return;
    }
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
      window.scrollTo(0, 0);
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
        // 首屏空结果停止；勿因 has_more 误报空转翻页
        if (!list.length) _hasMore = false;
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
          _hasPrev = false;
        } else {
          _hasMore = false;
        }
      }

      if (!autoNext && !autoPrev && (isReset || fresh.length || dup)) {
        var head = isReset
          ? (((_SOURCES[_source] || {}).nameLabel || '') + ' · +')
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
    stopBatch();
    hidePreview();
    syncToolbar();
    stopLazy();
    stopPageObserve();
    saveUiState();
  }

  function init() {
    var modeEl = document.getElementById('filter-mode');
    if (!modeEl) return;

    var saved = loadUiState();
    if (saved && (saved.mode === 'gelbooru' || saved.mode === 'danbooru' || saved.mode === 'bilibili' || saved.mode === 'local')) {
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

    // 依注册表绑定各来源工具栏: 搜索按钮 / 批量按钮 / Enter 触发
    for (var skey in _SOURCES) {
      (function (sdef) {
        if (!sdef.toolbarId) return;
        var sbtn = document.getElementById(sdef.searchBtn);
        if (sbtn) {
          sbtn.addEventListener('click', function () {
            search(true).catch(function (e) { toast(String(e.message || e)); });
          });
        }
        var bbtn = document.getElementById(sdef.batchBtn);
        if (bbtn) {
          bbtn.addEventListener('click', function () {
            if (!isRemote()) { toast('请先切换到图站模式'); return; }
            if (sdef.batchDialog) setBatchDlg(true);
            else runBatch().catch(function (e) { toast(String(e.message || e)); });
          });
        }
        if (sdef.enterIds) {
          for (var ei = 0; ei < sdef.enterIds.length; ei++) {
            var eel = document.getElementById(sdef.enterIds[ei]);
            if (!eel) continue;
            eel.addEventListener('keydown', function (ev) {
              if (ev.key === 'Enter') {
                ev.preventDefault();
                search(true).catch(function (err) { toast(String(err.message || err)); });
              }
            });
            eel.addEventListener('change', saveUiState);
          }
        }
      })(_SOURCES[skey]);
    }
    var batchDlg = document.getElementById('booru-batch-dlg');
    if (batchDlg) {
      batchDlg.addEventListener('click', function (e) {
        if (e.target === batchDlg) setBatchDlg(false);
      });
    }
    var batchStart = document.getElementById('booru-batch-start');
    if (batchStart) {
      batchStart.addEventListener('click', function () {
        runBatch().catch(function (e) {
          finishBatchUi();
          setBatchProgress(null);
          toast(String(e.message || e));
        });
      });
    }
    var batchClose = document.getElementById('booru-batch-close');
    if (batchClose) {
      batchClose.addEventListener('click', function () {
        setBatchDlg(false);
      });
    }
    var batchStop = document.getElementById('booru-batch-stop');
    if (batchStop) {
      batchStop.addEventListener('click', function () {
        stopBatch().catch(function (e) { toast(String(e.message || e)); });
      });
    }
    var batchRegen = document.getElementById('booru-batch-regenerate');
    if (batchRegen) {
      batchRegen.addEventListener('click', function () {
        regenerateCaptions().catch(function (e) { toast(String(e.message || e)); });
      });
    }
    var batchRefreshMeta = document.getElementById('booru-batch-refresh-meta');
    if (batchRefreshMeta) {
      batchRefreshMeta.addEventListener('click', function () {
        refreshMetadata().catch(function (e) { toast(String(e.message || e)); });
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
    var previewOpen = document.getElementById('booru-preview-open');
    if (previewOpen) previewOpen.addEventListener('click', hidePreview);
    var previewDl = document.getElementById('booru-preview-dl');
    if (previewDl) previewDl.addEventListener('click', togglePreviewDownload);

    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      var batchDlgEl = document.getElementById('booru-batch-dlg');
      if (batchDlgEl && batchDlgEl.classList.contains('show')) {
        setBatchDlg(false);
        return;
      }
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

    resumeBatch().catch(function () {});
  }

  return {
    init: init,
    isRemote: isRemote,
    render: render,
    mode: mode,
    currentPage: currentPage,
    scrollToPage: scrollToPage,
    refreshDiskCount: refreshDiskCount,
  };
})();