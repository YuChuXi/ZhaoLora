/**
 * tasks_home.js —— 首页任务卡片：添加、进入、复制链接、完成、重新打开。
 *
 * 引用：templates/home.html。
 */
(function () {
  'use strict';

  var _toastTimer = null;

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

    var actions = document.createElement('div');
    actions.className = 'task-actions';

    if (!completed) {
      var enter = document.createElement('a');
      enter.className = 'btn green';
      enter.href = href;
      enter.textContent = '进入';
      actions.appendChild(enter);
    }

    var copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'btn';
    copy.textContent = '复制链接';
    copy.addEventListener('click', function () {
      navigator.clipboard.writeText(abs).then(function () {
        toast('已复制');
      });
    });
    actions.appendChild(copy);

    if (completed) {
      var reopen = document.createElement('button');
      reopen.type = 'button';
      reopen.className = 'btn';
      reopen.textContent = '重新打开';
      reopen.addEventListener('click', function () {
        postJson('/api/tasks/' + encodeURIComponent(t.name) + '/reopen', {}).then(load);
      });
      actions.appendChild(reopen);
    } else {
      var done = document.createElement('button');
      done.type = 'button';
      done.className = 'btn';
      done.textContent = '完成';
      done.addEventListener('click', function () {
        postJson('/api/tasks/' + encodeURIComponent(t.name) + '/complete', {}).then(load);
      });
      actions.appendChild(done);
    }

    card.innerHTML =
      '<h3></h3><div class="meta"></div>';
    card.querySelector('h3').textContent = t.name;
    card.querySelector('.meta').textContent =
      '图像 ' + (t.image_count || 0) +
      (t.report_ready ? ' · 有美学报告' : '');
    card.appendChild(previews);
    card.appendChild(actions);
    return card;
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
   * 刷新任务列表。
   * 引用：表单提交、完成、重新打开。
   */
  async function load() {
    var res = await fetch('/api/tasks');
    if (!res.ok) throw new Error('无法读取任务列表');
    var data = await res.json();
    var tasks = data.tasks || [];
    var active = document.getElementById('task-active');
    var completedHost = document.getElementById('task-completed');
    var empty = document.getElementById('task-empty');
    var wrap = document.getElementById('completed-wrap');
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
    empty.hidden = nActive > 0;
    wrap.hidden = nDone === 0;
    document.getElementById('completed-count').textContent = String(nDone);
  }

  document.getElementById('task-add-form').addEventListener('submit', function (e) {
    e.preventDefault();
    var name = document.getElementById('task-name').value.trim();
    postJson('/api/tasks', { name: name })
      .then(function () {
        document.getElementById('task-name').value = '';
        toast('已添加 ' + name);
        return load();
      })
      .catch(function (err) { toast(String(err.message || err)); });
  });

  load().catch(function (err) { toast(String(err.message || err)); });
})();
