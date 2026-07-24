/**
 * paint.js —— 蒙版绘制
 */
(function() {
  'use strict';

  var _images = [], _index = 0;
  var _origW = 0, _origH = 0;
  var _alpha = null, _rgbImg = null, _scale = 1;
  var _viewW = 0, _viewH = 0, _dpr = 1;
  var _brushRadius = 80, _brushPct = 3, _defaultAlpha = 255;
  var _tool = 'brush', _isDrawing = false, _paintValue = 0;
  var _rectStart = null, _mouseScreen = null, _modified = false;
  var _preloadCache = {};
  var _renderScheduled = false;
  var _rgbScaledCanvas = null;
  var _compCanvas = null;
  var PRELOAD_RADIUS = 15;

  // ---- 笔刷半径: sqrt(w*h) * 百分比 ----
  function _calcBrush() {
    if (!_origW || !_origH) return;
    _brushRadius = Math.max(2, Math.round(Math.sqrt(_origW * _origH) * _brushPct / 100));
  }

  var canvas = document.getElementById('paint-canvas');
  var ctx = canvas.getContext('2d');
  var wrap = document.getElementById('canvas-wrap');
  var _brushPreview = document.getElementById('brush-preview');

  // ---- 全局错误显示 ----
  function errshow(msg) {
    document.getElementById('info-text').textContent = '\u26a0 ' + msg;
  }
  window.addEventListener('error', function(e) { errshow(e.message); });
  window.addEventListener('unhandledrejection', function(e) { errshow(e.reason); });

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
  async function loadImages() {
    var res = await (await api('/api/paint/images')).json();
    _images = res;
    if (_images.length > 0) loadIndex(0);
    else document.getElementById('info-text').textContent = '没有图片';
  }

  async function loadIndex(idx) {
    if (_images.length === 0) return;
    _index = Math.max(0, Math.min(_images.length - 1, idx));
    var img = _images[_index];

    var cached = _preloadCache[img.path];
    if (cached) {
      _rgbImg = cached.img; _origW = cached.w; _origH = cached.h;
      if (cached.alpha) {
        _alpha = new Uint8Array(cached.alpha);
      } else {
        _alpha = new Uint8Array(_origW * _origH);
        _alpha.fill(_defaultAlpha);
      }
      _modified = false;
      _calcBrush(); _updateBrushUI();
      invalidateScaledCache(); _alphaOrigCanvas = null;
      fitCanvas(); _scheduleRender(); updateNav();
      _preloadNeighbors(_index);
      return;
    }

    var blob = await (await api('/api/paint/image?path=' + encodeURIComponent(img.path))).blob();
    var url = URL.createObjectURL(blob);
    _rgbImg = new Image();
    _rgbImg.onload = function() {
      _origW = _rgbImg.naturalWidth; _origH = _rgbImg.naturalHeight;
      URL.revokeObjectURL(url);
      _alpha = new Uint8Array(_origW * _origH);
      var value = parseInt(document.getElementById('default-alpha').value);
      _defaultAlpha = Number.isNaN(value) ? 255 : value;
      _alpha.fill(_defaultAlpha);
      _modified = false;
      _preloadCache[img.path] = {
        img: _rgbImg,
        w: _origW,
        h: _origH,
        alpha: new Uint8Array(_alpha),
      };
      _calcBrush(); _updateBrushUI();
      invalidateScaledCache(); _alphaOrigCanvas = null;
      fitCanvas(); _scheduleRender(); updateNav();
      loadExistingMask(img.path).then(function() {
        _alphaOrigCanvas = null;
        var cached = _preloadCache[img.path];
        if (cached) cached.alpha = new Uint8Array(_alpha);
        _scheduleRender();
        updateStatusBar();
      });
      _preloadNeighbors(_index);
    };
    _rgbImg.onerror = function() { throw new Error('图片解码失败: ' + img.path); };
    _rgbImg.src = url;
  }

  async function loadExistingMask(path) {
    var resp = await fetch(API.apiPath('/paint/mask') + '?path=' + encodeURIComponent(path));
    if (!resp.ok) return;  // 404 = 无 mask, 正常
    var blob = await resp.blob();
    var url = URL.createObjectURL(blob);
    await new Promise(function(resolve) {
      var mi = new Image();
      mi.onload = function() {
        URL.revokeObjectURL(url);
        if (mi.naturalWidth !== _origW || mi.naturalHeight !== _origH) { resolve(); return; }
        var tc = document.createElement('canvas'); tc.width = _origW; tc.height = _origH;
        var tctx = tc.getContext('2d'); tctx.drawImage(mi, 0, 0);
        var d = tctx.getImageData(0, 0, _origW, _origH).data;
        for (var i = 0; i < _alpha.length; i++) _alpha[i] = d[i * 4];
        resolve();
      };
      mi.onerror = resolve;
      mi.src = url;
    });
  }

  // ---- 预加载 ----
  var _preloadSeq = 0;

  /**
   * 统计当前索引邻域内已进入缓冲区的数量。
   * @returns {{ready: number, want: number, total: number}}
   */
  function bufferStats() {
    var ready = 0;
    var want = 0;
    for (var di = -PRELOAD_RADIUS; di <= PRELOAD_RADIUS; di++) {
      var ni = _index + di;
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
   * 刷新底部状态栏（标注状态、分辨率、缓冲区）。
   */
  function updateStatusBar() {
    var img = _images[_index];
    var parts = [];
    if (img) parts.push(img.has_mask ? '已标注' : '待标注');
    if (_origW) parts.push(_origW + 'x' + _origH);
    var buf = bufferStats();
    parts.push('缓冲 ' + buf.ready + '/' + buf.want + '（缓存 ' + buf.total + '）');
    document.getElementById('info-text').textContent = parts.join('  |  ');
  }

  async function _preloadNeighbors(idx) {
    var seq = ++_preloadSeq;
    for (var di = 1; di <= PRELOAD_RADIUS; di++) {
      if (seq !== _preloadSeq) return;
      var ni = idx + di; if (ni < _images.length) await _preloadOne(_images[ni].path);
      ni = idx - di; if (ni >= 0) await _preloadOne(_images[ni].path);
    }
  }

  async function _preloadOne(path) {
    if (_preloadCache[path]) return;
    var blob = await fetch(API.apiPath('/paint/image') + '?path=' + encodeURIComponent(path)).then(function(r) { return r.blob(); });
    var url = URL.createObjectURL(blob);
    var img = new Image();
    img.onload = function() {
      var entry = { img: img, w: img.naturalWidth, h: img.naturalHeight };
      _preloadCache[path] = entry;
      URL.revokeObjectURL(url);
      updateStatusBar();
      fetch(API.apiPath('/paint/mask') + '?path=' + encodeURIComponent(path)).then(function(r) {
        if (!r.ok) return;
        return r.blob();
      }).then(function(b) {
        if (!b) return;
        var u = URL.createObjectURL(b);
        var mi = new Image();
        mi.onload = function() {
          if (mi.naturalWidth === entry.w && mi.naturalHeight === entry.h) {
            var tc = document.createElement('canvas'); tc.width = entry.w; tc.height = entry.h;
            var tctx = tc.getContext('2d'); tctx.drawImage(mi, 0, 0);
            var d = tctx.getImageData(0, 0, entry.w, entry.h).data;
            var a = new Uint8Array(entry.w * entry.h);
            for (var i = 0; i < a.length; i++) a[i] = d[i * 4];
            entry.alpha = a;
            entry.alphaCanvas = tc;
          }
          URL.revokeObjectURL(u);
          updateStatusBar();
        };
        mi.src = u;
      });
    };
    img.src = url;
  }
  function _updateBrushUI() {
    document.getElementById('brush-label').textContent = _brushPct.toFixed(1) + '% ~' + _brushRadius + 'px';
  }
  function fitCanvas() {
    var cw = wrap.clientWidth, ch = wrap.clientHeight;
    _dpr = window.devicePixelRatio || 1;
    _scale = Math.min(cw / _origW, ch / _origH);
    _viewW = Math.max(1, Math.floor(_origW * _scale));
    _viewH = Math.max(1, Math.floor(_origH * _scale));
    // 缓冲保持原图像素；CSS 缩小显示，交给浏览器缩放（与 <img> 同路径，避免先缩后画发糊）
    if (canvas.width !== _origW || canvas.height !== _origH) {
      canvas.width = _origW;
      canvas.height = _origH;
      invalidateScaledCache();
    }
    canvas.style.width = _viewW + 'px';
    canvas.style.height = _viewH + 'px';
    canvas.style.position = 'absolute';
    canvas.style.left = ((cw - _viewW) / 2) + 'px';
    canvas.style.top = ((ch - _viewH) / 2) + 'px';
    canvas.style.margin = '0';
    canvas.style.imageRendering = 'auto';
  }
  window.addEventListener('resize', function() { if (_rgbImg) { fitCanvas(); _scheduleRender(); } });

  // ---- 渲染 ----
  var _checkerCanvas = null, _alphaOrigCanvas = null;

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
    _checkerCanvas = null;
    _compCanvas = null;
  }

  function _ensureRgbFull() {
    if (_rgbScaledCanvas && _rgbScaledCanvas.width === _origW && _rgbScaledCanvas.height === _origH) return;
    _rgbScaledCanvas = document.createElement('canvas');
    _rgbScaledCanvas.width = _origW;
    _rgbScaledCanvas.height = _origH;
    var rctx = _rgbScaledCanvas.getContext('2d');
    rctx.drawImage(_rgbImg, 0, 0);
  }

  function _ensureChecker() {
    if (_checkerCanvas && _checkerCanvas.width === _origW && _checkerCanvas.height === _origH && _checkerCanvas._darken === _defaultAlpha) return;
    _checkerCanvas = document.createElement('canvas');
    _checkerCanvas.width = _origW;
    _checkerCanvas.height = _origH;
    _checkerCanvas._darken = _defaultAlpha;
    var cc = _checkerCanvas.getContext('2d');
    var dk = _defaultAlpha / 255;
    var c0 = Math.round(204 * dk), c1 = Math.round(153 * dk);
    var cell = 12;
    for (var y = 0; y < _origH; y += cell) {
      for (var x = 0; x < _origW; x += cell) {
        var isOdd = (Math.floor(x / cell) + Math.floor(y / cell)) % 2 === 1;
        cc.fillStyle = 'rgb(' + (isOdd ? c1 : c0) + ',' + (isOdd ? c1 : c0) + ',' + (isOdd ? c1 : c0) + ')';
        cc.fillRect(x, y, cell, cell);
      }
    }
  }

  function _doRender() {
    if (!_rgbImg || !_alpha) return;
    _ensureChecker();
    _ensureRgbFull();

    if (!_alphaOrigCanvas) {
      _alphaOrigCanvas = document.createElement('canvas');
      _alphaOrigCanvas.width = _origW; _alphaOrigCanvas.height = _origH;
      var aoctx = _alphaOrigCanvas.getContext('2d');
      var imgData = aoctx.createImageData(_origW, _origH);
      for (var i = 0; i < _alpha.length; i++) {
        var a = _alpha[i], j = i * 4;
        imgData.data[j] = a; imgData.data[j+1] = a; imgData.data[j+2] = a; imgData.data[j+3] = a;
      }
      aoctx.putImageData(imgData, 0, 0);
    }

    if (!_compCanvas || _compCanvas.width !== _origW || _compCanvas.height !== _origH) {
      _compCanvas = document.createElement('canvas');
      _compCanvas.width = _origW;
      _compCanvas.height = _origH;
    }
    var cctx = _compCanvas.getContext('2d');
    cctx.globalCompositeOperation = 'source-over';
    cctx.clearRect(0, 0, _origW, _origH);
    cctx.drawImage(_rgbScaledCanvas, 0, 0);
    cctx.globalCompositeOperation = 'destination-in';
    cctx.drawImage(_alphaOrigCanvas, 0, 0);

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, _origW, _origH);
    ctx.drawImage(_checkerCanvas, 0, 0);
    ctx.drawImage(_compCanvas, 0, 0);
  }

  // ---- 笔刷预览 ----
  function _updateBrushPreview() {
    if (_tool !== 'brush' || !_mouseScreen) {
      _brushPreview.style.display = 'none'; return;
    }
    var wrapRect = wrap.getBoundingClientRect();
    var canvasRect = canvas.getBoundingClientRect();
    var mx = _mouseScreen[0] - wrapRect.left, my = _mouseScreen[1] - wrapRect.top;
    var r = _brushRadius * (canvasRect.width / Math.max(1, _origW));
    if (r < 2) { _brushPreview.style.display = 'none'; return; }
    _brushPreview.style.display = 'block';
    _brushPreview.style.width = (r * 2) + 'px';
    _brushPreview.style.height = (r * 2) + 'px';
    _brushPreview.style.left = (mx - r) + 'px';
    _brushPreview.style.top = (my - r) + 'px';
  }

  function screenToOrig(sx, sy) {
    var rect = canvas.getBoundingClientRect();
    var x = (sx - rect.left) / Math.max(1e-6, rect.width) * _origW;
    var y = (sy - rect.top) / Math.max(1e-6, rect.height) * _origH;
    return [Math.max(0, Math.min(_origW - 1, Math.floor(x))),
            Math.max(0, Math.min(_origH - 1, Math.floor(y)))];
  }

  function stampDisc(cx, cy, r, val) {
    if (_alphaOrigCanvas) {
      var aoctx = _alphaOrigCanvas.getContext('2d');
      if (val === 0) {
        aoctx.globalCompositeOperation = 'destination-out';
        aoctx.fillStyle = '#fff';
        aoctx.beginPath();
        aoctx.arc(cx, cy, r, 0, Math.PI * 2);
        aoctx.fill();
        aoctx.globalCompositeOperation = 'source-over';
      } else {
        aoctx.globalAlpha = val / 255;
        aoctx.fillStyle = '#fff';
        aoctx.beginPath();
        aoctx.arc(cx, cy, r, 0, Math.PI * 2);
        aoctx.fill();
        aoctx.globalAlpha = 1;
      }
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
    var xa = Math.max(0, Math.min(x0, x1)), xb = Math.min(_origW-1, Math.max(x0, x1));
    var ya = Math.max(0, Math.min(y0, y1)), yb = Math.min(_origH-1, Math.max(y0, y1));
    if (_alphaOrigCanvas) {
      var aoctx = _alphaOrigCanvas.getContext('2d');
      if (val === 0) {
        aoctx.clearRect(xa, ya, xb - xa + 1, yb - ya + 1);
      } else {
        aoctx.globalAlpha = val / 255;
        aoctx.fillStyle = '#fff';
        aoctx.fillRect(xa, ya, xb - xa + 1, yb - ya + 1);
        aoctx.globalAlpha = 1;
      }
    }
  }

  // ---- 鼠标 ----
  canvas.addEventListener('mousedown', function(e) {
    e.preventDefault(); if (!_alpha) return;
    _isDrawing = true;
    var pos = screenToOrig(e.clientX, e.clientY);
    _paintValue = (e.button === 0) ? (document.getElementById('chk-left-transparent').checked ? 0 : 255) : 255;
    if (_tool === 'brush') stampDisc(pos[0], pos[1], _brushRadius, _paintValue);
    _rectStart = pos; _scheduleRender();
  });
  canvas.addEventListener('mousemove', function(e) {
    _mouseScreen = [e.clientX, e.clientY];
    if (!_alpha) return;
    _updateBrushPreview();
    if (!_isDrawing) return;
    var pos = screenToOrig(e.clientX, e.clientY);
    if (_tool === 'brush' && _rectStart) { strokeLine(_rectStart[0], _rectStart[1], pos[0], pos[1], _brushRadius, _paintValue); _rectStart = pos; }
    _scheduleRender();
  });
  canvas.addEventListener('mouseup', function(e) {
    if (!_isDrawing || !_alpha) return;
    if (_tool === 'rect' && _rectStart) { var pos = screenToOrig(e.clientX, e.clientY); fillRect(_rectStart[0], _rectStart[1], pos[0], pos[1], _paintValue); }
    _isDrawing = false; _rectStart = null; _brushPreview.style.display = 'none'; _modified = true; _scheduleRender();
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
    if (!_alpha || !_alphaOrigCanvas) return;
    var img = _images[_index];
    // 全新图片即使没有动笔，也要写出当前默认 alpha mask。
    if (!force && !_modified && img.has_mask) return;
    var fmt = document.getElementById('save-format').value || 'alpha_bw';
    // 从 canvas 回读 alpha 数组（保存时需要）
    var aoctx = _alphaOrigCanvas.getContext('2d');
    var imgData = aoctx.getImageData(0, 0, _origW, _origH);
    for (var i = 0; i < _alpha.length; i++) _alpha[i] = imgData.data[i * 4 + 3];
    return new Promise(function(resolve) {
      _alphaOrigCanvas.toBlob(function(blob) {
        if (!blob) { errshow('toBlob failed'); resolve(); return; }
        fetch(API.apiPath('/paint/upload-mask') + '?path=' + encodeURIComponent(img.path) + '&format=' + fmt, {
          method: 'POST', headers: { 'Content-Type': 'image/png' }, body: blob
        }).then(function(r) { return r.json(); }).then(function(res) {
          _modified = false;
          _images[_index].has_mask = true; updateNav();
          document.getElementById('info-text').textContent = '已保存: ' + res.saved;
          var cached = _preloadCache[img.path];
          if (cached) { cached.alpha = new Uint8Array(_alpha); cached.alphaCanvas = _alphaOrigCanvas; }
          resolve();
        }).catch(function() { errshow('上传失败'); resolve(); });
      }, 'image/png');
    });
  }

  async function goPrev() { if (_index > 0) { await doSave(false); loadIndex(_index - 1); } }
  async function goNext() { if (_index < _images.length - 1) { await doSave(false); loadIndex(_index + 1); } }
  function goSkip() { if (_index < _images.length - 1) loadIndex(_index + 1); }

  // ---- 工具栏 ----
  document.getElementById('btn-prev').addEventListener('click', goPrev);
  document.getElementById('btn-next').addEventListener('click', goNext);
  document.getElementById('btn-skip').addEventListener('click', goSkip);
  var toolSel = document.getElementById('tool-select');
  if (toolSel) toolSel.addEventListener('change', function() { _tool = this.value; });
  document.getElementById('brush-slider').addEventListener('input', function() { _brushPct = parseFloat(this.value); _calcBrush(); _updateBrushUI(); _updateBrushPreview(); });
  document.getElementById('btn-save').addEventListener('click', function() { doSave(true); });
  document.getElementById('btn-clear').addEventListener('click', function() { if (_alpha && _alphaOrigCanvas) { _alpha.fill(0); _alphaOrigCanvas.getContext('2d').clearRect(0, 0, _origW, _origH); _modified = true; _scheduleRender(); } });
  document.getElementById('btn-fill').addEventListener('click', function() { if (_alpha && _alphaOrigCanvas) { _alpha.fill(255); var fc = _alphaOrigCanvas.getContext('2d'); fc.fillStyle = '#fff'; fc.fillRect(0, 0, _origW, _origH); _modified = true; _scheduleRender(); } });
  document.getElementById('btn-default').addEventListener('click', function() {
    var value = parseInt(document.getElementById('default-alpha').value);
    _defaultAlpha = Number.isNaN(value) ? 255 : value;
    if (_alpha && _alphaOrigCanvas) { _alpha.fill(_defaultAlpha); var dc = _alphaOrigCanvas.getContext('2d'); dc.clearRect(0, 0, _origW, _origH); dc.globalAlpha = _defaultAlpha / 255; dc.fillStyle = '#fff'; dc.fillRect(0, 0, _origW, _origH); dc.globalAlpha = 1; _modified = true; _scheduleRender(); }
    saveConfig();
  });
  document.getElementById('default-alpha').addEventListener('change', saveConfig);

  document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); goPrev(); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); goNext(); }
    else if (e.key === 's' || e.key === 'S') { e.preventDefault(); doSave(true); }
    else if (e.key === 'x' || e.key === 'X') { goSkip(); }
  });

  function updateNav() {
    var img = _images[_index];
    var done = 0;
    for (var i = 0; i < _images.length; i++) { if (_images[i].has_mask) done++; }
    document.getElementById('paint-progress').textContent = (_index+1) + '/' + _images.length + ' (已标注' + done + ')';
    document.getElementById('paint-filename').textContent = img ? img.stem : '-';
    updateStatusBar();
  }

  loadConfig().then(loadImages);
})();
