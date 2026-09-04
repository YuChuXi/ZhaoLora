/**
 * video_extract.js —— 视频抽帧页。
 *
 * 交互参照 YSM/app.py 的桌面工具在浏览器中复刻：
 *   - 主预览区显示当前帧；左键存帧、右键删帧、滚轮调帧
 *   - 底部缩略图条按采样间隔提供快速跳转
 *   - 帧类型柱状图(Canvas)展示每帧编码类型(I/P/B)与包大小，点击柱状跳帧
 *   - 帧类型经由后端长任务(逐帧解码分析 + SSE 进度)，完成后回读并重绘柱状图
 *   - 键盘：A/D 切换视频、Q/E 单帧、←/→ 十帧、S/空格 存帧、X/Delete 删帧
 *
 * 引用：templates/video_extract.html。
 */
var VideoExtract = (function () {
  'use strict';

  var THUMB_COUNT = 60; // 缩略图条采样目标数量
  var THUMB_SIZE = 256; // 缩略图条帧最长边
  var FRAME_COLORS = { I: '#ff6b6b', P: '#f5c542', B: '#4a9eff' }; // 红I/黄P/蓝B
  var SEG_MAX_SEC = 60; // 帧窗口最大时长(秒), 前后各半

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
    hideB: true, // 隐藏 B 帧: 仅显示层——柱状图不绘制 B 帧柱(紧凑排列), 所有切帧操作(滚轮/键盘/按钮)跳过 B 帧, 只落在 I/P 帧; 段始终含 B 帧, 不影响请求
  };

  var _stripItems = []; // 缩略图条 DOM 集合
  var _globalCache = null; // 全局图柱状体缓存(逐帧绘制成本高, 仅宽度/帧数变化时重建)

  // ---- WebCodecs 帧窗口段: 后端从 start 前最近关键帧流复制源压缩包(不重编码),
  //      前端批量 decode + flush, 按 output 顺序(显示顺序)匹配目标帧; 段首对齐
  //      关键帧产生的前导帧与段尾 B 帧参考帧均由前端解码时自然消化 ----
  var _canvas = null;   // 主预览 canvas
  var _cctx = null;     // canvas 2D 上下文
  var _decoder = null;  // VideoDecoder 实例
  var _seg = null;      // 当前窗口: { start, end, chunks: [{bytes, key}] }
  var _segSeq = 0;      // 窗口段版本号, 换段递增以丢弃过期响应
  var _decodeTail = Promise.resolve(); // 解码串行链, 避免 decode/flush 交错
  var _pendingTarget = -1; // 最近一次请求的帧索引(用于丢弃已过期的目标)
  var _flushTarget = -1;   // 当前 flush 期望的 output 序号(显示顺序偏移)
  var _flushCount = 0;     // flush 期间已 output 的帧数
  var _flushResult = null; // flush 期间收集的目标帧(其余帧立即释放)

  /** ``document.getElementById`` 快捷别名, 全文件通用。 */
  function $(id) { return document.getElementById(id); }

  /** 构造抽帧缩略图 URL, 可选等比缩略最长边尺寸。Callers: buildStrip。 */
  function frameUrl(idx, size) {
    var url = API.apiPath('/video/frame')
      + '?path=' + encodeURIComponent(state.path)
      + '&index=' + idx;
    if (size) url += '&size=' + size;
    return url;
  }

  /** 构造帧窗口段 URL(后端流复制返回 H.264 Annex-B + X-Segment-Start/X-Frame-Count 头)。Callers: ensureSegment。 */
  function segmentUrl(start, end) {
    return API.apiPath('/video/segment')
      + '?path=' + encodeURIComponent(state.path)
      + '&start=' + start + '&end=' + end;
  }

  /** 构造封面 URL(用于视频切换时顶部信息)。Callers: 无(仅信息)。 */
  function coverUrl() {
    return API.apiPath('/video/cover') + '?path=' + encodeURIComponent(state.path);
  }

  /** 读取 path 查询参数并解码。Callers: init。 */
  function readQueryPath() {
    var p = new URLSearchParams(location.search).get('path');
    return p ? decodeURIComponent(p) : '';
  }

  /**
   * 更新状态栏与工具栏信息。
   *
   * Callers:
   *   - `goto`
   *   - `setPath`
   *   - `loadDetail`
   *   - `saveCurrent`
   *   - `deleteCurrent`
   *   - `analysisDone`
   *   - `bindHideB`
   */
  function paintStatus() {
    var name = state.path ? state.path.split('/').pop() : '';
    var seg = $('seg-name');
    if (seg) {
      seg.textContent = (state.vidIdx >= 0 ? '[' + (state.vidIdx + 1) + '/' + state.videoList.length + '] ' : '')
        + name
        + ' · ' + state.total + ' 帧'
        + (state.total ? ' · ' + fmtTime((state.idx + 1) / (state.total / (state.duration || 1))) : '');
    }
    var frame = $('seg-pos');
    if (frame) {
      var ft = state.types[state.idx];
      var tInfo = ft ? (' · ' + ft.type + '帧 ' + ft.size + 'KB') : '';
      frame.textContent = (state.idx + 1) + '/' + state.total + tInfo;
    }
    var saved = $('seg-done');
    if (saved) saved.textContent = '已保存: ' + state.saved.length;
    var size = $('seg-metric');
    if (size) size.textContent = state.width + '×' + state.height;
  }

  /** 秒数格式化为 mm:ss。Callers: paintStatus。 */
  function fmtTime(sec) {
    if (!(sec > 0) || !isFinite(sec)) return '';
    var s = Math.floor(sec % 60);
    var m = Math.floor(sec / 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  /**
   * 跳转到指定帧: 钳位、解码预览、同步缩略条与曲线光标。
   *
   * @param {number} idx 目标帧索引.
   *
   * Callers:
   *   - `buildStrip` 缩略图点击
   *   - `chartClick` 柱状图点击
   *   - `stepFrame` 切帧步进
   *   - `stepI` I 帧跳转
   */
  function goto(idx) {
    if (!state.total) return;
    idx = Math.max(0, Math.min(state.total - 1, idx));
    if (idx === state.idx) {
      paintStatus();
      return;
    }
    showFrame(idx);
  }

  /**
   * 解码并显示指定帧: 从最近关键帧批量 decode 到段尾, flush 强制按显示顺序
   * 输出所有帧, 按 output 序号匹配目标(B 帧重排序由解码器内部处理);
   * 移出窗口则先请求新段。
   *
   * @param {number} idx 目标帧索引.
   *
   * Callers:
   *   - `goto`
   *   - `setPath`
   *   - `bindHideB`
   */
  function showFrame(idx) {
    _pendingTarget = idx;
    ensureSegment(idx).then(function () {
      if (_pendingTarget !== idx) return;
      var pos = idx - _seg.start;
      _decodeTail = _decodeTail
        .then(function () {
          if (_pendingTarget !== idx) return null;
          return navigateTo(pos, idx);
        })
        .then(function (frame) {
          if (!frame) return;
          if (_pendingTarget !== idx) { frame.close(); return; }
          drawFrame(frame);
          state.idx = idx;
          paintStatus();
          updateCursor();
          syncStrip();
        });
    }).catch(function (e) {
      toast('帧 ' + idx + ' 加载失败: ' + ((e && e.message) || e));
      var stage = $('ve-stage');
      if (stage) stage.classList.remove('loading');
    });
  }

  /** 帧窗口半宽(帧数): 按 fps 和 SEG_MAX_SEC 计算, 前后各最多 SEG_MAX_SEC/2 秒。Callers: ensureSegment。 */
  function segHalf() {
    if (state.duration > 0 && state.total > 0) {
      var fps = state.total / state.duration;
      return Math.max(30, Math.round(fps * SEG_MAX_SEC / 2));
    }
    return 100; // 无时长信息时的回退值
  }

  /**
   * 拉取覆盖目标帧的窗口段并重建解码器。段首为 start 前最近关键帧, 其帧号经
   * ``X-Segment-Start`` 响应头告知; 实际帧数由 ``X-Frame-Count`` 告知(含段尾
   * B 帧参考帧), 前端不猜测。Callers: showFrame。
   */
  function ensureSegment(idx) {
    if (_seg && idx >= _seg.start && idx <= _seg.end) return Promise.resolve();
    var half = segHalf();
    var start = Math.max(0, idx - half);
    var end = Math.min(state.total - 1, idx + half);
    var seq = ++_segSeq;
    var stage = $('ve-stage');
    if (stage) stage.classList.add('loading');
    return fetch(segmentUrl(start, end))
      .then(function (r) {
        if (!r.ok) throw new Error('段获取失败 ' + r.status);
        var count = parseInt(r.headers.get('X-Frame-Count'), 10) || 0;
        var segStart = parseInt(r.headers.get('X-Segment-Start'), 10);
        return r.arrayBuffer().then(function (buf) { return { buf: buf, count: count, segStart: segStart }; });
      })
      .then(function (result) {
        if (seq !== _segSeq) return; // 期间已切换目标, 丢弃该段
        var parsed = demuxAnnexB(new Uint8Array(result.buf));
        if (!parsed.sps || !parsed.pps || !parsed.chunks.length) {
          throw new Error('段内无有效编码帧');
        }
        if (_decoder) { try { _decoder.close(); } catch (ignore) {} }
        _decoder = makeDecoder();
        _decoder.configure({
          codec: buildCodecString(parsed.sps),
          description: buildAvcConfig(parsed.sps, parsed.pps),
        });
        _seg = { start: result.segStart, end: result.segStart + result.count - 1, chunks: parsed.chunks };
      });
  }

  /**
   * 从最近关键帧批量 decode 到段尾, flush 强制输出。段为源流复制, 段首 GOP
   * 对齐的前导帧与段尾 B 帧参考帧均需解码消化; 解码器内部处理 B 帧重排序,
   * output 按显示顺序输出, 用 output 序号匹配目标帧。
   * @param {number} pos 窗口内目标位置(显示顺序)。
   * @param {number} idx 全局帧索引(用于中途抛弃过期目标)。
   * Callers: showFrame。
   */
  function navigateTo(pos, idx) {
    var keyPos = lastKeyAtOrBefore(pos);
    var end = _seg.chunks.length - 1;
    for (var p = keyPos; p <= end; p++) {
      var chunk = _seg.chunks[p];
      _decoder.decode(new EncodedVideoChunk({
        type: chunk.key ? 'key' : 'delta',
        timestamp: p,
        data: chunk.bytes,
      }));
    }
    _flushTarget = pos - keyPos;
    _flushCount = 0;
    _flushResult = null;
    return _decoder.flush().then(function () {
      if (_pendingTarget !== idx) {
        if (_flushResult) _flushResult.close();
        _flushResult = null;
        return null;
      }
      var f = _flushResult;
      _flushResult = null;
      return f;
    });
  }

  /** 窗口内最近一个关键帧位置(≤ pos); 无则 0。Callers: navigateTo。 */
  function lastKeyAtOrBefore(pos) {
    for (var i = pos; i >= 0; i--) {
      if (_seg.chunks[i] && _seg.chunks[i].key) return i;
    }
    return 0;
  }

  /** 构造 VideoDecoder, 解码结果经 onDecoded 收集。Callers: ensureSegment。 */
  function makeDecoder() {
    return new VideoDecoder({
      output: onDecoded,
      error: function (e) {
        console.error('VideoDecoder 解码错误', e);
      },
    });
  }

  /**
   * 解码器输出回调: flush 期间按 output 顺序计数, 仅保留序号匹配
   * ``_flushTarget`` 的目标帧, 其余中间帧立即释放。Callers: makeDecoder。
   */
  function onDecoded(frame) {
    if (_flushCount === _flushTarget) {
      _flushResult = frame;
    } else {
      frame.close();
    }
    _flushCount++;
  }

  /** 把解码帧绘制到主预览 canvas(按帧尺寸自适应画布)。Callers: showFrame。 */
  function drawFrame(frame) {
    var w = frame.displayWidth, h = frame.displayHeight;
    if (_canvas.width !== w || _canvas.height !== h) {
      _canvas.width = w;
      _canvas.height = h;
    }
    _cctx.drawImage(frame, 0, 0, w, h);
    frame.close();
    var stage = $('ve-stage');
    if (stage) stage.classList.remove('loading');
  }

  /**
   * 解析 H.264 Annex-B 裸流: 提取 SPS/PPS 并按片 NAL 分组成帧。
   * SPS/PPS 只提取用于 configure 的 description, 不包含在 chunk 中(AVCC 格式
   * 要求参数集通过 description 传入, chunk 只含 slice 数据)。
   * 关键帧判定: 片 NAL 为 IDR(类型 5) 即关键帧。
   * Callers: ensureSegment。
   */
  function demuxAnnexB(bin) {
    var nals = [];
    var i = 0, n = bin.length;
    while (i < n) {
      // 找起始码(3 字节 00 00 01 或 4 字节 00 00 00 01)
      var scLen = 0;
      if (i + 3 <= n && bin[i] === 0 && bin[i + 1] === 0 && bin[i + 2] === 1) {
        scLen = 3;
      } else if (i + 4 <= n && bin[i] === 0 && bin[i + 1] === 0 && bin[i + 2] === 0 && bin[i + 3] === 1) {
        scLen = 4;
      }
      if (!scLen) { i++; continue; }
      i += scLen;
      var start = i;
      // 扫描到下一个起始码或末尾
      while (i < n) {
        if (i + 3 <= n && bin[i] === 0 && bin[i + 1] === 0 && bin[i + 2] === 1) break;
        if (i + 4 <= n && bin[i] === 0 && bin[i + 1] === 0 && bin[i + 2] === 0 && bin[i + 3] === 1) break;
        i++;
      }
      nals.push(bin.subarray(start, i));
    }
    var sps = null, pps = null;
    for (var k = 0; k < nals.length; k++) {
      var t = nals[k][0] & 0x1F;
      if (t === 7 && !sps) sps = nals[k];
      else if (t === 8 && !pps) pps = nals[k];
    }
    // 按片 NAL 分组为帧: 每个 slice NAL(类型 1 或 5)为一个独立 chunk,
    // SPS/PPS 已通过 configure 的 description 传入, 不包含在 chunk 中。
    var chunks = [];
    for (var m = 0; m < nals.length; m++) {
      var ty = nals[m][0] & 0x1F;
      if (ty === 1 || ty === 5) {
        chunks.push({ bytes: toLengthPrefixed([nals[m]]), key: ty === 5 });
      }
    }
    return { sps: sps, pps: pps, chunks: chunks };
  }

  /** 把一组 NAL 封装为 4 字节长度前缀的 AVCC 数据(WebCodecs 期望的 chunk 格式)。Callers: demuxAnnexB。 */
  function toLengthPrefixed(nals) {
    var total = 0;
    for (var i = 0; i < nals.length; i++) total += 4 + nals[i].length;
    var out = new Uint8Array(total);
    var dv = new DataView(out.buffer), off = 0;
    for (var j = 0; j < nals.length; j++) {
      dv.setUint32(off, nals[j].length);
      out.set(nals[j], off + 4);
      off += 4 + nals[j].length;
    }
    return out;
  }

  /** 由 SPS 构造 avc1 codec 字符串(profile.compat.level)。Callers: ensureSegment。 */
  function buildCodecString(sps) {
    function hex2(b) { return ('0' + b.toString(16)).slice(-2); }
    return 'avc1.' + hex2(sps[1]) + hex2(sps[2]) + hex2(sps[3]);
  }

  /** 由 SPS/PPS 构造 AVCDecoderConfigurationRecord 作为解码器 description。Callers: ensureSegment。 */
  function buildAvcConfig(sps, pps) {
    var cfg = new Uint8Array(11 + sps.length + pps.length);
    var dv = new DataView(cfg.buffer);
    dv.setUint8(0, 1);       // configurationVersion
    dv.setUint8(1, sps[1]);  // AVCProfileIndication
    dv.setUint8(2, sps[2]);  // profile_compatibility
    dv.setUint8(3, sps[3]);  // AVCLevelIndication
    dv.setUint8(4, 0xFF);    // lengthSizeMinusOne = 3(4 字节长度)
    dv.setUint8(5, 0xE1);    // numOfSequenceParameterSets = 1
    dv.setUint16(6, sps.length);
    cfg.set(sps, 8);
    var off = 8 + sps.length;
    dv.setUint8(off, 1);     // numOfPictureParameterSets
    dv.setUint16(off + 1, pps.length);
    cfg.set(pps, off + 3);
    return cfg;
  }

  /** 关闭解码器并清空窗口段状态(切换视频时调用).
   *
   *  Callers:
   *    - `setPath`
   *    - `bindHideB`
   */
  function resetDecoder() {
    if (_decoder) { try { _decoder.close(); } catch (ignore) {} }
    _decoder = null;
    _seg = null;
    _segSeq++;
    _decodeTail = Promise.resolve();
    _pendingTarget = -1;
    _flushTarget = -1;
    _flushCount = 0;
    _flushResult = null;
  }

  /** 切换视频(上下一个), 重新加载详情并回到第 0 帧.
   *
   *  @param {number} offset 偏移量(-1 上一个, 1 下一个).
   *
   *  Callers:
   *    - `bindToolbar`
   *    - `bindKeys`
   */
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
   *
   * @param {string} p 视频相对路径.
   *
   * Callers:
   *   - `init`
   *   - `switchVideo`
   */
  async function setPath(p) {
    state.path = p;
    state.idx = 0;
    state.total = 0;
    state.types = {};
    state.saved = [];
    stopAnalysis();
    resetDecoder();
    var strip = $('ve-strip');
    if (strip) strip.innerHTML = '';
    _stripItems = [];
    _chart = { global: null, local: null };
    paintStatus();
    await loadDetail();
    // 加载首帧; goto(0) 会因 idx===state.idx 提前返回而跳过首帧
    showFrame(0);
  }

  /**
   * 把后端紧凑位流(类型串 + 大小数组)还原为 ``{帧号: {type, size}}``。
   * 数组下标即帧序号, 由 detail 接口压缩传输以减小负载。
   * Callers: loadDetail。
   */
  function decodeBitstream(bits) {
    var out = {};
    if (!bits || !bits.t) return out;
    var t = bits.t, s = bits.s || [];
    for (var i = 0; i < t.length; i++) {
      var c = t[i];
      if (c !== '?') out[i] = { type: c, size: s[i] || 0 };
    }
    return out;
  }

  /**
   * 加载视频详情(宽高/时长/总帧数/帧类型/已保存)。
   * Callers: setPath / analysisDone。
   */
  async function loadDetail() {
    var data = await API.request(API.apiPath('/video/detail')
      + '?path=' + encodeURIComponent(state.path));
    state.total = data.total || 0;
    state.width = data.width || 0;
    state.height = data.height || 0;
    state.duration = data.duration || 0;
    state.types = decodeBitstream(data.types);
    state.saved = data.saved || [];
    if (!state.total) {
      toast('视频无法读取或总帧数为 0');
      return;
    }
    buildStrip();
    drawChart();
    // 位流缓存命中时如实报告已分析状态; 未命中则自动分析(进度会覆盖该文本)
    var segExtra = $('seg-extra');
    if (segExtra && Object.keys(state.types).length) {
      segExtra.textContent = '帧类型: 已分析';
    }
    // 位流未缓存时自动分析帧类型, 使 I/P/B 曲线无需手动触发;
    // 分析完成回调会回读 detail(此时 types 非空)故不会重复启动
    if (!Object.keys(state.types).length && !state.analyzing) {
      toggleAnalysis();
    }
  }

  /**
   * 保存当前帧 PNG 到 main 目录。
   *
   * Callers:
   *   - `bindToolbar` 存帧按钮
   *   - `bindStage` 左键
   *   - `bindKeys` S/空格
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
   * 删除当前帧已保存的 PNG。
   *
   * Callers:
   *   - `bindToolbar` 删帧按钮
   *   - `bindStage` 右键
   *   - `bindKeys` X/Delete
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
   * Callers: loadDetail。
   */
  function buildStrip() {
    var strip = $('ve-strip');
    strip.innerHTML = '';
    _stripItems = [];
    if (!state.total) return;
    // 按视频真实宽高比设置缩略图占位, 使懒加载前布局与加载后一致(避免抖动/误判)
    strip.style.setProperty(
      '--thumb-ar',
      (state.width || 16) + '/' + (state.height || 9),
    );
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
        img.src = frameUrl(idx, THUMB_SIZE);
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
    syncStrip();
  }

  /** 同步缩略图条高亮: 当前帧与已保存帧。
   *
   *  Callers:
   *    - `goto`
   *    - `saveCurrent`
   *    - `deleteCurrent`
   *    - `analysisDone`
   */
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

  // ---- 帧类型柱状图 (SVG 原生横向滚动) ----

  /**
   * 帧类型双图, 均渲染整条视频时间轴:
   *   全局图: 视口宽度约对应 10 分钟时间窗, 尺度粗;
   *   局部图: 视口宽度约对应 200 帧, 尺度细, 承担逐帧 I/P/B 明细。
   * 每帧一根柱 I红/P黄/B蓝, 高度按最大包归一化; 叠加当前帧白竖线(游标)与
   * 已保存帧绿刻度。SVG 宽度 = 整条时间轴宽度, 超出视口部分由容器
   * ``overflow-x: auto`` 提供浏览器原生横向滚动, 滚动只浏览、不改变当前帧。
   * 数据变化(分析完成/存取帧/换片/缩放)调用 drawChart 全量重建; 仅换帧调用
   * updateCursor 只移动游标并滚动居中, 开销最小。
   */
  var _chart = { global: null, local: null }; // 各图渲染信息: {svgId, ppf, svgW, viewportW, cursor}

  /** 显示域帧数: 隐藏 B 帧时仅计非 B 帧, 否则即总帧数。Callers: globalPxPerFrame / localPxPerFrame / renderChart。 */
  function domainCount() {
    if (!state.hideB) return state.total;
    var n = 0;
    for (var i = 0; i < state.total; i++) {
      var ft = state.types[i];
      if (ft && ft.type !== 'B') n++;
    }
    return n;
  }

  /** 帧索引映射到显示域序号(0 基): 隐藏 B 帧时为其左侧非 B 帧个数, 否则即帧号。Callers: cursorX / renderChart。 */
  function barOrdinal(idx) {
    if (!state.hideB) return idx;
    var n = 0;
    for (var i = 0; i < idx; i++) {
      var ft = state.types[i];
      if (ft && ft.type !== 'B') n++;
    }
    return n;
  }

  /**
   * 已保存帧在显示域中的可见位置: 非隐藏 B 帧时即自身; 隐藏 B 帧时若该帧为 B
   * 则向外搜索最近的非 B 帧作为落点, 使标记始终可见。Callers: renderChart。
   * @param {number} idx 已保存帧的全局索引。
   * @returns {number} 可见帧的全局索引。
   */
  function nearestVisibleIdx(idx) {
    if (!state.hideB) return idx;
    var ft = state.types[idx];
    if (ft && ft.type !== 'B') return idx;
    for (var d = 1; d < state.total; d++) {
      if (idx - d >= 0 && state.types[idx - d] && state.types[idx - d].type !== 'B') return idx - d;
      if (idx + d < state.total && state.types[idx + d] && state.types[idx + d].type !== 'B') return idx + d;
    }
    return idx;
  }

  /** 全局图每帧像素宽: 视口最多对应 10 分钟帧跨度; 视频更短时整条铺满视口(最多 10 分钟而非固定 10 分钟)。Callers: renderChart。 */
  function globalPxPerFrame(viewportW) {
    var domain = domainCount() || 1;
    var dur = state.duration || 0;
    var WIN_SEC = 600; // 10 分钟
    var winFrames = dur > 0 ? Math.max(1, Math.round((WIN_SEC * domain) / dur)) : domain;
    var showFrames = Math.min(winFrames, domain);
    return viewportW / showFrames;
  }

  /** 局部图每帧像素宽: 视口最多对应 200 帧; 视频更短时整条铺满视口。Callers: renderChart。 */
  function localPxPerFrame(viewportW) {
    var domain = domainCount() || 1;
    var LOCAL_SPAN = 200;
    return viewportW / Math.min(LOCAL_SPAN, domain);
  }

  /**
   * 全量重建单张 SVG 柱状图(柱 + 已存刻度 + 游标)。
   * @param {string} svgId svg 元素 id。
   * @param {function(number):number} pxPerFrame 每帧像素宽计算函数。
   * @returns {{svgId:string, ppf:number, svgW:number, viewportW:number, cursor:SVGElement}|null}
   * Callers: drawChart。
   */
  function renderChart(svgId, pxPerFrame) {
    var svg = $(svgId);
    if (!svg) return null;
    var container = svg.parentElement;
    var viewportW = container.clientWidth, vh = container.clientHeight;
    if (!viewportW || !vh) return null;
    var total = state.total || 1;
    var domain = domainCount() || 1; // 显示域宽度: 隐藏 B 帧时即非 B 帧数
    var ppf = pxPerFrame(viewportW);
    var svgW = Math.max(viewportW, Math.ceil(domain * ppf));
    svg.setAttribute('width', svgW);
    svg.setAttribute('height', vh);
    svg.style.width = svgW + 'px';
    svg.style.height = vh + 'px';
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    var ns = 'http://www.w3.org/2000/svg';
    var plotH = vh - 2, bottom = vh - 1;

    var maxSize = 1;
    for (var k in state.types) {
      var s = state.types[k].size;
      if (s > maxSize) maxSize = s;
    }

    // 隐藏 B 帧时, 柱按显示顺序紧凑排列(不留空槽), 故用 pos 而非 idx 定位。
    var bars = document.createElementNS(ns, 'g');
    var slots = []; // 显示序号 -> 帧索引(点击反查)
    var pos = 0;
    for (var idx = 0; idx < total; idx++) {
      var ft = state.types[idx];
      if (!ft) continue;
      if (state.hideB && ft.type === 'B') continue;
      slots.push(idx);
      var hgt = (ft.size / maxSize) * plotH;
      var r = document.createElementNS(ns, 'rect');
      r.setAttribute('x', pos * ppf);
      r.setAttribute('y', bottom - hgt);
      r.setAttribute('width', Math.max(1, ppf));
      r.setAttribute('height', hgt);
      r.setAttribute('fill', FRAME_COLORS[ft.type] || '#888');
      bars.appendChild(r);
      pos++;
    }
    svg.appendChild(bars);

    // 已保存帧标记: 顶部彩色条, 颜色取帧类型(I 红/P 黄/B 蓝);
    // 隐藏 B 帧时已保存的 B 帧映射到最近的可见帧位置
    if (state.saved.length) {
      for (var s2 = 0; s2 < state.saved.length; s2++) {
        var saveIdx = state.saved[s2];
        var visIdx = nearestVisibleIdx(saveIdx);
        var saveFt = state.types[saveIdx];
        var markerColor = saveFt ? (FRAME_COLORS[saveFt.type] || '#888') : '#3ecf8e';
        var t = document.createElementNS(ns, 'rect');
        t.setAttribute('x', barOrdinal(visIdx) * ppf);
        t.setAttribute('y', 0);
        t.setAttribute('width', Math.max(2, ppf));
        t.setAttribute('height', 8);
        t.setAttribute('fill', markerColor);
        svg.appendChild(t);
      }
    }

    var cursor = document.createElementNS(ns, 'line');
    cursor.setAttribute('y1', 0);
    cursor.setAttribute('y2', vh);
    cursor.setAttribute('stroke', '#ffffff');
    cursor.setAttribute('stroke-width', 1);
    svg.appendChild(cursor);

    var info = { svgId: svgId, ppf: ppf, svgW: svgW, viewportW: viewportW, cursor: cursor, slots: slots };
    _chart[svgId === 've-chart-global' ? 'global' : 'local'] = info;
    positionCursor(info);
    return info;
  }

  /** 当前帧游标在柱上的 x 坐标: 柱中心(避免落在两柱交界)。Callers: positionCursor / syncScroll。 */
  function cursorX(info) {
    return barOrdinal(state.idx) * info.ppf + info.ppf / 2;
  }

  /** 将游标移动到当前帧柱中心(仅移动 line, 不重建柱)。Callers: renderChart / updateCursor。 */
  function positionCursor(info) {
    if (!info || !info.cursor) return;
    var x = cursorX(info);
    info.cursor.setAttribute('x1', x);
    info.cursor.setAttribute('x2', x);
  }

  /** 数据变化时全量重建两图并滚动居中游标。
   *
   *  Callers:
   *    - `loadDetail`
   *    - `analysisDone`
   *    - `saveCurrent`
   *    - `deleteCurrent`
   *    - `bindResize`
   *    - `bindHideB`
   */
  function drawChart() {
    renderChart('ve-chart-global', globalPxPerFrame);
    renderChart('ve-chart-local', localPxPerFrame);
    syncScroll();
  }

  /** 仅移动游标并滚动居中(换帧时调用, 开销最小)。Callers: showFrame。 */
  function updateCursor() {
    positionCursor(_chart.global);
    positionCursor(_chart.local);
    syncScroll();
  }

  /** 滚动各图容器, 使当前帧游标尽量居中(边缘处贴边)。Callers: drawChart / updateCursor。 */
  function syncScroll() {
    for (var key in _chart) {
      var c = _chart[key];
      if (!c) continue;
      var container = $(c.svgId).parentElement;
      var target = cursorX(c) - c.viewportW / 2;
      container.scrollLeft = Math.max(0, Math.min(target, c.svgW - c.viewportW));
    }
  }

  /** 将点击坐标换算为帧索引并跳转(按各图每帧像素宽映射)。
   *
   *  Callers:
   *    - `bindChart`
   */
  function chartClick(e) {
    if (!state.total) return;
    var svg = e.currentTarget;
    var c = _chart[svg.id === 've-chart-global' ? 'global' : 'local'];
    if (!c) return;
    var r = svg.getBoundingClientRect();
    var xCanvas = e.clientX - r.left;
    var slot = Math.floor(xCanvas / c.ppf);
    var idx = c.slots[slot];
    if (idx === undefined) idx = state.total - 1;
    goto(Math.max(0, Math.min(state.total - 1, idx)));
  }

  // ---- 帧类型分析 ----

  /** 停止进行中的分析 SSE 订阅并隐藏进度条。Callers: setPath / toggleAnalysis。 */
  function stopAnalysis() {
    if (state.jobWatch) {
      state.jobWatch.close();
      state.jobWatch = null;
    }
    state.analyzing = false;
    var btn = $('ve-analysis');
    if (btn) btn.disabled = false;
    var bar = $('ve-progress');
    if (bar) bar.hidden = true;
  }

  /**
   * 更新分析进度条(轨道+百分比)与状态栏消息。
   * Callers: toggleAnalysis。
   */
  function setProgress(pct, msg) {
    var bar = $('ve-progress');
    if (bar) bar.hidden = false;
    var fill = $('ve-progress-fill');
    if (fill) fill.style.width = Math.min(100, pct) + '%';
    var pctEl = $('ve-progress-pct');
    if (pctEl) pctEl.textContent = Math.round(pct) + '%';
    var seg = $('seg-extra');
    if (seg) seg.textContent = msg || '';
  }

  /** 设置分析结束状态文本并复位按钮。Callers: toggleAnalysis。 */
  function setStatus(t) {
    var prog = $('seg-extra');
    if (prog) prog.textContent = t;
    var btn = $('ve-analysis');
    if (btn) btn.textContent = '帧类型';
  }

  /**
   * 启动/停止位流帧类型分析长任务。
   *
   * Callers:
   *   - `bindToolbar` 分析按钮
   *   - `loadDetail` 自动分析
   */
  /**
   * 帧类型分析 SSE 回调(提交与重连共用).
   * @param {object} s JobSnapshot
   * 引用: ``toggleAnalysis`` / ``resumeAnalysis``.
   */
  function onAnalysisStatus(s) {
    if (s.status === 'running') {
      setProgress(s.pct || 0, (s.done || 0) + '/' + (s.total || 0)
        + (s.message ? ' · ' + s.message : ''));
    } else if (s.status === 'done') {
      stopAnalysis();
      setStatus('分析: 完成');
      analysisDone();
    } else if (s.status === 'error') {
      stopAnalysis();
      setStatus('分析: 失败');
      toast('帧类型分析失败: ' + (s.message || ''));
    } else if (s.status === 'cancelled') {
      stopAnalysis();
      setStatus('分析: 已取消');
    }
  }

  async function toggleAnalysis() {
    if (state.analyzing) {
      stopAnalysis();
      setStatus('分析: 已停止');
      return;
    }
    if (!state.total) return;
    var data = await API.request(API.apiPath('/video/analysis/start')
      + '?path=' + encodeURIComponent(state.path), { method: 'POST' });
    state.analyzing = true;
    var btn = $('ve-analysis');
    if (btn) btn.textContent = '停止';
    setProgress(0, '提交中…');
    state.jobWatch = API.watchJob(data.job_id, onAnalysisStatus);
  }

  /**
   * 页面刷新后恢复未结束的帧类型分析任务.
   * 引用: ``init``.
   */
  async function resumeAnalysis() {
    var snap = await API.findActiveJob('video_analysis', API.dataset());
    if (!snap) return;
    state.analyzing = true;
    var btn = $('ve-analysis');
    if (btn) btn.textContent = '停止';
    setProgress(snap.pct || 0, snap.message || '恢复中…');
    state.jobWatch = API.watchJob(snap.job_id, onAnalysisStatus);
  }

  /** 分析完成后回读帧类型并重绘柱状图。Callers: toggleAnalysis。 */
  async function analysisDone() {
    await loadDetail();
    drawChart();
    syncStrip();
    paintStatus();
  }

  // ---- 事件绑定 ----

  /**
   * 按步长切帧; 隐藏 B 帧开启时, 步长按非 B 帧(I/P)计数并跳过 B 帧。
   * 位流尚未分析时无类型信息, 退化为按实际帧数步进。
   *
   * @param {number} step 步长(正向后, 负向前).
   *
   * Callers:
   *   - `bindToolbar`
   *   - `bindStage`
   *   - `bindKeys`
   *   - `bindHideB`
   */
  function stepFrame(step) {
    if (!state.hideB) { goto(state.idx + step); return; }
    var dir = step > 0 ? 1 : -1;
    var n = Math.abs(step);
    var i = state.idx;
    while (n > 0) {
      i += dir;
      if (i < 0 || i >= state.total) break;
      var ft = state.types[i];
      if (!ft || ft.type !== 'B') n--;
    }
    goto(i);
  }

  /** 跳到下一个/上一个 I 帧(按方向 dir); 无 I 帧时保持当前帧。Callers: bindStage(Shift+滚轮)。 */
  function stepI(dir) {
    var i = state.idx + dir;
    while (i >= 0 && i < state.total) {
      var ft = state.types[i];
      if (ft && ft.type === 'I') break;
      i += dir;
    }
    if (i < 0 || i >= state.total) return;
    goto(i);
  }

  /** 绑定工具栏按钮与主预览交互。Callers: init。 */
  function bindToolbar() {
    var el = document.getElementById('ve-toolbar');
    if (!el) return;
    el.addEventListener('click', function (e) {
      var id = e.target && e.target.id;
      if (id === 've-prev-vid') switchVideo(-1);
      else if (id === 've-next-vid') switchVideo(1);
      else if (id === 've-prev-10') stepFrame(-10);
      else if (id === 've-prev-1') stepFrame(-1);
      else if (id === 've-next-1') stepFrame(1);
      else if (id === 've-next-10') stepFrame(10);
      else if (id === 've-save') {
        saveCurrent().catch(function (err) { toast('存帧失败: ' + (err.message || err)); });
      } else if (id === 've-del') {
        deleteCurrent().catch(function (err) { toast('删帧失败: ' + (err.message || err)); });
      } else if (id === 've-analysis') {
        toggleAnalysis().catch(function (err) { toast('帧类型分析失败: ' + (err.message || err)); });
      }
    });
  }

  /** 绑定"隐藏B帧"开关: 纯显示层切换, 重绘柱状图并落到非 B 帧(段与 B 帧无关, 无需重新请求)。Callers: init。 */
  function bindHideB() {
    var box = $('ve-hide-b');
    if (!box) return;
    state.hideB = box.checked;
    box.addEventListener('change', function () {
      state.hideB = box.checked;
      drawChart();
      paintStatus();
      // 开启隐藏 B 帧时, 若当前帧是 B 帧则跳到最近的 I/P 帧(stepFrame 内部调 showFrame)
      if (state.hideB) {
        var ft = state.types[state.idx];
        if (ft && ft.type === 'B') stepFrame(1);
      }
    });
  }

  /** 绑定帧类型柱状图交互(全局图 + 局部图): 点击跳帧; 垂直滚轮转水平滚动。Callers: init。 */
  function bindChart() {
    var g = $('ve-chart-global');
    var l = $('ve-chart-local');
    if (g) g.addEventListener('click', chartClick);
    if (l) l.addEventListener('click', chartClick);
  }

  /** 绑定主预览交互: 左键存帧、右键删帧、滚轮调帧。Callers: init。 */
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
      if (e.shiftKey) { stepI(e.deltaY < 0 ? -1 : 1); return; } // Shift: 按 I 帧跳
      stepFrame(e.deltaY < 0 ? -1 : 1);
    }, { passive: false });
  }

  /**
   * 绑定全局键盘快捷键。
   * Callers: init。
   */
  function bindKeys() {
    document.addEventListener('keydown', function (e) {
      var k = e.key;
      var handled = true;
      if (k === 'a' || k === 'A') switchVideo(-1);
      else if (k === 'd' || k === 'D') switchVideo(1);
      else if (k === 'q' || k === 'Q') stepFrame(-1);
      else if (k === 'e' || k === 'E') stepFrame(1);
      else if (k === 'ArrowLeft') stepFrame(-10);
      else if (k === 'ArrowRight') stepFrame(10);
      else if (k === 'PageUp') stepFrame(-100);
      else if (k === 'PageDown') stepFrame(100);
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

  /** 窗口尺寸变化时重绘画布。Callers: init。 */
  function bindResize() {
    var raf = null;
    window.addEventListener('resize', function () {
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(drawChart);
    });
  }

  /**
   * 页面初始化入口: 绑定 DOM 事件、加载视频列表、加载首帧。
   *
   * Callers:
   *   - `DOMContentLoaded` 事件监听器
   */
  async function init() {
    if (typeof API === 'undefined' || typeof API.dataset !== 'function') return;
    state.path = readQueryPath();
    if (!state.path) {
      var stageDom = $('ve-stage');
      if (stageDom) stageDom.innerHTML = '<p style="padding:40px;color:var(--text-dim);">缺少 path 参数</p>';
      return;
    }
    if (typeof VideoDecoder === 'undefined' || typeof EncodedVideoChunk === 'undefined') {
      var uns = $('ve-stage');
      if (uns) uns.innerHTML = '<p style="padding:40px;color:var(--text-dim);">当前浏览器不支持 WebCodecs, 无法逐帧解码</p>';
      return;
    }
    _canvas = $('ve-canvas');
    if (_canvas) _cctx = _canvas.getContext('2d');
    bindToolbar();
    bindChart();
    bindStage();
    bindKeys();
    bindResize();
    bindHideB();

    // 横向滚动容器(图表×2 + 缩略图条): 垂直滚轮转水平滚动
    document.querySelectorAll('.ve-chart-scroll, .ve-strip').forEach(function (el) {
      el.addEventListener('wheel', function (e) {
        if (!e.deltaY) return;
        e.preventDefault();
        el.scrollLeft += e.deltaY;
      }, { passive: false });
    });

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
    resumeAnalysis();
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