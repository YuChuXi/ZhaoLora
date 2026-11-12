/**
 * ui.js —— 全局 UI 原语，供所有页面共享。
 *
 * 引用：所有使用 toast 的模板页面（index/scoring/dedupe/crop/paint/tags/
 *       tag_check/ready/home）。
 */
(function () {
  'use strict';

  var _toastTimer = null;

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
   * 显示错误提示(带 ⚠ 前缀)。引用：各页面业务错误、全局 error/unhandledrejection。
   * @param {string} msg
   */
  function errshow(msg) {
    toast('\u26a0 ' + msg);
  }

  /**
   * 转义 HTML 特殊字符, 防止注入。引用：tag_check、scoring 等渲染用户数据的页面。
   * @param {string} s
   * @returns {string}
   */
  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
  }

  /**
   * 按 id 取元素。
   * @param {string} id
   * @returns {HTMLElement|null}
   */
  function byId(id) {
    return id ? document.getElementById(id) : null;
  }

  // ==================== 模态 ====================

  var _modals = [];   // {id, onClose} 按绑定顺序排列, Esc 只关闭最后一个已打开的模态

  /**
   * 打开模态对话框(切换 .open 与 aria-hidden)。
   * @param {string} id 模态根元素 id
   * @returns {void}
   * 引用：caption.js、tags.js、dedupe.js、booru_filter.js。
   */
  function openModal(id) {
    var dlg = byId(id);
    if (!dlg) return;
    dlg.classList.add('open');
    dlg.setAttribute('aria-hidden', 'false');
  }

  /**
   * 关闭模态对话框; 若该模态绑定了 onClose, 一并触发。
   * @param {string} id 模态根元素 id
   * @returns {void}
   * 引用：caption.js、tags.js、dedupe.js、booru_filter.js。
   */
  function closeModal(id) {
    var dlg = byId(id);
    if (!dlg) return;
    dlg.classList.remove('open');
    dlg.setAttribute('aria-hidden', 'true');
    for (var i = 0; i < _modals.length; i++) {
      if (_modals[i].id === id && _modals[i].onClose) {
        _modals[i].onClose();
        return;
      }
    }
  }

  /**
   * 绑定模态的遮罩点击与 Esc 关闭行为, 并登记 onClose 回调。
   * @param {string} id 模态根元素 id
   * @param {function} [onClose] 关闭时执行的收尾回调
   * @returns {void}
   * 引用：caption.js、tags.js、dedupe.js、booru_filter.js。
   */
  function bindModal(id, onClose) {
    var dlg = byId(id);
    if (!dlg) return;
    dlg.addEventListener('click', function (e) {
      if (e.target === dlg) closeModal(id);
    });
    for (var i = 0; i < _modals.length; i++) {
      if (_modals[i].id === id) {
        _modals[i].onClose = onClose || null;
        return;
      }
    }
    _modals.push({ id: id, onClose: onClose || null });
  }

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    for (var i = _modals.length - 1; i >= 0; i--) {
      var dlg = byId(_modals[i].id);
      if (dlg && dlg.classList.contains('open')) {
        closeModal(_modals[i].id);
        return;
      }
    }
  });

  // ==================== 选择集合 ====================

  /**
   * 创建前端选择集合: 供瀑布流页面统一实现单击选择、范围选择与批量操作。
   *
   * 选中状态只存在于当前页面, 不写入服务端; 变更后以 (数量, 路径列表) 通知页面。
   *
   * @param {function(number, string[])} onChange 选中数量或成员变化时的回调
   * @returns {{has: function(string): boolean, toggle: function(string): void,
   *   selectRange: function(string[]): void, selectAll: function(string[]): void,
   *   invert: function(string[]): void, retain: function(string[]): void,
   *   clear: function(): void, count: function(): number,
   *   paths: function(): string[]}}
   * 引用：caption.js。
   */
  function createSelection(onChange) {
    var set = {};

    function notify() {
      onChange(Object.keys(set).length, Object.keys(set));
    }

    return {
      has: function (path) { return !!set[path]; },
      toggle: function (path) {
        if (set[path]) delete set[path];
        else set[path] = true;
        notify();
      },
      selectRange: function (paths) {
        for (var i = 0; i < paths.length; i++) set[paths[i]] = true;
        notify();
      },
      selectAll: function (paths) {
        set = {};
        for (var i = 0; i < paths.length; i++) set[paths[i]] = true;
        notify();
      },
      invert: function (paths) {
        var next = {};
        for (var i = 0; i < paths.length; i++) {
          if (!set[paths[i]]) next[paths[i]] = true;
        }
        set = next;
        notify();
      },
      retain: function (paths) {
        var keep = {};
        for (var i = 0; i < paths.length; i++) keep[paths[i]] = true;
        var changed = false;
        Object.keys(set).forEach(function (path) {
          if (!keep[path]) {
            delete set[path];
            changed = true;
          }
        });
        if (changed) notify();
      },
      clear: function () {
        set = {};
        notify();
      },
      count: function () { return Object.keys(set).length; },
      paths: function () { return Object.keys(set); },
    };
  }

  // ==================== 标签补全 ====================

  /**
   * 为输入框绑定标签自动补全下拉.
   *
   * 只补全正在输入的最后一个标签片段: ``separator="comma"`` 以逗号分段并补
   * 逗号续输, ``separator="space"`` 以空白分段原样替换. 下拉固定定位到输入框
   * 下方, 支持上下键选择、回车或点击确认、Esc 关闭; 打开时的按键不冒泡到页面,
   * 避免同时触发页面的回车提交或模态关闭.
   *
   * @param {HTMLElement} input 目标输入框
   * @param {{separator?: string,
   *   fetchTags: function(string): Promise<Array<{name: string,
   *     note?: string, count?: number}>>}} opts
   *   - separator: ``"comma"`` 或 ``"space"``, 默认 space
   *   - fetchTags: 按当前片段取候选标签, 返回 name / 次要说明 / 计数
   * @returns {{close: function(): void}|null} 关闭下拉的控制句柄
   * 引用：booru_filter.js、app.js。
   */
  function bindTagSuggest(input, opts) {
    if (!input) return null;
    opts = opts || {};
    var comma = opts.separator === 'comma';
    var fetchTags = opts.fetchTags;
    var box = document.createElement('div');
    box.className = 'tag-suggest';
    box.style.display = 'none';
    document.body.appendChild(box);
    var timer = null;
    var items = [];
    var activeIndex = -1;
    var seq = 0;
    var suppress = false; // 选中补全后跳过紧随的 input 事件, 避免下拉立即重弹

    function close() {
      seq++; // 使在途补全响应过期, 避免输入清空后仍重弹下拉
      box.style.display = 'none';
      box.innerHTML = '';
      items = [];
      activeIndex = -1;
    }

    /** 取正在补全的标签片段. 引用: onInput. */
    function currentWord() {
      var v = input.value;
      if (comma) {
        var cut = v.lastIndexOf(',');
        return (cut >= 0 ? v.slice(cut + 1) : v).trim();
      }
      var m = v.match(/(\S+)\s*$/);
      return m ? m[1] : '';
    }

    /** 用选中的标签替换当前片段. 引用: choose. */
    function replaceWord(word) {
      var v = input.value;
      if (comma) {
        var cut = v.lastIndexOf(',');
        var head = cut >= 0 ? v.slice(0, cut + 1) + ' ' : '';
        input.value = head + word + ', ';
        return;
      }
      // 用占位符定位最后一个词起点, 避免标签名内含正则特殊字符引起转义
      var start = v.replace(/(\S+)\s*$/, '\u0000').indexOf('\u0000');
      input.value = v.slice(0, start) + word;
    }

    function render(data) {
      box.innerHTML = '';
      items = data;
      activeIndex = items.length ? 0 : -1;
      if (!items.length) { box.style.display = 'none'; return; }
      var table = document.createElement('table');
      table.className = 'tag-suggest-table';
      items.forEach(function (t, i) {
        var tr = document.createElement('tr');
        tr.className = 'tag-suggest-row' + (i === activeIndex ? ' active' : '');
        var tdName = document.createElement('td');
        tdName.className = 'tag-suggest-name';
        tdName.textContent = t.name;
        tdName.title = t.name;
        var tdNote = document.createElement('td');
        tdNote.className = 'tag-suggest-cat';
        tdNote.textContent = t.note || '';
        var tdCnt = document.createElement('td');
        tdCnt.className = 'tag-suggest-cnt';
        tdCnt.textContent = t.count > 0 ? ('' + t.count) : '';
        tr.appendChild(tdName);
        tr.appendChild(tdNote);
        tr.appendChild(tdCnt);
        tr.addEventListener('mousedown', function (e) {
          e.preventDefault();
          choose(i);
        });
        tr.addEventListener('mousemove', function () {
          if (activeIndex !== i) {
            activeIndex = i;
            highlight();
          }
        });
        table.appendChild(tr);
      });
      box.appendChild(table);
      var r = input.getBoundingClientRect();
      box.style.left = r.left + 'px';
      box.style.top = (r.bottom + 2) + 'px';
      box.style.minWidth = Math.max(r.width, 220) + 'px';
      box.style.display = 'block';
    }

    function highlight() {
      var table = box.querySelector('.tag-suggest-table');
      if (!table) return;
      Array.prototype.forEach.call(table.rows, function (tr, i) {
        tr.classList.toggle('active', i === activeIndex);
      });
      var act = table.rows[activeIndex];
      if (act) act.scrollIntoView({ block: 'nearest' });
    }

    function choose(i) {
      if (i < 0 || i >= items.length) return;
      suppress = true;
      replaceWord(items[i].name);
      close();
      setTimeout(function () { suppress = false; }, 0);
    }

    function onInput() {
      if (suppress) return;
      var word = currentWord();
      if (!word) { close(); return; }
      var mySeq = ++seq;
      clearTimeout(timer);
      timer = setTimeout(function () {
        fetchTags(word).then(function (data) {
          if (mySeq !== seq) return; // 丢弃过期响应
          render(data || []);
        }).catch(function () { close(); });
      }, 200);
    }

    input.addEventListener('input', onInput);
    input.addEventListener('keydown', function (e) {
      if (box.style.display === 'none') return;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        e.stopImmediatePropagation();
        activeIndex = (activeIndex + 1) % items.length;
        highlight();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        e.stopImmediatePropagation();
        activeIndex = (activeIndex - 1 + items.length) % items.length;
        highlight();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        e.stopImmediatePropagation();
        choose(activeIndex);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        e.stopImmediatePropagation();
        close();
      }
    });
    input.addEventListener('blur', function () {
      setTimeout(function () { close(); }, 120);
    });
    input.addEventListener('focus', onInput);

    return { close: close };
  }

  // ==================== 任务面板 ====================

  /**
   * 创建任务面板控制器: 统一进度条、百分比、日志、取消按钮与终态收尾。
   *
   * 事件到达时先渲染, 再交给 onEvent 追加页面逻辑; 终态时关闭订阅并隐藏取消按钮,
   * 最后调用 onFinish。页面只负责提交任务与在 onFinish 中刷新数据。
   *
   * @param {{bar: string, fill: string, pct: string, log?: string,
   *   cancelBtn?: string, statusEl?: string, onEvent?: function(object),
   *   onFinish?: function(object)}} opts 各控件 id 与回调
   * @returns {{start: function(string): void, cancel: function(): void,
   *   reset: function(): void, jobId: function(): string|null}}
   * 引用：caption.js。
   */
  function bindJobPanel(opts) {
    var bar = byId(opts.bar);
    var fill = byId(opts.fill);
    var pct = byId(opts.pct);
    var log = byId(opts.log);
    var cancelBtn = byId(opts.cancelBtn);
    var statusEl = byId(opts.statusEl);
    var jobId = null;
    var watcher = null;

    var TERMINAL = { done: 1, error: 1, cancelled: 1 };

    /**
     * 渲染一份任务快照。
     * @param {object} s 任务快照
     * @returns {void}
     */
    function render(s) {
      var pending = s.status === 'queued' || s.status === 'loading';
      var running = pending || s.status === 'running';
      if (bar) bar.hidden = !running;
      if (fill) {
        fill.classList.toggle('indeterminate', pending);
        if (s.status === 'running') fill.style.width = Math.min(100, s.pct || 0) + '%';
      }
      if (pct) {
        pct.textContent = pending
          ? '…'
          : Math.min(100, Math.round(s.pct || 0)) + '%';
      }
      if (statusEl && s.message) statusEl.textContent = s.message;
      if (log && s.logs && s.logs.length) {
        log.hidden = false;
        log.textContent = s.logs.join('\n');
        log.scrollTop = log.scrollHeight;
      }
    }

    /**
     * 订阅任务并接管面板。
     * @param {string} id 任务 id
     * @returns {void}
     */
    function start(id) {
      jobId = id;
      if (cancelBtn) {
        cancelBtn.hidden = false;
        cancelBtn.disabled = false;
      }
      render({ status: 'queued' });
      watcher = API.watchJob(id, function (s) {
        render(s);
        if (opts.onEvent) opts.onEvent(s);
        if (TERMINAL[s.status]) {
          jobId = null;
          if (watcher) {
            watcher.close();
            watcher = null;
          }
          if (cancelBtn) cancelBtn.hidden = true;
          if (opts.onFinish) opts.onFinish(s);
        }
      });
    }

    /** 请求取消当前任务。 */
    function cancel() {
      if (!jobId) return;
      if (cancelBtn) cancelBtn.disabled = true;
      API.cancelJob(jobId).catch(function (e) {
        if (cancelBtn) cancelBtn.disabled = false;
        errshow(String(e.message || e));
      });
    }

    /** 隐藏进度与日志, 恢复取消按钮的初始状态。 */
    function reset() {
      jobId = null;
      if (watcher) {
        watcher.close();
        watcher = null;
      }
      if (bar) bar.hidden = true;
      if (cancelBtn) cancelBtn.hidden = true;
    }

    return {
      start: start,
      cancel: cancel,
      reset: reset,
      jobId: function () { return jobId; },
    };
  }

  // 全局未捕获错误统一走 errshow, 避免各页面各自绑定监听
  window.addEventListener('error', function (e) { errshow(e.message); });
  window.addEventListener('unhandledrejection', function (e) {
    errshow(String(e.reason && e.reason.message ? e.reason.message : e.reason));
  });

  window.toast = toast;
  window.errshow = errshow;
  window.escHtml = escHtml;
  window.UI = {
    byId: byId,
    openModal: openModal,
    closeModal: closeModal,
    bindModal: bindModal,
    createSelection: createSelection,
    bindTagSuggest: bindTagSuggest,
    bindJobPanel: bindJobPanel,
  };
})();
