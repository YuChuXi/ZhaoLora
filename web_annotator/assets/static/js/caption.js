/**
 * caption.js —— 描述页: 调用 Ollama 视觉模型批量生成自然语言描述, 写入各工作目录的 captions.json。
 *
 * 依赖: api.js / dir_picker.js / nav.js / masonry.js
 */
(function () {
  'use strict';

  // 描述模式的后端取值 → 界面文案(取值集合由 /caption/dirs 返回)
  var MODE_LABELS = { coarse: '粗略', detailed: '详细' };

  var _items = [];
  var _jobId = null;
  var _templates = [];

  /**
   * 读取「caption」设置键的当前值。
   * 引用：loadSaved / loadInject / persistRules / persistInject。
   * @returns {{rules?:Array<object>, inject?:Object}}
   */
  function captionState() {
    var state = AppSettings.get('caption');
    return state && typeof state === 'object' && !Array.isArray(state) ? state : {};
  }

  /**
   * 读取已保存的提示词条目列表。
   * @returns {Array<object>}
   */
  function loadSaved() {
    var arr = captionState().rules;
    return Array.isArray(arr) ? arr : [];
  }

  /**
   * 写入提示词条目列表。
   * @param {Array<object>} arr
   */
  function saveSaved(arr) {
    var state = captionState();
    state.rules = arr;
    AppSettings.set('caption', state);
  }

  /** 保存 WD14 注入设置、NSFW 增强与描述模式/覆盖选项。引用: mode-seg/force 变更监听、closeSettings。 */
  function persistInject() {
    var state = captionState();
    state.inject = {
      general: document.getElementById('inject-general').checked,
      general_threshold: document.getElementById('general-threshold').value,
      character: document.getElementById('inject-character').checked,
      character_threshold: document.getElementById('character-threshold').value,
      nsfw: document.getElementById('inject-nsfw').checked,
      nsfw_threshold: document.getElementById('nsfw-threshold').value,
    };
    state.modes = selectedModes();
    state.force = document.getElementById('force').checked;
    AppSettings.set('caption', state);
  }

  /**
   * 点击注入开关时同步对应阈值输入框的可用状态。
   * @param {string} checkboxId
   * @param {string} inputId
   */
  function syncThresholdEnabled(checkboxId, inputId) {
    document.getElementById(inputId).disabled = !document.getElementById(checkboxId).checked;
  }

  /**
   * 读取一个标签类别的注入阈值。
   * @param {string} checkboxId
   * @param {string} inputId
   * @returns {number|null|boolean} 数=该类别下限; null=未勾选不注入; false=取值非法
   */
  function readThreshold(checkboxId, inputId) {
    if (!document.getElementById(checkboxId).checked) return null;
    var value = parseFloat(document.getElementById(inputId).value);
    return value >= 0 && value <= 1 ? value : false;
  }

  // ==================== 模态 ====================

  /**
   * 打开模态。
   * @param {string} id
   */
  function openDlg(id) {
    var dlg = document.getElementById(id);
    dlg.classList.add('open');
    dlg.setAttribute('aria-hidden', 'false');
  }

  /**
   * 关闭模态。
   * @param {string} id
   */
  function closeDlg(id) {
    var dlg = document.getElementById(id);
    dlg.classList.remove('open');
    dlg.setAttribute('aria-hidden', 'true');
  }

  // ==================== 描述设置 ====================

  /**
   * 创建文本输入框。
   * @param {string} placeholder 占位提示
   * @param {string} [value] 初始值
   * @returns {HTMLInputElement}
   */
  function mkText(placeholder, value) {
    var el = document.createElement('input');
    el.type = 'text';
    el.placeholder = placeholder;
    el.value = value || '';
    return el;
  }

  /**
   * 创建条目删除按钮(平时低调, 悬停转为强调色)。
   * @param {Function} onDelete 点击回调
   * @returns {HTMLButtonElement}
   */
  function mkDel(onDelete) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cap-del';
    btn.textContent = '✕';
    btn.title = '删除该条';
    btn.addEventListener('click', onDelete);
    return btn;
  }

  /**
   * 追加一行提示词条目。
   * @param {{text?: string, pattern?: string}} [rule]
   */
  function addRuleRow(rule) {
    rule = rule || {};
    var row = document.createElement('div');
    row.className = 'cap-table';

    var pattern = mkText('如 *tail*', rule.pattern);
    pattern.title = '逗号分隔的标签模式, * 为通配; 留空则对该数据集全部图片生效';

    var text = mkText('提示词内容, 如 尾巴的动作要详细描述', rule.text);

    row.appendChild(pattern);
    row.appendChild(text);
    row.appendChild(mkDel(function () { row.remove(); persistRules(); }));
    document.getElementById('rule-list').appendChild(row);
  }

  /**
   * 收集当前全部提示词条目(内容为空的行丢弃)。
   * @returns {Array<{text: string, pattern: string}>}
   */
  function collectRules() {
    var rows = document.querySelectorAll('#rule-list .cap-table');
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var inputs = rows[i].querySelectorAll('input[type="text"]');
      var text = inputs[1].value.trim();
      if (!text) continue;
      out.push({
        text: text,
        pattern: inputs[0].value.trim(),
      });
    }
    return out;
  }

  // ==================== 提示词模板 ====================

  /**
   * 返回当前选中的模板(下拉首项「自定义」返回 null)。
   * @returns {object|null}
   */
  function currentTemplate() {
    var label = document.getElementById('rule-template').value;
    for (var i = 0; i < _templates.length; i++) {
      if (_templates[i].label === label) return _templates[i];
    }
    return null;
  }

  /** 渲染模板句式: 句式原样显示, 其中的 {{ 字段 }} 空位替换为待填输入框。 */
  function renderSentence() {
    var box = document.getElementById('rule-fields');
    var tpl = currentTemplate();
    box.textContent = '';
    box.classList.toggle('empty', !tpl);
    if (!tpl) {
      box.textContent = '自定义条目: 添加后在下表直接填写提示词内容';
      return;
    }
    var re = /\{\{\s*([^}]+?)\s*\}\}/g;
    var last = 0;
    var m;
    while ((m = re.exec(tpl.text)) !== null) {
      box.appendChild(document.createTextNode(tpl.text.slice(last, m.index)));
      var blank = document.createElement('input');
      blank.type = 'text';
      blank.className = 'cap-blank';
      blank.dataset.field = m[1];
      blank.placeholder = m[1];
      blank.title = '填写「' + m[1] + '」';
      box.appendChild(blank);
      last = re.lastIndex;
    }
    box.appendChild(document.createTextNode(tpl.text.slice(last)));
  }

  /**
   * 用空位取值还原模板句式(Jinja 变量语法 {{ 字段 }})。
   * @param {string} source
   * @param {Object<string, string>} values
   * @returns {string}
   */
  function renderTemplate(source, values) {
    return source.replace(/\{\{\s*([^}]+?)\s*\}\}/g, function (match, name) {
      return Object.prototype.hasOwnProperty.call(values, name) ? values[name] : match;
    });
  }

  /**
   * 按句式行的空位取值生成一条条目(触发方式由用户在条目上设置)。
   * @returns {{text: string}|null} null 表示有空位未填
   */
  function buildFromTemplate() {
    var tpl = currentTemplate();
    if (!tpl) return { text: '' };
    var values = {};
    var blanks = document.querySelectorAll('#rule-fields .cap-blank');
    for (var i = 0; i < blanks.length; i++) {
      var value = blanks[i].value.trim();
      if (!value) {
        toast('请填写「' + blanks[i].dataset.field + '」');
        return null;
      }
      values[blanks[i].dataset.field] = value;
    }
    return { text: renderTemplate(tpl.text, values) };
  }

  /**
   * 渲染模板下拉框(首项为「自定义」)并显示所选模板的句式空位。
   * @param {Array<object>} templates /caption/dirs 返回的模板列表
   */
  function renderTemplates(templates) {
    _templates = templates || [];
    var sel = document.getElementById('rule-template');
    sel.innerHTML = '';
    var custom = document.createElement('option');
    custom.value = '';
    custom.textContent = '自定义（无模板）';
    sel.appendChild(custom);
    _templates.forEach(function (tpl) {
      var option = document.createElement('option');
      option.value = tpl.label;
      option.textContent = tpl.label;
      sel.appendChild(option);
    });
    renderSentence();
  }

  /** 收集并保存当前预设提示词。 */
  function persistRules() {
    saveSaved(collectRules());
  }

  /** 保存设置并关闭描述设置对话框。 */
  function closeSettings() {
    persistRules();
    persistInject();
    closeDlg('rules-dlg');
  }

  /** 按「caption」设置键恢复提示词条目。 */
  function loadSettings() {
    loadSaved().forEach(addRuleRow);
  }

  // ==================== 瀑布流 ====================

  /**
   * 卡片装饰: 已有描述的卡片标绿并在悬停提示中展示描述全文。
   * @param {HTMLElement} card
   * @param {object} img
   */
  function decorateCard(card, img) {
    card.classList.remove('tag-ok');
    if (img.captions && img.captions.length) {
      card.classList.add('tag-ok');
      card.title = img.captions.join('\n\n');
    } else {
      card.title = '(未描述)';
    }
  }

  /**
   * 点击卡片: 打开描述查看模态。
   * @param {object} img
   */
  function onCardClick(img) {
    document.getElementById('view-title').textContent = img.path;
    var body = document.getElementById('view-body');
    body.innerHTML = '';
    if (!img.captions || !img.captions.length) {
      var empty = document.createElement('div');
      empty.className = 'cap-sec-title';
      empty.textContent = '该图片尚无描述';
      body.appendChild(empty);
    } else {
      img.captions.forEach(function (text, i) {
        var box = document.createElement('div');
        box.className = 'view-caption';
        var label = document.createElement('span');
        label.className = 'view-label';
        label.textContent = '描述 ' + (i + 1);
        box.appendChild(label);
        box.appendChild(document.createTextNode(text));
        body.appendChild(box);
      });
    }
    openDlg('view-dlg');
  }

  /** 重新拉取所选目录的图片与描述现状。 */
  async function refresh() {
    var res = await API.request(API.apiPath('/caption/list'), {
      method: 'POST',
      body: JSON.stringify({ dirs: DirPicker.getDirs() }),
    });
    _items = res.items || [];
    Masonry.render(_items);
    var total = res.total || 0;
    var described = res.described || 0;
    document.getElementById('stat-total').textContent = total;
    document.getElementById('stat-described').textContent = described;
    document.getElementById('stat-missing').textContent = total - described;
  }

  // ==================== 标注任务 ====================

  /**
   * 当前选中的描述模式(按界面顺序)。
   * @returns {string[]}
   */
  function selectedModes() {
    var out = [];
    Array.prototype.forEach.call(document.querySelectorAll('#mode-seg button'), function (b) {
      if (b.classList.contains('active')) out.push(b.dataset.mode);
    });
    return out;
  }

  /**
   * 更新进度条与日志控制台。
   * @param {object} s 任务快照
   */
  function updateProgress(s) {
    var running = s.status === 'running' || s.status === 'queued';
    document.getElementById('job-progress').hidden = !running;
    var fill = document.getElementById('progress-fill');
    fill.classList.toggle('indeterminate', s.status === 'queued');
    if (s.status === 'running') fill.style.width = Math.min(100, s.pct || 0) + '%';
    document.getElementById('progress-pct').textContent = s.status === 'queued'
      ? '…'
      : (Math.min(100, Math.round(s.pct || 0)) + '%');
    if (s.message) document.getElementById('stat-job').textContent = s.message;
    var box = document.getElementById('log-box');
    if (s.logs && s.logs.length) {
      box.hidden = false;
      box.textContent = s.logs.join('\n');
      box.scrollTop = box.scrollHeight;
    }
  }

  /** 重置任务相关界面控件。 */
  function resetJobUi() {
    document.getElementById('job-progress').hidden = true;
    document.getElementById('btn-cancel').hidden = true;
    document.getElementById('btn-start').disabled = false;
  }

  /**
   * 任务终态处理: 提示结果并刷新列表。
   * @param {object} s 任务快照
   */
  function finishJob(s) {
    _jobId = null;
    resetJobUi();
    if (s.status === 'done') {
      toast(s.message || '标注完成');
    } else if (s.status === 'cancelled') {
      toast(s.message || '已取消');
    } else {
      toast('标注失败: ' + (s.message || ''));
    }
    refresh().catch(function (e) { toast(String(e.message || e)); });
  }

  /**
   * 订阅任务进度, 终态时收尾。
   * @param {string} jobId
   */
  function watch(jobId) {
    _jobId = jobId;
    document.getElementById('btn-cancel').hidden = false;
    API.watchJob(jobId, function (s) {
      updateProgress(s);
      if (s.status === 'done' || s.status === 'error' || s.status === 'cancelled') {
        finishJob(s);
      }
    });
  }

  /** 提交描述标注长任务并订阅进度。 */
  async function startJob() {
    var modes = selectedModes();
    if (!modes.length) {
      toast('请至少选择一种描述模式');
      return;
    }
    var rules = collectRules();
    var generalThreshold = readThreshold('inject-general', 'general-threshold');
    var characterThreshold = readThreshold('inject-character', 'character-threshold');
    var nsfwThreshold = readThreshold('inject-nsfw', 'nsfw-threshold');
    if (generalThreshold === false || characterThreshold === false || nsfwThreshold === false) {
      toast('注入标签与 NSFW 增强的置信度阈值需在 0~1 之间');
      return;
    }
    var body = {
      dirs: DirPicker.getDirs(),
      modes: modes,
      general_threshold: generalThreshold,
      character_threshold: characterThreshold,
      nsfw_threshold: nsfwThreshold,
      rules: rules,
      force: document.getElementById('force').checked,
    };
    document.getElementById('btn-start').disabled = true;
    document.getElementById('job-progress').hidden = false;
    document.getElementById('progress-fill').style.width = '0%';
    try {
      var res = await API.request(API.apiPath('/caption/start'), {
        method: 'POST',
        body: JSON.stringify(body),
      });
      closeSettings();
      watch(res.job_id);
    } catch (e) {
      resetJobUi();
      toast('提交失败: ' + String(e.message || e));
    }
  }

  /** 请求取消当前标注任务。 */
  async function cancelJob() {
    if (!_jobId) return;
    var btn = document.getElementById('btn-cancel');
    btn.disabled = true;
    try {
      await API.cancelJob(_jobId);
    } finally {
      btn.disabled = false;
    }
  }

  /**
   * 按后端返回的模式、注入默认值与提示词模板渲染对话框选项。
   * @param {object} data /caption/dirs 响应
   */
  function renderOptions(data) {
    var seg = document.getElementById('mode-seg');
    seg.innerHTML = '';
    var saved = captionState();
    (data.modes || []).forEach(function (m) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = saved.modes && saved.modes.indexOf(m) < 0 ? '' : 'active';
      b.dataset.mode = m;
      b.textContent = MODE_LABELS[m] || m;
      b.addEventListener('click', function () { b.classList.toggle('active'); persistInject(); });
      seg.appendChild(b);
    });
    var inject = saved.inject || {};
    document.getElementById('inject-general').checked = !!inject.general;
    document.getElementById('general-threshold').value =
      inject.general_threshold || data.default_wd14_threshold;
    document.getElementById('inject-character').checked = !!inject.character;
    document.getElementById('character-threshold').value =
      inject.character_threshold || data.default_wd14_threshold;
    document.getElementById('inject-nsfw').checked = !!inject.nsfw;
    document.getElementById('nsfw-threshold').value =
      inject.nsfw_threshold || data.default_wd14_threshold;
    document.getElementById('force').checked = !!saved.force;
    syncThresholdEnabled('inject-general', 'general-threshold');
    syncThresholdEnabled('inject-character', 'character-threshold');
    syncThresholdEnabled('inject-nsfw', 'nsfw-threshold');
    renderTemplates(data.rule_templates);
  }

  async function init() {
    await AppSettings.ready();
    Masonry.init('masonry', {
      getData: function () { return _items; },
      onClick: onCardClick,
      decorate: decorateCard,
    });

    loadSettings();

    document.getElementById('inject-general').addEventListener('change', function () {
      syncThresholdEnabled('inject-general', 'general-threshold');
    });
    document.getElementById('inject-character').addEventListener('change', function () {
      syncThresholdEnabled('inject-character', 'character-threshold');
    });
    document.getElementById('inject-nsfw').addEventListener('change', function () {
      syncThresholdEnabled('inject-nsfw', 'nsfw-threshold');
    });
    document.getElementById('force').addEventListener('change', persistInject);
    document.getElementById('rule-template').addEventListener('change', renderSentence);
    document.getElementById('rule-add').addEventListener('click', function () {
      var rule = buildFromTemplate();
      if (!rule) return;
      addRuleRow(rule);
      Array.prototype.forEach.call(document.querySelectorAll('#rule-fields .cap-blank'), function (b) {
        b.value = '';
      });
      persistRules();
    });
    document.getElementById('btn-open').addEventListener('click', function () { openDlg('rules-dlg'); });
    document.getElementById('rules-close').addEventListener('click', closeSettings);
    document.getElementById('view-close').addEventListener('click', function () { closeDlg('view-dlg'); });
    document.getElementById('rules-dlg').addEventListener('click', function (e) {
      if (e.target === this) closeSettings();
    });
    document.getElementById('view-dlg').addEventListener('click', function (e) {
      if (e.target === this) closeDlg('view-dlg');
    });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      closeSettings();
      closeDlg('view-dlg');
    });

    document.getElementById('btn-start').addEventListener('click', function () {
      startJob().catch(function (e) { toast(String(e.message || e)); });
    });
    document.getElementById('btn-cancel').addEventListener('click', function () {
      cancelJob().catch(function (e) { toast(String(e.message || e)); });
    });

    DirPicker.onChange(function () {
      refresh().catch(function (e) { toast(String(e.message || e)); });
    });

    // 页面刷新后重连未结束的标注任务
    API.findActiveJob('caption', API.dataset()).then(function (s) {
      if (s) watch(s.job_id);
    });

    API.request(API.apiPath('/caption/dirs'))
      .then(function (data) {
        renderOptions(data);
        renderServiceStatus(data);
        return refresh();
      })
      .catch(function (e) { toast(String(e.message || e)); });
  }

  /**
   * 渲染描述服务状态(就绪说明或失败原因)。
   * @param {object} data /caption/dirs 响应
   */
  function renderServiceStatus(data) {
    document.getElementById('stat-model').textContent =
      (data.ready ? '' : '未就绪: ') + (data.info || '');
  }

  function start() {
    init().catch(function (e) {
      toast('初始化失败: ' + String(e.message || e));
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
