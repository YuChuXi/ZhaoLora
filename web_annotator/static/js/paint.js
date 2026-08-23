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
  // _mouseOrig: 鼠标视口坐标经 screenToOrig 钳位后的原图坐标, 预览与实际绘制共用的单一事实源
  var _rectStart = null, _mouseOrig = null;
  // shift 直线模式: 落笔按下 shift 后, 从落笔点画直线到抬笔点, 终点即 _mouseOrig
  var _lineMode = false, _lineStart = null;
  var _preloadCache = {};
  var _renderScheduled = false;

  // ---- WebGL 渲染层: RGB 纹理 + alpha 纹理 shader 合成, 无 2D 中间画布 ----
  var AlphaGL = (function () {
    var gl = null, canvas = null;
    var texRGB = null, texAlpha = null, fbo = null;
    var W = 0, H = 0;
    var progDraw = null, progShow = null, quadBuf = null;
    var DLOC = {}, SLOC = {};

    var VS = 'attribute vec2 a_pos;varying vec2 v_uv;' +
      'void main(){v_uv=a_pos*0.5+0.5;gl_Position=vec4(a_pos,0.0,1.0);}';
    var FS_DRAW = 'precision mediump float;' +
      'uniform vec2 u_res;uniform vec2 u_center;uniform float u_radius;' +
      'uniform float u_alpha;uniform int u_shape;uniform vec4 u_rect;' +
      'varying vec2 v_uv;' +
      'void main(){' +
      'vec2 px=vec2(v_uv.x,1.0-v_uv.y)*u_res;' +
      'bool inside=false;' +
      'if(u_shape==0){inside=distance(px,u_center)<=u_radius;}' +
      'else{inside=px.x>=u_rect.x&&px.x<=u_rect.z&&px.y>=u_rect.y&&px.y<=u_rect.w;}' +
      'if(inside){gl_FragColor=vec4(1.0,1.0,1.0,u_alpha);}else{discard;}}';
    var FS_SHOW = 'precision mediump float;' +
      'uniform sampler2D u_rgb;uniform sampler2D u_alpha;' +
      'varying vec2 v_uv;' +
      'void main(){' +
      'vec4 rgb=texture2D(u_rgb,v_uv);' +
      'float a=texture2D(u_alpha,v_uv).a;' +
      'gl_FragColor=vec4(rgb.rgb,a);}';

    function _compile(type, src) {
      var s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
        throw new Error('Shader: ' + gl.getShaderInfoLog(s));
      return s;
    }
    function _link(vs, fs) {
      var p = gl.createProgram();
      gl.attachShader(p, vs); gl.attachShader(p, fs); gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS))
        throw new Error('Link: ' + gl.getProgramInfoLog(p));
      return p;
    }
    function _bindQuad(prog) {
      gl.useProgram(prog);
      gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
      var loc = gl.getAttribLocation(prog, 'a_pos');
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    }

    function _initTexParams(t) {
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    }

    /**
     * 初始化 WebGL context, 着色器, RGB/alpha 双纹理, framebuffer. 引用: paint.js 初始化.
     * @param {HTMLCanvasElement} c
     * @param {number} w
     * @param {number} h
     */
    function init(c, w, h) {
      canvas = c; canvas.width = w; canvas.height = h; W = w; H = h;
      gl = canvas.getContext('webgl', { premultipliedAlpha: false, alpha: true });
      if (!gl) throw new Error('WebGL 不可用');
      var vs = _compile(gl.VERTEX_SHADER, VS);
      progDraw = _link(vs, _compile(gl.FRAGMENT_SHADER, FS_DRAW));
      progShow = _link(vs, _compile(gl.FRAGMENT_SHADER, FS_SHOW));
      quadBuf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
      texRGB = gl.createTexture(); _initTexParams(texRGB);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, W, H, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      texAlpha = gl.createTexture(); _initTexParams(texAlpha);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, W, H, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      fbo = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texAlpha, 0);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      DLOC.res = gl.getUniformLocation(progDraw, 'u_res');
      DLOC.center = gl.getUniformLocation(progDraw, 'u_center');
      DLOC.radius = gl.getUniformLocation(progDraw, 'u_radius');
      DLOC.alpha = gl.getUniformLocation(progDraw, 'u_alpha');
      DLOC.shape = gl.getUniformLocation(progDraw, 'u_shape');
      DLOC.rect = gl.getUniformLocation(progDraw, 'u_rect');
      SLOC.rgb = gl.getUniformLocation(progShow, 'u_rgb');
      SLOC.alpha = gl.getUniformLocation(progShow, 'u_alpha');
    }

    /**
     * 调整 alpha 纹理尺寸(图片尺寸变化时; RGB 纹理由 loadRGB 重新上传).
     * 引用: fitCanvas.
     * @param {number} w
     * @param {number} h
     */
    function resize(w, h) {
      if (W === w && H === h) return;
      W = w; H = h; canvas.width = w; canvas.height = h;
      gl.bindTexture(gl.TEXTURE_2D, texAlpha);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, W, H, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texAlpha, 0);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    }

    function _toTex() {
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.viewport(0, 0, W, H);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ZERO);
      _bindQuad(progDraw);
      gl.uniform2f(DLOC.res, W, H);
    }

    function _scissor(cx, cy, r) {
      var sx = Math.max(0, Math.floor(cx - r - 1));
      var sy = Math.max(0, Math.floor(H - cy - r - 1));
      var sw = Math.min(W - sx, Math.ceil(2 * r + 2));
      var sh = Math.min(H - sy, Math.ceil(2 * r + 2));
      gl.enable(gl.SCISSOR_TEST);
      gl.scissor(sx, sy, sw, sh);
    }

    /** 画圆: 笔刷区域内 alpha 覆盖为 val. 引用: stampDisc. */
    function stampDisc(cx, cy, r, val) {
      _toTex();
      _scissor(cx, cy, r);
      gl.uniform2f(DLOC.center, cx, cy);
      gl.uniform1f(DLOC.radius, r);
      gl.uniform1f(DLOC.alpha, val / 255);
      gl.uniform1i(DLOC.shape, 0);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.disable(gl.SCISSOR_TEST);
    }

    /** 画矩形: 矩形区域内 alpha 覆盖为 val. 引用: fillRect. */
    function fillRect(x0, y0, x1, y1, val) {
      _toTex();
      var xa = Math.max(0, Math.min(x0, x1)), xb = Math.min(W - 1, Math.max(x0, x1));
      var ya = Math.max(0, Math.min(y0, y1)), yb = Math.min(H - 1, Math.max(y0, y1));
      var sx = Math.max(0, Math.floor(xa));
      var sy = Math.max(0, Math.floor(H - yb - 1));
      var sw = Math.min(W - sx, Math.ceil(xb - xa + 2));
      var sh = Math.min(H - sy, Math.ceil(yb - ya + 2));
      gl.enable(gl.SCISSOR_TEST);
      gl.scissor(sx, sy, sw, sh);
      gl.uniform1f(DLOC.alpha, val / 255);
      gl.uniform1i(DLOC.shape, 1);
      gl.uniform4f(DLOC.rect, x0, y0, x1, y1);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      gl.disable(gl.SCISSOR_TEST);
    }

    /** 全清(alpha=0). 引用: btn-clear / _initAlphaCanvas. */
    function clear() {
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.viewport(0, 0, W, H);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    }

    /** 全填充 alpha=val. 引用: btn-fill / btn-default / _initAlphaCanvas. */
    function fill(val) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.viewport(0, 0, W, H);
      gl.clearColor(1, 1, 1, val / 255);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    }

    /** 上传 RGB 图片到 RGB 纹理. 引用: _applyCurrent. */
    function loadRGB(image) {
      gl.bindTexture(gl.TEXTURE_2D, texRGB);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    }

    /** 上传蒙版 Image/Canvas 到 alpha 纹理. 引用: _initAlphaCanvas. */
    function loadAlpha(image) {
      gl.bindTexture(gl.TEXTURE_2D, texAlpha);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    }

    /**
     * 读取 alpha 通道数据(y 翻转为 Canvas 坐标). 引用: doSave.
     * @returns {Uint8Array} W*H 的 alpha 值数组.
     */
    function readAlpha() {
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      var pixels = new Uint8Array(W * H * 4);
      gl.readPixels(0, 0, W, H, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      var alpha = new Uint8Array(W * H);
      for (var y = 0; y < H; y++)
        for (var x = 0; x < W; x++)
          alpha[y * W + x] = pixels[(H - 1 - y) * W * 4 + x * 4 + 3];
      return alpha;
    }

    /**
     * 合成 RGB 纹理与 alpha 纹理到 canvas. 引用: _doRender.
     */
    function render() {
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, W, H);
      gl.disable(gl.BLEND);
      _bindQuad(progShow);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texRGB);
      gl.uniform1i(SLOC.rgb, 0);
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, texAlpha);
      gl.uniform1i(SLOC.alpha, 1);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    return {
      init: init, resize: resize, loadRGB: loadRGB, loadAlpha: loadAlpha,
      stampDisc: stampDisc, fillRect: fillRect, clear: clear, fill: fill,
      readAlpha: readAlpha, render: render,
      get canvas() { return canvas; }, get w() { return W; }, get h() { return H; }
    };
  })();
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
  AlphaGL.init(canvas, 1, 1);
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

  // ---- 配置 ----
  async function loadConfig() {
    var cfg = await API.request(API.apiPath('/paint/config'));
    _defaultAlpha = cfg.default_alpha == null ? 255 : cfg.default_alpha;
    document.getElementById('default-alpha').value = _defaultAlpha;
  }
  async function saveConfig() {
    var value = parseInt(document.getElementById('default-alpha').value);
    _defaultAlpha = Number.isNaN(value) ? 255 : value;
    await API.request(API.apiPath('/paint/config'), { method: 'POST',
      body: JSON.stringify({ default_alpha: _defaultAlpha }) });
  }

  // ---- 加载 ----
  async function loadImages() {
    var res = await API.request(API.apiPath('/paint/images') + '?dir=' + encodeURIComponent(API.getDir()));
    _images = res;
    if (_strip) _strip.refresh();
    if (_images.length > 0) loadIndex(0);
    else {
      updateStatusBar();
      toast('没有图片');
    }
  }

  /**
   * 构建蒙版画布 cur.alpha: 有蒙版时继承 cur.mask 的 A 通道(蒙版),
   * 无蒙版时填默认 alpha. 蒙版唯一权威为 cur.alpha 的 A 通道.
   * Callers: loadIndex(缓存命中与加载完成).
   */
  function _initAlphaCanvas(cur) {
    if (AlphaGL.w !== cur.w || AlphaGL.h !== cur.h) AlphaGL.resize(cur.w, cur.h);
    cur.alpha = AlphaGL.canvas;
    if (cur.item.has_mask) {
      AlphaGL.loadAlpha(cur.mask);
    } else {
      var value = parseInt(document.getElementById('default-alpha').value);
      _defaultAlpha = Number.isNaN(value) ? 255 : value;
      AlphaGL.clear();
      if (_defaultAlpha > 0) AlphaGL.fill(_defaultAlpha);
    }
  }

  /**
   * 将加载完成的状态对象整体交换为当前状态, 并一次性刷新状态栏与画布.
   * Callers: loadIndex(缓存命中与加载完成).
   */
  function _applyCurrent(cur) {
    _cur = cur;
    _calcBrush(); _updateBrushUI();
    AlphaGL.loadRGB(cur.rgbImg);
    fitCanvas(); _scheduleRender(); updateStatusBar();
  }

  async function loadIndex(idx) {
    if (_images.length === 0) return;
    idx = Math.max(0, Math.min(_images.length - 1, idx));
    // 切图终止进行中的绘制: 清理橡皮筋/落笔状态并解绑 document 级跟踪,
    // 防止抬笔事件在新图上执行绘制(跨图污染)或残留红线预览
    if (_isDrawing) {
      _isDrawing = false;
      document.removeEventListener('mousemove', _docMouseMove);
      document.removeEventListener('mouseup', _docMouseUp);
    }
    _rectStart = null; _lineMode = false; _lineStart = null; _mouseOrig = null;
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
   * 刷新底部六段状态栏。引用：各切图/加载/保存入口、预加载完成。
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
   * 加载单张图的视觉与蒙版: 视觉采用 AVIF(有损可接受, 已压缩), 蒙版采用无损 PNG(不可有损),
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
    _preloadCache[img.path] = await _loadEntry(img);
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
    AlphaGL.resize(_cur.w, _cur.h);
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

  function _doRender() {
    var cur = _cur;
    if (!cur || !cur.rgbImg || !cur.alpha) return;
    AlphaGL.render();
    _updateLinePreview();
  }

  // SVG 红线预览(叠加在 canvas 上方, 不受 alpha/mask 影响). 引用: _doRender.
  var _lineSvg = null;
  function _updateLinePreview() {
    if (!_lineSvg) {
      _lineSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      _lineSvg.style.position = 'absolute';
      _lineSvg.style.left = '0';
      _lineSvg.style.top = '0';
      _lineSvg.style.width = '100%';
      _lineSvg.style.height = '100%';
      _lineSvg.style.pointerEvents = 'none';
      _lineSvg.style.zIndex = '10';
      wrap.appendChild(_lineSvg);
    }
    if (!_lineMode || !_lineStart || !_mouseOrig || !_cur) {
      _lineSvg.style.display = 'none';
      return;
    }
    _lineSvg.style.display = '';
    // 两端点均从原图坐标映射回视口: 终点 _mouseOrig 由事件上游 screenToOrig 钳位产出,
    // 与笔刷预览同一条数据流, 鼠标移出图片时同步停在边缘
    var wrapRect = wrap.getBoundingClientRect();
    var p1 = _origToScreen(_lineStart[0], _lineStart[1]);
    var p2 = _origToScreen(_mouseOrig[0], _mouseOrig[1]);
    var lw = Math.max(1, _brushRadius * 2 * (canvas.getBoundingClientRect().width / Math.max(1, _cur.w)));
    _lineSvg.setAttribute('viewBox', '0 0 ' + wrapRect.width + ' ' + wrapRect.height);
    _lineSvg.innerHTML =
      '<line x1="' + (p1[0] - wrapRect.left) + '" y1="' + (p1[1] - wrapRect.top) +
      '" x2="' + (p2[0] - wrapRect.left) + '" y2="' + (p2[1] - wrapRect.top) +
      '" stroke="rgba(255,0,0,0.3)" stroke-width="' + lw + '" stroke-linecap="round"/>';
  }

  // 原图坐标 → 画布视口坐标(中心 +0.5 对齐). 引用: _updateLinePreview, _updateBrushPreview.
  function _origToScreen(x, y) {
    var rect = canvas.getBoundingClientRect();
    return [rect.left + (x + 0.5) / _cur.w * rect.width,
            rect.top + (y + 0.5) / _cur.h * rect.height];
  }
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

  // ---- 笔刷预览 ----
  function _updateBrushPreview() {
    if (_tool !== 'brush' || !_mouseOrig || !_cur) {
      _brushPreview.style.display = 'none'; return;
    }
    // _mouseOrig 由事件上游 screenToOrig 钳位产出, 与实际绘制位置一致
    var wrapRect = wrap.getBoundingClientRect();
    var p = _origToScreen(_mouseOrig[0], _mouseOrig[1]);
    var mx = p[0] - wrapRect.left, my = p[1] - wrapRect.top;
    var r = _brushRadius * (canvas.getBoundingClientRect().width / Math.max(1, _cur.w));
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
    if (!_cur || !_cur.alpha) return;
    AlphaGL.stampDisc(cx, cy, r, val);
  }

  function strokeLine(x0, y0, x1, y1, r, val) {
    var dist = Math.max(Math.abs(x1-x0), Math.abs(y1-y0), 1);
    for (var i = 0; i <= dist; i++) {
      var t = i/dist;
      stampDisc(Math.round(x0+(x1-x0)*t), Math.round(y0+(y1-y0)*t), r, val);
    }
  }

  function fillRect(x0, y0, x1, y1, val) {
    if (!_cur || !_cur.alpha) return;
    AlphaGL.fillRect(x0, y0, x1, y1, val);
  }

  /**
   * 读取指定鼠标按键对应的 Alpha 值; 左键 0 默认 0, 右键默认 255, 越界钳位到 0-255.
   * @param {number} button 鼠标按键(0=左, 其它=右)
   * @returns {number}
   */
  function _alphaFor(button) {
    var id = button === 0 ? 'left-alpha' : 'right-alpha';
    var fallback = button === 0 ? 0 : 255;
    var v = parseInt(document.getElementById(id).value, 10);
    return Number.isNaN(v) ? fallback : Math.max(0, Math.min(255, v));
  }

  // ---- 触屏 ----
  canvas.addEventListener('touchstart', function(e) {
    e.preventDefault(); if (!_cur || !_cur.alpha || e.touches.length !== 1) return;
    _isDrawing = true; _isTouch = true;
    var t = e.touches[0];
    var pos = screenToOrig(t.clientX, t.clientY);
    _paintValue = _alphaFor(0);
    if (_tool === 'brush') stampDisc(pos[0], pos[1], _brushRadius, _paintValue);
    _mouseOrig = pos;
    _rectStart = pos; _scheduleRender();
  }, { passive: false });
  canvas.addEventListener('touchmove', function(e) {
    e.preventDefault(); if (!_cur || !_cur.alpha || !_isDrawing) return;
    var t = e.touches[0];
    _mouseOrig = screenToOrig(t.clientX, t.clientY);
    _updateBrushPreview();
    var pos = _mouseOrig;
    if (_tool === 'brush' && _rectStart) { strokeLine(_rectStart[0], _rectStart[1], pos[0], pos[1], _brushRadius, _paintValue); _rectStart = pos; }
    _scheduleRender();
  }, { passive: false });
  canvas.addEventListener('touchend', function(e) {
    e.preventDefault(); if (!_isTouch || !_isDrawing || !_cur || !_cur.alpha) return;
    // 终点沿用 _mouseOrig(最后 touchmove 的钳位坐标), 与实际预览一致
    if (_tool === 'rect' && _rectStart && _mouseOrig) fillRect(_rectStart[0], _rectStart[1], _mouseOrig[0], _mouseOrig[1], _paintValue);
    _isDrawing = false; _isTouch = false; _rectStart = null; _mouseOrig = null; _brushPreview.style.display = 'none'; _cur.modified = true; _scheduleRender();
  }, { passive: false });
  canvas.addEventListener('touchcancel', function() { _isDrawing = false; _isTouch = false; _rectStart = null; _mouseOrig = null; _brushPreview.style.display = 'none'; });
  // 防止触屏时触发对应的 mouse 事件
  canvas.addEventListener('mousedown', function(e) { if (_isTouch) return; e.preventDefault(); if (!_cur || !_cur.alpha) return;
    _isDrawing = true;
    _mouseOrig = screenToOrig(e.clientX, e.clientY);
    var pos = _mouseOrig;
    _paintValue = _alphaFor(e.button);
    if (_tool === 'brush' && e.shiftKey) {
      // shift 直线: 只记录起点, 橡皮筋预览, 抬笔时一次 strokeLine 画落笔到抬笔的直线
      _lineMode = true; _lineStart = pos;
      _updateLinePreview();
      document.addEventListener('mousemove', _docMouseMove);
      document.addEventListener('mouseup', _docMouseUp);
      return;
    }
    if (_tool === 'brush') stampDisc(pos[0], pos[1], _brushRadius, _paintValue);
    _rectStart = pos; _scheduleRender();
    // 按住鼠标后用 document 事件跟踪, 离开画布仍可绘制
    document.addEventListener('mousemove', _docMouseMove);
    document.addEventListener('mouseup', _docMouseUp);
  });
  // 画布内 mouse 事件: 仅负责未按下时的 hover 预览; 按下后由 _docMouseMove 统一接管
  canvas.addEventListener('mousemove', function(e) {
    if (_isDrawing) return;
    _mouseOrig = screenToOrig(e.clientX, e.clientY);
    if (!_cur || !_cur.alpha) return;
    _updateBrushPreview();
  });
  // 移出画布: 仅未按下时隐藏预览; 按下中(按住绘制/直线)由 _docMouseMove 以钳位坐标
  // 继续更新预览, 与红线同步停在图片边缘, 不破坏数据流
  canvas.addEventListener('mouseleave', function() { if (!_isDrawing) _brushPreview.style.display = 'none'; });
  // 文档级跟踪: 按住鼠标后离开画布仍可绘制, 坐标统一经 screenToOrig 钳位到图片边缘
  function _docMouseMove(e) {
    _mouseOrig = screenToOrig(e.clientX, e.clientY);
    if (!_isDrawing || !_cur || !_cur.alpha) return;
    _updateBrushPreview();
    // 直线模式只改预览不落笔画布: 同步刷新红线与笔刷预览, 不经 rAF 避免两者错帧
    if (_lineMode) { _updateLinePreview(); return; }
    var pos = _mouseOrig;
    if (_tool === 'brush' && _rectStart) { strokeLine(_rectStart[0], _rectStart[1], pos[0], pos[1], _brushRadius, _paintValue); _rectStart = pos; }
    _scheduleRender();
  }
  function _docMouseUp() {
    if (!_isDrawing || !_cur || !_cur.alpha || !_mouseOrig) return;
    // 终点统一沿用 _mouseOrig(最后 mousemove 的钳位坐标), 与红线/笔刷预览同一条数据流
    if (_lineMode) {
      strokeLine(_lineStart[0], _lineStart[1], _mouseOrig[0], _mouseOrig[1], _brushRadius, _paintValue);
    } else if (_tool === 'rect' && _rectStart) {
      fillRect(_rectStart[0], _rectStart[1], _mouseOrig[0], _mouseOrig[1], _paintValue);
    }
    _isDrawing = false; _rectStart = null; _lineMode = false; _lineStart = null; _mouseOrig = null;
    _brushPreview.style.display = 'none'; _cur.modified = true; _scheduleRender();
    document.removeEventListener('mousemove', _docMouseMove);
    document.removeEventListener('mouseup', _docMouseUp);
  }
  canvas.addEventListener('contextmenu', function(e) { e.preventDefault(); });
  canvas.addEventListener('wheel', function(e) {
    e.preventDefault();
    var d = e.deltaY < 0 ? 1 : -1;
    _brushPct = Math.max(0.5, Math.min(20, _brushPct + d * Math.max(0.1, _brushPct * 0.15)));
    _calcBrush(); _updateBrushUI(); _updateBrushPreview();
    if (_lineMode) _updateLinePreview(); // 直线模式下同步刷新红线线宽
  });

  // ---- 保存 ----
  async function doSave(force) {
    var cur = _cur;
    if (!cur || !cur.alpha) return;
    var img = cur.item;
    // 全新图片即使没有动笔，也要写出当前默认 alpha mask。
    if (!force && !cur.modified && img.has_mask) return;
    // 从 WebGL 纹理读取 alpha(唯一权威), 构造 RGB=0 的 PNG 上传(后端仅取 A)
    var alphaArr = AlphaGL.readAlpha();
    var tmpCanvas = document.createElement('canvas');
    tmpCanvas.width = cur.w; tmpCanvas.height = cur.h;
    var tctx = tmpCanvas.getContext('2d');
    var imgData = tctx.createImageData(cur.w, cur.h);
    for (var i = 0; i < alphaArr.length; i++) {
      var j = i * 4;
      imgData.data[j] = 0; imgData.data[j + 1] = 0; imgData.data[j + 2] = 0;
      imgData.data[j + 3] = alphaArr[i];
    }
    tctx.putImageData(imgData, 0, 0);
    return new Promise(function(resolve) {
      tmpCanvas.toBlob(function(blob) {
        if (!blob) { errshow('toBlob failed'); resolve(); return; }
        fetch(API.apiPath('/paint/upload-mask') + '?path=' + encodeURIComponent(img.path), {
          method: 'POST', headers: { 'Content-Type': 'image/png' }, body: blob
        }).then(function(r) { return r.json(); }).then(function(res) {
          cur.modified = false;
          img.has_mask = true; updateStatusBar();
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
  if (toolSel) toolSel.addEventListener('change', function() { _tool = this.value; _brushPreview.style.display = 'none'; });
  document.getElementById('brush-slider').addEventListener('input', function() { _brushPct = parseFloat(this.value); _calcBrush(); _updateBrushUI(); _updateBrushPreview(); if (_lineMode) _updateLinePreview(); });
  document.getElementById('bg-style').addEventListener('change', function() {
    _bgStyle = this.value;
    _applyBackdrop();
  });
  document.getElementById('bg-color').addEventListener('change', function() { _bgColor = this.value; _applyBackdrop(); });
  document.getElementById('btn-save').addEventListener('click', function() { doSave(true); });
  document.getElementById('btn-clear').addEventListener('click', function() { if (_cur && _cur.alpha) { AlphaGL.clear(); _cur.modified = true; _scheduleRender(); } });
  document.getElementById('btn-fill').addEventListener('click', function() { if (_cur && _cur.alpha) { AlphaGL.fill(255); _cur.modified = true; _scheduleRender(); } });
  document.getElementById('btn-default').addEventListener('click', function() {
    var value = parseInt(document.getElementById('default-alpha').value);
    _defaultAlpha = Number.isNaN(value) ? 255 : value;
    if (_cur && _cur.alpha) { AlphaGL.clear(); if (_defaultAlpha > 0) AlphaGL.fill(_defaultAlpha); _cur.modified = true; _scheduleRender(); }
    saveConfig();
  });
  document.getElementById('default-alpha').addEventListener('change', function() { saveConfig(); _applyBackdrop(); });
  // 快捷交换左右键 Alpha
  document.getElementById('btn-swap-alpha').addEventListener('click', function() {
    var left = document.getElementById('left-alpha');
    var right = document.getElementById('right-alpha');
    var tmp = left.value;
    left.value = right.value;
    right.value = tmp;
  });

  document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === 'ArrowLeft' || e.key === 'a' || e.key === 'A') { e.preventDefault(); goPrev(); }
    else if (e.key === 'ArrowRight' || e.key === 'd' || e.key === 'D') { e.preventDefault(); goNext(); }
    else if (e.key === 's' || e.key === 'S') { e.preventDefault(); doSave(true); }
    else if (e.key === 'x' || e.key === 'X') { goSkip(); }
  });

  // 蒙版生成 (BiRefNet / SAM3)
  var birefnetBtn = document.getElementById('btn-birefnet-mask');
  var birefnetDlg = document.getElementById('birefnet-dlg');
  if (birefnetBtn && birefnetDlg) {
    var maskModeEl = document.getElementById('mask-mode');
    var birefnetParamsEl = document.getElementById('birefnet-params');
    var sam3ParamsEl = document.getElementById('sam3-params');
    // 按生成方式切换参数区: BiRefNet 显示扩展/收缩/值域, SAM3 显示蒙板表达式
    var syncMaskMode = function () {
      var isSam3 = maskModeEl.value === 'sam3';
      birefnetParamsEl.hidden = isSam3;
      sam3ParamsEl.hidden = !isSam3;
    };
    maskModeEl.addEventListener('change', syncMaskMode);
    syncMaskMode();
    birefnetBtn.addEventListener('click', function () { birefnetDlg.hidden = false; });
    document.getElementById('birefnet-cancel').addEventListener('click', function () { birefnetDlg.hidden = true; });
    // 「跳过已有蒙版」仅批量模式可用: 单图模式禁用(灰显由 CSS 派生), disabled 只能 JS 设
    var syncSkipEnabled = function () {
      document.getElementById('birefnet-skip').disabled = !document.getElementById('birefnet-batch').checked;
    };
    document.getElementById('birefnet-batch').addEventListener('change', syncSkipEnabled);
    syncSkipEnabled();
    document.getElementById('birefnet-start').addEventListener('click', function () {
      var mode = maskModeEl.value;
      var batch = document.getElementById('birefnet-batch').checked;
      // 单图模式无「跳过已有蒙版」概念, 始终重新生成; 仅批量模式应用该选项
      var skipExisting = batch ? document.getElementById('birefnet-skip').checked : false;
      var body = { dir: API.getDir(), mode: mode, skip_existing: skipExisting };
      if (mode === 'sam3') {
        var expression = document.getElementById('sam3-expression').value.trim();
        if (!expression) { toast('请输入蒙板表达式'); return; }
        body.expression = expression;
      } else {
        body.mask_expand = parseInt(document.getElementById('birefnet-expand').value) || 32;
        body.mask_shrink = parseInt(document.getElementById('birefnet-shrink').value) || 0;
        body.mask_min = parseInt(document.getElementById('birefnet-min').value) || 96;
        body.mask_max = parseInt(document.getElementById('birefnet-max').value) || 255;
      }
      // 默认单图: 仅重新生成当前图片; 勾选「批量」处理整个目录
      if (!batch) {
        if (!_cur || !_cur.item) { toast('当前没有图片'); return; }
        body.path = _cur.item.path;
      }
      birefnetDlg.hidden = true;
      API.request(API.apiPath('/mask-generate'), { method: 'POST', body: JSON.stringify(body) })
        .then(function (resp) {
          if (resp.ok && resp.job_id) {
            watchBirefnetJob(resp.job_id, batch);
          } else {
            toast('启动失败');
          }
        })
        .catch(function (e) { toast(String(e.message || e)); });
    });
  }

  /**
   * 订阅蒙版生成任务进度. batch 为真表示批量模式(整目录), 完成后使全部图片的
   * 蒙板缓存失效; 单图模式仅当前图变化, 由 reloadCurrentMask 就地更新.
   * Callers: birefnet-start 点击处理.
   */
  function watchBirefnetJob(jobId, batch) {
    var bar = document.getElementById('birefnet-bar');
    var fill = document.getElementById('birefnet-fill');
    var pct = document.getElementById('birefnet-pct');
    var text = document.getElementById('birefnet-text');
    bar.hidden = false;
    // 复用 API.watchJob 统一 SSE 订阅(断线重连/终态自动 close), 这里只做 UI 与后续动作
    API.watchJob(jobId, function (snap) {
      if (snap.total) {
        var p = Math.round(snap.done / snap.total * 100);
        fill.style.width = p + '%';
        pct.textContent = p + '%';
        text.textContent = snap.done + '/' + snap.total + (snap.failed ? ' 失败' + snap.failed : '');
      }
      if (snap.status === 'done') {
        fill.style.width = '100%';
        pct.textContent = '100%';
        text.textContent = '完成 ' + snap.done + '/' + snap.total;
        setTimeout(function () { bar.hidden = true; }, 1500);
        if (batch) {
          // 批量: 整目录蒙板可能都已更新, 清空缓存并重拉列表刷新状态;
          // 保持当前图片位置, 切换图片时重新加载新蒙板
          _preloadCache = {};
          refreshAfterMaskDone().catch(function (e) { toast(String(e.message || e)); });
        } else {
          // 单图: 仅当前图蒙板更新, 标记 has_mask 后只重载当前图蒙板, 不重拉列表
          if (_cur && _cur.item) {
            _cur.item.has_mask = true;
            reloadCurrentMask().catch(function (e) { toast(String(e.message || e)); });
          }
        }
        return;
      }
      if (snap.status === 'error') {
        text.textContent = '失败: ' + (snap.message || '未知错误');
        setTimeout(function () { bar.hidden = true; }, 5000);
        return;
      }
      if (snap.status === 'cancelled') {
        setTimeout(function () { bar.hidden = true; }, 3000);
      }
    });
  }

  /**
   * 批量蒙版生成完成后: 重拉图片列表刷新各图蒙版状态, 并只重新加载当前图片的蒙板.
   * 不重载整张图片, 保持当前图片位置. 单图模式不走此函数(仅 reloadCurrentMask).
   * Callers: watchBirefnetJob(批量任务完成).
   */
  async function refreshAfterMaskDone() {
    var curPath = _cur && _cur.item ? _cur.item.path : null;
    var res = await API.request(API.apiPath('/paint/images') + '?dir=' + encodeURIComponent(API.getDir()));
    _images = res;
    if (_strip) _strip.refresh();
    if (!curPath) { updateStatusBar(); return; }
    // 按路径定位当前图片(列表顺序可能变化), 更新 _cur 指向后只重载其蒙板
    for (var i = 0; i < _images.length; i++) {
      if (_images[i].path === curPath) {
        _cur.index = i;
        _cur.item = _images[i];
        break;
      }
    }
    await reloadCurrentMask();
  }

  /**
   * 只重新加载当前图片的蒙板并重绘, 不重载整张图片.
   * Callers: watchBirefnetJob(单图完成)、refreshAfterMaskDone(批量完成).
   */
  async function reloadCurrentMask() {
    if (!_cur || !_cur.item || !_cur.item.has_mask) return;
    var blob = await _fetchBlob('/paint/mask', _cur.item.path);
    var el = await _decodeImage(blob);
    var mCanvas = document.createElement('canvas');
    mCanvas.width = _cur.w; mCanvas.height = _cur.h;
    mCanvas.getContext('2d').drawImage(el, 0, 0);
    _cur.mask = mCanvas;
    // 同步预加载缓存, 切回该图时直接命中新蒙板
    _preloadCache[_cur.item.path] = { img: _cur.rgbImg, mask: mCanvas, w: _cur.w, h: _cur.h };
    _initAlphaCanvas(_cur);
    _scheduleRender();
    updateStatusBar();
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
