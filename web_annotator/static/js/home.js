/**
 * home.js —— 首页：任务卡片、添加补全、左侧文档导航。
 *
 * 引用：templates/home.html。
 */
(function () {
  'use strict';

  var _toastTimer = null;
  var _candidates = [];
  var _suggestIndex = -1;
  var _nav = [];

  /**
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
   * @param {string} url
   * @param {object} body
   * @returns {Promise<object>}
   */
  async function postJson(url, body) {
    var res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      var t = await res.text();
      throw new Error(t || res.statusText);
    }
    return res.json();
  }

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
    var res = await fetch('/api/docs/' + encodeURIComponent(slug));
    if (!res.ok) {
      article.innerHTML = '<p class="docs-empty">文档加载失败：' + res.status + '</p>';
      return;
    }
    var data = await res.json();
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
   * @param {object} t
   * @param {boolean} completed
   * @returns {HTMLElement}
   */
  function buildCard(t, completed) {
    var card = document.createElement('div');
    card.className = 'task-card';
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
      var reopen = document.createElement('button');
      reopen.type = 'button';
      reopen.className = 'btn';
      reopen.textContent = '重新打开';
      reopen.addEventListener('click', function () {
        postJson('/api/tasks/' + encodeURIComponent(t.name) + '/reopen', {}).then(loadTasks);
      });
      actions.appendChild(reopen);
    } else {
      var done = document.createElement('button');
      done.type = 'button';
      done.className = 'btn';
      done.textContent = '完成';
      done.addEventListener('click', function () {
        postJson('/api/tasks/' + encodeURIComponent(t.name) + '/complete', {}).then(loadTasks);
      });
      actions.appendChild(done);
    }

    body.appendChild(title);
    body.appendChild(meta);
    body.appendChild(actions);
    card.appendChild(previews);
    card.appendChild(body);
    return card;
  }

  async function loadTasks() {
    var res = await fetch('/api/tasks');
    if (!res.ok) throw new Error('无法读取任务列表');
    var data = await res.json();
    var tasks = data.tasks || [];
    var active = document.getElementById('task-active');
    var completedHost = document.getElementById('task-completed');
    active.innerHTML = '';
    completedHost.innerHTML = '';
    var nActive = 0;
    var nDone = 0;
    tasks.forEach(function (t) {
      if (t.completed) {
        nDone += 1;
        completedHost.appendChild(buildCard(t, true));
      } else {
        nActive += 1;
        active.appendChild(buildCard(t, false));
      }
    });
    document.getElementById('task-empty').hidden = nActive > 0;
    document.getElementById('completed-wrap').hidden = nDone === 0;
    document.getElementById('completed-count').textContent = String(nDone);
  }

  async function loadCandidates() {
    var res = await fetch('/api/tasks/candidates');
    _candidates = res.ok ? ((await res.json()).candidates || []) : [];
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
    postJson('/api/tasks', { name: name })
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
    var res = await fetch('/api/docs');
    if (!res.ok) throw new Error('无法读取文档目录');
    var data = await res.json();
    _nav = data.nav || [];
    await Promise.all([loadDoc(currentSlug()), loadTasks(), loadCandidates()]);
  }

  init().catch(function (err) {
    toast(String(err.message || err));
  });
})();
