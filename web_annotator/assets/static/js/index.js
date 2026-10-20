/**
 * index.js —— 首页：任务卡片、添加补全、左侧文档导航。
 *
 * 引用：templates/index.html。
 */
(function () {
  'use strict';

  var _candidates = [];
  var _suggestIndex = -1;
  var _nav = [];
  var _sortBy = 'sort_order';
  var _sortOrder = 'asc';
  var _dragSrcName = null;

  /**
   * @returns {string}
   */
  function currentSlug() {
    var q = new URLSearchParams(location.search).get('doc');
    if (q == null || q === '') return '';
    if (q === 'index') return 'readme';
    return String(q).trim();
  }

  /**
   * @param {string} slug
   * @param {boolean} replace
   */
  function setSlug(slug, replace) {
    var url = new URL(location.href);
    if (!slug) url.searchParams.delete('doc');
    else url.searchParams.set('doc', slug);
    if (replace) history.replaceState(null, '', url);
    else history.pushState(null, '', url);
  }

  /**
   * 无 slug = 任务列表；有 slug = 文档正文（含 readme 概览）。
   * @param {string} slug
   */
  function showPane(slug) {
    document.getElementById('pane-tasks').hidden = !!slug;
    document.getElementById('pane-docs').hidden = !slug;
  }

  /**
   * @param {string} active 空字符串表示任务页；否则为文档 slug
   */
  function renderNav(active) {
    var tasksLink = document.getElementById('nav-tasks');
    if (tasksLink) {
      tasksLink.className = 'side-tasks' + (active ? '' : ' active');
    }
    var host = document.getElementById('docs-nav');
    host.innerHTML = '';
    _nav.forEach(function (item) {
      var a = document.createElement('a');
      a.href = '/?doc=' + encodeURIComponent(item.slug);
      a.textContent = item.label;
      if (item.slug === active) a.className = 'active';
      a.addEventListener('click', function (e) {
        e.preventDefault();
        setSlug(item.slug, false);
        loadDoc(item.slug);
      });
      host.appendChild(a);
    });
  }

  /**
   * @param {string} slug
   * @returns {Promise<void>}
   */
  async function loadDoc(slug) {
    showPane(slug);
    renderNav(slug || '');
    if (!slug) {
      document.title = '任务 · ZhaoLora 标注工具';
      return;
    }
    var article = document.getElementById('docs-article');
    article.innerHTML = '<p class="docs-empty">加载中…</p>';
    var data;
    try {
      data = await API.request('/api/docs/' + encodeURIComponent(slug));
    } catch (e) {
      article.innerHTML = '<p class="docs-empty">文档加载失败：' + e.message + '</p>';
      return;
    }
    article.innerHTML = data.html || '';
    var title = (data.title || data.file || '文档').replace(/\.md$/, '');
    if (slug === 'readme') title = '概览';
    document.title = title + ' · ZhaoLora 标注工具';
    article.querySelectorAll('a[href^="?doc="]').forEach(function (a) {
      a.addEventListener('click', function (e) {
        e.preventDefault();
        var u = new URL(a.getAttribute('href'), location.origin + '/');
        var s = u.searchParams.get('doc') || '';
        var hash = u.hash || '';
        setSlug(s, false);
        loadDoc(s).then(function () {
          if (!hash) return;
          var target = document.getElementById(decodeURIComponent(hash.slice(1)));
          if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
      });
    });
  }

  /**
   * 内联编辑长文本: 将展示元素切换为 textarea, 失焦/回车保存, Esc 取消.
   * 备注与触发词共用此实现, 编辑高度随内容自动扩展.
   *
   * Args:
   *   host (HTMLElement): 承载编辑的容器, 用于重入检查.
   *   display (HTMLElement): 只读展示元素, 编辑时被替换为 textarea.
   *   old (string): 初始文本.
   *   placeholder (string): 空值时的占位文案.
   *   save (function(string): Promise): 保存回调, resolve 后刷新展示; reject 时 toast 并退出编辑.
   *
   * Returns:
   *   void.
   *
   * Callers:
   *   - ``static/js/index.js:startEditNote`` (备注)
   *   - ``static/js/index.js:buildCard`` (触发词)
   */
  function inlineEdit(host, display, old, placeholder, save) {
    if (host.querySelector('textarea')) return;
    var ta = document.createElement('textarea');
    ta.className = 'task-edit-input';
    ta.value = old;
    function resize() {
      ta.style.height = 'auto';
      ta.style.height = ta.scrollHeight + 'px';
    }
    host.replaceChild(ta, display);
    // 先挂入 DOM 再计算高度, 否则 scrollHeight 不准确.
    resize();
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    function exit() {
      display.textContent = old || placeholder;
      display.classList.toggle('empty', !old);
      host.replaceChild(display, ta);
    }
    function finish() {
      var v = ta.value.trim();
      if (v === old) { exit(); return; }
      save(v).then(function () {
        old = v;
        display.textContent = v || placeholder;
        display.classList.toggle('empty', !v);
        host.replaceChild(display, ta);
      }).catch(function (err) {
        toast(String(err.message || err));
        host.replaceChild(display, ta);
      });
    }
    ta.addEventListener('blur', finish);
    ta.addEventListener('input', resize);
    ta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ta.blur(); }
      if (e.key === 'Escape') { ta.value = old; ta.blur(); }
    });
  }

  /**
   * @param {HTMLElement} noteWrap
   * @param {string} name
   */
  function startEditNote(noteWrap, name) {
    inlineEdit(
      noteWrap.parentNode,
      noteWrap,
      noteWrap.textContent === '双击添加备注…' ? '' : noteWrap.textContent,
      '双击添加备注…',
      function (val) {
        return API.request('/api/tasks/' + encodeURIComponent(name) + '/note', { method: 'POST', body: JSON.stringify({ note: val }) });
      }
    );
  }

  /**
   * 在卡片上叠加进度条, 返回 { bar, msg } 用于更新.
   * @param {HTMLElement} card
   * @returns {{ bar: HTMLElement, msg: HTMLElement }}
   */
  function attachProgress(card) {
    card.classList.add('archiving');
    var bar = document.createElement('div');
    bar.className = 'job-bar';
    bar.appendChild(document.createElement('div'));
    card.appendChild(bar);
    var msg = document.createElement('div');
    msg.className = 'job-msg';
    card.appendChild(msg);
    return { bar: bar, msg: msg };
  }

  /**
   * 移除卡片上的进度叠层.
   * @param {HTMLElement} card
   */
  function removeProgress(card) {
    card.classList.remove('archiving');
    var bar = card.querySelector('.job-bar');
    var msg = card.querySelector('.job-msg');
    if (bar) bar.remove();
    if (msg) msg.remove();
  }

  /**
   * 归档已完成任务.
   * @param {string} name
   * @param {HTMLElement} card
   */
  function archiveTask(name, card) {
    var prog = attachProgress(card);
    prog.msg.textContent = '准备归档…';
    API.request('/api/tasks/' + encodeURIComponent(name) + '/archive', { method: 'POST' })
      .then(function (data) {
        API.watchJob(data.job_id, function (ev) {
          if (ev.pct != null) {
            prog.bar.firstChild.style.width = ev.pct + '%';
          }
          prog.msg.textContent = ev.message || ev.phase || '';
          if (ev.status === 'done') {
            toast('已归档: ' + name);
            loadTasks();
          } else if (ev.status === 'error') {
            toast('归档失败: ' + (ev.message || ''));
            removeProgress(card);
          }
        });
      })
      .catch(function (err) {
        toast(String(err.message || err));
        removeProgress(card);
      });
  }

  /**
   * 解压归档恢复任务.
   * @param {string} base
   * @param {HTMLElement} card
   */
  function restoreArchive(base, card) {
    var prog = attachProgress(card);
    prog.msg.textContent = '准备恢复…';
    API.request('/api/archived/' + encodeURIComponent(base) + '/restore', { method: 'POST' })
      .then(function (data) {
        API.watchJob(data.job_id, function (ev) {
          prog.msg.textContent = ev.message || ev.phase || '';
          if (ev.status === 'done') {
            toast('恢复完成');
            loadTasks();
          } else if (ev.status === 'error') {
            toast('恢复失败: ' + (ev.message || ''));
            removeProgress(card);
          }
        });
      })
      .catch(function (err) {
        toast(String(err.message || err));
        removeProgress(card);
      });
  }

  /**
   * @param {object} t
   * @param {boolean} completed
   * @param {boolean} sortable
   * @returns {HTMLElement}
   */
  function buildCard(t, completed, sortable) {
    var card = document.createElement('div');
    card.className = 'task-card';
    if (sortable) {
      card.classList.add('sortable');
      card.draggable = true;
      card.dataset.taskName = t.name;
    }
    var href = '/d/' + encodeURIComponent(t.name) + '/filter';
    var abs = location.origin + href;

    var previews = document.createElement('div');
    previews.className = 'task-previews';
    var hashes = t.preview_hashes || [];
    for (var i = 0; i < 4; i++) {
      if (hashes[i]) {
        var img = document.createElement('img');
        img.src = t.archived
          ? '/api/archived/' + encodeURIComponent(t.archive_base || t.name) + '/preview/' + hashes[i] + '.avif'
          : '/d/' + encodeURIComponent(t.name) + '/thumb/' + hashes[i] + '.avif';
        img.alt = '';
        previews.appendChild(img);
      } else {
        var slot = document.createElement('div');
        slot.className = 'empty-slot';
        previews.appendChild(slot);
      }
    }
    if (!t.archived && !completed) {
      previews.style.cursor = 'pointer';
      previews.title = '进入处理';
      previews.addEventListener('click', function () {
        location.href = href;
      });
    }

    var body = document.createElement('div');
    body.className = 'task-body';
    var title = document.createElement('h3');
    title.textContent = t.name;
    var meta = document.createElement('div');
    meta.className = 'meta';
    if (t.archived) {
      meta.textContent = '图像 ' + (t.image_count || 0) + ' · 归档 ' + new Date((t.archived_at || 0) * 1000).toLocaleString();
    } else {
      meta.textContent =
        '图像 ' + (t.image_count || 0) + (t.report_ready ? ' · 有美学报告' : '');
    }

    var noteWrap = document.createElement('div');
    noteWrap.className = 'task-note' + (t.note ? '' : ' empty');
    noteWrap.textContent = t.note || (t.archived ? '无' : '双击添加备注…');
    if (!t.archived) {
      noteWrap.addEventListener('dblclick', function () {
        startEditNote(noteWrap, t.name);
      });
    }

    var triggerRow = document.createElement('div');
    triggerRow.className = 'task-trigger';
    var triggerLabel = document.createElement('span');
    triggerLabel.className = 'task-trigger-label';
    triggerLabel.textContent = '触发词';
    var triggerText = document.createElement('div');
    triggerText.className = 'task-trigger-text' + (t.trigger_word ? '' : ' empty');
    triggerText.textContent = t.trigger_word || (t.archived ? '无' : '点击设置触发词…');
    if (!t.archived) {
      triggerText.addEventListener('click', function () {
        inlineEdit(triggerRow, triggerText, t.trigger_word || '', '点击设置触发词…', function (v) {
          return API.request('/api/tasks/' + encodeURIComponent(t.name) + '/trigger-word', { method: 'POST', body: JSON.stringify({ trigger_word: v }) })
            .then(function () { toast('触发词已更新'); });
        });
      });
    }
    var triggerCopy = document.createElement('button');
    triggerCopy.type = 'button';
    triggerCopy.className = 'task-trigger-copy';
    triggerCopy.textContent = '复制';
    triggerCopy.addEventListener('click', function () {
      if (!t.trigger_word) {
        toast('触发词为空');
        return;
      }
      navigator.clipboard.writeText(t.trigger_word).then(function () { toast('已复制触发词'); });
    });
    triggerRow.appendChild(triggerLabel);
    triggerRow.appendChild(triggerText);
    triggerRow.appendChild(triggerCopy);

    var actions = document.createElement('div');
    actions.className = 'task-actions';
    if (t.archived) {
      var restore = document.createElement('button');
      restore.type = 'button';
      restore.className = 'btn';
      restore.textContent = '恢复';
      restore.addEventListener('click', function () {
        restoreArchive(t.archive_base, card);
      });
      actions.appendChild(restore);
    } else {
      if (!completed) {
        var enter = document.createElement('a');
        enter.className = 'btn green';
        enter.href = href;
        enter.textContent = '进入处理';
        actions.appendChild(enter);
      }
      var copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'btn';
      copy.textContent = '复制链接';
      copy.addEventListener('click', function () {
        navigator.clipboard.writeText(abs).then(function () { toast('已复制'); });
      });
      actions.appendChild(copy);
      if (completed) {
        var archive = document.createElement('button');
        archive.type = 'button';
        archive.className = 'btn';
        archive.textContent = '归档';
        archive.addEventListener('click', function () {
          archiveTask(t.name, card);
        });
        actions.appendChild(archive);
        var reopen = document.createElement('button');
        reopen.type = 'button';
        reopen.className = 'btn';
        reopen.textContent = '重新打开';
        reopen.addEventListener('click', function () {
          API.request('/api/tasks/' + encodeURIComponent(t.name) + '/reopen', { method: 'POST', body: '{}' }).then(loadTasks);
        });
        actions.appendChild(reopen);
      } else {
        var done = document.createElement('button');
        done.type = 'button';
        done.className = 'btn';
        done.textContent = '完成';
        done.addEventListener('click', function () {
          API.request('/api/tasks/' + encodeURIComponent(t.name) + '/complete', { method: 'POST', body: '{}' }).then(loadTasks);
        });
        actions.appendChild(done);
      }
    }

    body.appendChild(title);
    body.appendChild(meta);
    body.appendChild(noteWrap);
    body.appendChild(triggerRow);
    body.appendChild(actions);
    card.appendChild(previews);
    card.appendChild(body);

    return card;
  }

  async function loadTasks() {
    var url = '/api/tasks?sort_by=' + encodeURIComponent(_sortBy) + '&sort_order=' + encodeURIComponent(_sortOrder);
    var data = await API.request(url);
    var tasks = data.tasks || [];
    var active = document.getElementById('task-active');
    var completedHost = document.getElementById('task-completed');
    var archivedHost = document.getElementById('task-archived');
    active.innerHTML = '';
    completedHost.innerHTML = '';
    archivedHost.innerHTML = '';
    var nActive = 0;
    var nDone = 0;
    var nArch = 0;
    var isManual = _sortBy === 'sort_order';
    tasks.forEach(function (t) {
      if (t.archived) {
        nArch += 1;
        archivedHost.appendChild(buildCard(t, true, false));
      } else if (t.completed) {
        nDone += 1;
        completedHost.appendChild(buildCard(t, true, false));
      } else {
        nActive += 1;
        active.appendChild(buildCard(t, false, isManual));
      }
    });
    document.getElementById('task-empty').hidden = nActive > 0;
    document.getElementById('completed-wrap').hidden = nDone === 0;
    document.getElementById('completed-count').textContent = String(nDone);
    document.getElementById('archived-wrap').hidden = nArch === 0;
    document.getElementById('archived-count').textContent = String(nArch);
  }

  async function loadCandidates() {
    var data = await API.request('/api/tasks/candidates');
    _candidates = (data && data.candidates) || [];
  }

  /**
   * @param {string} q
   * @returns {string[]}
   */
  function filterCandidates(q) {
    var needle = String(q || '').toLowerCase();
    if (!needle) return _candidates.slice(0, 12);
    return _candidates.filter(function (n) {
      return n.toLowerCase().indexOf(needle) === 0;
    }).slice(0, 12);
  }

  function setSuggestOpen(open) {
    var box = document.getElementById('task-suggest');
    box.classList.toggle('open', !!open);
    if (!open) _suggestIndex = -1;
  }

  function renderSuggest() {
    var input = document.getElementById('task-name');
    var box = document.getElementById('task-suggest');
    var items = filterCandidates(input.value.trim());
    box.innerHTML = '';
    if (!items.length) {
      setSuggestOpen(false);
      return;
    }
    items.forEach(function (name, idx) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = name;
      if (idx === _suggestIndex) btn.className = 'active';
      btn.addEventListener('mousedown', function (e) {
        e.preventDefault();
        pickSuggest(name);
      });
      box.appendChild(btn);
    });
    setSuggestOpen(true);
  }

  /**
   * @param {string} name
   */
  function pickSuggest(name) {
    document.getElementById('task-name').value = name;
    setSuggestOpen(false);
    document.getElementById('task-name').focus();
  }

  document.getElementById('task-add-form').addEventListener('submit', function (e) {
    e.preventDefault();
    setSuggestOpen(false);
    var name = document.getElementById('task-name').value.trim();
    API.request('/api/tasks', { method: 'POST', body: JSON.stringify({ name: name }) })
      .then(function () {
        document.getElementById('task-name').value = '';
        toast('已添加 ' + name);
        return Promise.all([loadTasks(), loadCandidates()]);
      })
      .catch(function (err) { toast(String(err.message || err)); });
  });

  var nameInput = document.getElementById('task-name');
  nameInput.addEventListener('input', function () {
    _suggestIndex = -1;
    renderSuggest();
  });
  nameInput.addEventListener('focus', renderSuggest);
  nameInput.addEventListener('blur', function () {
    setTimeout(function () { setSuggestOpen(false); }, 120);
  });
  nameInput.addEventListener('keydown', function (e) {
    var box = document.getElementById('task-suggest');
    if (!box.classList.contains('open')) return;
    var buttons = box.querySelectorAll('button');
    if (!buttons.length) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      _suggestIndex = (_suggestIndex + 1) % buttons.length;
      renderSuggest();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      _suggestIndex = (_suggestIndex <= 0) ? (buttons.length - 1) : (_suggestIndex - 1);
      renderSuggest();
    } else if (e.key === 'Enter' && _suggestIndex >= 0) {
      e.preventDefault();
      pickSuggest(buttons[_suggestIndex].textContent);
    } else if (e.key === 'Escape') {
      setSuggestOpen(false);
    }
  });

  document.getElementById('nav-tasks').addEventListener('click', function (e) {
    e.preventDefault();
    setSlug('', false);
    loadDoc('');
  });

  // ---- 旧版浏览器设置导入(设置已改为服务端两层结构, 一次性入口) ----

  // 旧全局键: 目录选择/排序/两处列数; 任务键带任务名后缀: 描述规则与裁剪部位
  var LEGACY_FIXED_KEYS = ['annotator_work_dirs', 'annotator_work_dir', 'annotator_sort', 'zhao_filter_cols', 'zhao_tagcheck_cols'];
  var LEGACY_PREFIXES = ['zhao_caption_rules:', 'zhao_caption_inject:', 'crop_part_defaults:', 'crop_auto_parts:', 'crop_auto_parts_deny:'];

  /**
   * 解析 JSON 文本, 损坏内容返回 null(导入时跳过并保留原键).
   * @param {string|null} text
   * @returns {*}
   */
  function readJson(text) {
    if (text == null) return null;
    try {
      return JSON.parse(text);
    } catch (e) {
      return null;
    }
  }

  /**
   * 扫描 localStorage 中实际残留的旧设置键.
   * @returns {string[]}
   */
  function legacyKeys() {
    var out = [];
    LEGACY_FIXED_KEYS.forEach(function (k) {
      if (localStorage.getItem(k) != null) out.push(k);
    });
    for (var i = 0; i < localStorage.length; i++) {
      var k = localStorage.key(i);
      for (var p = 0; p < LEGACY_PREFIXES.length; p++) {
        if (k.indexOf(LEGACY_PREFIXES[p]) === 0) { out.push(k); break; }
      }
    }
    return out;
  }

  /**
   * 将浏览器 localStorage 中的旧版设置导入服务端: 全局参数合并写入全局缺省层,
   * 描述规则与裁剪部位按任务名分别合并写入对应任务的数据集层, 全部成功后
   * 清除旧键; 任一步失败即中止并保留旧键, 可重试(重复写入同值无害).
   *
   * Returns:
   *   Promise<number>: 覆盖到的任务数; 无旧数据时为 0.
   *
   * Callers:
   *   - ``static/js/index.js:btn-import-legacy`` click 监听
   */
  async function importLegacy() {
    var keys = legacyKeys();
    if (!keys.length) return 0;

    var raw = {};
    keys.forEach(function (k) { raw[k] = localStorage.getItem(k); });

    // 全局参数: 目录选择(work_dirs 优先, 回落旧版单目录键)、排序、两处列数
    var dirsRaw = raw['annotator_work_dirs'] != null ? raw['annotator_work_dirs'] : raw['annotator_work_dir'];
    var globalPatch = {};
    if (dirsRaw != null) {
      var dirs = dirsRaw.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      if (dirs.length) globalPatch.dirs = dirs;
    }
    if (raw['annotator_sort'] != null) globalPatch.sort = raw['annotator_sort'];
    var cols = parseInt(raw['zhao_filter_cols'], 10);
    if (cols >= 0) globalPatch.filter = { cols: cols };
    var tagCols = parseInt(raw['zhao_tagcheck_cols'], 10);
    if (tagCols >= 0) globalPatch.tag_check = { cols: tagCols };

    // 任务键按后缀分组为数据集层 patch 的子键
    var tasks = {};
    LEGACY_PREFIXES.forEach(function (prefix) {
      for (var k in raw) {
        if (k.indexOf(prefix) !== 0) continue;
        var name = k.slice(prefix.length);
        var v = readJson(raw[k]);
        if (!v) continue;
        var p = tasks[name] || (tasks[name] = {});
        if (prefix === 'zhao_caption_rules:') p.rules = v;
        else if (prefix === 'zhao_caption_inject:') p.inject = v;
        else if (prefix === 'crop_part_defaults:') p.part_defaults = v;
        else if (prefix === 'crop_auto_parts:') p.auto_parts = v;
        else p.denied_parts = v;
      }
    });

    if (Object.keys(globalPatch).length) {
      var g = await API.request('/api/settings');
      var gs = (g && g.settings) || {};
      // 列数是 filter/tag_check 的子键, 合并进全局层现值避免覆盖其余子键
      if (globalPatch.filter) {
        var f = (gs.filter && typeof gs.filter === 'object') ? gs.filter : {};
        f.cols = globalPatch.filter.cols;
        globalPatch.filter = f;
      }
      if (globalPatch.tag_check) {
        var t = (gs.tag_check && typeof gs.tag_check === 'object') ? gs.tag_check : {};
        t.cols = globalPatch.tag_check.cols;
        globalPatch.tag_check = t;
      }
      await API.request('/api/settings', { method: 'PUT', body: JSON.stringify(globalPatch) });
    }

    // 已归档或已删除任务的旧键无处可写, 随本次导入一并丢弃
    var known = {};
    var list = await API.request('/api/tasks');
    ((list && list.tasks) || []).forEach(function (t) { known[t.name] = true; });

    var n = 0;
    var names = Object.keys(tasks);
    for (var i = 0; i < names.length; i++) {
      if (!known[names[i]]) continue;
      var base = await API.request('/api/d/' + encodeURIComponent(names[i]) + '/settings');
      var st = (base && base.settings) || {};
      // caption/crop 仅存于数据集层, 合并视图取到的即数据集层现值
      var tp = tasks[names[i]];
      if (tp.rules != null || tp.inject != null) {
        var c = (st.caption && typeof st.caption === 'object') ? st.caption : {};
        if (tp.rules != null) c.rules = tp.rules;
        if (tp.inject != null) c.inject = tp.inject;
        st.caption = c;
      }
      if (tp.part_defaults != null || tp.auto_parts != null || tp.denied_parts != null) {
        var cr = (st.crop && typeof st.crop === 'object') ? st.crop : {};
        if (tp.part_defaults != null) cr.part_defaults = tp.part_defaults;
        if (tp.auto_parts != null) cr.auto_parts = tp.auto_parts;
        if (tp.denied_parts != null) cr.denied_parts = tp.denied_parts;
        st.crop = cr;
      }
      var patch = {};
      if (st.caption) patch.caption = st.caption;
      if (st.crop) patch.crop = st.crop;
      if (Object.keys(patch).length) {
        await API.request('/api/d/' + encodeURIComponent(names[i]) + '/settings', {
          method: 'PUT', body: JSON.stringify(patch),
        });
      }
      n++;
    }

    keys.forEach(function (k) { localStorage.removeItem(k); });
    return n;
  }

  /**
   * 残留旧设置时展示导入按钮并绑定导入流程, 成功后隐藏按钮.
   *
   * Returns:
   *   void
   *
   * Callers:
   *   - ``static/js/index.js:init``
   */
  function bindImportLegacy() {
    var btn = document.getElementById('btn-import-legacy');
    if (!btn || !legacyKeys().length) return;
    btn.hidden = false;
    btn.addEventListener('click', function () {
      btn.disabled = true;
      importLegacy().then(function (n) {
        btn.hidden = true;
        toast(n > 0 ? '已导入 ' + n + ' 个任务的旧设置' : '旧设置已导入');
      }).catch(function (e) {
        toast('导入失败: ' + String(e.message || e));
        btn.disabled = false;
      });
    });
  }

  window.addEventListener('popstate', function () {
    loadDoc(currentSlug());
  });

  async function init() {
    var data = await API.request('/api/docs');
    _nav = data.nav || [];

    var sortGroup = document.getElementById('sort-group');
    if (sortGroup) {
      sortGroup.addEventListener('click', function (e) {
        var btn = e.target.closest('.sort-btn');
        if (!btn) return;
        var newBy = btn.getAttribute('data-sort');
        var newOrder = btn.getAttribute('data-order');
        if (!newBy) return;

        if (_sortBy === newBy) {
          if (newBy === 'sort_order') return;
          newOrder = newOrder === 'asc' ? 'desc' : 'asc';
        }
        _sortBy = newBy;
        _sortOrder = newOrder || 'asc';

        updateSortButtons();
        loadTasks();
      });
    }

    setupDragDrop();
    await Promise.all([loadDoc(currentSlug()), loadTasks(), loadCandidates()]);
    bindImportLegacy();
    reconnectArchive();
  }

  /**
   * 页面加载时检查未结束的归档/恢复任务并重连 SSE。
   */
  async function reconnectArchive() {
    var snap = await API.findActiveJob('archive');
    if (!snap) return;
    var card = document.querySelector('.task-card[data-task-name="' + snap.dataset + '"]');
    if (!card) {
      card = document.querySelector('#task-archived .task-card');
    }
    if (!card) return;
    var prog = attachProgress(card);
    prog.msg.textContent = snap.message || snap.phase || '恢复连接…';
    if (snap.pct != null) prog.bar.firstChild.style.width = snap.pct + '%';
    API.watchJob(snap.job_id, function (ev) {
      if (ev.pct != null) prog.bar.firstChild.style.width = ev.pct + '%';
      prog.msg.textContent = ev.message || ev.phase || '';
      if (ev.status === 'done') {
        toast('归档完成');
        loadTasks();
      } else if (ev.status === 'error') {
        toast('归档失败: ' + (ev.message || ''));
        removeProgress(card);
      }
    });
  }

  function updateSortButtons() {
    var group = document.getElementById('sort-group');
    if (!group) return;
    group.querySelectorAll('.sort-btn').forEach(function (b) {
      b.classList.remove('active');
      var s = b.getAttribute('data-sort');
      var label = '';
      if (s === 'sort_order') { label = '手动'; }
      else if (s === 'name') { label = '名称'; }
      else if (s === 'image_count') { label = '图像数'; }
      else if (s === 'created') { label = '创建时间'; }
      if (s === _sortBy) {
        b.classList.add('active');
        b.setAttribute('data-order', _sortOrder);
        if (s !== 'sort_order') {
          label += ' ' + (_sortOrder === 'asc' ? '↑' : '↓');
        }
      }
      b.textContent = label;
    });
  }

  function setupDragDrop() {
    var activeGrid = document.getElementById('task-active');

    // 按下时按位置动态决定卡片是否可拖: 仅卡片本体(空白/底部区域)可拖排序,
    // 内部元素按下则临时禁拖, 保留其浏览器默认行为(选中文本/拖拽图片等).
    activeGrid.addEventListener('mousedown', function (e) {
      var card = e.target.closest('.task-card.sortable');
      if (card) card.draggable = (e.target === card);
    });

    activeGrid.addEventListener('dragstart', function (e) {
      var card = e.target.closest('.task-card.sortable');
      if (!card) { e.preventDefault(); return; }
      _dragSrcName = card.dataset.taskName;
      card.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', _dragSrcName);
    });

    activeGrid.addEventListener('dragend', function (e) {
      var card = e.target.closest('.task-card.sortable');
      if (card) card.classList.remove('dragging');
      _dragSrcName = null;
      activeGrid.querySelectorAll('.drag-over').forEach(function (c) { c.classList.remove('drag-over'); });
    });

    activeGrid.addEventListener('dragover', function (e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      var card = e.target.closest('.task-card.sortable');
      if (!card || card.dataset.taskName === _dragSrcName) return;
      activeGrid.querySelectorAll('.drag-over').forEach(function (c) { c.classList.remove('drag-over'); });
      card.classList.add('drag-over');
    });

    activeGrid.addEventListener('dragleave', function (e) {
      var card = e.target.closest('.task-card.sortable');
      if (card) card.classList.remove('drag-over');
    });

    activeGrid.addEventListener('drop', function (e) {
      e.preventDefault();
      var target = e.target.closest('.task-card.sortable');
      if (!target || !_dragSrcName || target.dataset.taskName === _dragSrcName) return;
      target.classList.remove('drag-over');

      var cards = Array.from(activeGrid.querySelectorAll('.task-card.sortable'));
      var ordered = cards.map(function (c) { return c.dataset.taskName; });
      var srcIdx = ordered.indexOf(_dragSrcName);
      var dstIdx = ordered.indexOf(target.dataset.taskName);
      ordered.splice(srcIdx, 1);
      ordered.splice(dstIdx, 0, _dragSrcName);

      API.request('/api/tasks/reorder', { method: 'POST', body: JSON.stringify({ order: ordered }) }).then(function () {
        return loadTasks();
      });
    });
  }

  init().catch(function (err) {
    toast(String(err.message || err));
  });
})();