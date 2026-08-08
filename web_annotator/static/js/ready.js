/**
 * ready.js —— 导出训练 TOML；侧栏蒙版状态与目录，顶栏仅操作与 subset 数。
 *
 * 引用：templates/ready.html。
 */
(function () {
  'use strict';

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
   * 刷新侧栏工作目录表格：目录、张数、蒙版状态。引用：init、generate。
   * @param {object} data
   */
  function renderSide(data) {
    var tbody = document.getElementById('dir-tbody');
    tbody.innerHTML = '';
    var dirs = data.dirs || [];
    if (!dirs.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="empty">无含图目录</td></tr>';
      return;
    }
    var statsByDir = {};
    (data.mask_stats || []).forEach(function (m) { statsByDir[m.name] = m; });
    dirs.forEach(function (d) {
      var m = statsByDir[d.name];
      var row = document.createElement('tr');

      var nameCell = document.createElement('td');
      nameCell.textContent = d.name;

      var countCell = document.createElement('td');
      countCell.textContent = (d.image_count != null ? d.image_count : 0) + ' 张';

      var maskCell = document.createElement('td');
      maskCell.className = 'mask';
      if (m) {
        var dot = document.createElement('span');
        dot.className = 'status ' + (m.mask_status === '完整' ? 'ok' : 'none');
        maskCell.appendChild(dot);
        maskCell.title = '蒙版完成 / 图片总数';
        maskCell.appendChild(document.createTextNode(
          m.mask_complete + '/' + (m.mask_complete + m.mask_none)
        ));
      } else {
        maskCell.textContent = '-';
      }

      row.appendChild(nameCell);
      row.appendChild(countCell);
      row.appendChild(maskCell);
      tbody.appendChild(row);
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
    var subsets = data.subsets || [];
    var totalImages = subsets.reduce(function (sum, s) {
      return sum + (s.image_count || 0);
    }, 0);
    document.getElementById('stat-summary').textContent =
      subsets.length + ' 个 subset · 共 ' + totalImages + ' 张图片';
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
