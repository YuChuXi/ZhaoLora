/**
 * tag_check.js —— 标签检查页：瀑布流展示标签存在性与位置，点击卡片添加标签。
 *
 * 依赖: api.js / dir_picker.js / nav.js / masonry.js
 */
(function () {
  'use strict';

  var _items = [];
  var _manualCols = 0;
  var _singleIndex = -1;
  var _fitMode = 'contain';
  var _busyPaths = {};
  var _centerOnLoad = false;  // 进入单图/切换 fit 时置 true, img.load 时居中滚动, 切换图片不置 (保持偏移)
  var _strip = null;          // 横排预览条控制器(Filmstrip)
  var _applyMask = false;     // 瀑布图是否应用蒙板显示(去背 + 棋盘格背景)

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
   * 卡片图片 URL 构造: 开启"应用蒙板"时改用后端合成图(棋盘格底 + 蒙板抠图, AVIF).
   *
   * @param {object} img
   * @returns {string}
   */
  function imageSrc(img) {
    if (_applyMask) {
      return API.apiPath('/paint/mask-preview') + '?path=' + encodeURIComponent(img.path);
    }
    return API.thumbnailUrl(img.thumbnail_hash);
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
    // 相似度 S 前缀 / 掩码占比 R 前缀 (与 A / Q 指标同栏); 仅 null 表示无对应值
    var nameEl = card.querySelector('.name');
    if (nameEl && img._sim != null) {
      nameEl.textContent = 'S' + img._sim.toFixed(3) + ' ' + nameEl.textContent;
    }
    if (nameEl && img._ratio != null) {
      nameEl.textContent = 'R' + img._ratio.toFixed(3) + ' ' + nameEl.textContent;
    }
    // 概念分割掩码叠加: 命中概念时叠加半透明 PNG, 覆盖在图片区
    var overlay = card.querySelector('.mask-overlay');
    if (overlay) overlay.remove();
    if (img._mask) {
      var mo = document.createElement('img');
      mo.className = 'mask-overlay';
      mo.src = img._mask;
      mo.alt = '';
      card.insertBefore(mo, card.querySelector('.info'));
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
   * 任意模式追加到所选区域末尾: 任意位置→全局末尾, ||| 前→头部末尾, ||| 后→正文末尾。
   * @returns {string} "top" | "bottom" | "before-sep" | "after-sep"
   */
  function getInsertPosition() {
    var sep = document.getElementById('check-position').value;
    var mode = document.getElementById('check-mode').value;
    if (mode === 'random') return sep === 'before' ? 'before-sep' : 'bottom';
    if (sep === 'any') return mode === 'top' ? 'top' : 'bottom';
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
      body = JSON.stringify({
        path: img.path, tag: tag,
        position: getInsertPosition(), check_position: getCheckPosition(),
      });
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
   * @param {boolean} [preserveSort] 为 true 时保留既有顺序与排序指标, 仅原位更新
   *   各图片的标签状态 (exists/position_ok/caption_preview); 语义排序后批量改标签用它.
   * @returns {Promise<void>}
   */
  async function runCheck(preserveSort) {
    var tag = document.getElementById('check-tag').value.trim();
    var dirs = DirPicker.getDirs();
    var position = getCheckPosition();

    if (!preserveSort) _embSortActive = false;
    var btn = document.getElementById('btn-check');
    btn.disabled = true;
    try {
      var res = await API.request(API.apiPath('/tags/check'), {
        method: 'POST',
        body: JSON.stringify({ dirs: dirs, tag: tag, position: position }),
      });
      if (preserveSort) {
        // 仅按 path 原位更新状态, 不动顺序与 _sim/_ratio/_mask
        var byPath = {};
        (res.items || []).forEach(function (it) { byPath[it.path] = it; });
        for (var i = 0; i < _items.length; i++) {
          var it = byPath[_items[i].path];
          if (!it) continue;
          _items[i].exists = it.exists;
          _items[i].position_ok = it.position_ok;
          _items[i].caption_preview = it.caption_preview;
        }
      } else {
        _items = res.items || [];
      }
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
        // 语义排序状态下保留排序, 仅刷新标签状态
        runCheck(_embSortActive).catch(function (err) { toast('检查失败: ' + String(err.message || err)); });
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
      body = JSON.stringify({
        path: img.path, tag: tag,
        position: getInsertPosition(), check_position: getCheckPosition(),
      });
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

  /** 切帧：delta = +1(下一张) / -1(上一张)。 */
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
   * 放大镜以图像原始分辨率为基准 (尺寸=naturalWidth×zoom, 最邻近插值),
   * 偏移使鼠标点居中。位置默认在鼠标上方, 顶部不足翻下方, 并约束在视口内。
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
    var ZOOM = 4;        // 当前放大倍数 (相对原始分辨率)
    var ZOOM_MIN = 1, ZOOM_MAX = 32;
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
      // 放大图: 以原始分辨率为基准, 鼠标点映射到原始坐标再×zoom居中
      var iw = imgEl.naturalWidth, ih = imgEl.naturalHeight;
      var origX = x / fr.width * iw;
      var origY = y / fr.height * ih;
      lensImg.src = imgEl.src;
      lensImg.style.width = (iw * ZOOM) + 'px';
      lensImg.style.height = (ih * ZOOM) + 'px';
      lensImg.style.left = (-origX * ZOOM + LENS / 2) + 'px';
      lensImg.style.top = (-origY * ZOOM + LENS / 2) + 'px';
    }

    stage.addEventListener('mousemove', function (e) { lastE = e; update(); });
    stage.addEventListener('mouseleave', function () { lens.classList.remove('show'); lastE = null; });
    // cover 模式 stage 可滚动, 滚动时图片视口位置变化需重算放大镜
    stage.addEventListener('scroll', update);
    // 滚轮调整放大倍数: 放大镜激活时拦截滚轮, 否则不干扰 cover 模式滚动
    stage.addEventListener('wheel', function (e) {
      if (!lens.classList.contains('show')) return;
      e.preventDefault();
      var delta = e.deltaY < 0 ? 1.25 : 0.8;
      ZOOM = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, ZOOM * delta));
      update();
    }, { passive: false });
  }

  // ---- 语义检索 ----

  var _embSortActive = false;  // 是否处于排序模式
  var _embEngine = 'similarity';  // 当前引擎: similarity(相似度) | concept(概念分割)

  /**
   * 当前引擎选择。
   * @returns {string}
   */
  function embEngine() {
    var el = document.getElementById('emb-engine');
    return el ? el.value : 'similarity';
  }

  /**
   * 引擎切换: 更新相关控件文案; 引擎真正变化时清空既有排序状态。
   * @param {boolean} [reset] 是否重置排序状态; 打开对话框仅同步文案时传 false。
   */
  function embSetEngine(reset) {
    var engine = embEngine();
    var sortBtn = document.getElementById('emb-sort');
    var metric = document.getElementById('emb-metric-label');
    var qLabel = document.getElementById('emb-query-label');
    var q = document.getElementById('emb-query');
    if (sortBtn) sortBtn.textContent = engine === 'concept' ? '按掩码占比排序' : '按相似度排序';
    if (metric) metric.textContent = engine === 'concept' ? '掩码占比' : '相似度';
    if (qLabel) qLabel.textContent = engine === 'concept' ? '概念提示词' : '查询文本';
    if (q) q.placeholder = engine === 'concept' ? '概念提示词, 如 cat' : '查询文本';
    // 概念分割模式: 标签默认取概念词
    if (engine === 'concept' && q && q.value.trim()) {
      var tagEl = document.getElementById('emb-tag');
      if (tagEl && !tagEl.value.trim()) tagEl.value = q.value.trim();
    }
    var changed = _embEngine !== engine;
    _embEngine = engine;
    if (!changed || reset === false) return;
    // 引擎真正切换时清空既有排序与掩码, 重新渲染原始瀑布流
    _embSortActive = false;
    for (var i = 0; i < _items.length; i++) {
      _items[i]._sim = null;
      _items[i]._ratio = null;
      _items[i]._mask = null;
    }
    hideSingle();
    Masonry.render(_items);
    updateStats();
  }

  /**
   * 更新向量查询进度条（query 采用 JobManager，SSE 推送进度）。
   * @param {Object} s - 任务快照
   */
  function embProgress(s) {
    var bar = document.getElementById('emb-progress');
    var running = s.status === 'running' || s.status === 'queued';
    if (bar) bar.hidden = !running;
    var fill = document.getElementById('emb-progress-fill');
    if (fill) fill.classList.toggle('indeterminate', s.status === 'queued');
    if (fill && s.status === 'running') {
      fill.style.width = Math.min(100, s.pct || 0) + '%';
    }
    var pctEl = document.getElementById('emb-progress-pct');
    if (pctEl) pctEl.textContent = s.status === 'queued'
      ? '…'
      : (Math.min(100, Math.round(s.pct || 0)) + '%');
    var statusEl = document.getElementById('emb-status');
    if (statusEl && s.message) statusEl.textContent = s.message;
  }

  /**
   * 按查询文本排序瀑布流（提交 JobManager 实时计算，SSE 订阅进度）。
   * - similarity 引擎: 按相似度评分排序
   * - concept 引擎: 按掩码占比排序并叠加掩码
   * @returns {Promise<void>}
   */
  async function embSort() {
    var query = document.getElementById('emb-query').value.trim();
    if (!query) { toast('请输入查询文本'); return; }
    var engine = embEngine();
    var endpoint = engine === 'concept' ? '/tags/concept/query' : '/tags/embedding/query';

    var btn = document.getElementById('emb-sort');
    btn.disabled = true;
    document.getElementById('emb-status').textContent = '提交中…';
    try {
      var res = await API.request(API.apiPath(endpoint), {
        method: 'POST',
        body: JSON.stringify({ query: query, dirs: DirPicker.getDirs() }),
      });
      if (!res.job_id) {
        document.getElementById('emb-status').textContent = '查询失败';
        btn.disabled = false;
        return;
      }
      API.watchJob(res.job_id, function (s) {
        embProgress(s);
        if (s.status === 'done') {
          btn.disabled = false;
          var items = (s.result && s.result.items) || [];
          if (items.length === 0) {
            document.getElementById('emb-status').textContent = '无匹配结果';
            return;
          }
          var simMap = {}, ratioMap = {}, maskMap = {};
          for (var i = 0; i < items.length; i++) {
            simMap[items[i].path] = items[i].similarity;
            ratioMap[items[i].path] = items[i].ratio;
            maskMap[items[i].path] = items[i].mask_data || null;
          }
          for (var j = 0; j < _items.length; j++) {
            var p = _items[j].path;
            _items[j]._sim = engine === 'concept' ? null : (simMap[p] !== undefined ? simMap[p] : null);
            _items[j]._ratio = engine === 'concept' ? (ratioMap[p] !== undefined ? ratioMap[p] : null) : null;
            _items[j]._mask = engine === 'concept' ? (maskMap[p] || null) : null;
          }
          // 降序排列; 相似度/占比可为 0, 仅 null 表示无值, 排最后
          _items.sort(function (a, b) {
            var va = engine === 'concept' ? a._ratio : a._sim;
            var vb = engine === 'concept' ? b._ratio : b._sim;
            var sa = va == null ? -Infinity : va;
            var sb = vb == null ? -Infinity : vb;
            return sb - sa;
          });
          _embSortActive = true;
          hideSingle();
          Masonry.render(_items);
          updateStats();
          var doneMsg = engine === 'concept'
            ? '已按掩码占比排序 (' + items.length + ' 条)'
            : '已按相似度排序 (' + items.length + ' 条)';
          document.getElementById('emb-status').textContent = doneMsg;
          toast(doneMsg);
        } else if (s.status === 'error') {
          btn.disabled = false;
          document.getElementById('emb-status').textContent = '查询失败';
          toast('查询失败: ' + (s.message || ''));
        }
      });
    } catch (e) {
      document.getElementById('emb-status').textContent = '查询失败';
      toast('查询失败: ' + String(e.message || e));
      btn.disabled = false;
    }
  }

  /**
   * 基于已排序瀑布流缓存的指标（相似度/掩码占比），按阈值筛选匹配图片并批量添加/删除标签。
   *
   * 指标来自 embSort 的实时计算结果（_items[i]._sim / _items[i]._ratio），本函数仅做
   * 前端筛选后提交同步写接口，因此开销极小，无需长任务与进度条。
   * 概念分割模式下标签默认取概念词。
   * @returns {Promise<void>}
   */
  async function embBatchTag() {
    var engine = embEngine();
    var tag = document.getElementById('emb-tag').value.trim();
    if (!tag && engine === 'concept') {
      tag = document.getElementById('emb-query').value.trim();
    }
    if (!tag) { toast('请输入标签'); return; }
    if (!_embSortActive) { toast('请先按' + (engine === 'concept' ? '掩码占比' : '相似度') + '排序'); return; }

    var operator = document.getElementById('emb-operator').value;
    var threshold = parseFloat(document.getElementById('emb-threshold').value);
    if (isNaN(threshold) || threshold < 0 || threshold > 1) {
      toast('阈值必须在 0~1 之间');
      return;
    }
    var action = document.getElementById('emb-action').value;
    var fuzzy = document.getElementById('emb-fuzzy').checked;

    // 按阈值筛选匹配路径
    var paths = [];
    for (var i = 0; i < _items.length; i++) {
      var s = engine === 'concept' ? _items[i]._ratio : _items[i]._sim;
      if (s == null) continue;
      if ((operator === 'gt' && s >= threshold) || (operator === 'lt' && s <= threshold)) {
        paths.push(_items[i].path);
      }
    }
    if (paths.length === 0) { toast('无匹配图片'); return; }

    var btn = document.getElementById('emb-batch-tag');
    btn.disabled = true;
    document.getElementById('emb-status').textContent = '执行中…';
    try {
      var res = await API.request(API.apiPath('/tags/embedding/batch-tag'), {
        method: 'POST',
        body: JSON.stringify({
          paths: paths,
          tag: tag,
          action: action,
          position: getInsertPosition(),
          fuzzy: fuzzy,
        }),
      });
      document.getElementById('emb-status').textContent = res.message || '完成';
      toast(res.message || '批量执行完成');
      // 刷新标签状态, 保留语义排序与顺序
      runCheck(true).catch(function (e) { toast('刷新失败: ' + String(e.message || e)); });
    } catch (e) {
      document.getElementById('emb-status').textContent = '执行失败';
      toast('批量执行失败: ' + String(e.message || e));
    } finally {
      btn.disabled = false;
    }
  }

  async function init() {
    await AppSettings.ready();
    Masonry.init('masonry', {
      getData: function () { return _items; },
      onClick: onCardClick,
      decorate: decorateCard,
      imageSrc: imageSrc,
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
    var state = AppSettings.get('tag_check');
    if (state && typeof state === 'object') saved = state.cols;
    if (!(saved >= 0 && saved <= 16)) saved = 0;
    applyCols(saved);

    var tc = AppSettings.get('tag_check') || {};
    if (tc.position) document.getElementById('check-position').value = tc.position;
    if (tc.mode) document.getElementById('check-mode').value = tc.mode;
    if (tc.apply_mask != null) {
      _applyMask = !!tc.apply_mask;
      var chkMaskEl = document.getElementById('chk-apply-mask');
      if (chkMaskEl) chkMaskEl.checked = _applyMask;
    }
    var te = tc.emb || {};
    if (te.engine) document.getElementById('emb-engine').value = te.engine;
    if (te.operator) document.getElementById('emb-operator').value = te.operator;
    if (te.threshold) document.getElementById('emb-threshold').value = String(te.threshold);
    if (te.fuzzy != null) document.getElementById('emb-fuzzy').checked = !!te.fuzzy;
    if (te.action) document.getElementById('emb-action').value = te.action;
    embSetEngine(false);
    embUpdateAction();

    if (colsSlider) {
      colsSlider.addEventListener('input', function () {
        var v = parseInt(this.value, 10);
        if (isNaN(v) || v < 0) v = 0;
        var s = AppSettings.get('tag_check') || {};
        s.cols = v;
        AppSettings.set('tag_check', s);
        applyCols(v);
      });
    }
    window.addEventListener('resize', function () {
      if (_manualCols === 0) Masonry.setColumns(autoColumns());
    });

    /**
     * 将检查参数与向量对话框参数写入设置（「tag_check」键）。
     *
     * Returns:
     *   void
     *
     * Callers:
     *   - tag_check.js:check-position/check-mode/chk-apply-mask change 监听
     *   - tag_check.js:emb-engine/emb-operator/emb-threshold/emb-fuzzy/emb-action change 监听
     */
    function persistTagCheck() {
      var s = AppSettings.get('tag_check') || {};
      s.position = document.getElementById('check-position').value;
      s.mode = document.getElementById('check-mode').value;
      s.apply_mask = !!document.getElementById('chk-apply-mask').checked;
      s.emb = {
        engine: document.getElementById('emb-engine').value,
        operator: document.getElementById('emb-operator').value,
        threshold: document.getElementById('emb-threshold').value,
        fuzzy: !!document.getElementById('emb-fuzzy').checked,
        action: document.getElementById('emb-action').value,
      };
      AppSettings.set('tag_check', s);
    }

    document.getElementById('btn-check').addEventListener('click', function () {
      // 语义排序状态下保留排序, 仅刷新标签状态
      runCheck(_embSortActive).catch(function (e) { toast('检查失败: ' + String(e.message || e)); });
    });
    document.getElementById('check-position').addEventListener('change', function () {
      persistTagCheck();
      // 语义排序状态下保留排序, 仅刷新标签状态
      runCheck(_embSortActive).catch(function (e) { toast('检查失败: ' + String(e.message || e)); });
    });
    document.getElementById('check-mode').addEventListener('change', persistTagCheck);
    // 应用蒙板显示开关: 切换后重建瀑布流(重新触发合成/恢复)
    var chkMask = document.getElementById('chk-apply-mask');
    if (chkMask) {
      chkMask.addEventListener('change', function () {
        _applyMask = chkMask.checked;
        persistTagCheck();
        hideSingle();
        Masonry.render(_items);
      });
    }

    // ---- 向量嵌入事件 ----
    function embPositionLabel() {
      var sep = document.getElementById('check-position');
      var mode = document.getElementById('check-mode');
      var sepText = sep ? sep.options[sep.selectedIndex].text : '-';
      var modeText = mode ? mode.options[mode.selectedIndex].text : '-';
      return sepText + ' · ' + modeText;
    }
    function embUpdateAction() {
      var wrap = document.getElementById('emb-fuzzy-wrap');
      var action = document.getElementById('emb-action').value;
      if (wrap) wrap.style.display = action === 'delete' ? '' : 'none';
      var btn = document.getElementById('emb-batch-tag');
      if (btn) btn.textContent = action === 'delete' ? '批量删除' : '批量添加';
    }
    function openEmbDlg() {
      var dlg = document.getElementById('emb-dlg');
      dlg.classList.add('open');
      dlg.setAttribute('aria-hidden', 'false');
      document.getElementById('emb-position-display').textContent = embPositionLabel();
      embUpdateAction();
      embSetEngine(false);
      document.getElementById('emb-query').focus();
    }
    function closeEmbDlg() {
      var dlg = document.getElementById('emb-dlg');
      dlg.classList.remove('open');
      dlg.setAttribute('aria-hidden', 'true');
    }
    document.getElementById('btn-emb-dlg').addEventListener('click', openEmbDlg);
    document.getElementById('btn-emb-close').addEventListener('click', closeEmbDlg);
    document.getElementById('emb-dlg').addEventListener('click', function (e) {
      if (e.target === this) closeEmbDlg();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && document.getElementById('emb-dlg').classList.contains('open')) {
        closeEmbDlg();
      }
    });
    document.getElementById('emb-action').addEventListener('change', function () {
      embUpdateAction();
      persistTagCheck();
    });
    document.getElementById('emb-engine').addEventListener('change', function () {
      embSetEngine();
      persistTagCheck();
    });
    document.getElementById('emb-operator').addEventListener('change', persistTagCheck);
    document.getElementById('emb-threshold').addEventListener('change', persistTagCheck);
    document.getElementById('emb-fuzzy').addEventListener('change', persistTagCheck);
    document.getElementById('emb-sort').addEventListener('click', function () {
      embSort().catch(function (e) { toast('排序失败: ' + String(e.message || e)); });
    });
    document.getElementById('emb-batch-tag').addEventListener('click', function () {
      embBatchTag().catch(function (e) { toast('批量执行失败: ' + String(e.message || e)); });
    });
    // Enter 键触发查询排序
    document.getElementById('emb-query').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        embSort().catch(function (err) { toast('查询失败: ' + String(err.message || err)); });
      }
    });
    // Enter 键触发批量执行
    document.getElementById('emb-tag').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        embBatchTag().catch(function (err) { toast('批量执行失败: ' + String(err.message || err)); });
      }
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

    // 单图键盘切帧 (AD 与左右键等价)
    document.addEventListener('keydown', function (e) {
      if (_singleIndex < 0) return;
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
      if (e.key === 'ArrowLeft' || e.key === 'a' || e.key === 'A') { e.preventDefault(); navigateSingle(-1); }
      else if (e.key === 'ArrowRight' || e.key === 'd' || e.key === 'D') { e.preventDefault(); navigateSingle(1); }
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

  function start() {
    init().catch(function (e) {
      toast('初始化失败: ' + String(e.message || e));
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
