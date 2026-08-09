/**
 * video_extract.js —— 视频抽帧页。
 *
 * 交互参照 YSM/app.py 的桌面工具在浏览器中复刻：
 *   - 主预览区显示当前帧；左键存帧、右键删帧、滚轮调帧
 *   - 底部缩略图条按采样间隔提供快速跳转
 *   - 帧类型柱状图(Canvas)展示每帧编码类型(I/P/B)与包大小，点击柱状跳帧
 *   - 帧类型走后端长任务(ffprobe 分析 + SSE 进度)，完成后回读并重绘柱状图
 *   - 键盘：A/D 切换视频、Q/E 单帧、←/→ 十帧、S/空格 存帧、X/Delete 删帧
 *
 * 引用：templates/video_extract.html。
 */
var VideoExtract = (function () {
  'use strict';

  var THUMB_COUNT = 60; // 缩略图条采样目标数量
  var FRAME_COLORS = { I: '#ff6b6b', P: '#f5c542', B: '#4a9eff' }; // 红I/黄P/蓝B

  var state = {
    path: '',      // 相对数据集根的视频路径
    total: 0,      // 总帧数
    idx: 0,        // 当前帧索引
    width: 0,
    height: 0,
    duration: 0,
    types: {},     // 帧索引 -> {type: I/P/B, size: 包大小}
    saved: [],     // 已保存帧索引(升序)
    videoList: [], // 全部视频列表(用于 A/D 切换)
    vidIdx: -1,    // 当前视频在列表中的位置
    jobWatch: null,
    analyzing: false,
  };

  var _stripItems = []; // 缩略图条 DOM 集合
  var _stripIo = null;  // 缩略图懒加载观察器

  function $(id) { return document.getElementById(id); }

  /** 构造帧预览 URL。引用: goto。 */
  function frameUrl(idx) {
    return API.apiPath('/video/frame')
      + '?path=' + encodeURIComponent(state.path)
      + '&index=' + idx;
  }

  /** 构造封面 URL(用于视频切换时顶部信息)。引用: 无(仅信息)。 */
  function coverUrl() {
    return API.apiPath('/video/cover') + '?path=' + encodeURIComponent(state.path);
  }

  /** 读取 path 查询参数并解码。引用: init。 */
  function readQueryPath() {
    var p = new URLSearchParams(location.search).get('path');
    return p ? decodeURIComponent(p) : '';
  }

  /**
   * 更新状态栏与工具栏信息。引用: goto / loadDetail / refreshSaved。
   */
  function paintStatus() {
    var name = state.path ? state.path.split('/').pop() : '';
    var seg = $('ve-video');
    if (seg) {
      seg.textContent = (state.vidIdx >= 0 ? '[' + (state.vidIdx + 1) + '/' + state.videoList.length + '] ' : '')
        + name
        + ' · ' + state.total + ' 帧'
        + (state.total ? ' · ' + fmtTime((state.idx + 1) / (state.total / (state.duration || 1))) : '');
    }
    var frame = $('ve-frame');
    if (frame) {
      var ft = state.types[state.idx];
      var tInfo = ft ? (' · ' + ft.type + '帧 '
        + (ft.size >= 1024 ? (ft.size / 1024).toFixed(1) + 'KB' : ft.size + 'B')) : '';
      frame.textContent = (state.idx + 1) + '/' + state.total + tInfo;
    }
    var saved = $('ve-saved-seg');
    if (saved) saved.textContent = '已保存: ' + state.saved.length;
    var size = $('ve-size');
    if (size) size.textContent = state.width + '×' + state.height;
  }

  /** 秒数格式化为 mm:ss。引用: paintStatus。 */
  function fmtTime(sec) {
    if (!(sec > 0) || !isFinite(sec)) return '';
    var s = Math.floor(sec % 60);
    var m = Math.floor(sec / 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  /**
   * 跳转到指定帧: 钳位、加载预览、同步缩略条与曲线光标。
   * @param {number} idx
   * 引用: init / 各导航回调 / 键盘 / 滚轮。
   */
  function goto(idx) {
    if (!state.total) return;
    idx = Math.max(0, Math.min(state.total - 1, idx));
    if (idx === state.idx) {
      paintStatus();
      return;
    }
    // 若目标帧已缓存为当前帧图像的一部分则直接使用; 否则即时请求
    loadFrame(idx);
  }

  /** 请求并显示指定帧。引用: goto。 */
  function loadFrame(idx) {
    var stage = $('ve-stage');
    var img = $('ve-img');
    if (stage) stage.classList.add('loading');
    var url = frameUrl(idx);
    img.onload = function () {
      img.setAttribute('data-idx', String(idx));
      state.idx = idx;
      if (stage) stage.classList.remove('loading');
      paintStatus();
      drawChart();
      syncStrip();
    };
    img.onerror = function () {
      if (stage) stage.classList.remove('loading');
      toast('帧 ' + idx + ' 读取失败');
    };
    img.src = url;
  }

  /** 切换视频(上下一个)，重新加载详情并回到第 0 帧。引用: prevVideo/nextVideo。 */
  function switchVideo(offset) {
    if (!state.videoList.length) return;
    var i = state.vidIdx + offset;
    if (i < 0 || i >= state.videoList.length) {
      toast('已是第一个/最后一个视频');
      return;
    }
    var v = state.videoList[i];
    setPath(v.path);
  }

  /**
   * 切换到一个新视频路径: 重置状态并加载详情。
   * @param {string} p
   * 引用: init / switchVideo。
   */
  async function setPath(p) {
    state.path = p;
    state.idx = 0;
    state.total = 0;
    state.types = {};
    state.saved = [];
    stopAnalysis();
    var img = $('ve-img');
    if (img) img.removeAttribute('src');
    $('ve-strip').innerHTML = '';
    _stripItems = [];
    paintStatus();
    await loadDetail();
    // 直接加载第 0 帧: goto(0) 会因 idx===state.idx 提前返回而跳过首帧
    loadFrame(0);
  }

  /**
   * 加载视频详情(宽高/时长/总帧数/帧类型/已保存)。
   * 引用: setPath / analysisDone。
   */
  async function loadDetail() {
    var data = await API.request(API.apiPath('/video/detail')
      + '?path=' + encodeURIComponent(state.path));
    state.total = data.total || 0;
    state.width = data.width || 0;
    state.height = data.height || 0;
    state.duration = data.duration || 0;
    state.types = data.types || {};
    state.saved = data.saved || [];
    if (!state.total) {
      toast('视频无法读取或总帧数为 0');
      return;
    }
    buildStrip();
  }

  /**
   * 保存当前帧 PNG 到 main 目录。引用: 存帧按钮/左键/S/空格。
   */
  async function saveCurrent() {
    var res = await API.request(API.apiPath('/video/frame/save'), {
      method: 'POST',
      body: JSON.stringify({ path: state.path, index: state.idx }),
    });
    state.saved = res.saved || [];
    paintStatus();
    drawChart();
    syncStrip();
    toast('已存帧 ' + state.idx);
  }

  /**
   * 删除当前帧已保存的 PNG。引用: 删帧按钮/右键/X/Delete。
   */
  async function deleteCurrent() {
    var res = await API.request(API.apiPath('/video/frame/delete'), {
      method: 'POST',
      body: JSON.stringify({ path: state.path, index: state.idx }),
    });
    state.saved = res.saved || [];
    paintStatus();
    drawChart();
    syncStrip();
    toast('已删帧 ' + state.idx);
  }

  // ---- 缩略图条 ----

  /**
   * 按采样间隔构建缩略图条(目标 THUMB_COUNT 个)。
   * 引用: loadDetail。
   */
  function buildStrip() {
    var strip = $('ve-strip');
    strip.innerHTML = '';
    _stripItems = [];
    if (!state.total) return;
    var interval = Math.max(1, Math.round(state.total / THUMB_COUNT));
    var indices = [];
    for (var i = 0; i < state.total; i += interval) indices.push(i);
    if (indices[indices.length - 1] !== state.total - 1) indices.push(state.total - 1);

    for (var k = 0; k < indices.length; k++) {
      (function (idx) {
        var box = document.createElement('div');
        box.className = 've-thumb';
        box.dataset.idx = String(idx);
        var img = document.createElement('img');
        img.loading = 'lazy';
        img.dataset.src = frameUrl(idx);
        box.appendChild(img);
        var tag = document.createElement('span');
        tag.className = 'tag';
        tag.textContent = String(idx);
        box.appendChild(tag);
        box.addEventListener('click', function () { goto(idx); });
        strip.appendChild(box);
        _stripItems.push({ idx: idx, box: box, img: img });
      })(indices[k]);
    }
    observeStrip(strip);
    syncStrip();
  }

  /** 为缩略图条绑定懒加载观察器。引用: buildStrip。 */
  function observeStrip(container) {
    if (_stripIo) _stripIo.disconnect();
    _stripIo = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (!entries[i].isIntersecting) continue;
        var img = entries[i].target;
        var url = img.dataset.src;
        if (!url) continue;
        img.src = url;
        delete img.dataset.src;
        _stripIo.unobserve(img);
      }
    }, { root: container, rootMargin: '300px 0px', threshold: 0 });
    var imgs = container.querySelectorAll('img[data-src]');
    for (var j = 0; j < imgs.length; j++) _stripIo.observe(imgs[j]);
  }

  /** 同步缩略图条高亮: 当前帧与已保存帧。引用: goto / saveCurrent / deleteCurrent。 */
  function syncStrip() {
    var savedSet = {};
    for (var i = 0; i < state.saved.length; i++) savedSet[state.saved[i]] = true;
    for (var k = 0; k < _stripItems.length; k++) {
      var it = _stripItems[k];
      it.box.classList.toggle('active', it.idx === state.idx);
      it.box.classList.toggle('saved', !!savedSet[it.idx]);
    }
    // 将当前帧缩略图滚入可视区
    var cur = null;
    for (var m = 0; m < _stripItems.length; m++) {
      if (_stripItems[m].idx === state.idx) { cur = _stripItems[m]; break; }
    }
    if (cur && cur.box.scrollIntoView) {
      cur.box.scrollIntoView({ block: 'nearest', inline: 'center' });
    }
  }

  // ---- 帧类型柱状图 ----

  /**
   * 绘制帧类型柱状图: 每帧一根柱(红I/黄P/蓝B), 高度按包大小归一化到最大包;
   * 叠加当前帧竖线(白)与已保存帧底部刻度(绿)。
   * 大量帧时按像素列分桶, 每列取该帧(最大包)绘制以避免卡顿。
   * 引用: goto / loadFrame / analysisDone / saveCurrent / deleteCurrent。
   */
  function drawChart() {
    var canvas = $('ve-chart');
    if (!canvas) return;
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    var pad = 2;
    var plotW = w - pad * 2;
    var plotH = h - pad * 2;
    var bottom = h - pad;
    var total = state.total || 1;
    var xOf = function (idx) { return pad + (idx / total) * plotW; };

    var keys = Object.keys(state.types).map(Number).sort(function (a, b) { return a - b; });
    if (keys.length) {
      // 最大包大小用于高度归一化
      var maxSize = 0;
      for (var i = 0; i < keys.length; i++) {
        var s = state.types[keys[i]].size;
        if (s > maxSize) maxSize = s;
      }
      if (maxSize <= 0) maxSize = 1;

      if (keys.length <= plotW) {
        // 帧数少: 逐帧绘制
        var barW = Math.max(1, plotW / total);
        for (var j = 0; j < keys.length; j++) {
          var idx = keys[j];
          var ft = state.types[idx];
          var hgt = (ft.size / maxSize) * plotH;
          ctx.fillStyle = FRAME_COLORS[ft.type] || '#888';
          ctx.fillRect(xOf(idx), bottom - hgt, barW, hgt);
        }
      } else {
        // 帧数多: 按像素列分桶, 每列取包最大的帧
        var colMax = {};   // 列 -> 最大 size
        var colType = {};  // 列 -> 对应帧类型
        for (var k = 0; k < keys.length; k++) {
          var c = Math.floor(xOf(keys[k]));
          var sz = state.types[keys[k]].size;
          if (colMax[c] == null || sz > colMax[c]) {
            colMax[c] = sz;
            colType[c] = state.types[keys[k]].type;
          }
        }
        for (var col in colMax) {
          var bx = Number(col);
          var bh = (colMax[col] / maxSize) * plotH;
          ctx.fillStyle = FRAME_COLORS[colType[col]] || '#888';
          ctx.fillRect(bx, bottom - bh, 1, bh);
        }
      }
    }

    // 当前帧竖线(白)
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 1;
    ctx.beginPath();
    var cx = xOf(state.idx);
    ctx.moveTo(cx, 0);
    ctx.lineTo(cx, h);
    ctx.stroke();

    // 已保存帧底部刻度(绿)
    if (state.saved.length) {
      ctx.fillStyle = '#3ecf8e';
      for (var s = 0; s < state.saved.length; s++) {
        var sx = xOf(state.saved[s]);
        ctx.fillRect(sx - 1, bottom - 4, 2, 4);
      }
    }
  }

  /** 将画布坐标换算为帧索引并跳转。引用: 柱状图点击。 */
  function chartClick(e) {
    if (!state.total) return;
    var canvas = $('ve-chart');
    var r = canvas.getBoundingClientRect();
    var x = e.clientX - r.left - 2;
    var plotW = r.width - 4;
    var idx = Math.round((x / (plotW || 1)) * state.total);
    goto(Math.max(0, Math.min(state.total - 1, idx)));
  }

  // ---- 帧类型分析 ----

  /** 停止进行中的分析 SSE 订阅。引用: setPath / stopAnalysis。 */
  function stopAnalysis() {
    if (state.jobWatch) {
      state.jobWatch.close();
      state.jobWatch = null;
    }
    state.analyzing = false;
    var btn = $('ve-analysis');
    if (btn) btn.disabled = false;
  }

  /**
   * 启动/停止位流帧类型分析长任务。
   * 引用: 分析按钮。
   */
  async function toggleAnalysis() {
    if (state.analyzing) {
      stopAnalysis();
      setProgressText('分析: 已停止');
      return;
    }
    if (!state.total) return;
    var data = await API.request(API.apiPath('/video/analysis/start')
      + '?path=' + encodeURIComponent(state.path), { method: 'POST' });
    state.analyzing = true;
    var btn = $('ve-analysis');
    if (btn) btn.textContent = '停止';
    var prog = $('ve-progress-seg');
    if (prog) prog.textContent = '分析: 提交中…';
    state.jobWatch = API.watchJob(data.job_id, function (s) {
      if (prog) {
        prog.textContent = '分析: ' + (s.done || 0) + '/' + (s.total || 0)
          + (s.message ? ' · ' + s.message : '');
      }
      if (s.status === 'done') {
        stopAnalysis();
        setProgressText('分析: 完成');
        analysisDone();
      } else if (s.status === 'error') {
        stopAnalysis();
        setProgressText('分析: 失败');
        toast('帧类型分析失败: ' + (s.message || ''));
      } else if (s.status === 'cancelled') {
        stopAnalysis();
        setProgressText('分析: 已取消');
      }
    });
  }

  /** 更新分析进度文本。引用: toggleAnalysis / stopAnalysis。 */
  function setProgressText(t) {
    var prog = $('ve-progress-seg');
    if (prog) prog.textContent = t;
    var btn = $('ve-analysis');
    if (btn) btn.textContent = '帧类型';
  }

  /** 分析完成后回读帧类型并重绘柱状图。引用: toggleAnalysis。 */
  async function analysisDone() {
    await loadDetail();
    drawChart();
    syncStrip();
    paintStatus();
  }

  // ---- 事件绑定 ----

  /** 绑定工具栏按钮与主预览交互。引用: init。 */
  function bindToolbar() {
    var el = document.getElementById('ve-toolbar');
    if (!el) return;
    el.addEventListener('click', function (e) {
      var id = e.target && e.target.id;
      if (id === 've-prev-vid') switchVideo(-1);
      else if (id === 've-next-vid') switchVideo(1);
      else if (id === 've-prev-10') goto(state.idx - 10);
      else if (id === 've-prev-1') goto(state.idx - 1);
      else if (id === 've-next-1') goto(state.idx + 1);
      else if (id === 've-next-10') goto(state.idx + 10);
      else if (id === 've-save') {
        saveCurrent().catch(function (err) { toast('存帧失败: ' + (err.message || err)); });
      } else if (id === 've-del') {
        deleteCurrent().catch(function (err) { toast('删帧失败: ' + (err.message || err)); });
      } else if (id === 've-analysis') {
        toggleAnalysis().catch(function (err) { toast('帧类型分析失败: ' + (err.message || err)); });
      }
    });
  }

  /** 绑定帧类型柱状图点击。引用: init。 */
  function bindChart() {
    var canvas = $('ve-chart');
    if (canvas) canvas.addEventListener('click', chartClick);
  }

  /** 绑定主预览交互: 左键存帧、右键删帧、滚轮调帧。引用: init。 */
  function bindStage() {
    var stage = $('ve-stage');
    if (!stage) return;
    stage.addEventListener('mousedown', function (e) {
      if (e.button === 0) {
        e.preventDefault();
        saveCurrent().catch(function (err) { toast('存帧失败: ' + (err.message || err)); });
      } else if (e.button === 2) {
        e.preventDefault();
        deleteCurrent().catch(function (err) { toast('删帧失败: ' + (err.message || err)); });
      }
    });
    stage.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    stage.addEventListener('wheel', function (e) {
      e.preventDefault();
      var step = e.shiftKey ? 30 : 1;
      goto(state.idx + (e.deltaY < 0 ? -step : step));
    }, { passive: false });
  }

  /**
   * 绑定全局键盘快捷键。
   * 引用: init。
   */
  function bindKeys() {
    document.addEventListener('keydown', function (e) {
      var k = e.key;
      var handled = true;
      if (k === 'a' || k === 'A') switchVideo(-1);
      else if (k === 'd' || k === 'D') switchVideo(1);
      else if (k === 'q' || k === 'Q') goto(state.idx - 1);
      else if (k === 'e' || k === 'E') goto(state.idx + 1);
      else if (k === 'ArrowLeft') goto(state.idx - 10);
      else if (k === 'ArrowRight') goto(state.idx + 10);
      else if (k === 'PageUp') goto(state.idx - 100);
      else if (k === 'PageDown') goto(state.idx + 100);
      else if (k === 's' || k === 'S' || k === ' ') {
        e.preventDefault();
        saveCurrent().catch(function (err) { toast('存帧失败: ' + (err.message || err)); });
      } else if (k === 'x' || k === 'X' || k === 'Delete') {
        e.preventDefault();
        deleteCurrent().catch(function (err) { toast('删帧失败: ' + (err.message || err)); });
      } else {
        handled = false;
      }
      if (handled) e.preventDefault();
    });
  }

  /** 窗口尺寸变化时重绘画布。引用: init。 */
  function bindResize() {
    var raf = null;
    window.addEventListener('resize', function () {
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(drawChart);
    });
  }

  /**
   * 页面初始化入口。
   * 引用: 页面底部自动调用。
   */
  async function init() {
    if (typeof API === 'undefined' || typeof API.dataset !== 'function') return;
    state.path = readQueryPath();
    if (!state.path) {
      var stage = $('ve-stage');
      if (stage) stage.innerHTML = '<p style="padding:40px;color:var(--text-dim);">缺少 path 参数</p>';
      return;
    }
    bindToolbar();
    bindChart();
    bindStage();
    bindKeys();
    bindResize();

    // 拉取视频列表以支持 A/D 切换
    try {
      var list = await API.request(API.apiPath('/videos'));
      state.videoList = list || [];
      for (var i = 0; i < state.videoList.length; i++) {
        if (state.videoList[i].path === state.path) { state.vidIdx = i; break; }
      }
    } catch (e) {
      toast(String(e.message || e));
    }

    await setPath(state.path);
  }

  document.addEventListener('DOMContentLoaded', function () {
    init().catch(function (e) {
      toast('初始化失败: ' + (e.message || e));
    });
  });

  return {
    init: init,
    goto: goto,
    drawChart: drawChart,
  };
})();