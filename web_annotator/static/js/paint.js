/**
 * paint.js —— 蒙版绘制
 */
(function() {
  'use strict';

  var _images = [];
  // 当前图片状态: 随图片切换的可变状态收敛于单一对象, 异步加载各自持有副本,
  // 完成后再交换到 _cur; 过期加载不会污染当前图(避免把上一张的蒙版保存到新图路径).
  var _cur = null; // { index, item, w, h, rgbImg, alpha, modified }
  var _strip = null; // 横排预览条控制器(Filmstrip)
  var _scale = 1;
  var _viewW = 0, _viewH = 0, _dpr = 1;
  var _brushRadius = 80, _brushPct = 3, _defaultAlpha = 255;
  var _tool = 'brush', _isDrawing = false, _isTouch = false, _paintValue = 0;
  var _rectStart = null, _mouseScreen = null;
  var _preloadCache = {};
  var _renderScheduled = false;
  var _rgbScaledCanvas = null;
  var _compCanvas = null;
  // 画布背景: checker 棋盘格 / white 纯白 / black 纯黑 / custom 自定义色(CSS 背景实现)
  var _bgStyle = 'checker', _bgColor = '#ffffff';
  var PRELOAD_RADIUS = 15;
  // 加载令牌: 递增序号, 过期请求/过期 onload 据此丢弃, 保证只有最新一次切图生效
  var _loadToken = 0;

  // ---- 笔刷半径: sqrt(w*h) * 百分比 ----
  function _calcBrush() {
    if (!_cur || !_cur.w || !_cur.h) return;
    _brushRadius = Math.max(2, Math.round(Math.sqrt(_cur.w * _cur.h) * _brushPct / 100));
  }

  var canvas = document.getElementById('paint-canvas');
  var ctx = canvas.getContext('2d');
  var wrap = document.getElementById('canvas-wrap');
  var _brushPreview = document.getElementById('brush-preview');
  var _bgStyleEl = document.getElementById('bg-style');
  var _bgColorEl = document.getElementById('bg-color');
  if (_bgStyleEl) _bgStyle = _bgStyleEl.value;
  if (_bgColorEl) _bgColor = _bgColorEl.value;
  _applyBackdrop();

  /**
   * @param {string} msg
   * 引用：保存成功、错误提示。
   */
  function errshow(msg) {
    toast('\u26a0 ' + msg);
  }
  window.addEventListener('error', function(e) { errshow(e.message); });
  window.addEventListener('unhandledrejection', function(e) {
    errshow(String(e.reason && e.reason.message ? e.reason.message : e.reason));
  });

  // ---- API ----
  async function api(path, opts) {
    opts = opts || {};
    var url = path.indexOf('/api/') === 0 ? API.apiPath(path.slice(4)) : path;
    var res = await fetch(url, { headers: opts.headers || {}, method: opts.method || 'GET', body: opts.body || undefined });
    if (!res.ok) {
      var msg = await res.text();
      throw new Error(res.status + ': ' + msg);
    }
    return res;
  }

  // ---- 配置 ----
  async function loadConfig() {
    var cfg = await (await api('/api/paint/config')).json();
    _defaultAlpha = cfg.default_alpha == null ? 255 : cfg.default_alpha;
    document.getElementById('default-alpha').value = _defaultAlpha;
  }
  async function saveConfig() {
    var value = parseInt(document.getElementById('default-alpha').value);
    _defaultAlpha = Number.isNaN(value) ? 255 : value;
    await api('/api/paint/config', { method: 'POST',
      body: JSON.stringify({ default_alpha: _defaultAlpha }),
      headers: { 'Content-Type': 'application/json' } });
  }

  // ---- 加载 ----
  function currentDir() {
    return (typeof API !== 'undefined' && API.getDir) ? API.getDir() : 'main';
  }

  async function loadImages() {
    var res = await (await api('/api/paint/images?dir=' + encodeURIComponent(currentDir()))).json();
    _images = res;
    if (_strip) _strip.refresh();
    if (_images.length > 0) loadIndex(0);
    else {
      updateNav();
      toast('没有图片');
    }
  }

  /**
   * 构建蒙版画布 cur.alpha: 有蒙版时继承 cur.mask 的 A 通道(蒙版),
   * 无蒙版时填默认 alpha. 蒙版唯一权威为 cur.alpha 的 A 通道.
   * Callers: loadIndex(缓存命中与加载完成).
   */
  function _initAlphaCanvas(cur) {
    cur.alpha = document.createElement('canvas');
    cur.alpha.width = cur.w; cur.alpha.height = cur.h;
    var ctx = cur.alpha.getContext('2d');
    if (cur.item.has_mask) {
      ctx.drawImage(cur.mask, 0, 0);
    } else {
      var value = parseInt(document.getElementById('default-alpha').value);
      _defaultAlpha = Number.isNaN(value) ? 255 : value;
      ctx.clearRect(0, 0, cur.w, cur.h);
      ctx.globalAlpha = _defaultAlpha / 255;
      ctx.fillStyle = '#fff';
      ctx.fillRect(0, 0, cur.w, cur.h);
      ctx.globalAlpha = 1;
    }
  }

  /**
   * 将加载完成的状态对象整体交换为当前状态, 并一次性刷新状态栏与画布.
   * Callers: loadIndex(缓存命中与加载完成).
   */
  function _applyCurrent(cur) {
    _cur = cur;
    _calcBrush(); _updateBrushUI();
    invalidateScaledCache();
    fitCanvas(); _scheduleRender(); updateNav();
  }

  async function loadIndex(idx) {
    if (_images.length === 0) return;
    idx = Math.max(0, Math.min(_images.length - 1, idx));
    var img = _images[idx];
    var token = ++_loadToken;
    // 每次加载持有独立状态副本, 完成后经 _applyCurrent 整体交换到 _cur;
    // 加载完成前 _cur 保持上一张图, 状态栏与工作区始终一致(不会"状态栏已变、工作区未变")
    var cur = {
      index: idx, item: img, w: 0, h: 0,
      rgbImg: null, mask: null, alpha: null, modified: false,
    };

    var cached = _preloadCache[img.path];
    if (cached) {
      cur.w = cached.w; cur.h = cached.h;
      cur.rgbImg = cached.img;
      cur.mask = cached.mask;
      _initAlphaCanvas(cur);
      _applyCurrent(cur);
      _preloadNeighbors(idx);
      return;
    }

    var entry = await _loadEntry(img);
    if (token !== _loadToken) return; // 已被更新的加载取代
    cur.w = entry.w; cur.h = entry.h;
    cur.rgbImg = entry.img;
    cur.mask = entry.mask;
    _preloadCache[img.path] = entry;
    _initAlphaCanvas(cur);
    _applyCurrent(cur);
    _preloadNeighbors(idx);
  }

  // ---- 预加载 ----
  var _preloadSeq = 0;

  /**
   * 统计当前索引邻域内已进入缓冲区的数量。
   * @returns {{ready: number, want: number, total: number}}
   */
  function bufferStats() {
    var idx = _cur ? _cur.index : -1;
    var ready = 0;
    var want = 0;
    for (var di = -PRELOAD_RADIUS; di <= PRELOAD_RADIUS; di++) {
      var ni = idx + di;
      if (ni < 0 || ni >= _images.length) continue;
      want++;
      if (_preloadCache[_images[ni].path]) ready++;
    }
    return {
      ready: ready,
      want: want,
      total: Object.keys(_preloadCache).length,
    };
  }

  /**
   * 刷新底部六段状态栏。引用：updateNav、预加载完成。
   */
  function updateStatusBar() {
    var cur = _cur;
    var img = cur ? _images[cur.index] : null;
    var done = 0;
    for (var i = 0; i < _images.length; i++) {
      if (_images[i].has_mask) done++;
    }
    document.getElementById('seg-pos').textContent =
      (_images.length && cur ? (cur.index + 1) : 0) + '/' + _images.length;
    document.getElementById('seg-done').textContent = '已标注 ' + done;
    document.getElementById('seg-name').textContent = img ? img.stem : '-';
    document.getElementById('seg-obj').textContent = img
      ? (img.has_mask ? '已标注' : '待标注')
      : '-';
    document.getElementById('seg-metric').textContent = (cur && cur.w)
      ? (cur.w + '×' + cur.h)
      : '-';
    var buf = bufferStats();
    document.getElementById('seg-extra').textContent =
      '缓冲 ' + buf.ready + '/' + buf.want + '（缓存 ' + buf.total + '）';
    if (_strip) _strip.sync();
  }

  async function _preloadNeighbors(idx) {
    var seq = ++_preloadSeq;
    for (var di = 1; di <= PRELOAD_RADIUS; di++) {
      if (seq !== _preloadSeq) return;
      var ni = idx + di; if (ni < _images.length) await _preloadOne(_images[ni]);
      ni = idx - di; if (ni >= 0) await _preloadOne(_images[ni]);
    }
  }

  /**
   * 下载接口 blob. Callers: _loadEntry.
   */
  function _fetchBlob(apiPath, imgPath) {
    return fetch(API.apiPath(apiPath) + '?path=' + encodeURIComponent(imgPath))
      .then(function(r) { return r.blob(); });
  }

  /**
   * 将 blob 解码为 Image. Callers: _loadEntry.
   */
  function _decodeImage(blob) {
    return new Promise(function(resolve, reject) {
      var url = URL.createObjectURL(blob);
      var el = new Image();
      el.onload = function() { URL.revokeObjectURL(url); resolve(el); };
      el.onerror = function() { URL.revokeObjectURL(url); reject(new Error('图片解码失败')); };
      el.src = url;
    });
  }

  /**
   * 加载单张图的视觉与蒙版: 视觉走 AVIF(有损可接受, 已压缩), 蒙版走无损 PNG(不可有损),
   * 前端合成. 返回 { img, mask, w, h }. Callers: loadIndex、_preloadOne.
   */
  async function _loadEntry(img) {
    var visualP = _fetchBlob('/paint/image', img.path);
    var maskP = img.has_mask ? _fetchBlob('/paint/mask', img.path) : Promise.resolve(null);
    var results = await Promise.all([visualP, maskP]);
    var visualEl = await _decodeImage(results[0]);
    var maskEl = results[1] ? await _decodeImage(results[1]) : null;
    // 原图尺寸取自图片列表(预览已压缩, 蒙版须按原尺寸绘制)
    return { img: visualEl, mask: maskEl, w: img.width, h: img.height };
  }

  async function _preloadOne(img) {
    if (_preloadCache[img.path]) return;
    try {
      _preloadCache[img.path] = await _loadEntry(img);
    } catch (e) { /* 预加载失败不阻断翻页 */ }
    updateStatusBar();
  }
  function _updateBrushUI() {
    var labelEl = document.getElementById('brush-label');
    if (labelEl) labelEl.textContent = _brushPct.toFixed(1) + '% ~' + _brushRadius + 'px';
    // 同步滑动条位置: 滚轮/其它途径改笔刷时滑块跟随, 避免数值与滑块脱节
    var sliderEl = document.getElementById('brush-slider');
    if (sliderEl && parseFloat(sliderEl.value) !== _brushPct) sliderEl.value = _brushPct.toFixed(1);
  }
  function fitCanvas() {
    if (!_cur) return;
    var cw = wrap.clientWidth, ch = wrap.clientHeight;
    _dpr = window.devicePixelRatio || 1;
    _scale = Math.min(cw / _cur.w, ch / _cur.h);
    _viewW = Math.max(1, Math.floor(_cur.w * _scale));
    _viewH = Math.max(1, Math.floor(_cur.h * _scale));
    // 缓冲保持原图像素；CSS 缩小显示，交给浏览器缩放（与 <img> 同路径，避免先缩后画发糊）
    if (canvas.width !== _cur.w || canvas.height !== _cur.h) {
      canvas.width = _cur.w;
      canvas.height = _cur.h;
      invalidateScaledCache();
    }
    // 居中由 .paint-canvas-wrap 的 flex 负责, JS 只算适配宽高
    canvas.style.width = _viewW + 'px';
    canvas.style.height = _viewH + 'px';
    canvas.style.imageRendering = 'auto';
  }
  window.addEventListener('resize', function() { if (_cur && _cur.rgbImg) { fitCanvas(); _scheduleRender(); } });

  // ---- 渲染 ----

  function _scheduleRender() {
    if (!_renderScheduled) {
      _renderScheduled = true;
      requestAnimationFrame(function() {
        _renderScheduled = false;
        _doRender();
      });
    }
  }

  function invalidateScaledCache() {
    _rgbScaledCanvas = null;
    _compCanvas = null;
  }

  function _ensureRgbFull() {
    var cur = _cur;
    if (!cur || !cur.rgbImg) return;
    // 以来源引用做缓存键: 同尺寸的不同图片也不会串用旧缓冲
    if (_rgbScaledCanvas && _rgbScaledCanvas._src === cur.rgbImg) return;
    _rgbScaledCanvas = document.createElement('canvas');
    _rgbScaledCanvas.width = cur.w;
    _rgbScaledCanvas.height = cur.h;
    _rgbScaledCanvas._src = cur.rgbImg;
    var rctx = _rgbScaledCanvas.getContext('2d');
    // 预览已压缩(小于原尺寸), 必须缩放铺满画布, 否则只画在左上角
    rctx.drawImage(cur.rgbImg, 0, 0, cur.w, cur.h);
  }

  /**
   * 以 CSS 背景实现画布背景(蒙版透明区透过): 棋盘格按默认 alpha 调暗, 屏幕固定 12px/格,
   * 纯色/自定义按所选颜色. CSS 图案按屏幕像素渲染, 随显示缩放自动适配,
   * 相比 JS 生成整幅原分辨率棋盘画布不占内存. Callers: 初始化、bg-style/bg-color/default-alpha 变化.
   */
  function _applyBackdrop() {
    if (_bgStyle === 'checker') {
      var value = parseInt(document.getElementById('default-alpha').value);
      var dk = (Number.isNaN(value) ? 255 : value) / 255;
      var v0 = Math.round(204 * dk), v1 = Math.round(153 * dk);
      canvas.style.background =
        'repeating-conic-gradient(rgb(' + v0 + ',' + v0 + ',' + v0 + ') 0 25%, rgb(' + v1 + ',' + v1 + ',' + v1 + ') 0 50%) 0 0 / 24px 24px';
    } else {
      var color = _bgStyle === 'white' ? '#ffffff' : (_bgStyle === 'black' ? '#000000' : _bgColor);
      canvas.style.background = color;
    }
  }

  function _doRender() {
    var cur = _cur;
    if (!cur || !cur.rgbImg || !cur.alpha) return;
    _ensureRgbFull();

    if (!_compCanvas || _compCanvas.width !== cur.w || _compCanvas.height !== cur.h) {
      _compCanvas = document.createElement('canvas');
      _compCanvas.width = cur.w;
      _compCanvas.height = cur.h;
    }
    var cctx = _compCanvas.getContext('2d');
    cctx.globalCompositeOperation = 'source-over';
    cctx.clearRect(0, 0, cur.w, cur.h);
    cctx.drawImage(_rgbScaledCanvas, 0, 0);
    cctx.globalCompositeOperation = 'destination-in';
    cctx.drawImage(cur.alpha, 0, 0);

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, cur.w, cur.h);
    // 蒙版透明区透过 CSS 背景显示(棋盘格/纯色), 画布只负责图像+蒙版合成
    ctx.drawImage(_compCanvas, 0, 0);
  }

  // ---- 笔刷预览 ----
  function _updateBrushPreview() {
    if (_tool !== 'brush' || !_mouseScreen || !_cur) {
      _brushPreview.style.display = 'none'; return;
    }
    var wrapRect = wrap.getBoundingClientRect();
    var canvasRect = canvas.getBoundingClientRect();
    var mx = _mouseScreen[0] - wrapRect.left, my = _mouseScreen[1] - wrapRect.top;
    var r = _brushRadius * (canvasRect.width / Math.max(1, _cur.w));
    if (r < 2) { _brushPreview.style.display = 'none'; return; }
    _brushPreview.style.display = 'block';
    _brushPreview.style.width = (r * 2) + 'px';
    _brushPreview.style.height = (r * 2) + 'px';
    _brushPreview.style.left = (mx - r) + 'px';
    _brushPreview.style.top = (my - r) + 'px';
  }

  function screenToOrig(sx, sy) {
    var cur = _cur;
    if (!cur) return [0, 0];
    var rect = canvas.getBoundingClientRect();
    var x = (sx - rect.left) / Math.max(1e-6, rect.width) * cur.w;
    var y = (sy - rect.top) / Math.max(1e-6, rect.height) * cur.h;
    return [Math.max(0, Math.min(cur.w - 1, Math.floor(x))),
            Math.max(0, Math.min(cur.h - 1, Math.floor(y)))];
  }

  function stampDisc(cx, cy, r, val) {
    var cur = _cur;
    if (!cur || !cur.alpha) return;
    var ctx2 = cur.alpha.getContext('2d');
    if (val === 0) {
      ctx2.globalCompositeOperation = 'destination-out';
      ctx2.fillStyle = '#fff';
      ctx2.beginPath();
      ctx2.arc(cx, cy, r, 0, Math.PI * 2);
      ctx2.fill();
      ctx2.globalCompositeOperation = 'source-over';
    } else {
      ctx2.globalAlpha = val / 255;
      ctx2.fillStyle = '#fff';
      ctx2.beginPath();
      ctx2.arc(cx, cy, r, 0, Math.PI * 2);
      ctx2.fill();
      ctx2.globalAlpha = 1;
    }
  }

  function strokeLine(x0, y0, x1, y1, r, val) {
    var dist = Math.max(Math.abs(x1-x0), Math.abs(y1-y0), 1);
    for (var i = 0; i <= dist; i++) {
      var t = i/dist;
      stampDisc(Math.round(x0+(x1-x0)*t), Math.round(y0+(y1-y0)*t), r, val);
    }
  }

  function fillRect(x0, y0, x1, y1, val) {
    var cur = _cur;
    if (!cur || !cur.alpha) return;
    var xa = Math.max(0, Math.min(x0, x1)), xb = Math.min(cur.w-1, Math.max(x0, x1));
    var ya = Math.max(0, Math.min(y0, y1)), yb = Math.min(cur.h-1, Math.max(y0, y1));
    var ctx2 = cur.alpha.getContext('2d');
    if (val === 0) {
      ctx2.clearRect(xa, ya, xb - xa + 1, yb - ya + 1);
    } else {
      ctx2.globalAlpha = val / 255;
      ctx2.fillStyle = '#fff';
      ctx2.fillRect(xa, ya, xb - xa + 1, yb - ya + 1);
      ctx2.globalAlpha = 1;
    }
  }

  // ---- 触屏 ----
  canvas.addEventListener('touchstart', function(e) {
    e.preventDefault(); if (!_cur || !_cur.alpha || e.touches.length !== 1) return;
    _isDrawing = true; _isTouch = true;
    var t = e.touches[0];
    var pos = screenToOrig(t.clientX, t.clientY);
    var swapped = document.getElementById('chk-swap-buttons').checked;
    _paintValue = swapped ? 255 : 0;
    if (_tool === 'brush') stampDisc(pos[0], pos[1], _brushRadius, _paintValue);
    _mouseScreen = [t.clientX, t.clientY];
    _rectStart = pos; _scheduleRender();
  }, { passive: false });
  canvas.addEventListener('touchmove', function(e) {
    e.preventDefault(); if (!_cur || !_cur.alpha || !_isDrawing) return;
    var t = e.touches[0];
    _mouseScreen = [t.clientX, t.clientY];
    _updateBrushPreview();
    var pos = screenToOrig(t.clientX, t.clientY);
    if (_tool === 'brush' && _rectStart) { strokeLine(_rectStart[0], _rectStart[1], pos[0], pos[1], _brushRadius, _paintValue); _rectStart = pos; }
    _scheduleRender();
  }, { passive: false });
  canvas.addEventListener('touchend', function(e) {
    e.preventDefault(); if (!_isTouch || !_isDrawing || !_cur || !_cur.alpha) return;
    var t = e.changedTouches[0];
    if (_tool === 'rect' && _rectStart && t) { var pos = screenToOrig(t.clientX, t.clientY); fillRect(_rectStart[0], _rectStart[1], pos[0], pos[1], _paintValue); }
    _isDrawing = false; _isTouch = false; _rectStart = null; _brushPreview.style.display = 'none'; _cur.modified = true; _scheduleRender();
  }, { passive: false });
  canvas.addEventListener('touchcancel', function() { _isDrawing = false; _isTouch = false; _rectStart = null; _mouseScreen = null; _brushPreview.style.display = 'none'; });
  // 防止触屏时触发对应的 mouse 事件
  canvas.addEventListener('mousedown', function(e) { if (_isTouch) return; e.preventDefault(); if (!_cur || !_cur.alpha) return;
    _isDrawing = true;
    var pos = screenToOrig(e.clientX, e.clientY);
    var swapped = document.getElementById('chk-swap-buttons').checked;
    _paintValue = (e.button === 0) ? (swapped ? 255 : 0) : (swapped ? 0 : 255);
    if (_tool === 'brush') stampDisc(pos[0], pos[1], _brushRadius, _paintValue);
    _rectStart = pos; _scheduleRender();
  });
  canvas.addEventListener('mousemove', function(e) {
    _mouseScreen = [e.clientX, e.clientY];
    if (!_cur || !_cur.alpha) return;
    _updateBrushPreview();
    if (!_isDrawing) return;
    var pos = screenToOrig(e.clientX, e.clientY);
    if (_tool === 'brush' && _rectStart) { strokeLine(_rectStart[0], _rectStart[1], pos[0], pos[1], _brushRadius, _paintValue); _rectStart = pos; }
    _scheduleRender();
  });
  canvas.addEventListener('mouseup', function(e) {
    if (!_isDrawing || !_cur || !_cur.alpha) return;
    if (_tool === 'rect' && _rectStart) { var pos = screenToOrig(e.clientX, e.clientY); fillRect(_rectStart[0], _rectStart[1], pos[0], pos[1], _paintValue); }
    _isDrawing = false; _rectStart = null; _brushPreview.style.display = 'none'; _cur.modified = true; _scheduleRender();
  });
  canvas.addEventListener('mouseleave', function() { _isDrawing = false; _rectStart = null; _mouseScreen = null; _brushPreview.style.display = 'none'; });
  canvas.addEventListener('contextmenu', function(e) { e.preventDefault(); });
  canvas.addEventListener('wheel', function(e) {
    e.preventDefault();
    var d = e.deltaY < 0 ? 1 : -1;
    _brushPct = Math.max(0.5, Math.min(20, _brushPct + d * Math.max(0.1, _brushPct * 0.15)));
    _calcBrush(); _updateBrushUI(); _updateBrushPreview();
  });

  // ---- 保存 ----
  async function doSave(force) {
    var cur = _cur;
    if (!cur || !cur.alpha) return;
    var img = cur.item;
    // 全新图片即使没有动笔，也要写出当前默认 alpha mask。
    if (!force && !cur.modified && img.has_mask) return;
    // 从蒙版画布读取 alpha(唯一权威), 供上传 RGB 纯色与缓存替换使用
    var ctx = cur.alpha.getContext('2d');
    var imgData = ctx.getImageData(0, 0, cur.w, cur.h);
    var alphaArr = new Uint8Array(cur.w * cur.h);
    for (var i = 0; i < alphaArr.length; i++) alphaArr[i] = imgData.data[i * 4 + 3];
    // 上传时把 RGB 统一为纯色(黑), 蒙版只在 A 通道(后端仅取 A), 避免传输混合 RGB
    for (var i = 0; i < alphaArr.length; i++) {
      imgData.data[i * 4] = 0;
      imgData.data[i * 4 + 1] = 0;
      imgData.data[i * 4 + 2] = 0;
    }
    ctx.putImageData(imgData, 0, 0);
    return new Promise(function(resolve) {
      cur.alpha.toBlob(function(blob) {
        if (!blob) { errshow('toBlob failed'); resolve(); return; }
        fetch(API.apiPath('/paint/upload-mask') + '?path=' + encodeURIComponent(img.path), {
          method: 'POST', headers: { 'Content-Type': 'image/png' }, body: blob
        }).then(function(r) { return r.json(); }).then(function(res) {
          cur.modified = false;
          img.has_mask = true; updateNav();
          toast('已保存: ' + res.saved);
          var cached = _preloadCache[img.path];
          if (cached) {
            // 保存后刷新蒙版缓存: 视觉不变, 蒙版 A 换成最新(RGB 恒白)
            var mCanvas = document.createElement('canvas');
            mCanvas.width = cur.w; mCanvas.height = cur.h;
            var mctx = mCanvas.getContext('2d');
            var mData = mctx.createImageData(cur.w, cur.h);
            var md = mData.data;
            for (var i = 0; i < alphaArr.length; i++) {
              var j = i * 4, v = alphaArr[i];
              md[j] = 255; md[j + 1] = 255; md[j + 2] = 255; md[j + 3] = v;
            }
            mctx.putImageData(mData, 0, 0);
            cached.mask = mCanvas;
          }
          resolve();
        }).catch(function() { errshow('上传失败'); resolve(); });
      }, 'image/png');
    });
  }

  async function goPrev() { if (_cur && _cur.index > 0) { await doSave(false); loadIndex(_cur.index - 1); } }
  async function goNext() { if (_cur && _cur.index < _images.length - 1) { await doSave(false); loadIndex(_cur.index + 1); } }
  function goSkip() { if (_cur && _cur.index < _images.length - 1) loadIndex(_cur.index + 1); }

  // ---- 工具栏 ----
  document.getElementById('btn-prev').addEventListener('click', goPrev);
  document.getElementById('btn-next').addEventListener('click', goNext);
  document.getElementById('btn-skip').addEventListener('click', goSkip);
  // 横排预览条: 点击第一段(位置)展开, 点击缩略图跳转
  _strip = Filmstrip.create({
    container: document.getElementById('filmstrip'),
    getItems: function () { return _images; },
    thumbSrc: function (it) {
      return it.thumbnail_hash ? API.thumbnailUrl(it.thumbnail_hash) : API.imageUrl(it.path);
    },
    getActive: function () { return _cur ? _cur.index : 0; },
    onSelect: function (i) { loadIndex(i); },
  });
  document.getElementById('seg-pos').addEventListener('click', function () { _strip.toggle(); });
  var toolSel = document.getElementById('tool-select');
  if (toolSel) toolSel.addEventListener('change', function() { _tool = this.value; });
  document.getElementById('brush-slider').addEventListener('input', function() { _brushPct = parseFloat(this.value); _calcBrush(); _updateBrushUI(); _updateBrushPreview(); });
  document.getElementById('bg-style').addEventListener('change', function() {
    _bgStyle = this.value;
    _applyBackdrop();
  });
  document.getElementById('bg-color').addEventListener('change', function() { _bgColor = this.value; _applyBackdrop(); });
  document.getElementById('btn-save').addEventListener('click', function() { doSave(true); });
  document.getElementById('btn-clear').addEventListener('click', function() { if (_cur && _cur.alpha) { _cur.alpha.getContext('2d').clearRect(0, 0, _cur.w, _cur.h); _cur.modified = true; _scheduleRender(); } });
  document.getElementById('btn-fill').addEventListener('click', function() { if (_cur && _cur.alpha) { var fc = _cur.alpha.getContext('2d'); fc.fillStyle = '#fff'; fc.fillRect(0, 0, _cur.w, _cur.h); _cur.modified = true; _scheduleRender(); } });
  document.getElementById('btn-default').addEventListener('click', function() {
    var value = parseInt(document.getElementById('default-alpha').value);
    _defaultAlpha = Number.isNaN(value) ? 255 : value;
    if (_cur && _cur.alpha) { var dc = _cur.alpha.getContext('2d'); dc.clearRect(0, 0, _cur.w, _cur.h); dc.globalAlpha = _defaultAlpha / 255; dc.fillStyle = '#fff'; dc.fillRect(0, 0, _cur.w, _cur.h); dc.globalAlpha = 1; _cur.modified = true; _scheduleRender(); }
    saveConfig();
  });
  document.getElementById('default-alpha').addEventListener('change', function() { saveConfig(); _applyBackdrop(); });

  document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === 'ArrowLeft' || e.key === 'a' || e.key === 'A') { e.preventDefault(); goPrev(); }
    else if (e.key === 'ArrowRight' || e.key === 'd' || e.key === 'D') { e.preventDefault(); goNext(); }
    else if (e.key === 's' || e.key === 'S') { e.preventDefault(); doSave(true); }
    else if (e.key === 'x' || e.key === 'X') { goSkip(); }
  });

  function updateNav() {
    updateStatusBar();
  }

  // BiRefNet 蒙版批量生成
  var birefnetBtn = document.getElementById('btn-birefnet-mask');
  var birefnetDlg = document.getElementById('birefnet-dlg');
  if (birefnetBtn && birefnetDlg) {
    birefnetBtn.addEventListener('click', function () { birefnetDlg.hidden = false; });
    document.getElementById('birefnet-cancel').addEventListener('click', function () { birefnetDlg.hidden = true; });
    // 「跳过已有蒙版」仅批量模式可用: 单图模式禁用(灰显由 CSS 派生), disabled 只能 JS 设
    var syncSkipEnabled = function () {
      document.getElementById('birefnet-skip').disabled = !document.getElementById('birefnet-batch').checked;
    };
    document.getElementById('birefnet-batch').addEventListener('change', syncSkipEnabled);
    syncSkipEnabled();
    document.getElementById('birefnet-start').addEventListener('click', function () {
      var expand = parseInt(document.getElementById('birefnet-expand').value) || 32;
      var shrink = parseInt(document.getElementById('birefnet-shrink').value) || 0;
      var maskMin = parseInt(document.getElementById('birefnet-min').value) || 96;
      var maskMax = parseInt(document.getElementById('birefnet-max').value) || 255;
      var batch = document.getElementById('birefnet-batch').checked;
      // 单图模式无「跳过已有蒙版」概念, 始终重新生成; 仅批量模式应用该选项
      var skipExisting = batch ? document.getElementById('birefnet-skip').checked : false;
      var body = {
        dir: currentDir(),
        mask_expand: expand, mask_shrink: shrink,
        mask_min: maskMin, mask_max: maskMax,
        skip_existing: skipExisting,
      };
      // 默认单图: 仅重新生成当前图片; 勾选「批量」处理整个目录
      if (!batch) {
        if (!_cur || !_cur.item) { toast('当前没有图片'); return; }
        body.path = _cur.item.path;
      }
      birefnetDlg.hidden = true;
      fetch('/api/d/' + API.dataset() + '/birefnet-mask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      })
        .then(function (r) { return r.json(); })
        .then(function (resp) {
          if (resp.ok && resp.job_id) {
            watchBirefnetJob(resp.job_id);
          } else {
            toast('启动失败');
          }
        })
        .catch(function (e) { toast(String(e.message || e)); });
    });
  }

  function watchBirefnetJob(jobId) {
    var bar = document.getElementById('birefnet-bar');
    var fill = document.getElementById('birefnet-fill');
    var pct = document.getElementById('birefnet-pct');
    var text = document.getElementById('birefnet-text');
    bar.hidden = false;
    var evtSource = new EventSource('/api/jobs/' + jobId + '/events');
    evtSource.onmessage = function (e) {
      var snap = JSON.parse(e.data);
      if (snap.total) {
        var p = Math.round(snap.done / snap.total * 100);
        fill.style.width = p + '%';
        pct.textContent = p + '%';
        text.textContent = snap.done + '/' + snap.total + (snap.failed ? ' 失败' + snap.failed : '');
      }
      if (snap.status === 'done') {
        evtSource.close();
        location.reload();
        return;
      }
      if (snap.status === 'cancelled') {
        evtSource.close();
        setTimeout(function () { bar.hidden = true; }, 3000);
      }
    };
    evtSource.onerror = function () { evtSource.close(); };
  }

  if (typeof DirPicker !== 'undefined') {
    // 蒙版页为单目录工作流: 一次只处理一个目录的蒙版
    if (DirPicker.setSingle) DirPicker.setSingle(true);
    DirPicker.onChange(function () {
      loadImages().catch(function (e) { toast(String(e.message || e)); });
    });
  }

  loadConfig().then(loadImages);
})();
