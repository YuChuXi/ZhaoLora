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
   * @param {HTMLElement} noteWrap
   * @param {string} name
   */
  function startEditNote(noteWrap, name) {
    if (noteWrap.querySelector('textarea')) return;
    var old = noteWrap.textContent;
    if (old === '双击添加备注…') old = '';
    var input = document.createElement('textarea');
    input.className = 'task-note-input';
    input.value = old;
    input.rows = Math.max(1, old.split('\n').length) || 1;
    noteWrap.textContent = '';
    noteWrap.appendChild(input);
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    function finish() {
      var val = input.value.trim();
      if (val === old) {
        noteWrap.textContent = old || '双击添加备注…';
        noteWrap.className = 'task-note' + (val ? '' : ' empty');
        return;
      }
      API.request('/api/tasks/' + encodeURIComponent(name) + '/note', { method: 'POST', body: JSON.stringify({ note: val }) })
        .then(function () {
          noteWrap.textContent = val || '双击添加备注…';
          noteWrap.className = 'task-note' + (val ? '' : ' empty');
        })
        .catch(function (err) {
          noteWrap.textContent = old || '双击添加备注…';
          noteWrap.className = 'task-note' + (old ? '' : ' empty');
          toast(String(err.message || err));
        });
    }
    input.addEventListener('blur', finish);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        input.blur();
      }
      if (e.key === 'Escape') {
        input.value = old;
        input.blur();
      }
    });
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
            loadArchived();
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
            loadArchived();
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
   * 构建已归档任务卡片.
   * @param {object} a
   * @returns {HTMLElement}
   */
  function buildArchivedCard(a) {
    var card = document.createElement('div');
    card.className = 'task-card';

    var previews = document.createElement('div');
    previews.className = 'task-previews';
    var hashes = a.preview_hashes || [];
    for (var i = 0; i < 4; i++) {
      if (hashes[i]) {
        var img = document.createElement('img');
        img.src = '/api/archived/' + encodeURIComponent(a.base) + '/preview/' + hashes[i] + '.avif';
        img.alt = '';
        previews.appendChild(img);
      } else {
        var slot = document.createElement('div');
        slot.className = 'empty-slot';
        previews.appendChild(slot);
      }
    }

    var body = document.createElement('div');
    body.className = 'task-body';
    var title = document.createElement('h3');
    title.textContent = a.name;
    var meta = document.createElement('div');
    meta.className = 'meta';
    var d = new Date(a.archived_at * 1000);
    var ds = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    meta.textContent = '图像 ' + a.image_count + ' · ' + ds + ' · ' + a.format.toUpperCase();

    var actions = document.createElement('div');
    actions.className = 'task-actions';
    var restore = document.createElement('button');
    restore.type = 'button';
    restore.className = 'btn green';
    restore.textContent = '恢复';
    restore.addEventListener('click', function () {
      restoreArchive(a.base, card);
    });
    actions.appendChild(restore);

    body.appendChild(title);
    body.appendChild(meta);
    body.appendChild(actions);
    card.appendChild(previews);
    card.appendChild(body);
    return card;
  }

  /**
   * 加载已归档任务列表.
   * @returns {Promise<void>}
   */
  async function loadArchived() {
    var data = await API.request('/api/archived');
    var archives = data.archives || [];
    var host = document.getElementById('task-archived');
    host.innerHTML = '';
    document.getElementById('archived-wrap').hidden = archives.length === 0;
    document.getElementById('archived-count').textContent = String(archives.length);
    archives.forEach(function (a) {
      host.appendChild(buildArchivedCard(a));
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
        img.src = '/d/' + encodeURIComponent(t.name) + '/thumb/' + hashes[i] + '.avif';
        img.alt = '';
        previews.appendChild(img);
      } else {
        var slot = document.createElement('div');
        slot.className = 'empty-slot';
        previews.appendChild(slot);
      }
    }
    if (!completed) {
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
    meta.textContent =
      '图像 ' + (t.image_count || 0) + (t.report_ready ? ' · 有美学报告' : '');

    var noteWrap = document.createElement('div');
    noteWrap.className = 'task-note' + (t.note ? '' : ' empty');
    noteWrap.textContent = t.note || '双击添加备注…';
    noteWrap.addEventListener('dblclick', function () {
      startEditNote(noteWrap, t.name);
    });

    var triggerRow = document.createElement('div');
    triggerRow.className = 'task-trigger';
    var triggerLabel = document.createElement('span');
    triggerLabel.className = 'task-trigger-label';
    triggerLabel.textContent = '触发词';
    var triggerInput = document.createElement('input');
    triggerInput.type = 'text';
    triggerInput.className = 'task-trigger-input';
    triggerInput.value = t.trigger_word || '';
    triggerInput.placeholder = '无';
    triggerInput.maxLength = 100;
    triggerInput.addEventListener('change', function () {
      API.request('/api/tasks/' + encodeURIComponent(t.name) + '/trigger-word', { method: 'POST', body: JSON.stringify({ trigger_word: triggerInput.value.trim() }) })
        .catch(function (err) { toast(String(err.message || err)); });
    });
    var triggerCopy = document.createElement('button');
    triggerCopy.type = 'button';
    triggerCopy.className = 'task-trigger-copy';
    triggerCopy.textContent = '复制';
    triggerCopy.addEventListener('click', function () {
      var v = triggerInput.value.trim();
      if (!v) {
        toast('触发词为空');
        return;
      }
      navigator.clipboard.writeText(v).then(function () { toast('已复制触发词'); });
    });
    triggerRow.appendChild(triggerLabel);
    triggerRow.appendChild(triggerInput);
    triggerRow.appendChild(triggerCopy);

    var actions = document.createElement('div');
    actions.className = 'task-actions';
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

    body.appendChild(title);
    body.appendChild(meta);
    body.appendChild(noteWrap);
    body.appendChild(triggerRow);
    body.appendChild(actions);
    card.appendChild(previews);
    card.appendChild(body);

    if (sortable) {
      var handle = document.createElement('div');
      handle.className = 'task-drag-handle';
      handle.textContent = '⠿';
      card.appendChild(handle);
    }
    return card;
  }

  async function loadTasks() {
    var url = '/api/tasks?sort_by=' + encodeURIComponent(_sortBy) + '&sort_order=' + encodeURIComponent(_sortOrder);
    var data = await API.request(url);
    var tasks = data.tasks || [];
    var active = document.getElementById('task-active');
    var completedHost = document.getElementById('task-completed');
    active.innerHTML = '';
    completedHost.innerHTML = '';
    var nActive = 0;
    var nDone = 0;
    var isManual = _sortBy === 'sort_order';
    tasks.forEach(function (t) {
      if (t.completed) {
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
    await Promise.all([loadDoc(currentSlug()), loadTasks(), loadCandidates(), loadArchived()]);
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
        loadArchived();
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