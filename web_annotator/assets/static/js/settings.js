/**
 * settings.js —— 前端设置的读写客户端(全局缺省层 + 数据集覆盖层)。
 *
 * 设置分两层持久化: 全局缺省层(datasets/.annotator_settings.json)存跨任务
 * 通用的输入参数习惯, 数据集层(datasets/xxx/.annotator_settings.json)存任务
 * 特有的目录选择、描述规则与裁剪部位。页面启动时先调用 ready()(服务端已按
 * 数据集键覆盖全局键合并两层), 之后 get(key) 同步读取内存, set(key, value)
 * 按键归属写入对应层并更新内存。
 *
 * 引用：nav.js boot；各业务页 init 与保存点；index.js 导入旧设置。
 */
var AppSettings = (function () {
  'use strict';

  // 任务特有的设置键, 写入数据集层; 其余键写入全局缺省层
  var DATASET_KEYS = ['dirs', 'caption', 'crop'];

  var _data = null;    // 两层合并后的设置全量(ready 完成后可用)
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
   * 写入一个设置键: 按键归属更新内存并异步写回对应层。
   * 引用：各业务页保存点。
   * @param {string} key 页面/组件名
   * @param {*} value 可 JSON 序列化的值
   * @returns {Promise<void>} 写回完成的 Promise(失败已提示, 不抛出)
   */
  function set(key, value) {
    if (_data) _data[key] = value;
    var patch = {};
    patch[key] = value;
    var path = DATASET_KEYS.indexOf(key) >= 0
      ? API.apiPath('/settings')
      : '/api/settings';
    return API.request(path, {
      method: 'PUT',
      body: JSON.stringify(patch),
    }).catch(function (e) {
      toast('设置保存失败: ' + String(e.message || e));
    });
  }

  return { ready: ready, get: get, set: set };
})();
