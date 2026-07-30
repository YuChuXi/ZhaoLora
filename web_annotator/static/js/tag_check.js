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
    if (img.exists && img.position_ok) {
      card.classList.add('tag-ok');
    } else if (img.exists && !img.position_ok) {
      card.classList.add('tag-bad');
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
   * @param {object} img
   * @param {HTMLElement} card
   */
  function onCardClick(img, card) {
    var tag = document.getElementById('check-tag').value.trim();
    if (!tag) { toast('请先输入标签'); return; }

    card.classList.add('jump-flash');

    var endpoint, body;
    if (img.exists) {
      endpoint = API.apiPath('/tags/delete-one');
      body = JSON.stringify({ path: img.path, tag: tag });
    } else {
      endpoint = API.apiPath('/tags/edit-one');
      body = JSON.stringify({ path: img.path, tag: tag, position: getInsertPosition() });
    }

    API.request(endpoint, { method: 'POST', body: body }).then(function (updated) {
      for (var i = 0; i < _items.length; i++) {
        if (_items[i].path === img.path) { _items[i] = updated; break; }
      }
      decorateCard(card, updated);
      updateStats();
    }).catch(function (e) {
      toast('操作失败: ' + String(e.message || e));
    }).finally(function () {
      card.classList.remove('jump-flash');
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

  function init() {
    Masonry.init('masonry', {
      getData: function () { return _items; },
      onClick: onCardClick,
      decorate: decorateCard,
    });
    acInit();

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
