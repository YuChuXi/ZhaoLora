/**
 * crop.js —— 多区域吸附裁剪
 */
(function () {
  'use strict';

  var SIDE_MIN = 512;
  var SIDE_MAX = 1536 * 2;
  var SIDE_STEP = 128;
  var ZOOM_BUCKETS = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0];
  var MAX_OUTPUT_PIXELS = 1536 * 1536;
  var NEAR_RADIUS = 2;
  var HANDLE_TOL = 10;

  var _images = [];
  var _index = 0;
  var _origW = 0;
  var _origH = 0;
  var _img = null;
  var _scale = 1;
  var _offsetX = 0;
  var _offsetY = 0;
  var _crops = []; // {x1,y1,x2,y2,target_w,target_h,zoom,name}
  var _selected = -1;
  var _datasetRoot = '';
  var _autoParts = []; // 已添加的部位名，后续每张图自动生成选区
  var _deniedParts = []; // 永久删除的部位，刷新后也不再自动纳入
  var _noCrop = false;
  var _dragMode = null;
  var _dragStart = null;
  var _dragBase = null;
  var _dragIdx = -1;
  var _toastTimer = null;

  var canvas = document.getElementById('crop-canvas');
  var ctx = canvas.getContext('2d');
  var wrap = document.getElementById('canvas-wrap');

  function toast(msg) {
    var el = document.getElementById('toast');
    if (!el) return;
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () { el.classList.remove('show'); }, 2200);
  }

  function errshow(msg) {
    toast('⚠ ' + msg);
  }
  window.addEventListener('error', function (e) { errshow(e.message); });
  window.addEventListener('unhandledrejection', function (e) {
    errshow(String(e.reason && e.reason.message ? e.reason.message : e.reason));
  });

  async function api(path, opts) {
    opts = opts || {};
    var url = path.indexOf('/api/') === 0 ? API.apiPath(path.slice(4)) : path;
    var res = await fetch(url, {
      headers: opts.headers || {},
      method: opts.method || 'GET',
      body: opts.body || undefined,
    });
    if (!res.ok) {
      var msg = await res.text();
      throw new Error(res.status + ': ' + msg);
    }
    return res;
  }

  // ---- 吸附：按 zoom 反推 target，不建 SIDE×SIDE×ZOOM 全表 ----
  function snapSide(x) {
    var t = Math.round((x - SIDE_MIN) / SIDE_STEP);
    return Math.max(SIDE_MIN, Math.min(SIDE_MAX, SIDE_MIN + t * SIDE_STEP));
  }

  function maxSideFitting(maxCrop, z) {
    if (maxCrop < 1 || z <= 0) return null;
    var s = Math.min(SIDE_MAX, Math.floor(maxCrop / z));
    s = SIDE_MIN + Math.floor((s - SIDE_MIN) / SIDE_STEP) * SIDE_STEP;
    while (s >= SIDE_MIN) {
      if (Math.round(s * z) <= maxCrop) return s;
      s -= SIDE_STEP;
    }
    return null;
  }

  function sideNeighbors(ideal, maxS) {
    if (maxS < SIDE_MIN) return [];
    var upper = SIDE_MIN + Math.floor((Math.min(SIDE_MAX, maxS) - SIDE_MIN) / SIDE_STEP) * SIDE_STEP;
    if (upper < SIDE_MIN) return [];
    var base = Math.max(SIDE_MIN, Math.min(upper, snapSide(ideal)));
    var set = {};
    set[SIDE_MIN] = 1; set[upper] = 1; set[base] = 1;
    for (var d = -NEAR_RADIUS; d <= NEAR_RADIUS; d++) {
      if (d === 0) continue;
      var s = base + d * SIDE_STEP;
      if (s >= SIDE_MIN && s <= upper) set[s] = 1;
    }
    return Object.keys(set).map(Number).sort(function (a, b) { return a - b; });
  }

  function fitPixelLimit(tw, th) {
    if (tw * th <= MAX_OUTPUT_PIXELS) return [tw, th];
    var scale = Math.sqrt(MAX_OUTPUT_PIXELS / (tw * th));
    var tw2 = snapSide(tw * scale);
    var th2 = snapSide(th * scale);
    while (tw2 >= SIDE_MIN && th2 >= SIDE_MIN && tw2 * th2 > MAX_OUTPUT_PIXELS) {
      if (tw2 >= th2) tw2 -= SIDE_STEP;
      else th2 -= SIDE_STEP;
    }
    if (tw2 < SIDE_MIN || th2 < SIDE_MIN || tw2 * th2 > MAX_OUTPUT_PIXELS) return null;
    return [tw2, th2];
  }

  function chooseBest(maxW, maxH, desiredW, desiredH, weightW, weightH) {
    weightW = weightW == null ? 1 : weightW;
    weightH = weightH == null ? 1 : weightH;
    var bestByCrop = {};
    for (var zi = 0; zi < ZOOM_BUCKETS.length; zi++) {
      var z = ZOOM_BUCKETS[zi];
      var maxTw = maxSideFitting(maxW, z);
      var maxTh = maxSideFitting(maxH, z);
      if (maxTw == null || maxTh == null) continue;
      var tws = sideNeighbors(desiredW / z, maxTw);
      var ths = sideNeighbors(desiredH / z, maxTh);
      for (var i = 0; i < tws.length; i++) {
        for (var j = 0; j < ths.length; j++) {
          var fitted = fitPixelLimit(tws[i], ths[j]);
          if (!fitted) continue;
          var tw = fitted[0], th = fitted[1];
          if (tw > maxTw || th > maxTh) continue;
          var cw = Math.round(tw * z);
          var ch = Math.round(th * z);
          if (cw < 1 || ch < 1 || cw > maxW || ch > maxH) continue;
          var score0 = weightW * Math.abs(cw - desiredW) + weightH * Math.abs(ch - desiredH);
          var score1 = -(tw * th);
          var score2 = -z;
          var cropKey = cw + 'x' + ch;
          var prev = bestByCrop[cropKey];
          if (!prev || score0 < prev.s0 || (score0 === prev.s0 && (score1 < prev.s1 || (score1 === prev.s1 && score2 < prev.s2)))) {
            bestByCrop[cropKey] = {
              s0: score0, s1: score1, s2: score2,
              cand: { crop_w: cw, crop_h: ch, target_w: tw, target_h: th, zoom: z },
            };
          }
        }
      }
    }
    var best = null, bs0 = 0, bs1 = 0, bs2 = 0;
    for (var k in bestByCrop) {
      var e = bestByCrop[k];
      if (!best || e.s0 < bs0 || (e.s0 === bs0 && (e.s1 < bs1 || (e.s1 === bs1 && e.s2 < bs2)))) {
        best = e.cand; bs0 = e.s0; bs1 = e.s1; bs2 = e.s2;
      }
    }
    return best;
  }

  function findByTargetZoom(maxW, maxH, tw, th, zoom) {
    if (tw < SIDE_MIN || th < SIDE_MIN || tw > SIDE_MAX || th > SIDE_MAX) return null;
    if ((tw - SIDE_MIN) % SIDE_STEP !== 0 || (th - SIDE_MIN) % SIDE_STEP !== 0) return null;
    var okZ = false;
    for (var i = 0; i < ZOOM_BUCKETS.length; i++) {
      if (Math.abs(ZOOM_BUCKETS[i] - zoom) < 1e-4) { okZ = true; break; }
    }
    if (!okZ) return null;
    if (tw * th > MAX_OUTPUT_PIXELS) return null;
    var cw = Math.round(tw * zoom), ch = Math.round(th * zoom);
    if (cw > maxW || ch > maxH) return null;
    return { crop_w: cw, crop_h: ch, target_w: tw, target_h: th, zoom: zoom };
  }

  function applyMatchConfig(cfg) {
    if (!cfg) return;
    if (cfg.side_min != null) SIDE_MIN = cfg.side_min;
    if (cfg.side_max != null) SIDE_MAX = cfg.side_max;
    if (cfg.side_step != null) SIDE_STEP = cfg.side_step;
    if (cfg.zooms && cfg.zooms.length) ZOOM_BUCKETS = cfg.zooms;
    if (cfg.max_output_pixels != null) MAX_OUTPUT_PIXELS = cfg.max_output_pixels;
  }

  function makeCrop(x1, y1, x2, y2, cand, name) {
    return {
      x1: x1, y1: y1, x2: x2, y2: y2,
      target_w: cand ? cand.target_w : Math.min(x2 - x1, 1536),
      target_h: cand ? cand.target_h : Math.min(y2 - y1, 1536),
      zoom: cand ? cand.zoom : 1.0,
      name: name || '',
    };
  }

  /**
   * 按部位解析默认匹配候选。空部位名使用全局默认。
   * 引用：makeDefaultSizedCrop。
   * @param {string} [name]
   * @returns {object|null}
   */
  function resolveDefaultCand(name) {
    if (!_origW || !_origH) return null;
    var pref = getPartDefault(name || '');
    if (pref) {
      var fixed = findByTargetZoom(
        _origW, _origH,
        pref.target_w, pref.target_h, pref.zoom
      );
      if (fixed) return fixed;
    }
    return chooseBest(_origW, _origH, _origW, _origH);
  }

  /**
   * 按默认参数生成选区。引用：initDefaultCrop、addCrop、addPart。
   * @param {string} name
   * @param {number} index
   */
  function makeDefaultSizedCrop(name, index) {
    var init = resolveDefaultCand(name);
    if (!init) return makeCrop(0, 0, _origW, _origH, null, name || '');
    var cw = init.crop_w, ch = init.crop_h;
    var shift = (index || 0) * 40;
    var x1 = Math.floor((_origW - cw) / 2) + shift;
    var y1 = Math.floor((_origH - ch) / 2) + shift;
    x1 = Math.max(0, Math.min(_origW - cw, x1));
    y1 = Math.max(0, Math.min(_origH - ch, y1));
    return makeCrop(x1, y1, x1 + cw, y1 + ch, init, name || '');
  }

  /**
   * localStorage 键：crop_part_defaults:{任务名}。
   * 引用：loadPartDefaults / savePartDefaults。
   * @returns {string}
   */
  function defaultsStorageKey() {
    return 'crop_part_defaults:' + API.dataset();
  }

  /**
   * @returns {Object.<string, {target_w:number, target_h:number, zoom:number}>}
   */
  function loadPartDefaults() {
    var raw = localStorage.getItem(defaultsStorageKey());
    if (!raw) return {};
    var data = JSON.parse(raw);
    if (!data || typeof data !== 'object' || Array.isArray(data)) return {};
    return data;
  }

  /**
   * @param {Object} map
   */
  function savePartDefaults(map) {
    localStorage.setItem(defaultsStorageKey(), JSON.stringify(map));
  }

  /**
   * 解析「宽x高@缩放」文本。引用：侧栏默认裁切输入。
   * @param {string} text
   * @returns {{target_w:number, target_h:number, zoom:number}|null}
   */
  function parseDefaultSpec(text) {
    var m = String(text || '').trim().match(/^(\d+)\s*[x×]\s*(\d+)\s*@\s*([\d.]+)\s*x?$/i);
    if (!m) return null;
    var tw = parseInt(m[1], 10);
    var th = parseInt(m[2], 10);
    var z = parseFloat(m[3]);
    if (!(tw > 0 && th > 0 && z > 0)) return null;
    return { target_w: tw, target_h: th, zoom: z };
  }

  /**
   * @param {{target_w:number, target_h:number, zoom:number}|null} d
   * @returns {string}
   */
  function formatDefaultSpec(d) {
    if (!d) return '';
    return d.target_w + 'x' + d.target_h + '@' + Number(d.zoom).toFixed(1);
  }

  /**
   * 读取部位默认；无部位项时回退全局（空键）。
   * @param {string} name
   * @returns {{target_w:number, target_h:number, zoom:number}|null}
   */
  function getPartDefault(name) {
    var map = loadPartDefaults();
    var key = name || '';
    if (map[key] && typeof map[key] === 'object') return map[key];
    if (key && map[''] && typeof map[''] === 'object') return map[''];
    return null;
  }

  /**
   * 写入某一部位（或全局）默认。引用：侧栏 change。
   * @param {string} part
   * @param {string} text
   */
  function commitPartDefault(part, text) {
    var map = loadPartDefaults();
    var trimmed = String(text || '').trim();
    if (!trimmed) {
      delete map[part];
      savePartDefaults(map);
      toast('已清除默认裁切');
      return;
    }
    var spec = parseDefaultSpec(trimmed);
    if (!spec) {
      toast('格式：宽x高@缩放，如 1536x1536@1.0');
      syncDefaultsUi();
      return;
    }
    map[part] = spec;
    savePartDefaults(map);
    toast('已保存默认裁切');
  }

  /**
   * 刷新侧栏默认裁切输入。引用：loadParts、addPart、init。
   */
  function syncDefaultsUi() {
    var map = loadPartDefaults();
    var globalEl = document.getElementById('crop-default-global');
    if (globalEl) globalEl.value = formatDefaultSpec(map[''] || null);
    var host = document.getElementById('crop-default-parts');
    if (!host) return;
    host.innerHTML = '';
    for (var i = 0; i < _autoParts.length; i++) {
      var name = _autoParts[i];
      var label = document.createElement('label');
      label.appendChild(document.createTextNode(name + ' '));
      var input = document.createElement('input');
      input.type = 'text';
      input.placeholder = '1536x1536@1.0';
      input.spellcheck = false;
      input.value = formatDefaultSpec(map[name] || null);
      input.dataset.part = name;
      input.addEventListener('change', function (e) {
        commitPartDefault(e.target.dataset.part, e.target.value);
      });
      label.appendChild(input);
      host.appendChild(label);
    }
  }

  function partsStorageKey() {
    return 'crop_auto_parts:' + (_datasetRoot || 'default');
  }

  function partsDenyKey() {
    return 'crop_auto_parts_deny:' + (_datasetRoot || 'default');
  }

  function loadParts(serverParts) {
    var saved = null;
    var denied = null;
    try { saved = JSON.parse(localStorage.getItem(partsStorageKey()) || 'null'); } catch (e) { saved = null; }
    try { denied = JSON.parse(localStorage.getItem(partsDenyKey()) || 'null'); } catch (e) { denied = null; }
    _deniedParts = Array.isArray(denied) ? denied.filter(function (n) { return typeof n === 'string'; }) : [];
    _autoParts = [];
    var seen = {};
    function isDenied(name) {
      return _deniedParts.indexOf(name) >= 0;
    }
    function pushName(name) {
      if (!name || seen[name] || isDenied(name)) return;
      seen[name] = true;
      _autoParts.push(name);
    }
    if (Array.isArray(saved)) {
      for (var i = 0; i < saved.length; i++) {
        if (typeof saved[i] === 'string') pushName(saved[i]);
        else if (saved[i] && saved[i].name && saved[i].enabled !== false) pushName(saved[i].name);
      }
    }
    // 服务器已有部位目录也自动纳入（永久删除过的除外）
    if (Array.isArray(serverParts)) {
      for (var k = 0; k < serverParts.length; k++) pushName(serverParts[k]);
    }
    syncDefaultsUi();
  }

  function saveParts() {
    localStorage.setItem(partsStorageKey(), JSON.stringify(_autoParts));
  }

  function saveDeniedParts() {
    localStorage.setItem(partsDenyKey(), JSON.stringify(_deniedParts));
  }

  function registerAutoPart(name) {
    if (!name) return;
    _deniedParts = _deniedParts.filter(function (n) { return n !== name; });
    saveDeniedParts();
    if (_autoParts.indexOf(name) >= 0) return;
    _autoParts.push(name);
    saveParts();
  }

  function unregisterAutoPart(name, permanent) {
    if (!name) return;
    _autoParts = _autoParts.filter(function (n) { return n !== name; });
    saveParts();
    if (permanent && _deniedParts.indexOf(name) < 0) {
      _deniedParts.push(name);
      saveDeniedParts();
    }
    syncDefaultsUi();
  }

  function removeCropPermanently(idx) {
    if (idx < 0 || idx >= _crops.length) return;
    var name = (_crops[idx].name || '').trim();
    removeCropAt(idx);
    if (name) {
      for (var i = _crops.length - 1; i >= 0; i--) {
        if (_crops[i].name === name) removeCropAt(i);
      }
      unregisterAutoPart(name, true);
    }
  }

  function ensurePartCrop(name) {
    for (var i = 0; i < _crops.length; i++) {
      if (_crops[i].name === name) return;
    }
    _crops.push(makeDefaultSizedCrop(name, _crops.length));
    _selected = _crops.length - 1;
  }

  function removeCropAt(idx) {
    if (idx < 0 || idx >= _crops.length) return;
    _crops.splice(idx, 1);
    // 只删当前图选区，不注销自动部位（下一张仍会带上）
    if (_crops.length === 0) _selected = -1;
    else _selected = Math.min(idx, _crops.length - 1);
  }

  function addPart() {
    var input = document.getElementById('part-add-input');
    var name = (input.value || '').trim();
    if (!/^\w[\w\-]{0,63}$/.test(name)) {
      toast('部位名无效');
      return;
    }
    registerAutoPart(name);
    input.value = '';
    syncDefaultsUi();
    if (!_noCrop && _origW) {
      ensurePartCrop(name);
      redraw();
      renderList();
      updateNav();
    }
    toast('已添加 ' + name);
  }

  function initDefaultCrop() {
    _crops = [];
    _selected = -1;
    if (!_origW || !_origH) return;
    if (_autoParts.length) {
      for (var i = 0; i < _autoParts.length; i++) {
        _crops.push(makeDefaultSizedCrop(_autoParts[i], i));
      }
      _selected = 0;
      return;
    }
    _crops.push(makeDefaultSizedCrop('', 0));
    _selected = 0;
  }

  // ---- 坐标变换（画布缓冲=原图像素；CSS 缩小显示）----
  function cssToRaw(sx, sy) {
    var rect = canvas.getBoundingClientRect();
    var rx = Math.round(sx / Math.max(1e-6, rect.width) * _origW);
    var ry = Math.round(sy / Math.max(1e-6, rect.height) * _origH);
    rx = Math.max(0, Math.min(_origW - 1, rx));
    ry = Math.max(0, Math.min(_origH - 1, ry));
    return { x: rx, y: ry };
  }

  function uiPx(n) {
    // CSS 像素 → 缓冲像素，保证缩小后线宽/手柄仍可见
    return Math.max(1, n / Math.max(1e-6, _scale));
  }

  function fitCanvas() {
    var cw = Math.max(1, wrap.clientWidth);
    var ch = Math.max(1, wrap.clientHeight);
    if (!_origW || !_origH) {
      canvas.width = cw;
      canvas.height = ch;
      return;
    }
    _scale = Math.min(cw / _origW, ch / _origH);
    var vw = Math.max(1, Math.floor(_origW * _scale));
    var vh = Math.max(1, Math.floor(_origH * _scale));
    _offsetX = 0;
    _offsetY = 0;
    if (canvas.width !== _origW || canvas.height !== _origH) {
      canvas.width = _origW;
      canvas.height = _origH;
    }
    canvas.style.width = vw + 'px';
    canvas.style.height = vh + 'px';
    canvas.style.position = 'absolute';
    canvas.style.left = Math.floor((cw - vw) / 2) + 'px';
    canvas.style.top = Math.floor((ch - vh) / 2) + 'px';
    canvas.style.imageRendering = 'auto';
  }

  function drawChecker(vw, vh) {
    var gs = 16;
    for (var y = 0; y < vh; y += gs) {
      for (var x = 0; x < vw; x += gs) {
        var light = ((x / gs + y / gs) % 2) === 0;
        ctx.fillStyle = light ? '#cccccc' : '#eeeeee';
        ctx.fillRect(x, y, Math.min(gs, vw - x), Math.min(gs, vh - y));
      }
    }
  }

  function redraw() {
    var cw = canvas.width, ch = canvas.height;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, cw, ch);
    ctx.fillStyle = '#222';
    ctx.fillRect(0, 0, cw, ch);
    if (!_img || !_origW) return;

    drawChecker(_origW, _origH);
    ctx.drawImage(_img, 0, 0);

    if (_noCrop) {
      updateLive(null);
      return;
    }

    var lw = uiPx(2.5);
    var lwInactive = uiPx(1.5);
    var hs = uiPx(5);
    var fontPx = uiPx(12);
    var labelH = uiPx(30);
    var labelGap = uiPx(32);

    for (var i = 0; i < _crops.length; i++) {
      var box = _crops[i];
      var x1 = box.x1, y1 = box.y1, x2 = box.x2, y2 = box.y2;
      var active = i === _selected;
      ctx.strokeStyle = active ? '#00ffff' : '#00cc66';
      ctx.lineWidth = active ? lw : lwInactive;
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);

      var corners = [
        [x1, y1], [x2, y1], [x1, y2], [x2, y2],
      ];
      ctx.fillStyle = active ? '#00ffff' : '#00cc66';
      for (var c = 0; c < corners.length; c++) {
        ctx.fillRect(corners[c][0] - hs, corners[c][1] - hs, hs * 2, hs * 2);
      }

      var label = (i + 1) + (box.name ? ' ' + box.name : '');
      var info = (box.x2 - box.x1) + 'x' + (box.y2 - box.y1) +
        ' → ' + box.target_w + 'x' + box.target_h + '@' + Number(box.zoom).toFixed(1) + 'x';
      ctx.font = fontPx + 'px sans-serif';
      ctx.fillStyle = active ? '#003333' : '#003320';
      var tw = Math.max(ctx.measureText(label).width, ctx.measureText(info).width) + uiPx(8);
      var ly = Math.max(0, y1 - labelGap);
      ctx.fillRect(x1, ly, tw, labelH);
      ctx.fillStyle = active ? '#00ffff' : '#a0ffc0';
      ctx.fillText(label, x1 + uiPx(4), ly + fontPx);
      ctx.fillText(info, x1 + uiPx(4), ly + fontPx * 2 + uiPx(2));
    }
    updateLive(_selected >= 0 ? _crops[_selected] : null);
  }

  function updateLive(box) {
    var live = document.getElementById('live-match');
    if (!live) return;
    if (_noCrop) {
      live.textContent = ' | 整图输出 · ' + _origW + '×' + _origH;
      return;
    }
    if (!box) {
      live.textContent = '';
      return;
    }
    live.textContent = ' | ' + (box.x2 - box.x1) + '×' + (box.y2 - box.y1) +
      ' → ' + box.target_w + '×' + box.target_h + ' @' + Number(box.zoom).toFixed(1) + 'x';
  }

  function renderList() {
    var list = document.getElementById('crop-list');
    list.innerHTML = '';
    if (_noCrop) {
      list.innerHTML = '<div class="crop-item"><div class="body">整图输出 · ' + _origW + '×' + _origH +
        '<div class="meta">写入对应部位目录</div></div></div>';
      return;
    }
    if (!_crops.length) {
      list.innerHTML = '<div style="font-size:11px;color:var(--text-dim);padding:4px 0;">暂无选区</div>';
      return;
    }
    for (var i = 0; i < _crops.length; i++) {
      (function (idx) {
        var box = _crops[idx];
        var el = document.createElement('div');
        el.className = 'crop-item' + (idx === _selected ? ' active' : '');
        var body = document.createElement('div');
        body.className = 'body';
        body.innerHTML =
          '<div><b>#' + (idx + 1) + (box.name ? ' · ' + box.name : '') + '</b></div>' +
          '<div class="meta">' + (box.x2 - box.x1) + 'x' + (box.y2 - box.y1) +
          ' → ' + box.target_w + 'x' + box.target_h + ' @' + Number(box.zoom).toFixed(1) + 'x</div>';
        var input = document.createElement('input');
        input.type = 'text';
        input.placeholder = '部位名';
        input.value = box.name || '';
        input.addEventListener('click', function (e) { e.stopPropagation(); });
        input.addEventListener('change', function () {
          var oldName = _crops[idx].name || '';
          var newName = input.value.trim();
          _crops[idx].name = newName;
          if (oldName && oldName !== newName) {
            var still = false;
            for (var j = 0; j < _crops.length; j++) {
              if (_crops[j].name === oldName) { still = true; break; }
            }
            if (!still) unregisterAutoPart(oldName);
          }
          if (newName) registerAutoPart(newName);
          redraw();
          renderList();
        });
        body.appendChild(input);
        var nameHint = (box.name || '').trim();
        var actions = document.createElement('div');
        actions.className = 'crop-item-actions';
        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'btn-del';
        del.textContent = '删除';
        del.title = '仅当前图';
        del.addEventListener('click', function (e) {
          e.stopPropagation();
          removeCropAt(idx);
          redraw();
          renderList();
          updateNav();
        });
        var delPerm = document.createElement('button');
        delPerm.type = 'button';
        delPerm.className = 'btn-del btn-del-perm';
        delPerm.textContent = '永久';
        delPerm.title = '取消自动应用';
        delPerm.addEventListener('click', function (e) {
          e.stopPropagation();
          removeCropPermanently(idx);
          redraw();
          renderList();
          updateNav();
          toast(nameHint ? ('已移除部位 ' + nameHint) : '已移除选区');
        });
        actions.appendChild(del);
        actions.appendChild(delPerm);
        el.appendChild(body);
        el.appendChild(actions);
        el.addEventListener('click', function () {
          _selected = idx;
          renderList();
          redraw();
        });
        list.appendChild(el);
      })(i);
    }
  }

  /**
   * 底栏六段：位置、完成量、文件名、对象状态、分辨率、附加。
   * 引用：换图、保存、选区变更。
   */
  function updateNav() {
    var img = _images[_index];
    var done = 0;
    for (var i = 0; i < _images.length; i++) if (_images[i].done) done++;
    document.getElementById('seg-pos').textContent =
      (_images.length ? (_index + 1) : 0) + '/' + _images.length;
    document.getElementById('seg-done').textContent = '已裁 ' + done;
    var live = document.getElementById('live-match');
    var extra = document.getElementById('seg-extra');
    function setExtraPrefix(text) {
      if (!extra || !live) {
        if (extra) extra.textContent = text;
        return;
      }
      while (extra.firstChild && extra.firstChild !== live) {
        extra.removeChild(extra.firstChild);
      }
      extra.insertBefore(document.createTextNode(text), live);
    }
    if (!img) {
      document.getElementById('seg-name').textContent = '-';
      document.getElementById('seg-name').title = '';
      document.getElementById('seg-obj').textContent = '-';
      document.getElementById('seg-metric').textContent = '-';
      setExtraPrefix('选区 0');
      if (live) live.textContent = '';
      return;
    }
    var base = img.path.split(/[/\\]/).pop() || img.path;
    document.getElementById('seg-name').textContent = base;
    document.getElementById('seg-name').title = img.path;
    document.getElementById('seg-obj').textContent = img.done ? '已裁剪' : '未裁剪';
    document.getElementById('seg-metric').textContent = _origW + '×' + _origH;
    var prefix = '选区 ' + _crops.length;
    if (_autoParts.length) prefix += ' · 自动：' + _autoParts.join(', ');
    setExtraPrefix(prefix);
    updateLive(_selected >= 0 ? _crops[_selected] : null);
  }

  // ---- hit test（原图像素坐标）----
  function hitTestBox(rx, ry, box) {
    var left = Math.min(box.x1, box.x2), right = Math.max(box.x1, box.x2);
    var top = Math.min(box.y1, box.y2), bottom = Math.max(box.y1, box.y2);
    var edgeTol = uiPx(Math.max(8, HANDLE_TOL));
    // 角点用更大圆形命中区，且比边/整框优先
    var cornerTol = uiPx(Math.max(16, HANDLE_TOL * 1.8));

    var corners = [
      { mode: 'resize_tl', x: left, y: top },
      { mode: 'resize_tr', x: right, y: top },
      { mode: 'resize_bl', x: left, y: bottom },
      { mode: 'resize_br', x: right, y: bottom },
    ];
    var bestCorner = null;
    var bestCornerD = Infinity;
    for (var i = 0; i < corners.length; i++) {
      var c = corners[i];
      var d = Math.hypot(rx - c.x, ry - c.y);
      if (d < bestCornerD) {
        bestCornerD = d;
        bestCorner = c.mode;
      }
    }
    if (bestCorner && bestCornerD <= cornerTol) return bestCorner;

    // 边：若同时靠近某角点，仍让角点赢（上面已返回）；此处排除角点附近残留边命中
    var nearL = Math.abs(rx - left) <= edgeTol && top <= ry && ry <= bottom;
    var nearR = Math.abs(rx - right) <= edgeTol && top <= ry && ry <= bottom;
    var nearT = Math.abs(ry - top) <= edgeTol && left <= rx && rx <= right;
    var nearB = Math.abs(ry - bottom) <= edgeTol && left <= rx && rx <= right;

    // 边上但更靠近角时，不抢边（距离角 < 边容差*1.2）
    if (bestCornerD <= edgeTol * 1.25) {
      nearL = nearL && !(bestCorner === 'resize_tl' || bestCorner === 'resize_bl');
      nearR = nearR && !(bestCorner === 'resize_tr' || bestCorner === 'resize_br');
      nearT = nearT && !(bestCorner === 'resize_tl' || bestCorner === 'resize_tr');
      nearB = nearB && !(bestCorner === 'resize_bl' || bestCorner === 'resize_br');
    }

    if (nearL) return 'resize_left';
    if (nearR) return 'resize_right';
    if (nearT) return 'resize_top';
    if (nearB) return 'resize_bottom';

    var inside = left <= rx && rx <= right && top <= ry && ry <= bottom;
    if (inside) return 'move';
    return null;
  }

  function hitTest(rx, ry) {
    var cornerTol = uiPx(Math.max(16, HANDLE_TOL * 1.8));
    var best = null;
    var bestD = Infinity;

    // 先全局找最近角点：比焦点框的移动/边更优先
    for (var i = _crops.length - 1; i >= 0; i--) {
      var box = _crops[i];
      var left = Math.min(box.x1, box.x2), right = Math.max(box.x1, box.x2);
      var top = Math.min(box.y1, box.y2), bottom = Math.max(box.y1, box.y2);
      var pts = [
        { mode: 'resize_tl', x: left, y: top },
        { mode: 'resize_tr', x: right, y: top },
        { mode: 'resize_bl', x: left, y: bottom },
        { mode: 'resize_br', x: right, y: bottom },
      ];
      for (var p = 0; p < pts.length; p++) {
        var d = Math.hypot(rx - pts[p].x, ry - pts[p].y);
        if (d < bestD) {
          bestD = d;
          best = { idx: i, mode: pts[p].mode };
        }
      }
    }
    if (best && bestD <= cornerTol) return best;

    if (_selected >= 0 && _selected < _crops.length) {
      var m = hitTestBox(rx, ry, _crops[_selected]);
      if (m) return { idx: _selected, mode: m };
    }
    for (var j = _crops.length - 1; j >= 0; j--) {
      if (j === _selected) continue;
      var m2 = hitTestBox(rx, ry, _crops[j]);
      if (m2) return { idx: j, mode: m2 };
    }
    return { idx: -1, mode: 'new' };
  }

  function setCursor(mode) {
    var c = 'crosshair';
    if (mode === 'move') c = 'move';
    else if (mode === 'resize_tl' || mode === 'resize_br') c = 'nwse-resize';
    else if (mode === 'resize_tr' || mode === 'resize_bl') c = 'nesw-resize';
    else if (mode === 'resize_left' || mode === 'resize_right') c = 'ew-resize';
    else if (mode === 'resize_top' || mode === 'resize_bottom') c = 'ns-resize';
    wrap.style.cursor = c;
  }

  function clampBox(x1, y1, x2, y2) {
    x1 = Math.max(0, Math.min(_origW - 1, x1));
    y1 = Math.max(0, Math.min(_origH - 1, y1));
    x2 = Math.max(x1 + 1, Math.min(_origW, x2));
    y2 = Math.max(y1 + 1, Math.min(_origH, y2));
    return { x1: x1, y1: y1, x2: x2, y2: y2 };
  }

  function applySnap(box, cand, mode) {
    if (!cand) return box;
    var sw = cand.crop_w, sh = cand.crop_h;
    var x1 = box.x1, y1 = box.y1, x2 = box.x2, y2 = box.y2;
    if (mode === 'move') {
      x2 = Math.min(_origW, x1 + sw);
      y2 = Math.min(_origH, y1 + sh);
      x1 = x2 - sw;
      y1 = y2 - sh;
    } else if (mode === 'resize_left') {
      x1 = x2 - sw; y2 = y1 + sh;
    } else if (mode === 'resize_right') {
      x2 = x1 + sw; y2 = y1 + sh;
    } else if (mode === 'resize_top') {
      y1 = y2 - sh; x2 = x1 + sw;
    } else if (mode === 'resize_bottom') {
      y2 = y1 + sh; x2 = x1 + sw;
    } else if (mode === 'resize_tl') {
      x1 = x2 - sw; y1 = y2 - sh;
    } else if (mode === 'resize_tr') {
      x2 = x1 + sw; y1 = y2 - sh;
    } else if (mode === 'resize_bl') {
      x1 = x2 - sw; y2 = y1 + sh;
    } else if (mode === 'resize_br') {
      x2 = x1 + sw; y2 = y1 + sh;
    } else {
      x2 = Math.min(_origW, x1 + sw);
      y2 = Math.min(_origH, y1 + sh);
    }
    var cl = clampBox(x1, y1, x2, y2);
    return makeCrop(cl.x1, cl.y1, cl.x2, cl.y2, cand, box.name);
  }

  function onPress(e) {
    if (!_img || _noCrop) return;
    var rect = canvas.getBoundingClientRect();
    var raw = cssToRaw(e.clientX - rect.left, e.clientY - rect.top);
    var hit = hitTest(raw.x, raw.y);
    _dragMode = hit.mode;
    _dragIdx = hit.idx;
    _dragStart = raw;
    if (hit.idx >= 0) {
      _selected = hit.idx;
      _dragBase = Object.assign({}, _crops[hit.idx]);
      renderList();
    } else {
      _dragBase = null;
    }
    setCursor(_dragMode);
  }

  function onDrag(e) {
    if (!_img || !_dragMode || _noCrop) return;
    var rect = canvas.getBoundingClientRect();
    var raw = cssToRaw(e.clientX - rect.left, e.clientY - rect.top);
    var iw = _origW, ih = _origH;
    var cand = null;
    var newBox = null;
    var name = '';

    if (_dragMode === 'new') {
      var sx0 = _dragStart.x, sy0 = _dragStart.y;
      var desiredW = Math.abs(raw.x - sx0);
      var desiredH = Math.abs(raw.y - sy0);
      if (desiredW < 2 || desiredH < 2) return;
      var signX = raw.x >= sx0 ? 1 : -1;
      var signY = raw.y >= sy0 ? 1 : -1;
      var maxW = signX > 0 ? (iw - sx0) : sx0;
      var maxH = signY > 0 ? (ih - sy0) : sy0;
      cand = chooseBest(maxW, maxH, desiredW, desiredH);
      if (!cand) return;
      var cw = cand.crop_w, ch = cand.crop_h;
      var x1 = signX > 0 ? sx0 : sx0 - cw;
      var y1 = signY > 0 ? sy0 : sy0 - ch;
      var cl = clampBox(x1, y1, x1 + cw, y1 + ch);
      newBox = makeCrop(cl.x1, cl.y1, cl.x2, cl.y2, cand, '');

      if (_dragIdx < 0) {
        // 新建临时框
        _crops.push(newBox);
        _dragIdx = _crops.length - 1;
        _selected = _dragIdx;
      } else {
        _crops[_dragIdx] = newBox;
      }
      redraw();
      renderList();
      return;
    }

    if (_dragIdx < 0 || !_dragBase) return;
    name = _dragBase.name || '';
    var bx1 = _dragBase.x1, by1 = _dragBase.y1, bx2 = _dragBase.x2, by2 = _dragBase.y2;
    var curW = Math.max(1, bx2 - bx1);
    var curH = Math.max(1, by2 - by1);

    if (_dragMode === 'move') {
      var dx = raw.x - _dragStart.x;
      var dy = raw.y - _dragStart.y;
      var nx1 = bx1 + dx, ny1 = by1 + dy, nx2 = bx2 + dx, ny2 = by2 + dy;
      if (nx1 < 0) { nx2 -= nx1; nx1 = 0; }
      if (ny1 < 0) { ny2 -= ny1; ny1 = 0; }
      if (nx2 > iw) { nx1 -= (nx2 - iw); nx2 = iw; }
      if (ny2 > ih) { ny1 -= (ny2 - ih); ny2 = ih; }
      newBox = clampBox(nx1, ny1, nx2, ny2);
      cand = chooseBest(iw, ih, nx2 - nx1, ny2 - ny1);
    } else if (_dragMode === 'resize_left') {
      cand = chooseBest(Math.max(1, bx2), ih, Math.max(1, bx2 - raw.x), curH, 1, 4);
      if (!cand) return;
      newBox = { x1: bx2 - cand.crop_w, y1: by1, x2: bx2, y2: by1 + cand.crop_h };
    } else if (_dragMode === 'resize_right') {
      cand = chooseBest(Math.max(1, iw - bx1), ih, Math.max(1, raw.x - bx1), curH, 1, 4);
      if (!cand) return;
      newBox = { x1: bx1, y1: by1, x2: bx1 + cand.crop_w, y2: by1 + cand.crop_h };
    } else if (_dragMode === 'resize_top') {
      cand = chooseBest(iw, Math.max(1, by2), curW, Math.max(1, by2 - raw.y), 4, 1);
      if (!cand) return;
      newBox = { x1: bx1, y1: by2 - cand.crop_h, x2: bx1 + cand.crop_w, y2: by2 };
    } else if (_dragMode === 'resize_bottom') {
      cand = chooseBest(iw, Math.max(1, ih - by1), curW, Math.max(1, raw.y - by1), 4, 1);
      if (!cand) return;
      newBox = { x1: bx1, y1: by1, x2: bx1 + cand.crop_w, y2: by1 + cand.crop_h };
    } else if (_dragMode === 'resize_tl') {
      cand = chooseBest(Math.max(1, bx2), Math.max(1, by2), Math.max(1, bx2 - raw.x), Math.max(1, by2 - raw.y));
      if (!cand) return;
      newBox = { x1: bx2 - cand.crop_w, y1: by2 - cand.crop_h, x2: bx2, y2: by2 };
    } else if (_dragMode === 'resize_tr') {
      cand = chooseBest(Math.max(1, iw - bx1), Math.max(1, by2), Math.max(1, raw.x - bx1), Math.max(1, by2 - raw.y));
      if (!cand) return;
      newBox = { x1: bx1, y1: by2 - cand.crop_h, x2: bx1 + cand.crop_w, y2: by2 };
    } else if (_dragMode === 'resize_bl') {
      cand = chooseBest(Math.max(1, bx2), Math.max(1, ih - by1), Math.max(1, bx2 - raw.x), Math.max(1, raw.y - by1));
      if (!cand) return;
      newBox = { x1: bx2 - cand.crop_w, y1: by1, x2: bx2, y2: by1 + cand.crop_h };
    } else if (_dragMode === 'resize_br') {
      cand = chooseBest(Math.max(1, iw - bx1), Math.max(1, ih - by1), Math.max(1, raw.x - bx1), Math.max(1, raw.y - by1));
      if (!cand) return;
      newBox = { x1: bx1, y1: by1, x2: bx1 + cand.crop_w, y2: by1 + cand.crop_h };
    }

    if (!newBox) return;
    var base = makeCrop(newBox.x1, newBox.y1, newBox.x2, newBox.y2, cand, name);
    // 二次吸附
    if (!cand) cand = chooseBest(base.x2 - base.x1, base.y2 - base.y1, base.x2 - base.x1, base.y2 - base.y1);
    _crops[_dragIdx] = applySnap(base, cand, _dragMode);
    redraw();
    renderList();
  }

  function onRelease() {
    _dragMode = null;
    _dragBase = null;
    _dragStart = null;
  }

  function onMove(e) {
    if (_noCrop || !_img) return;
    if (_dragMode) return;
    var rect = canvas.getBoundingClientRect();
    var raw = cssToRaw(e.clientX - rect.left, e.clientY - rect.top);
    var hit = hitTest(raw.x, raw.y);
    setCursor(hit.mode);
  }

  // ---- 加载 ----
  async function loadImages() {
    var res = await (await api('/api/crop/images')).json();
    _images = res;
    if (_images.length > 0) {
      var start = findNextUnfinished(-1);
      if (start < 0) {
        await loadIndex(0);
        toast('全部已裁剪');
      } else {
        await loadIndex(start);
      }
    } else {
      updateNav();
      toast('没有图片');
    }
  }

  function findNextUnfinished(fromIdx) {
    for (var i = fromIdx + 1; i < _images.length; i++) {
      if (!_images[i].done) return i;
    }
    return -1;
  }

  function goNextUnfinished() {
    var next = findNextUnfinished(_index);
    if (next < 0) {
      toast('后面没有未裁剪的图');
      return;
    }
    loadIndex(next);
  }

  function goPrev() {
    if (_index > 0) loadIndex(_index - 1);
  }

  async function loadIndex(idx) {
    if (!_images.length) return;
    _index = Math.max(0, Math.min(_images.length - 1, idx));
    var imgMeta = _images[_index];
    var blob = await (await api('/api/crop/image?path=' + encodeURIComponent(imgMeta.path))).blob();
    var url = URL.createObjectURL(blob);
    var im = new Image();
    await new Promise(function (resolve, reject) {
      im.onload = resolve;
      im.onerror = function () { reject(new Error('图片解码失败: ' + imgMeta.path)); };
      im.src = url;
    });
    URL.revokeObjectURL(url);
    _img = im;
    _origW = im.naturalWidth;
    _origH = im.naturalHeight;
    _noCrop = false;
    document.getElementById('chk-no-crop').checked = false;
    document.getElementById('part-name-wrap').style.display = 'none';
    initDefaultCrop();
    fitCanvas();
    redraw();
    renderList();
    updateNav();
  }

  async function saveAndNext() {
    if (!_images.length || !_img) return;
    var imgMeta = _images[_index];
    var partName = (document.getElementById('part-name').value || '').trim();
    if (_noCrop && !partName) {
      toast('整图输出需填写部位名');
      return;
    }
    var body = {
      path: imgMeta.path,
      no_crop_resize: _noCrop,
      part_name: partName,
      crops: _noCrop ? [] : _crops.map(function (c) {
        return {
          x1: c.x1, y1: c.y1, x2: c.x2, y2: c.y2,
          target_w: c.target_w, target_h: c.target_h, zoom: c.zoom,
          name: c.name || '',
        };
      }),
    };
    if (!_noCrop && body.crops.length === 0) {
      toast('请至少保留一个选区');
      return;
    }
    var res = await (await api('/api/crop/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })).json();
    imgMeta.done = true;
    toast('已保存 ' + res.count + ' 张');
    var next = findNextUnfinished(_index);
    if (next < 0) {
      updateNav();
      toast('全部完成');
      return;
    }
    await loadIndex(next);
  }

  function addCrop() {
    if (_noCrop || !_origW) return;
    var crop = makeDefaultSizedCrop('', _crops.length);
    if (!crop) {
      toast('无法创建选区');
      return;
    }
    _crops.push(crop);
    _selected = _crops.length - 1;
    redraw();
    renderList();
    updateNav();
  }

  function delSelected() {
    if (_noCrop || _selected < 0 || _selected >= _crops.length) return;
    removeCropAt(_selected);
    redraw();
    renderList();
    updateNav();
  }

  function cycleSelected(delta) {
    if (_noCrop || _crops.length < 2) return;
    if (_selected < 0) _selected = 0;
    else _selected = (_selected + delta + _crops.length) % _crops.length;
    redraw();
    renderList();
    updateNav();
  }

  function clearCrops() {
    if (_noCrop) return;
    initDefaultCrop();
    redraw();
    renderList();
    updateNav();
  }

  function onToggleNoCrop() {
    _noCrop = document.getElementById('chk-no-crop').checked;
    var wrapEl = document.getElementById('part-name-wrap');
    wrapEl.style.display = _noCrop ? 'flex' : 'none';
    if (_noCrop) {
      wrap.style.cursor = 'default';
      if (!document.getElementById('part-name').value && _crops[_selected] && _crops[_selected].name) {
        document.getElementById('part-name').value = _crops[_selected].name;
      }
    } else if (_crops.length === 0) {
      initDefaultCrop();
    }
    redraw();
    renderList();
    updateNav();
  }

  async function init() {
    var cfg = await (await api('/api/crop/config')).json();
    _datasetRoot = cfg.root || '';
    loadParts(cfg.parts || []);
    var hint = document.getElementById('layout-hint');
    if (hint) {
      hint.textContent = (_datasetRoot || '-') +
        ' · 默认裁切见侧栏（按部位保存在本机）' +
        (_autoParts.length ? ' · 自动部位 ' + _autoParts.join(', ') : '');
    }

    var matchCfg = await (await api('/api/crop/candidates')).json();
    applyMatchConfig(matchCfg);

    var globalDefault = document.getElementById('crop-default-global');
    if (globalDefault) {
      globalDefault.addEventListener('change', function () {
        commitPartDefault('', globalDefault.value);
      });
    }

    document.getElementById('btn-prev').addEventListener('click', goPrev);
    document.getElementById('btn-next').addEventListener('click', goNextUnfinished);
    document.getElementById('btn-skip').addEventListener('click', goNextUnfinished);
    document.getElementById('btn-save').addEventListener('click', function () {
      saveAndNext();
    });
    document.getElementById('btn-add-crop').addEventListener('click', addCrop);
    document.getElementById('btn-clear-crops').addEventListener('click', clearCrops);
    document.getElementById('chk-no-crop').addEventListener('change', onToggleNoCrop);
    document.getElementById('btn-add-part').addEventListener('click', addPart);
    document.getElementById('part-add-input').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); addPart(); }
    });

    canvas.addEventListener('mousedown', onPress);
    wrap.addEventListener('wheel', function (e) {
      if (_noCrop || _crops.length < 2 || _dragMode) return;
      e.preventDefault();
      cycleSelected(e.deltaY > 0 ? 1 : -1);
    }, { passive: false });
    window.addEventListener('mousemove', function (e) {
      if (_dragMode) onDrag(e);
      else onMove(e);
    });
    window.addEventListener('mouseup', onRelease);

    window.addEventListener('resize', function () {
      fitCanvas();
      redraw();
    });

    document.addEventListener('keydown', function (e) {
      var inField = e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT';
      var key = e.key;
      var code = e.code;

      // Ctrl+S / Ctrl+N：输入框内也生效
      if ((e.ctrlKey || e.metaKey) && (key === 's' || key === 'S' || code === 'KeyS')) {
        e.preventDefault();
        if (inField) e.target.blur();
        saveAndNext();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && (key === 'n' || key === 'N' || code === 'KeyN')) {
        e.preventDefault();
        if (inField) e.target.blur();
        goNextUnfinished();
        return;
      }
      if (inField) return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;

      if (key === 'Delete' || key === 'Backspace' || key === 'd' || key === 'D' || code === 'KeyD') {
        e.preventDefault();
        delSelected();
      } else if (key === 's' || key === 'S' || code === 'KeyS') {
        e.preventDefault();
        saveAndNext();
      } else if (key === ' ' || code === 'Space') {
        e.preventDefault();
        goNextUnfinished();
      } else if (key === 'ArrowLeft') {
        e.preventDefault();
        goPrev();
      } else if (key === 'ArrowRight') {
        e.preventDefault();
        goNextUnfinished();
      } else if (key === 'a' || key === 'A' || code === 'KeyA') {
        e.preventDefault();
        addCrop();
      } else if (key === 'c' || key === 'C' || code === 'KeyC') {
        e.preventDefault();
        clearCrops();
      }
    });

    await loadImages();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
