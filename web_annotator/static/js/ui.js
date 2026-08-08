/**
 * ui.js —— 全局 UI 原语，供所有页面共享。
 *
 * 引用：所有使用 toast 的模板页面（index/scoring/dedupe/crop/paint/tags/
 *       tag_check/ready/home）。
 */
(function () {
  'use strict';

  var _toastTimer = null;

  /**
   * 显示短暂提示。
   * @param {string} msg
   */
  function toast(msg) {
    var el = document.getElementById('toast');
    if (!el) return;
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () { el.classList.remove('show'); }, 2200);
  }

  window.toast = toast;
})();
