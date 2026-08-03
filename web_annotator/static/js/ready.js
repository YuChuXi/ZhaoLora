/**
 * ready.js —— 导出训练 TOML；侧栏蒙版状态与目录，顶栏仅操作与 subset 数。
 *
 * 引用：templates/ready.html。
 */
(function () {
  'use strict';

  var _toastTimer = null;

  /**
   * @param {string} msg
   */
  function toast(msg) {
    var el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () { el.classList.remove('show'); }, 2200);
  }

  /**
   * @param {string} url
   * @param {object} [opts]
   * @returns {Promise<object>}
   */
  async function api(url, opts) {
    var res = await fetch(url, opts);
    if (!res.ok) {
      var t = await res.text();
      throw new Error(t || res.statusText);
    }
    return res.json();
  }

  /**
   * 刷新侧栏。引用：init、generate。
   * @param {object} data
   */
  function renderSide(data) {
    var status = data.mask_status || '无';
    var complete = data.mask_complete || 0;
    var none = data.mask_none || 0;
    var total = data.main_count || 0;
    var box = document.getElementById('mask-summary');
    box.innerHTML =
      '<span class="status ' + (status === '完整' ? 'ok' : 'none') + '">' + status + '</span>' +
      '完整 ' + complete + ' · 无 ' + none +
      (total ? '（共 ' + total + '）' : '');

    var list = document.getElementById('dir-list');
    list.innerHTML = '';
    var dirs = data.dirs || [];
    if (!dirs.length) {
      list.innerHTML = '<div class="dir-item"><span class="meta">无含图目录</span></div>';
      return;
    }
    var statsByDir = {};
    (data.mask_stats || []).forEach(function (m) { statsByDir[m.name] = m; });
    dirs.forEach(function (d) {
      var row = document.createElement('div');
      row.className = 'dir-item';
      var name = document.createElement('span');
      name.textContent = d.name;
      var meta = document.createElement('span');
      meta.className = 'meta';
      var m = statsByDir[d.name];
      var txt = (d.image_count != null ? d.image_count : 0) + ' 张';
      if (m) txt += ' · ' + m.mask_status + '（完整 ' + m.mask_complete + ' / 无 ' + m.mask_none + '）';
      meta.textContent = txt;
      row.appendChild(name);
      row.appendChild(meta);
      list.appendChild(row);
    });
  }

  async function generate() {
    var repeats = parseInt(document.getElementById('num-repeats').value, 10);
    if (!(repeats >= 1)) {
      toast('num_repeats 无效');
      return;
    }
    var data = await api(API.apiPath('/ready/toml'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dirs: null, num_repeats: repeats })
    });
    document.getElementById('toml-box').value = data.toml || '';
    document.getElementById('stat-summary').textContent =
      (data.subsets || []).length + ' 个 subset';
    renderSide(data);
    toast('已生成');
  }

  async function copyText() {
    var tb = document.getElementById('toml-box');
    var text = tb.value;
    if (!text) {
      toast('无可复制内容');
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      toast('已复制');
      return;
    }
    // 非安全上下文(局域网 http 访问)下 clipboard API 不可用: 选中文本供手动复制
    tb.select();
    tb.setSelectionRange(0, tb.value.length);
    toast('剪贴板不可用，已选中文本，请按 Ctrl+C');
  }

  document.getElementById('btn-gen').addEventListener('click', function () {
    generate().catch(function (e) { toast(String(e.message || e)); });
  });
  document.getElementById('btn-copy').addEventListener('click', function () {
    copyText().catch(function (e) { toast(String(e.message || e)); });
  });

  api(API.apiPath('/ready/dirs'))
    .then(function (data) {
      renderSide(data);
      return generate();
    })
    .catch(function (e) { toast(String(e.message || e)); });
})();
