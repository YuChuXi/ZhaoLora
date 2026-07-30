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
   * @param {string} action "add" | "delete"
   */
  function singleTagOp(action) {
    var idx = _singleIndex;
    if (idx < 0 || idx >= _items.length) return;
    var tag = document.getElementById('check-tag').value.trim();
    if (!tag) { toast('请先输入标签'); return; }

    var img = _items[idx];
    var endpoint, body;
    if (action === 'delete' || img.exists) {
      endpoint = API.apiPath('/tags/delete-one');
      body = JSON.stringify({ path: img.path, tag: tag });
    } else {
      endpoint = API.apiPath('/tags/edit-one');
      body = JSON.stringify({ path: img.path, tag: tag, position: getInsertPosition() });
    }

    API.request(endpoint, { method: 'POST', body: body }).then(function (updated) {
      _items[idx] = updated;
      renderSingle();
    }).catch(function (e) {
      toast('操作失败: ' + String(e.message || e));
    });
  }

  /**
   * 渲染当前单图（图片、stem、caption、着色边框）。
   */
  function renderSingle() {
    var idx = _singleIndex;
    if (idx < 0 || idx >= _items.length) return;
    var img = _items[idx];
    var imgEl = document.getElementById('single-img');
    imgEl.src = API.imageUrl(img.path);
    document.getElementById('single-stem').textContent = img.stem;
    document.getElementById('single-counter').textContent = (idx + 1) + '/' + _items.length;
    document.getElementById('single-caption').textContent = img.caption_preview || '(无 .txt)';

    // 位置着色
    var viewer = document.getElementById('single-viewer');
    viewer.classList.remove('single-ok', 'single-bad');
    if (img.exists && img.position_ok) {
      viewer.classList.add('single-ok');
    } else if (img.exists && !img.position_ok) {
      viewer.classList.add('single-bad');
    }
  }

  /**
   * 进入单图模式，展示第 index 张。
   * @param {number} index
   */
  function showSingle(index) {
    if (index < 0 || index >= _items.length) return;
    _singleIndex = index;
    document.getElementById('masonry').hidden = true;
    document.getElementById('single-viewer').hidden = false;
    document.getElementById('check-hint').hidden = true;
    document.getElementById('btn-single-mode').textContent = '网格';
    applyFitMode();
    renderSingle();
  }

  /** 退出单图模式。 */
  function hideSingle() {
    _singleIndex = -1;
    document.getElementById('masonry').hidden = false;
    document.getElementById('single-viewer').hidden = true;
    document.getElementById('check-hint').hidden = false;
    document.getElementById('btn-single-mode').textContent = '单图';
  }

  /** 切换 contain / cover。 */
  function toggleFit() {
    _fitMode = _fitMode === 'contain' ? 'cover' : 'contain';
    applyFitMode();
  }

  function applyFitMode() {
    var canvas = document.getElementById('single-canvas');
    var btn = document.getElementById('btn-fit');
    if (_fitMode === 'cover') {
      canvas.classList.remove('contain');
      canvas.classList.add('cover');
      btn.textContent = '⊟';
    } else {
      canvas.classList.add('contain');
      canvas.classList.remove('cover');
      btn.textContent = '⊞';
    }
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

  function magnifierInit() {
    var canvas = document.getElementById('single-canvas');
    var imgEl = document.getElementById('single-img');
    var lens = document.getElementById('magnifier');
    var lensW = lens.offsetWidth;
    var lensH = lens.offsetHeight;
    var zoom = 2;

    canvas.addEventListener('mousemove', function (e) {
      var rect = imgEl.getBoundingClientRect();
      var x = e.clientX - rect.left;
      var y = e.clientY - rect.top;
      if (x < 0 || y < 0 || x > rect.width || y > rect.height) {
        lens.classList.remove('show');
        return;
      }
      lens.classList.add('show');

      // 放大镜位置（跟随鼠标，偏移使其不遮挡光标）
      var lx = e.clientX - canvas.getBoundingClientRect().left - lensW / 2;
      var ly = e.clientY - canvas.getBoundingClientRect().top - lensH - 20;
      if (ly < 0) ly = e.clientY - canvas.getBoundingClientRect().top + 20;
      lens.style.left = lx + 'px';
      lens.style.top = ly + 'px';

      // 背景偏移：放大区域在图片上的实际位置
      var bgX = -(x * zoom - lensW / 2);
      var bgY = -(y * zoom - lensH / 2);
      lens.style.backgroundImage = 'url(' + imgEl.src + ')';
      lens.style.backgroundSize = (rect.width * zoom) + 'px ' + (rect.height * zoom) + 'px';
      lens.style.backgroundPosition = bgX + 'px ' + bgY + 'px';
    });

    canvas.addEventListener('mouseleave', function () {
      lens.classList.remove('show');
    });
  }

  function init() {
    Masonry.init('masonry', {
      getData: function () { return _items; },
      onClick: onCardClick,
      decorate: decorateCard,
    });
    acInit();
    magnifierInit();

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
    document.getElementById('btn-single-mode').addEventListener('click', function () {
      if (_singleIndex >= 0) {
        hideSingle();
      } else {
        showSingle(0);
      }
    });

    document.getElementById('btn-prev').addEventListener('click', function () { navigateSingle(-1); });
    document.getElementById('btn-next').addEventListener('click', function () { navigateSingle(1); });
    document.getElementById('btn-fit').addEventListener('click', toggleFit);
    document.getElementById('btn-single-close').addEventListener('click', hideSingle);

    // 单图点击：左键添加, 右键删除
    document.getElementById('single-img').addEventListener('click', function (e) {
      singleTagOp('add');
    });
    document.getElementById('single-img').addEventListener('contextmenu', function (e) {
      e.preventDefault();
      singleTagOp('delete');
    });

    // 单图键盘导航
    document.addEventListener('keydown', function (e) {
      if (_singleIndex < 0) return;
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      if (e.key === 'ArrowLeft') { e.preventDefault(); navigateSingle(-1); }
      if (e.key === 'ArrowRight') { e.preventDefault(); navigateSingle(1); }
      if (e.key === 'Escape') { e.preventDefault(); hideSingle(); }
    });

    // cover 模式滚轮上下滚动
    document.getElementById('single-canvas').addEventListener('wheel', function (e) {
      if (_fitMode !== 'cover') return;
      e.preventDefault();
      this.scrollTop += e.deltaY;
    }, { passive: false });

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
