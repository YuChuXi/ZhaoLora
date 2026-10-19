/**
 * ready.js —— 就绪页: 按配置格式(由 assets/configs/ 下的模板目录决定)与画风/角色双模式
 * 生成训练配置, 标签页展示与编辑, 导出以编辑后的文本为准; 侧栏蒙版状态与目录。
 *
 * 引用：templates/ready.html。
 */
(function () {
  'use strict';

  var _format = '';            // 配置导出格式, 取自顶栏格式切换段当前选中项的 data-format
  var _variant = '';            // 配置变体, 取自顶栏格式切换段当前选中项的 data-variant
  var _mode = 'style';         // 训练模式: style=画风(rank 16) / character=角色(rank 32)
  var _fileNames = [];         // 当前格式的文件名集合, 取自预览响应(后端按模板目录扫描)
  var _activeFile = null;      // 当前展示的标签页
  var _enabled = {};
  var _resolutions = {};
  var _sideKey = null;

  /**
   * 按预览响应给出的文件名集合重建文件标签页与文本区。集合未变化时不重建,
   * 以保留各文本区的内容(含编辑)与当前标签; 重建后激活首个文件。
   * 文件名由后端扫描 assets/configs/<格式>/ 下的模板得到, 前端不再另行登记。
   * 引用：generate、onFormat。
   * @param {string[]} names 文件名集合
   */
  function renderFileTabs(names) {
    if (names.join('\n') === _fileNames.join('\n')) return;
    _fileNames = names;
    var tabs = document.getElementById('file-tabs');
    var files = document.getElementById('ready-files');
    tabs.innerHTML = '';
    files.innerHTML = '';
    names.forEach(function (name, i) {
      var tab = document.createElement('button');
      tab.className = 'file-tab' + (i === 0 ? ' active' : '');
      tab.dataset.file = name;
      tab.textContent = name;
      tab.addEventListener('click', function () { switchFile(name); });
      tabs.appendChild(tab);
      var box = document.createElement('textarea');
      box.className = 'toml-box' + (i === 0 ? '' : ' hidden');
      box.id = 'file-' + name;
      box.dataset.file = name;
      box.spellcheck = false;
      box.placeholder = '生成后显示';
      files.appendChild(box);
    });
    _activeFile = names.length ? names[0] : null;
  }

  /**
   * 切换当前展示的文件标签页。引用：renderFileTabs(动态绑定的标签页点击)。
   * @param {string} name 目标文件名
   */
  function switchFile(name) {
    _activeFile = name;
    Array.prototype.forEach.call(document.querySelectorAll('#file-tabs .file-tab'), function (t) {
      t.classList.toggle('active', t.getAttribute('data-file') === name);
    });
    Array.prototype.forEach.call(document.querySelectorAll('.toml-box'), function (box) {
      box.classList.toggle('hidden', box.getAttribute('data-file') !== name);
    });
  }

  /**
   * 响应格式切换段点击: 切换状态、清空文件区(待新格式的预览响应按实际文件集合重建)并重新生成。
   * 引用：init。
   * @param {HTMLButtonElement} btn 被点击的格式按钮
   */
  function onFormat(btn) {
    _format = btn.dataset.format;
    _variant = btn.dataset.variant;
    document.querySelectorAll('#format-seg button').forEach(function (b) {
      b.classList.toggle('active', b === btn);
    });
    renderFileTabs([]);
    generate(true);
  }

  /**
   * 刷新侧栏工作目录列表：每目录一个条目, 首行为启用勾选/目录/张数·蒙版, 次行为全宽分辨率输入。
   * 目录集合与蒙版统计均未变化时跳过重建, 保留分辨率输入的焦点与未提交编辑。引用：init、generate。
   * @param {object} data
   */
  function renderSide(data) {
    var list = document.getElementById('dir-list');
    var dirs = data.dirs || [];
    var key = JSON.stringify([
      dirs.map(function (d) { return [d.name, d.image_count]; }),
      data.mask_stats || []
    ]);
    if (key === _sideKey) return;
    _sideKey = key;
    list.innerHTML = '';
    if (!dirs.length) {
      list.innerHTML = '<div class="dir-empty">无含图目录</div>';
      return;
    }
    dirs.forEach(function (d) {
      if (_enabled[d.name] === undefined) _enabled[d.name] = true;
      if (_resolutions[d.name] === undefined) _resolutions[d.name] = '1536,1024,512';
    });
    var statsByDir = {};
    (data.mask_stats || []).forEach(function (m) { statsByDir[m.name] = m; });
    dirs.forEach(function (d) {
      var m = statsByDir[d.name];
      var item = document.createElement('div');
      item.className = 'dir-item';

      var row = document.createElement('div');
      row.className = 'dir-row';

      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = !!_enabled[d.name];
      cb.title = '是否导出该目录';
      cb.addEventListener('change', function () {
        _enabled[d.name] = cb.checked;
        generate(true);
      });
      row.appendChild(cb);

      var name = document.createElement('span');
      name.className = 'dir-name';
      name.textContent = d.name;
      name.title = d.name;
      row.appendChild(name);

      var meta = document.createElement('span');
      meta.className = 'dir-meta';
      meta.title = '图片 | 标注(.txt) | 描述(captions.json) | 蒙版 (后三项仅统计有对应图片的)';
      if (m) {
        var dot = document.createElement('span');
        dot.className = 'status ' + (m.ready ? 'ok' : 'none');
        dot.title = m.ready ? '就绪: 标注齐全, 蒙版齐全或全无' : '未就绪: 标注未齐全或蒙版部分存在';
        meta.appendChild(dot);
        meta.appendChild(document.createTextNode(
          (d.image_count != null ? d.image_count : 0) + ' | ' + (m.text_count || 0) +
          ' | ' + (m.caption_count || 0) + ' | ' + m.mask_complete
        ));
      } else {
        meta.textContent = (d.image_count != null ? d.image_count : 0) + ' | 0 | 0 | 0';
      }
      row.appendChild(meta);

      var resInput = document.createElement('input');
      resInput.type = 'text';
      resInput.className = 'res-input';
      resInput.value = _resolutions[d.name];
      resInput.setAttribute('data-name', d.name);
      resInput.title = '逗号分隔的多个分辨率, 每个分辨率按子集是否携带蒙版目录拆分为蒙版块与普通块; 留空则该子集生成单块无 resolution';
      resInput.addEventListener('input', function () {
        _resolutions[d.name] = resInput.value;
        autoGenerate();
      });

      item.appendChild(row);
      item.appendChild(resInput);
      list.appendChild(item);
    });
  }

  /**
   * 读取顶栏与侧栏参数构造请求体; 参数无效时提示并返回 null。引用：generate、doExport。
   * @param {boolean} silent
   * @returns {object|null} 含 dirs / target_images / subset_resolutions / use_reg / mode 的请求体。
   */
  function collectBody(silent) {
    var target = parseInt(document.getElementById('target-images').value, 10);
    if (!(target >= 1)) {
      if (!silent) toast('目标图片数无效');
      return null;
    }
    var subsetRes = {};
    var inputs = document.querySelectorAll('#dir-list .res-input');
    for (var i = 0; i < inputs.length; i++) {
      var input = inputs[i];
      var name = input.getAttribute('data-name');
      var text = input.value.trim();
      var list = [];
      if (text) {
        var parts = text.split(/\s*,\s*/);
        for (var j = 0; j < parts.length; j++) {
          var n = parseInt(parts[j], 10);
          if (!(n > 0)) {
            if (!silent) toast('目录 ' + name + ' 无效分辨率: ' + parts[j]);
            return null;
          }
          list.push(n);
        }
      }
      subsetRes[name] = list;
    }
    return {
      dirs: Object.keys(_enabled).filter(function (n) { return _enabled[n]; }),
      target_images: target,
      subset_resolutions: subsetRes,
      use_reg: document.getElementById('use-reg').checked,
      mode: _mode,
      train_format: _format,
      variant: _variant
    };
  }

  /**
   * 读取当前全部文本区内容构造 files; 任一为空时提示并返回 null
   * (导出以编辑后的文本为准, 不写空文件)。引用：doExport。
   * @returns {object|null} 文件名→文本。
   */
  function collectFiles() {
    var boxes = document.querySelectorAll('.toml-box');
    var files = {};
    for (var i = 0; i < boxes.length; i++) {
      var name = boxes[i].getAttribute('data-file');
      var text = boxes[i].value;
      if (!text.trim()) {
        toast(name + ' 内容为空, 请先生成');
        return null;
      }
      files[name] = text;
    }
    return files;
  }

  /**
   * 生成当前格式的配置预览并更新侧栏; 输入变化自动调用时静默(silent=true), 仅按钮手动生成时 toast.
   * 文件标签页按响应的 files 键重建(键随格式与模板目录变化)。
   * 引用：init、tpl btn-gen、格式切换、模式切换、autoGenerate。
   * @param {boolean} [silent]
   */
  async function generate(silent) {
    var body = collectBody(silent);
    if (!body) return;
    var format = _format;
    var data;
    try {
      data = await API.request(API.apiPath('/ready/toml'), {
        method: 'POST',
        body: JSON.stringify(body)
      });
    } catch (e) {
      document.getElementById('stat-summary').textContent = String(e.message || e);
      if (!silent) toast(String(e.message || e));
      return;
    }
    if (format !== _format) return;  // 响应返回前格式已切换, 该结果对应旧格式, 丢弃
    var dataFiles = data.files || {};
    renderFileTabs(Object.keys(dataFiles));
    Object.keys(dataFiles).forEach(function (name) {
      var box = document.getElementById('file-' + name);
      if (box) box.value = dataFiles[name];
    });
    var subsets = data.subsets || [];
    var repeats = data.subset_repeats || {};
    var totalImages = subsets.reduce(function (sum, s) {
      return sum + (s.image_count || 0);
    }, 0);
    var repeated = subsets.reduce(function (sum, s) {
      var rs = repeats[s.name] || [];
      return sum + (s.image_count || 0) * rs.reduce(function (a, b) { return a + b; }, 0);
    }, 0);
    var repStr;
    if (!subsets.length) {
      repStr = '0';
    } else {
      var first = (repeats[subsets[0].name] || []).join('+');
      var allSame = subsets.every(function (s) {
        return (repeats[s.name] || []).join('+') === first;
      });
      if (allSame) {
        repStr = first.indexOf('+') >= 0 ? '(' + first + ')' : first;
      } else {
        repStr = subsets.map(function (s) {
          var rs = (repeats[s.name] || []).join('+');
          return s.name + '×' + (rs.indexOf('+') >= 0 ? '(' + rs + ')' : rs);
        }).join(', ');
      }
    }
    document.getElementById('stat-summary').textContent =
      subsets.length + ' 个 subset · ' + totalImages + ' 张 × ' + repStr + ' = ' + repeated + ' 张';
    renderSide(data);
    if (!silent) toast('已生成');
  }

  var _autoTimer = null;

  /**
   * 输入变化触发自动生成(防抖 300ms, 静默).
   */
  function autoGenerate() {
    clearTimeout(_autoTimer);
    _autoTimer = setTimeout(function () { generate(true); }, 300);
  }

  /**
   * 复制当前标签页文本区内容; 非安全上下文(局域网 http 访问)下选中文本供手动复制.
   * 引用: tpl btn-copy。
   */
  async function copyText() {
    var tb = document.getElementById('file-' + _activeFile);
    var text = tb ? tb.value : '';
    if (!text) {
      toast('无可复制内容');
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      toast('已复制 ' + _activeFile);
      return;
    }
    // 非安全上下文(局域网 http 访问)下 clipboard API 不可用: 选中文本供手动复制
    tb.select();
    tb.setSelectionRange(0, tb.value.length);
    toast('剪贴板不可用，已选中文本，请按 Ctrl+C');
  }

  document.getElementById('btn-gen').addEventListener('click', function () {
    generate(false);
  });
  document.getElementById('target-images').addEventListener('input', autoGenerate);
  document.getElementById('btn-copy').addEventListener('click', function () {
    copyText().catch(function (e) { toast(String(e.message || e)); });
  });

  // 配置格式取自顶栏格式切换段当前选中项; 切换后重渲染文件区并重新生成
  var _formatSeg = document.querySelector('#format-seg button.active')
    || document.querySelector('#format-seg button');
  _format = _formatSeg ? _formatSeg.dataset.format : '';
  _variant = _formatSeg ? _formatSeg.dataset.variant : '';
  Array.prototype.forEach.call(document.querySelectorAll('#format-seg button'), function (btn) {
    btn.addEventListener('click', function () { onFormat(btn); });
  });

  // 训练模式切换(画风/角色): 切换后按新模式重新生成配置
  Array.prototype.forEach.call(document.querySelectorAll('#mode-seg button'), function (btn) {
    btn.addEventListener('click', function () {
      var next = btn.getAttribute('data-mode');
      if (next === _mode) return;
      _mode = next;
      Array.prototype.forEach.call(document.querySelectorAll('#mode-seg button'), function (b) {
        b.classList.toggle('active', b === btn);
      });
      generate(true);
    });
  });

  // 标签页切换监听由 renderFileTabs 动态绑定: 仅切换展示的文本区, 各文本区内容(含编辑)独立保留

  /**
   * 按当前参数与编辑后的配置文本导出整套训练配置到格式对应的配置路径.
   * 引用: tpl btn-export、exportConfig。
   * @param {boolean} overwrite 目标目录已存在配置时置 true 覆盖写入。
   */
  async function doExport(overwrite) {
    var body = collectBody(false);
    if (!body) return;
    var files = collectFiles();
    if (!files) return;
    body.files = files;
    var url = API.apiPath('/ready/export');
    if (overwrite) url += '?overwrite=1';
    var data = await API.request(url, {
      method: 'POST',
      body: JSON.stringify(body)
    });
    toast('已导出: ' + data.files.join(', '));
  }

  /**
   * 按当前参数把数据集导出为整套训练配置(文件集合随格式变化)到格式对应的配置路径.
   * 目标目录已存在配置时(409)先提示确认覆盖.
   * 引用: tpl btn-export。
   */
  async function exportConfig() {
    try {
      await doExport(false);
    } catch (e) {
      var status = parseInt(String(e.message), 10);
      if (status === 409) {
        if (window.confirm('目标目录已存在配置, 是否覆盖写入?')) {
          await doExport(true);
          return;
        }
      }
      throw e;
    }
  }

  document.getElementById('btn-export').addEventListener('click', function () {
    exportConfig().catch(function (e) { toast(String((e && e.message) || e)); });
  });

  API.request(API.apiPath('/ready/dirs'))
    .then(function (data) {
      renderSide(data);
      return generate();
    })
    .catch(function (e) { toast(String(e.message || e)); });
})();
