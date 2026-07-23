/**
 * ready.js —— 导出训练 datasets TOML（全部子目录）
 */
(function () {
  'use strict';

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

  async function generate() {
    var repeats = parseInt(document.getElementById('num-repeats').value, 10);
    if (!(repeats >= 1)) {
      toast('num_repeats 无效');
      return;
    }
    var data = await api('/api/ready/toml', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dirs: null, num_repeats: repeats })
    });
    document.getElementById('toml-box').value = data.toml || '';
    document.getElementById('stat-summary').textContent =
      (data.subsets || []).length + ' 个 subset';
    if (data.root) document.getElementById('root-path').textContent = data.root;
    toast('已生成');
  }

  async function copyText() {
    var text = document.getElementById('toml-box').value;
    if (!text) {
      toast('无可复制内容');
      return;
    }
    await navigator.clipboard.writeText(text);
    toast('已复制');
  }

  document.getElementById('btn-gen').addEventListener('click', function () {
    generate().catch(function (e) { toast(String(e.message || e)); });
  });
  document.getElementById('btn-copy').addEventListener('click', function () {
    copyText().catch(function (e) { toast(String(e.message || e)); });
  });

  api('/api/ready/dirs')
    .then(function (data) {
      document.getElementById('root-path').textContent = data.root || '';
      return generate();
    })
    .catch(function (e) { toast(String(e.message || e)); });
})();
