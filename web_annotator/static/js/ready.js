/**
 * ready.js —— 导出训练 datasets TOML
 */
(function () {
  'use strict';

  var _dirs = [];
  var _toastTimer = null;

  function toast(msg) {
    var el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () { el.classList.remove('show'); }, 2200);
  }

  async function api(url, opts) {
    var res = await fetch(url, opts);
    if (!res.ok) {
      var t = await res.text();
      throw new Error(t || res.statusText);
    }
    return res.json();
  }

  function selectedNames() {
    return _dirs.filter(function (d) { return d.checked; }).map(function (d) { return d.name; });
  }

  function renderDirs() {
    var list = document.getElementById('dir-list');
    list.innerHTML = '';
    if (!_dirs.length) {
      list.innerHTML = '<div style="padding:8px;color:var(--text-dim);font-size:12px;">暂无图片目录</div>';
      return;
    }
    _dirs.forEach(function (d, idx) {
      var row = document.createElement('label');
      row.className = 'dir-item';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = !!d.checked;
      cb.addEventListener('change', function () {
        _dirs[idx].checked = cb.checked;
      });
      var name = document.createElement('span');
      name.textContent = d.label || d.name;
      var meta = document.createElement('span');
      meta.className = 'meta';
      meta.textContent = d.image_count + ' 图';
      row.appendChild(cb);
      row.appendChild(name);
      row.appendChild(meta);
      list.appendChild(row);
    });
  }

  async function loadDirs() {
    var data = await api('/api/ready/dirs');
    _dirs = (data.dirs || []).map(function (d) {
      return {
        name: d.name,
        label: d.label,
        image_count: d.image_count,
        checked: true
      };
    });
    renderDirs();
    document.getElementById('root-path').textContent = data.root || '';
  }

  async function generate() {
    var dirs = selectedNames();
    if (!dirs.length) {
      toast('请选择目录');
      return;
    }
    var repeats = parseInt(document.getElementById('num-repeats').value, 10);
    if (!(repeats >= 1)) {
      toast('num_repeats 无效');
      return;
    }
    var data = await api('/api/ready/toml', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dirs: dirs, num_repeats: repeats })
    });
    document.getElementById('toml-box').value = data.toml || '';
    document.getElementById('stat-summary').textContent =
      (data.subsets || []).length + ' 个 subset';
    toast('已生成');
  }

  async function copyText() {
    var text = document.getElementById('toml-box').value;
    if (!text) {
      toast('没有可复制内容');
      return;
    }
    await navigator.clipboard.writeText(text);
    toast('已复制');
  }

  document.getElementById('btn-all').addEventListener('click', function () {
    _dirs.forEach(function (d) { d.checked = true; });
    renderDirs();
  });
  document.getElementById('btn-none').addEventListener('click', function () {
    _dirs.forEach(function (d) { d.checked = false; });
    renderDirs();
  });
  document.getElementById('btn-gen').addEventListener('click', function () {
    generate().catch(function (e) { toast(String(e.message || e)); });
  });
  document.getElementById('btn-copy').addEventListener('click', function () {
    copyText().catch(function (e) { toast(String(e.message || e)); });
  });

  loadDirs()
    .then(function () { return generate(); })
    .catch(function (e) { toast(String(e.message || e)); });
})();
