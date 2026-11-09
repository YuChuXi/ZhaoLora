/**
 * caption.js —— 描述页: 调用 OpenAI 兼容视觉模型批量生成自然语言描述, 写入各工作目录的 captions.json。
 *
 * 依赖: ui.js / api.js / dir_picker.js / nav.js / masonry.js
 *
 * 操作约定与其它标签页一致: 单击卡片选择, Shift 连选, 右键或长按查看描述;
 * 工具栏「生成描述」按当前所见图片与已保存设置直接提交任务, 「设置」只编辑参数。
 */
(function () {
  'use strict';

  // 描述模式的后端取值 → 界面文案(取值集合由 /caption/dirs 返回)
  var MODE_LABELS = { coarse: '粗略', detailed: '详细' };

  var _items = [];
  var _filtered = [];
  var _filterPresets = [];
  var _filterTimer = null;
  var _viewImg = null;
  var _templates = [];
  var _selection = null;
  var _job = null;

  /**
   * 读取「caption」设置键的当前值。
   * @returns {{rules?:Array<object>, groups?:Array<object>, inject?:Object,
   *   modes?:string[], force?:boolean}}
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

  /**
   * 读取已保存的随机抽取组列表。
   * @returns {Array<object>}
   */
  function loadGroups() {
    var arr = captionState().groups;
    return Array.isArray(arr) ? arr : [];
  }

  /**
   * 写入随机抽取组列表。
   * @param {Array<object>} arr
   */
  function saveGroups(arr) {
    var state = captionState();
    state.groups = arr;
    AppSettings.set('caption', state);
  }

  /** 保存 WD14 注入设置、NSFW 增强、描述模式与清空选项。 */
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
    el.className = 'input';
    el.placeholder = placeholder;
    el.value = value || '';
    return el;
  }

  /**
   * 创建组内删除按钮。
   * @param {string} title
   * @param {Function} onDelete 点击回调
   * @returns {HTMLButtonElement}
   */
  function mkDel(title, onDelete) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cap-del';
    btn.textContent = '✕';
    btn.title = title;
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
    row.className = 'cap-table cap-rule';

    var pattern = mkText('如 *tail*', rule.pattern);
    pattern.classList.add('cap-rule-pattern');
    pattern.title = '逗号分隔的标签模式, * 为通配; 留空则对该数据集全部图片生效';

    var text = mkText('提示词内容, 如 尾巴的动作要详细描述', rule.text);
    text.classList.add('cap-rule-text');

    row.appendChild(pattern);
    row.appendChild(text);
    row.appendChild(mkDel('删除该条', function () { row.remove(); persistRules(); }));
    document.getElementById('rule-list').appendChild(row);
  }

  /**
   * 收集当前全部提示词条目(内容为空的行丢弃)。
   * @returns {Array<{text: string, pattern: string}>}
   */
  function collectRules() {
    var rows = document.querySelectorAll('#rule-list .cap-rule');
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var text = rows[i].querySelector('.cap-rule-text').value.trim();
      if (!text) continue;
      out.push({
        text: text,
        pattern: rows[i].querySelector('.cap-rule-pattern').value.trim(),
      });
    }
    return out;
  }

  /**
   * 在组内追加一个候选条目行。
   * @param {HTMLDivElement} box 候选容器
   * @param {{text?: string, weight?: number}} [item]
   */
  function addCandidate(box, item) {
    item = item || {};
    var row = document.createElement('div');
    row.className = 'field-row cap-item';

    var text = mkText('提示词内容, 如 尾巴的动作要详细描述', item.text);
    text.classList.add('cap-text');
    text.title = '命中该组时按组内抽取方式注入这条提示词';

    var weight = document.createElement('input');
    weight.type = 'number';
    weight.className = 'input cap-weight';
    weight.min = '0';
    weight.step = '0.1';
    weight.value = item.weight === undefined ? '1' : String(item.weight);
    weight.title = '权重: 互斥组按 softmax 抽取一条, 非互斥组钳制到 0~1 作为命中概率; 0 不参与';

    row.appendChild(text);
    row.appendChild(weight);
    row.appendChild(mkDel('删除该候选', function () { row.remove(); persistGroups(); }));
    box.appendChild(row);
  }

  /**
   * 追加一个提示词组: 组内候选共享匹配标签与抽取方式。
   * @param {{pattern?: string, exclusive?: boolean, items: Array<object>}} group
   */
  function addGroupRow(group) {
    var row = document.createElement('div');
    row.className = 'cap-group';

    var head = document.createElement('div');
    head.className = 'field-row cap-group-head';
    var label = document.createElement('span');
    label.className = 'cap-note';
    label.textContent = '匹配标签';
    var pattern = mkText('如 *tail*; 留空对全部图片生效', group.pattern);
    pattern.classList.add('cap-pattern');
    pattern.title = '逗号分隔的标签模式, * 为通配; 留空则该组对该数据集全部图片生效';

    var mutex = document.createElement('label');
    mutex.className = 'cap-ck';
    mutex.title = '互斥: 组内按 softmax 随机抽取一条; 非互斥: 每条按钳制后的权重独立判定';
    var checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'cap-mutex';
    checkbox.checked = group.exclusive !== false;
    mutex.appendChild(checkbox);
    mutex.appendChild(document.createTextNode(' 互斥'));

    head.appendChild(label);
    head.appendChild(pattern);
    head.appendChild(mutex);
    head.appendChild(mkDel('删除该组', function () { row.remove(); persistGroups(); }));

    var items = document.createElement('div');
    items.className = 'cap-items';
    group.items.forEach(function (item) { addCandidate(items, item); });

    var foot = document.createElement('div');
    foot.className = 'cap-group-foot';
    var addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'btn';
    addBtn.textContent = '+ 候选';
    addBtn.title = '在同一组内追加候选条目';
    addBtn.addEventListener('click', function () {
      addCandidate(items, null);
      persistGroups();
    });
    foot.appendChild(addBtn);

    row.appendChild(head);
    row.appendChild(items);
    row.appendChild(foot);
    document.getElementById('group-list').appendChild(row);
  }

  /**
   * 收集当前全部抽取组(内容为空的候选丢弃)。
   * @returns {Array<{pattern: string, exclusive: boolean,
   *   items: Array<{text: string, weight: number}>}>}
   */
  function collectGroups() {
    var rows = document.querySelectorAll('#group-list .cap-group');
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var items = [];
      var cells = rows[i].querySelectorAll('.cap-item');
      for (var j = 0; j < cells.length; j++) {
        var text = cells[j].querySelector('.cap-text').value.trim();
        if (!text) continue;
        var weight = parseFloat(cells[j].querySelector('.cap-weight').value);
        items.push({ text: text, weight: weight >= 0 ? weight : 1 });
      }
      if (!items.length) continue;
      out.push({
        pattern: rows[i].querySelector('.cap-pattern').value.trim(),
        exclusive: rows[i].querySelector('.cap-mutex').checked,
        items: items,
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
      box.textContent = '自定义条目: 添加后在表格行直接填写提示词内容';
      return;
    }
    var re = /\{\{\s*([^}]+?)\s*\}\}/g;
    var last = 0;
    var m;
    while ((m = re.exec(tpl.text)) !== null) {
      box.appendChild(document.createTextNode(tpl.text.slice(last, m.index)));
      var blank = document.createElement('input');
      blank.type = 'text';
      blank.className = 'input cap-blank';
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
   * 按句式行的空位取值生成一条条目。
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

  /** 收集并保存当前预设提示词条目。 */
  function persistRules() {
    saveSaved(collectRules());
  }

  /** 收集并保存当前随机抽取组。 */
  function persistGroups() {
    saveGroups(collectGroups());
  }

  /** 保存提示词条目、抽取组与注入设置。 */
  function saveSettings() {
    persistRules();
    persistGroups();
    persistInject();
  }

  /** 保存设置并关闭描述设置对话框。 */
  function closeSettings() {
    saveSettings();
    UI.closeModal('rules-dlg');
  }

  /** 按「caption」设置键恢复提示词条目与抽取组。 */
  function loadSettings() {
    loadSaved().forEach(addRuleRow);
    loadGroups().forEach(addGroupRow);
  }

  // ==================== 描述筛选 ====================

  /**
   * 转义正则元字符。
   * @param {string} s
   * @returns {string}
   */
  function escapeRegExp(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  /**
   * 把 Danbooru 通配模式转为整串匹配正则（小写, 下划线等同空格, * 为通配）。
   * @param {string} pattern
   * @returns {RegExp}
   */
  function danbooruRe(pattern) {
    var body = pattern.toLowerCase().replace(/_/g, ' ').split('*').map(escapeRegExp).join('.*');
    return new RegExp('^' + body + '$');
  }

  /**
   * 编译正则, 支持 Python 风格的内联标志前缀(如 `(?i)`), 转成 JS 的 flags。
   * @param {string} source
   * @returns {RegExp}
   */
  function compileRe(source) {
    var inline = /^\(\?([imsx]+)\)/.exec(source);
    if (!inline) return new RegExp(source);
    var flags = inline[1].replace(/[^ims]/g, '');
    return new RegExp(source.slice(inline[0].length), flags);
  }

  /**
   * 构造单条描述的命中判定。
   * @param {string} mode exact | danbooru | regex
   * @param {string} query
   * @returns {function(string): boolean}
   */
  function makeCaptionMatcher(mode, query) {
    if (mode === 'exact') {
      return function (text) { return text.indexOf(query) !== -1; };
    }
    if (mode === 'danbooru') {
      var wildcard = danbooruRe(query);
      return function (text) {
        return wildcard.test(text.toLowerCase().replace(/_/g, ' ').trim());
      };
    }
    var re = compileRe(query);
    return function (text) { return re.test(text); };
  }

  /** 按当前筛选条件渲染瀑布流, 并按所见图片更新统计。 */
  function applyFilter() {
    var mode = document.getElementById('cap-filter-mode').value;
    var query = document.getElementById('cap-filter-input').value;
    var matcher = null;
    if (query) {
      try {
        matcher = makeCaptionMatcher(mode, query);
      } catch (e) {
        toast('筛选条件无效: ' + String(e.message || e));
        return;
      }
    }
    _filtered = matcher
      ? _items.filter(function (img) {
          return (img.captions || []).some(matcher);
        })
      : _items.slice();
    _selection.retain(visiblePaths());
    Masonry.render(_filtered);
    updateStats();
  }

  /** 统计当前所见图片的共/已描述/未描述数量。 */
  function updateStats() {
    var described = 0;
    _filtered.forEach(function (img) {
      if (img.captions && img.captions.length) described += 1;
    });
    document.getElementById('stat-total').textContent = _filtered.length;
    document.getElementById('stat-described').textContent = described;
    document.getElementById('stat-missing').textContent = _filtered.length - described;
  }

  /**
   * 渲染筛选预置下拉(首项为「自定义」)。
   * @param {Array<{label: string, mode: string, pattern: string}>} presets
   */
  function renderFilterPresets(presets) {
    _filterPresets = presets || [];
    var sel = document.getElementById('cap-filter-preset');
    sel.innerHTML = '';
    var custom = document.createElement('option');
    custom.value = '';
    custom.textContent = '自定义';
    sel.appendChild(custom);
    _filterPresets.forEach(function (p, i) {
      var option = document.createElement('option');
      option.value = String(i);
      option.textContent = p.label;
      sel.appendChild(option);
    });
  }

  // ==================== 选择 ====================

  /**
   * 选中集合变化时刷新计数与批量按钮可用状态。
   * @param {number} count 选中数量
   * @param {string[]} _paths 选中路径列表(未使用)
   */
  function onSelectionChange(count, _paths) {
    Masonry.syncSelection();
    document.getElementById('stat-sel').hidden = !count;
    document.getElementById('stat-selected').textContent = count;
    document.getElementById('btn-clear-sel').disabled = !count;
    document.getElementById('btn-delete').disabled = !count;
  }

  /**
   * 返回当前选中的图片条目(按列表顺序)。
   * @returns {Array<object>}
   */
  function selectedItems() {
    return _items.filter(function (img) { return _selection.has(img.path); });
  }

  /**
   * 当前视图内全部路径。
   * @returns {string[]}
   */
  function visiblePaths() {
    return _filtered.map(function (img) { return img.path; });
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
   * 打开描述查看模态(卡片右键或长按触发)。
   * @param {object} img
   */
  function openView(img) {
    _viewImg = img;
    document.getElementById('view-title').textContent = img.path;
    document.getElementById('view-delete').hidden = !(img.captions && img.captions.length);
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
    UI.openModal('view-dlg');
  }

  // ==================== 描述列表 ====================

  /** 重新拉取所选目录的图片与描述现状, 并清空选择。 */
  async function refresh() {
    var res = await API.request(API.apiPath('/caption/list'), {
      method: 'POST',
      body: JSON.stringify({ dirs: DirPicker.getDirs() }),
    });
    _items = res.items || [];
    _selection.clear();
    applyFilter();
  }

  // ==================== 描述删除 ====================

  /**
   * 批量删除指定图片的描述。
   * @param {string[]} paths 图片相对路径列表
   * @param {boolean} confirmFirst 是否先确认
   * @returns {Promise<void>}
   */
  async function deleteCaptions(paths, confirmFirst) {
    if (!paths.length) {
      toast('未选择图片');
      return;
    }
    if (confirmFirst && !confirm('删除 ' + paths.length + ' 张图片的描述？')) return;
    var res = await API.request(API.apiPath('/caption/delete'), {
      method: 'POST',
      body: JSON.stringify({ paths: paths }),
    });
    toast(res.deleted ? ('已删除 ' + res.deleted + ' 张的描述') : '所选图片均无描述');
    UI.closeModal('view-dlg');
    await refresh();
  }

  /** 删除当前选中图片的描述。 */
  async function deleteSelectedCaptions() {
    await deleteCaptions(_selection.paths(), true);
  }

  /** 删除当前查看图片的描述。 */
  async function deleteViewedCaption() {
    if (!_viewImg) return;
    await deleteCaptions([_viewImg.path], false);
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
   * 本次任务的目标图片: 有选择时取选中图片, 否则取全部可见图片。
   * 选择集合在筛选时已收窄到可见图片, 故两者都属于当前所见。
   * @returns {string[]} 图片相对路径列表
   */
  function targetPaths() {
    return _selection.count() ? _selection.paths() : visiblePaths();
  }

  /** 提交描述标注长任务。 */
  async function startJob() {
    var modes = selectedModes();
    if (!modes.length) {
      toast('请至少选择一种描述模式');
      return;
    }
    var paths = targetPaths();
    if (!paths.length) {
      toast('没有可处理的图片');
      return;
    }
    var generalThreshold = readThreshold('inject-general', 'general-threshold');
    var characterThreshold = readThreshold('inject-character', 'character-threshold');
    var nsfwThreshold = readThreshold('inject-nsfw', 'nsfw-threshold');
    if (generalThreshold === false || characterThreshold === false || nsfwThreshold === false) {
      toast('注入标签与 NSFW 增强的置信度阈值需在 0~1 之间');
      return;
    }
    var body = {
      dirs: DirPicker.getDirs(),
      paths: paths,
      modes: modes,
      general_threshold: generalThreshold,
      character_threshold: characterThreshold,
      nsfw_threshold: nsfwThreshold,
      rules: collectRules(),
      groups: collectGroups(),
      force: document.getElementById('force').checked,
    };
    var runBtn = document.getElementById('btn-run');
    runBtn.disabled = true;
    var log = document.getElementById('log-box');
    log.hidden = true;
    log.textContent = '';
    try {
      var res = await API.request(API.apiPath('/caption/start'), {
        method: 'POST',
        body: JSON.stringify(body),
      });
      UI.closeModal('rules-dlg');
      _job.start(res.job_id);
    } catch (e) {
      runBtn.disabled = false;
      toast('提交失败: ' + String(e.message || e));
    }
  }

  // ==================== 初始化 ====================

  /**
   * 按后端返回的模式、注入默认值与提示词模板渲染设置项。
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
    renderFilterPresets(data.filter_presets);
  }

  /**
   * 渲染描述服务状态(就绪说明或失败原因)。
   * @param {object} data /caption/dirs 响应
   */
  function renderServiceStatus(data) {
    document.getElementById('stat-model').textContent =
      (data.ready ? '' : '未就绪: ') + (data.info || '');
  }

  /** 绑定工具栏、筛选、快捷键与模态事件并加载数据。 */
  async function init() {
    await AppSettings.ready();

    _selection = UI.createSelection(onSelectionChange);
    Masonry.init('masonry', {
      getData: function () { return _filtered; },
      selection: _selection,
      onPreview: openView,
      decorate: decorateCard,
    });

    _job = UI.bindJobPanel({
      bar: 'job-progress',
      fill: 'progress-fill',
      pct: 'progress-pct',
      log: 'log-box',
      cancelBtn: 'btn-cancel',
      statusEl: 'stat-job',
      onFinish: function (s) {
        document.getElementById('btn-run').disabled = false;
        if (s.status === 'done') toast(s.message || '标注完成');
        else if (s.status === 'cancelled') toast(s.message || '已取消');
        else toast('标注失败: ' + (s.message || ''));
        refresh().catch(function (e) { toast(String(e.message || e)); });
      },
    });

    UI.bindModal('rules-dlg', saveSettings);
    UI.bindModal('view-dlg');

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
    document.getElementById('cap-filter-mode').addEventListener('change', applyFilter);
    document.getElementById('cap-filter-input').addEventListener('input', function () {
      document.getElementById('cap-filter-preset').value = '';
      clearTimeout(_filterTimer);
      _filterTimer = setTimeout(applyFilter, 120);
    });
    document.getElementById('cap-filter-preset').addEventListener('change', function () {
      if (this.value === '') { applyFilter(); return; }
      var preset = _filterPresets[Number(this.value)];
      document.getElementById('cap-filter-mode').value = preset.mode;
      document.getElementById('cap-filter-input').value = preset.pattern;
      applyFilter();
    });
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
    document.getElementById('group-add').addEventListener('click', function () {
      addGroupRow({ items: [{}] });
      persistGroups();
    });
    document.getElementById('group-sample').addEventListener('click', function () {
      addGroupRow({
        items: [
          { text: 'Using English', weight: 1 },
          { text: '使用中文', weight: 1 },
        ],
      });
      persistGroups();
    });
    document.getElementById('btn-settings').addEventListener('click', function () {
      UI.openModal('rules-dlg');
    });
    document.getElementById('btn-run').addEventListener('click', function () {
      startJob().catch(function (e) { toast(String(e.message || e)); });
    });
    document.getElementById('rules-close').addEventListener('click', closeSettings);
    document.getElementById('rules-save').addEventListener('click', closeSettings);

    document.getElementById('btn-select-all').addEventListener('click', function () {
      _selection.selectAll(visiblePaths());
    });
    document.getElementById('btn-invert').addEventListener('click', function () {
      _selection.invert(visiblePaths());
    });
    document.getElementById('btn-clear-sel').addEventListener('click', function () {
      _selection.clear();
    });
    document.getElementById('btn-delete').addEventListener('click', function () {
      deleteSelectedCaptions().catch(function (e) { toast(String(e.message || e)); });
    });
    document.getElementById('btn-cancel').addEventListener('click', function () {
      _job.cancel();
    });
    document.getElementById('view-close').addEventListener('click', function () {
      UI.closeModal('view-dlg');
    });
    document.getElementById('view-delete').addEventListener('click', function () {
      deleteViewedCaption().catch(function (e) { toast(String(e.message || e)); });
    });

    document.addEventListener('keydown', function (e) {
      var tag = (e.target && e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      if (e.key === 'a' && (e.ctrlKey || e.metaKey) && !e.shiftKey) {
        e.preventDefault();
        _selection.selectAll(visiblePaths());
      } else if (e.key === 'i' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        _selection.invert(visiblePaths());
      } else if (e.key === 'Delete') {
        e.preventDefault();
        deleteSelectedCaptions().catch(function (err) { toast(String(err.message || err)); });
      }
    });

    DirPicker.onChange(function () {
      refresh().catch(function (e) { toast(String(e.message || e)); });
    });

    // 页面刷新后重连未结束的标注任务
    API.findActiveJob('caption', API.dataset()).then(function (s) {
      if (!s) return;
      document.getElementById('btn-run').disabled = true;
      _job.start(s.job_id);
    });

    API.request(API.apiPath('/caption/dirs'))
      .then(function (data) {
        renderOptions(data);
        renderServiceStatus(data);
        return refresh();
      })
      .catch(function (e) { toast(String(e.message || e)); });
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
