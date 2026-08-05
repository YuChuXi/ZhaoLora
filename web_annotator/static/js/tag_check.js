/**
 * tag_check.js —— 标签检查页：瀑布流展示标签存在性与位置，点击卡片添加标签。
 *
 * 依赖: api.js / dir_picker.js / nav.js / masonry.js
 */
(function () {
  'use strict';

  var _items = [];
  var _toastTimer = null;
  var _colsKey = 'zhao_tagcheck_cols';
  var _manualCols = 0;
  var _singleIndex = -1;
  var _fitMode = 'contain';
  var _busyPaths = {};
  var _centerOnLoad = false;  // 进入单图/切换 fit 时置 true, img.load 时居中滚动, 切换图片不置 (保持偏移)
  var _strip = null;          // 横排预览条控制器(Filmstrip)

  // ---- 标签补全 ----
  var _acTags = [];          // [{tag, count, zh}] 按频次降序
  var _acPopup = null;       // 下拉 DOM
  var _acInput = null;       // 输入框 DOM
  var _acItems = [];         // 当前可见建议 DOM 列表
  var _acIndex = -1;         // 当前高亮索引
  var _acDebounce = null;    // 输入防抖计时器
  var _acReqId = 0;          // 统计请求序号(丢弃过期响应)
  var _AC_LIMIT = 60;        // 最多显示建议数

  /**
   * 显示短暂提示。
   * @param {string} msg
   */
  function toast(msg) {
    var el = document.getElementById('toast');
    if (!el) return;
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () { el.classList.remove('show'); }, 2200);
  }

  /**
   * 更新工具栏统计。
   */
  function updateStats() {
    var total = _items.length;
    var ok = 0;
    for (var i = 0; i < _items.length; i++) {
      if (_items[i].position_ok) ok++;
    }
    document.getElementById('stat-total').textContent = total;
    document.getElementById('stat-ok').textContent = ok;
    document.getElementById('stat-bad').textContent = total - ok;
  }

  /**
   * 为卡片追加 tag-ok / tag-bad 类并设置 caption tooltip。
   * - tag-ok: 标签存在且位置符合
   * - tag-bad: 标签存在但位置不符（不存在则不标色）
   * @param {HTMLElement} card
   * @param {object} img
   */
  function decorateCard(card, img) {
    card.classList.remove('tag-ok', 'tag-bad');
    card.dataset.tagExists = '0';
    if (img.exists && img.position_ok) {
      card.classList.add('tag-ok');
      card.dataset.tagExists = '1';
    } else if (img.exists && !img.position_ok) {
      card.classList.add('tag-bad');
      card.dataset.tagExists = '1';
    }
    if (img.caption_preview) {
      card.title = img.caption_preview;
    } else {
      card.title = '(无 .txt)';
    }
  }

  /**
   * 从双下拉框计算检查用的位置值（position + mode → check_tag_in_caption 的 position）。
   * @returns {string} "any" | "top" | "bottom" | "before-sep" | "after-sep" | "bottom-before"
   */
  function getCheckPosition() {
    var sep = document.getElementById('check-position').value;
    var mode = document.getElementById('check-mode').value;
    if (mode === 'random') {
      if (sep === 'before') return 'before-sep';
      if (sep === 'after') return 'after-sep';
      return 'any';
    }
    if (sep === 'any') return mode === 'top' ? 'top' : 'bottom';
    if (sep === 'before') return mode === 'top' ? 'top' : 'bottom-before';
    return mode === 'top' ? 'after-sep' : 'bottom';
  }

  /**
   * 从双下拉框计算插入用的位置值（position + mode → add_tag_to_caption 的 position）。
   * @returns {string} "top" | "bottom" | "random" | "before-sep" | "after-sep" | "random-before" | "random-after"
   */
  function getInsertPosition() {
    var sep = document.getElementById('check-position').value;
    var mode = document.getElementById('check-mode').value;
    if (sep === 'any') {
      if (mode === 'top') return 'top';
      if (mode === 'bottom') return 'bottom';
      return 'random';
    }
    if (mode === 'random') return sep === 'before' ? 'random-before' : 'random-after';
    if (sep === 'before') return mode === 'top' ? 'top' : 'before-sep';
    return mode === 'top' ? 'after-sep' : 'bottom';
  }

  /**
   * 点击卡片：标签存在则删除, 不存在则按当前位置模式添加。
   *
   * 使用 card.dataset.tagExists 判断当前标签状态, 避免闭包捕获的
   * 旧 img 引用与 _items 更新不同步导致的重复点击行为错乱。
   *
   * @param {object} img  仅用于 path 定位
   * @param {HTMLElement} card
   */
  function onCardClick(img, card) {
    var tag = document.getElementById('check-tag').value.trim();
    if (!tag) { toast('请先输入标签'); return; }
    if (_busyPaths[img.path]) return;

    var exists = card.dataset.tagExists === '1';

    card.classList.add('jump-flash');
    _busyPaths[img.path] = true;

    var endpoint, body;
    if (exists) {
      endpoint = API.apiPath('/tags/delete-one');
      body = JSON.stringify({ path: img.path, tag: tag });
    } else {
      endpoint = API.apiPath('/tags/edit-one');
      body = JSON.stringify({ path: img.path, tag: tag, position: getInsertPosition() });
    }

    API.request(endpoint, { method: 'POST', body: body }).then(function (updated) {
      for (var j = 0; j < _items.length; j++) {
        if (_items[j].path === img.path) { _items[j] = updated; break; }
      }
      decorateCard(card, updated);
      updateStats();
    }).catch(function (e) {
      toast('操作失败: ' + String(e.message || e));
    }).finally(function () {
      card.classList.remove('jump-flash');
      delete _busyPaths[img.path];
    });
  }

  /**
   * 调用 /tags/check 加载检查结果并渲染。
   * @returns {Promise<void>}
   */
  async function runCheck() {
    var tag = document.getElementById('check-tag').value.trim();
    var dirs = DirPicker.getDirs();
    var position = getCheckPosition();

    var btn = document.getElementById('btn-check');
    btn.disabled = true;
    try {
      var res = await API.request(API.apiPath('/tags/check'), {
        method: 'POST',
        body: JSON.stringify({ dirs: dirs, tag: tag, position: position }),
      });
      _items = res.items || [];
      if (_strip) _strip.refresh();
      hideSingle();
      Masonry.render(_items);
      updateStats();
    } finally {
      btn.disabled = false;
    }
  }

  /**
   * 按视口宽度自动选取列数。
   * @returns {number}
   */
  function autoColumns() {
    var w = window.innerWidth;
    if (w >= 1600) return 6;
    if (w >= 1280) return 5;
    if (w >= 960) return 4;
    if (w >= 640) return 3;
    return 2;
  }

  // ==================== 标签补全 ====================

  /**
   * 归一化标签用于匹配：小写 + 空格转下划线。
   * @param {string} s
   * @returns {string}
   */
  function normTag(s) {
    return s.toLowerCase().replace(/\s+/g, '_');
  }

  /**
   * 从缓存的标签列表中模糊匹配（子串匹配，按匹配位置与频次排序）。
   * @param {string} query 用户输入
   * @returns {Array<{tag:string,count:number,zh:string,pos:number}>}
   */
  function matchTags(query) {
    var q = normTag(query.trim());
    if (!q) return _acTags.slice(0, _AC_LIMIT);
    var matched = [];
    for (var i = 0; i < _acTags.length; i++) {
      var t = _acTags[i];
      var idx = normTag(t.tag).indexOf(q);
      if (idx >= 0) matched.push({ tag: t.tag, count: t.count, zh: t.zh, pos: idx });
    }
    matched.sort(function (a, b) {
      if (a.pos !== b.pos) return a.pos - b.pos;
      return b.count - a.count;
    });
    return matched.slice(0, _AC_LIMIT);
  }

  /**
   * 转义 HTML 特殊字符。
   * @param {string} s
   * @returns {string}
   */
  function escHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
  }

  /**
   * 渲染补全下拉列表。
   * @param {Array} matches matchTags 返回值
   */
  function acRender(matches) {
    _acIndex = matches.length > 0 ? 0 : -1;
    var q = normTag(_acInput.value.trim());
    if (matches.length === 0) {
      _acPopup.innerHTML = '<div class="ac-empty">无匹配标签</div>';
      _acItems = [];
      _acPopup.hidden = false;
      return;
    }
    var html = '';
    for (var i = 0; i < matches.length; i++) {
      var m = matches[i];
      var tag = m.tag;
      // 高亮匹配子串
      var hi = escHtml(tag);
      if (q) {
        var ni = normTag(tag);
        var idx = ni.indexOf(q);
        if (idx >= 0) {
          hi = escHtml(tag.substring(0, idx)) +
               '<span class="ac-match">' + escHtml(tag.substring(idx, idx + q.length)) + '</span>' +
               escHtml(tag.substring(idx + q.length));
        }
      }
      html += '<div class="ac-item' + (i === 0 ? ' ac-active' : '') + '" data-tag="' + escHtml(tag) + '">';
      html += '<span class="ac-tag">' + hi + '</span>';
      if (m.zh) html += '<span class="ac-zh">' + escHtml(m.zh) + '</span>';
      html += '<span class="ac-count">' + m.count + '</span>';
      html += '</div>';
    }
    _acPopup.innerHTML = html;
    _acPopup.hidden = false;
    _acItems = _acPopup.querySelectorAll('.ac-item');
    // mousedown 而非 click：在 blur 前触发，preventDefault 阻止输入框失焦
    for (var j = 0; j < _acItems.length; j++) {
      _acItems[j].addEventListener('mousedown', function (e) {
        e.preventDefault();
        acSelect(this.dataset.tag);
      });
    }
  }

  /** 关闭补全下拉。 */
  function acClose() {
    if (_acPopup) _acPopup.hidden = true;
    _acIndex = -1;
    _acItems = [];
  }

  /**
   * 选中一个标签填入输入框并关闭下拉。
   * @param {string} tag
   */
  function acSelect(tag) {
    _acInput.value = tag;
    acClose();
    _acInput.focus();
  }

  /**
   * 移动高亮项并滚动至可见。
   * @param {number} delta +1=下移, -1=上移
   */
  function acMove(delta) {
    if (_acItems.length === 0) return;
    _acIndex = (_acIndex + delta + _acItems.length) % _acItems.length;
    for (var i = 0; i < _acItems.length; i++) {
      _acItems[i].classList.toggle('ac-active', i === _acIndex);
    }
    var active = _acItems[_acIndex];
    if (!active) return;
    var pTop = _acPopup.scrollTop;
    var pBottom = pTop + _acPopup.clientHeight;
    var eTop = active.offsetTop;
    var eBottom = eTop + active.offsetHeight;
    if (eBottom > pBottom) _acPopup.scrollTop = eBottom - _acPopup.clientHeight;
    else if (eTop < pTop) _acPopup.scrollTop = eTop;
  }

  /**
   * 初始化补全组件：包裹输入框、创建下拉、绑定事件。
   */
  function acInit() {
    _acInput = document.getElementById('check-tag');
    // 用 .ac-wrap 包裹输入框以提供定位上下文
    var wrap = document.createElement('div');
    wrap.className = 'ac-wrap';
    _acInput.parentNode.insertBefore(wrap, _acInput);
    wrap.appendChild(_acInput);
    _acPopup = document.createElement('div');
    _acPopup.className = 'ac-popup';
    _acPopup.id = 'ac-tags';
    _acPopup.hidden = true;
    wrap.appendChild(_acPopup);

    // 输入事件：防抖后匹配并渲染
    _acInput.addEventListener('input', function () {
      clearTimeout(_acDebounce);
      _acDebounce = setTimeout(function () {
        if (_acTags.length === 0) return;
        acRender(matchTags(_acInput.value));
      }, 120);
    });

    // 失焦：延迟关闭(让 mousedown 先触发)
    _acInput.addEventListener('blur', function () {
      setTimeout(acClose, 150);
    });

    // 键盘导航与确认
    _acInput.addEventListener('keydown', function (e) {
      var open = !_acPopup.hidden;
      if (e.key === 'ArrowDown') {
        if (open) { e.preventDefault(); acMove(1); }
      } else if (e.key === 'ArrowUp') {
        if (open) { e.preventDefault(); acMove(-1); }
      } else if (e.key === 'Escape') {
        if (open) { e.preventDefault(); acClose(); }
      } else if (e.key === 'Tab') {
        if (open && _acIndex >= 0 && _acItems[_acIndex]) {
          e.preventDefault();
          acSelect(_acItems[_acIndex].dataset.tag);
        }
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (open && _acIndex >= 0 && _acItems[_acIndex]) {
          acSelect(_acItems[_acIndex].dataset.tag);
        } else {
          acClose();
        }
        runCheck().catch(function (err) { toast('检查失败: ' + String(err.message || err)); });
      }
    });
  }

  /**
   * 后台加载所选目录的标签统计作为补全数据源。
   * @param {Array<string>} dirs
   */
  function loadTags(dirs) {
    var id = ++_acReqId;
    API.request(API.apiPath('/tags/stats'), {
      method: 'POST',
      body: JSON.stringify({ dirs: dirs, min_count: 1, per_sample: true }),
    }).then(function (res) {
      if (id !== _acReqId) return; // 已过期
      _acTags = (res.tags || []).map(function (t) {
        return { tag: t.tag, count: t.count, zh: t.zh || '' };
      });
    }).catch(function () {
      // 统计加载失败时不阻塞检查功能，补全不可用
      _acTags = [];
    });
  }

  // ==================== 单图模式 ====================

  /**
   * 对单图执行添加/删除标签操作。
   * @param {string} action "add" 强制添加(位置由下拉框决定), "delete" 强制删除
   */
  function singleTagOp(action) {
    var idx = _singleIndex;
    if (idx < 0 || idx >= _items.length) return;
    var tag = document.getElementById('check-tag').value.trim();
    if (!tag) { toast('请先输入标签'); return; }

    var img = _items[idx];
    var endpoint, body;
    if (action === 'delete') {
      endpoint = API.apiPath('/tags/delete-one');
      body = JSON.stringify({ path: img.path, tag: tag });
    } else {
      endpoint = API.apiPath('/tags/edit-one');
      body = JSON.stringify({ path: img.path, tag: tag, position: getInsertPosition() });
    }

    API.request(endpoint, { method: 'POST', body: body }).then(function (updated) {
      _items[idx] = updated;
      updateStats();
      navigateSingle(1);  // 操作成功后自动跳到下一张
    }).catch(function (e) {
      toast('操作失败: ' + String(e.message || e));
    });
  }

  /**
   * 渲染当前单图（图片、stem、caption、着色边框、状态栏）。
   *
   * 状态栏与 paint / crop 页统一为 seg-* 六段：
   * - seg-pos    : 当前位置 x/N
   * - seg-done   : 位置符合的图片数（进度统计）
   * - seg-name   : 文件名
   * - seg-obj    : 标签状态（无此标签 / 位置符合 / 位置不符）
   * - seg-metric : 图片尺寸
   * - seg-extra  : 标签文本（悬停看完整）
   */
  function renderSingle() {
    var idx = _singleIndex;
    if (idx < 0 || idx >= _items.length) return;
    var img = _items[idx];
    var imgEl = document.getElementById('single-img');
    imgEl.src = API.imageUrl(img.path);
    // 位置段
    document.getElementById('seg-pos').textContent = (idx + 1) + '/' + _items.length;
    // 进度段: 位置符合的图片数
    var ok = 0;
    for (var i = 0; i < _items.length; i++) if (_items[i].position_ok) ok++;
    document.getElementById('seg-done').textContent = '符合 ' + ok;
    // 文件名段
    var stemEl = document.getElementById('seg-name');
    stemEl.textContent = img.stem;
    stemEl.title = img.path;
    // 目标状态段 (标签状态)
    var objEl = document.getElementById('seg-obj');
    if (!img.exists) {
      objEl.textContent = '无此标签';
    } else if (img.position_ok) {
      objEl.textContent = '位置符合';
    } else {
      objEl.textContent = '位置不符';
    }
    // 尺寸段
    document.getElementById('seg-metric').textContent =
      (img.width && img.height) ? (img.width + '×' + img.height) : '-';
    // 额外段: 标签文本, 有内容则显示并设 title 悬停看完整, 无内容显示 -
    var capEl = document.getElementById('seg-extra');
    var cap = img.caption_preview || '';
    capEl.textContent = cap || '-';
    capEl.title = cap;

    // 位置着色 (放大镜光晕与 stage 内阴影据此变色)
    var view = document.getElementById('view-single');
    view.classList.remove('single-ok', 'single-bad');
    if (img.exists && img.position_ok) {
      view.classList.add('single-ok');
    } else if (img.exists && !img.position_ok) {
      view.classList.add('single-bad');
    }
    applyFitMode();
    if (_strip) _strip.sync();
  }

  /**
   * 进入单图视图，展示第 index 张。
   * @param {number} index
   */
  function showSingle(index) {
    if (index < 0 || index >= _items.length) return;
    _singleIndex = index;
    _centerOnLoad = true;  // 进入单图: 首张居中显示
    document.body.classList.add('mode-single');
    document.getElementById('view-multi').hidden = true;
    document.getElementById('view-single').hidden = false;
    renderSingle();
  }

  /** 退出单图视图，返回瀑布流。 */
  function hideSingle() {
    _singleIndex = -1;
    if (_strip) _strip.close();
    document.body.classList.remove('mode-single');
    document.getElementById('view-single').hidden = true;
    document.getElementById('view-multi').hidden = false;
    document.getElementById('single-img').removeAttribute('src');
    document.getElementById('magnifier').classList.remove('show');
  }

  /** 切换 contain / cover。切到 cover 时标记需居中。 */
  function toggleFit() {
    _fitMode = _fitMode === 'contain' ? 'cover' : 'contain';
    if (_fitMode === 'cover') _centerOnLoad = true;
    applyFitMode();
  }

  /**
   * 应用 contain / cover 模式 (仅切换 stage class, 由 CSS 控制图片尺寸与滚动)。
   *
   * Callers:
   * - `tag_check.js:renderSingle`: 渲染单图时调用。
   * - `tag_check.js:toggleFit`: 点击 fit 按钮时调用。
   */
  function applyFitMode() {
    var stage = document.getElementById('single-stage');
    var btn = document.getElementById('btn-fit');
    stage.classList.remove('contain', 'cover');
    stage.classList.add(_fitMode);
    btn.textContent = _fitMode === 'cover' ? '⊟' : '⊞';
  }

  /** 导航：delta = +1(下一张) / -1(上一张)。 */
  function navigateSingle(delta) {
    var n = _singleIndex + delta;
    if (n < 0) n = _items.length - 1;
    if (n >= _items.length) n = 0;
    _singleIndex = n;
    renderSingle();
  }

  // ---- 放大镜 ----

  /**
   * 初始化跟随鼠标的圆形放大镜。
   *
   * 监听 stage 的 mousemove, 鼠标位于图片 object-fit 实际显示区域内时显示放大镜;
   * 放大镜用内部 img 元素显示放大区域 (尺寸=实际显示区域×zoom, 偏移使鼠标点居中)。
   * 位置默认在鼠标上方, 顶部不足翻下方, 并约束在视口内不脱离屏幕。
   *
   * Callers:
   * - `tag_check.js:init`: 初始化时调用一次。
   */
  function magnifierInit() {
    var stage = document.getElementById('single-stage');
    var imgEl = document.getElementById('single-img');
    var lens = document.getElementById('magnifier');
    var lensImg = document.getElementById('magnifier-img');
    var LENS = 180;      // 放大镜直径 (与 CSS width/height 一致)
    var ZOOM = 2;        // 放大倍数
    var lastE = null;    // 最近一次 mousemove 事件, 供 scroll 时重算

    // 计算图片实际显示矩形 (相对视口): cover=图片元素直接; contain=object-fit 居中偏移
    function fitRect() {
      var r = imgEl.getBoundingClientRect();
      if (_fitMode === 'cover') return { left: r.left, top: r.top, width: r.width, height: r.height };
      var iw = imgEl.naturalWidth, ih = imgEl.naturalHeight;
      if (!iw || !ih) return null;
      var cw = r.width, ch = r.height;
      var scale = Math.min(cw / iw, ch / ih);
      var dw = iw * scale, dh = ih * scale;
      return { left: r.left + (cw - dw) / 2, top: r.top + (ch - dh) / 2, width: dw, height: dh };
    }

    function update() {
      if (!lastE) return;
      var e = lastE;
      var fr = fitRect();
      if (!fr) { lens.classList.remove('show'); return; }
      var x = e.clientX - fr.left;
      var y = e.clientY - fr.top;
      if (x < 0 || y < 0 || x > fr.width || y > fr.height) {
        lens.classList.remove('show');
        return;
      }
      lens.classList.add('show');
      // 放大镜位置: 默认鼠标上方 16px, 顶部不足翻下方; 约束在视口内
      var lx = e.clientX - LENS / 2;
      var ly = e.clientY - LENS - 16;
      if (ly < 0) ly = e.clientY + 16;
      if (lx < 0) lx = 0;
      if (lx + LENS > window.innerWidth) lx = window.innerWidth - LENS;
      if (ly + LENS > window.innerHeight) ly = window.innerHeight - LENS;
      lens.style.left = lx + 'px';
      lens.style.top = ly + 'px';
      // 放大图: 尺寸=实际显示区域×zoom, 偏移使鼠标点位于放大镜中心
      lensImg.src = imgEl.src;
      lensImg.style.width = (fr.width * ZOOM) + 'px';
      lensImg.style.height = (fr.height * ZOOM) + 'px';
      lensImg.style.left = (-x * ZOOM + LENS / 2) + 'px';
      lensImg.style.top = (-y * ZOOM + LENS / 2) + 'px';
    }

    stage.addEventListener('mousemove', function (e) { lastE = e; update(); });
    stage.addEventListener('mouseleave', function () { lens.classList.remove('show'); lastE = null; });
    // cover 模式 stage 可滚动, 滚动时图片视口位置变化需重算放大镜
    stage.addEventListener('scroll', update);
  }

  function init() {
    Masonry.init('masonry', {
      getData: function () { return _items; },
      onClick: onCardClick,
      decorate: decorateCard,
    });
    acInit();
    magnifierInit();
    // 横排预览条: 点击第一段(位置)展开, 点击缩略图跳转
    _strip = Filmstrip.create({
      container: document.getElementById('filmstrip'),
      getItems: function () { return _items; },
      thumbSrc: function (it) {
        return it.thumbnail_hash ? API.thumbnailUrl(it.thumbnail_hash) : API.imageUrl(it.path);
      },
      getActive: function () { return _singleIndex; },
      onSelect: function (i) {
        _singleIndex = i;
        renderSingle();
      },
    });

    var colsSlider = document.getElementById('cols-slider');
    var colsLabel = document.getElementById('cols-label');

    function applyCols(v) {
      _manualCols = v;
      if (colsSlider) colsSlider.value = String(v);
      if (v === 0) {
        if (colsLabel) colsLabel.textContent = '自动';
        Masonry.setColumns(autoColumns());
      } else {
        if (colsLabel) colsLabel.textContent = String(v);
        Masonry.setColumns(v);
      }
    }

    var saved = 0;
    try { saved = parseInt(localStorage.getItem(_colsKey), 10); } catch (e) { saved = 0; }
    if (!(saved >= 0 && saved <= 16)) saved = 0;
    applyCols(saved);

    if (colsSlider) {
      colsSlider.addEventListener('input', function () {
        var v = parseInt(this.value, 10);
        if (isNaN(v) || v < 0) v = 0;
        try { localStorage.setItem(_colsKey, String(v)); } catch (e) {}
        applyCols(v);
      });
    }
    window.addEventListener('resize', function () {
      if (_manualCols === 0) Masonry.setColumns(autoColumns());
    });

    document.getElementById('btn-check').addEventListener('click', function () {
      runCheck().catch(function (e) { toast('检查失败: ' + String(e.message || e)); });
    });
    document.getElementById('check-position').addEventListener('change', function () {
      runCheck().catch(function (e) { toast('检查失败: ' + String(e.message || e)); });
    });

    // ---- 单图模式事件 ----
    // 工具栏按钮: 打开单图(从第一张或当前选中张开始). 模态浮层会覆盖主工具栏, 关闭由模态内的 ✕ 或 Esc 负责.
    document.getElementById('btn-single-mode').addEventListener('click', function () {
      if (_singleIndex >= 0) return;
      showSingle(0);
    });

    // 瀑布流卡片右键: 进入该卡片的单图模式 (左键仍为添加/删除标签)
    document.getElementById('masonry').addEventListener('contextmenu', function (e) {
      var card = e.target.closest('.card');
      if (!card) return;
      e.preventDefault();
      var path = card.dataset.path;
      for (var i = 0; i < _items.length; i++) {
        if (_items[i].path === path) { showSingle(i); return; }
      }
    });

    document.getElementById('btn-prev').addEventListener('click', function () { navigateSingle(-1); });
    document.getElementById('btn-next').addEventListener('click', function () { navigateSingle(1); });
    document.getElementById('btn-fit').addEventListener('click', toggleFit);
    document.getElementById('btn-single-close').addEventListener('click', hideSingle);
    // 点击第一段(位置)切换横排预览条
    document.getElementById('seg-pos').addEventListener('click', function () { _strip.toggle(); });

    // 单图点击：左键添加, 右键删除
    document.getElementById('single-img').addEventListener('click', function () {
      singleTagOp('add');
    });
    document.getElementById('single-img').addEventListener('contextmenu', function (e) {
      e.preventDefault();
      singleTagOp('delete');
    });

    // 单图键盘导航
    document.addEventListener('keydown', function (e) {
      if (_singleIndex < 0) return;
      if (e.key === 'ArrowLeft') { e.preventDefault(); navigateSingle(-1); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); navigateSingle(1); }
      else if (e.key === 'Escape') { e.preventDefault(); hideSingle(); }
    });

    // cover 模式图片加载后居中显示溢出区域 (进入单图/切换 fit 时触发; 左右键切换不触发, 保持滚动偏移)
    document.getElementById('single-img').addEventListener('load', function () {
      if (_fitMode !== 'cover' || !_centerOnLoad) return;
      var stage = document.getElementById('single-stage');
      stage.scrollLeft = (stage.scrollWidth - stage.clientWidth) / 2;
      stage.scrollTop = (stage.scrollHeight - stage.clientHeight) / 2;
      _centerOnLoad = false;
    });

    DirPicker.onChange(function () {
      loadTags(DirPicker.getDirs());
      runCheck().catch(function (e) { toast('检查失败: ' + String(e.message || e)); });
    });

    // 初始加载全部图片
    runCheck().catch(function (e) { toast('加载失败: ' + String(e.message || e)); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
