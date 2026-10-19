/**
 * settings.js —— 数据集级输入参数的读写客户端。
 *
 * 各业务页的输入参数按页面/组件名分键, 经后端持久化为数据集根下的
 * .annotator_settings.json(键值结构见各页脚本); 页面启动时先调用 ready(),
 * 之后 get(key) 同步读取内存, set(key, value) 更新内存并异步写回。
 *
 * 引用：nav.js boot；各业务页 init 与保存点。
 */
var AppSettings = (function () {
  'use strict';

  var _data = null;    // 当前数据集的设置全量(ready 完成后可用)
  var _ready = null;   // 进行中的加载 Promise(幂等)

  /**
   * 加载当前数据集的设置全量; 幂等, 重复调用返回同一 Promise。
   * 引用：nav.js boot；各业务页 init。
   * @returns {Promise<void>}
   */
  function ready() {
    if (!_ready) {
      _ready = API.request(API.apiPath('/settings')).then(function (res) {
        _data = (res && res.settings) || {};
      }).catch(function (e) {
        _data = {};
        toast('设置加载失败: ' + String(e.message || e));
      });
    }
    return _ready;
  }

  /**
   * 读取一个设置键的当前值。
   * @param {string} key 页面/组件名
   * @returns {*} 未加载或未保存过时为 undefined
   */
  function get(key) {
    return _data ? _data[key] : undefined;
  }

  /**
   * 写入一个设置键: 更新内存后异步写回服务端。
   * 引用：各业务页保存点。
   * @param {string} key 页面/组件名
   * @param {*} value 可 JSON 序列化的值
   * @returns {Promise<void>} 写回完成的 Promise(失败已提示, 不抛出)
   */
  function set(key, value) {
    if (_data) _data[key] = value;
    var patch = {};
    patch[key] = value;
    return API.request(API.apiPath('/settings'), {
      method: 'PUT',
      body: JSON.stringify(patch),
    }).catch(function (e) {
      toast('设置保存失败: ' + String(e.message || e));
    });
  }

  return { ready: ready, get: get, set: set };
})();
