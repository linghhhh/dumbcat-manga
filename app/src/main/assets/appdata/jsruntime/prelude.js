/**
 * 呆猫漫画 · JS 图源运行时（prelude）
 *
 * 由 JsRuntime.java 在创建 QuickJS 上下文后执行，提供：
 *   - registerSource(def)      注册一个图源（脚本里必须调用一次）
 *   - Network.get/post/json    同步 HTTP（宿主 HttpURLConnection，支持自定义头）
 *   - Html.parse(html)         无 DOM 环境下的轻量解析：tag/#id/.class/[attr] 选择器
 *   - Crypto.* / console.*     摘要、日志（写 logcat）
 *
 * 约定：图源函数**必须同步返回**对象，不能是 async/Promise —— 宿主跨语言调用无法 await。
 * 入参与返回值都是普通对象，prelude 负责与 JSON 互换。
 */
(function (global) {
  'use strict';

  // ---------------------------------------------------------------- 宿主桥

  var Native = global.Native;
  if (!Native || typeof Native.call !== 'function') {
    throw new Error('prelude 需要宿主提供 Native.call');
  }

  function host(name, payload) {
    var text = Native.call(name, JSON.stringify(payload || {}));
    if (text === null || text === undefined) {
      return null;
    }
    var parsed;
    try {
      parsed = JSON.parse(text);
    } catch (error) {
      throw new Error('宿主返回的不是 JSON：' + String(text).slice(0, 200));
    }
    if (parsed && parsed.error) {
      throw new Error(parsed.error);
    }
    return parsed;
  }

  // ---------------------------------------------------------------- 图源注册

  var sources = (global.__dumbcat_sources = global.__dumbcat_sources || {});

  global.registerSource = function registerSource(definition) {
    if (!definition || typeof definition !== 'object') {
      throw new Error('registerSource 需要传入对象');
    }
    var key = String(definition.key || '').trim();
    if (!/^[a-z][a-z0-9_]{1,31}$/.test(key)) {
      throw new Error('图源 key 不合法（小写字母开头，2-32 位小写字母/数字/下划线）：' + key);
    }
    definition.key = key;
    sources[key] = definition;
    return definition;
  };

  /** 宿主调用图源函数：返回值 JSON。 */
  global.__dumbcat_invoke = function dumbcatInvoke(key, fn, payloadJson) {
    var source = sources[key];
    if (!source) {
      throw new Error('图源没有注册：' + key);
    }
    var handler = source[fn];
    if (typeof handler !== 'function') {
      throw new Error('图源 ' + key + ' 不支持 ' + fn);
    }
    var payload = {};
    if (payloadJson) {
      try {
        payload = JSON.parse(payloadJson) || {};
      } catch (error) {
        throw new Error('入参不是合法 JSON：' + String(payloadJson).slice(0, 200));
      }
    }
    var result = handler(payload);
    if (result && typeof result.then === 'function') {
      throw new Error('图源函数 ' + fn + ' 返回了 Promise，JS 图源必须是同步函数');
    }
    return JSON.stringify(result === undefined ? null : result);
  };

  // ---------------------------------------------------------------- 网络

  var DEFAULT_UA =
    'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) ' +
    'Chrome/120.0 Mobile Safari/537.36';

  function request(options) {
    var payload = {
      method: String((options && options.method) || 'GET').toUpperCase(),
      url: String((options && options.url) || ''),
      headers: Object.assign({ 'User-Agent': DEFAULT_UA }, (options && options.headers) || {}),
      body: options && options.body != null ? String(options.body) : '',
      base64: Boolean(options && options.base64)
    };
    if (!payload.url) {
      throw new Error('Network 请求缺少 url');
    }
    var response = host('http', payload) || {};
    var status = Number(response.status || 0);
    if (status >= 400) {
      throw new Error('HTTP ' + status + '：' + payload.url);
    }
    return {
      status: status,
      headers: response.headers || {},
      body: response.body || '',
      base64: response.base64 || ''
    };
  }

  var Network = {
    request: request,
    get: function get(url, headers) {
      return request({ url: url, headers: headers }).body;
    },
    getJson: function getJson(url, headers) {
      var text = request({ url: url, headers: headers }).body;
      try {
        return JSON.parse(text);
      } catch (error) {
        throw new Error('响应不是 JSON：' + String(text).slice(0, 200));
      }
    },
    post: function post(url, body, headers) {
      return request({ method: 'POST', url: url, body: body, headers: headers }).body;
    },
    postJson: function postJson(url, body, headers) {
      var text = request({
        method: 'POST',
        url: url,
        body: typeof body === 'string' ? body : JSON.stringify(body),
        headers: Object.assign({ 'Content-Type': 'application/json' }, headers || {})
      }).body;
      try {
        return JSON.parse(text);
      } catch (error) {
        throw new Error('响应不是 JSON：' + String(text).slice(0, 200));
      }
    },
    getBase64: function getBase64(url, headers) {
      return request({ url: url, headers: headers, base64: true }).base64;
    }
  };
  global.Network = Network;

  // ---------------------------------------------------------------- 工具

  var Entities = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };

  function decodeEntities(text) {
    return String(text == null ? '' : text).replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, function (match, body) {
      if (body.charAt(0) === '#') {
        var code = body.charAt(1).toLowerCase() === 'x'
          ? parseInt(body.slice(2), 16)
          : parseInt(body.slice(1), 10);
        return isFinite(code) && code > 0 ? String.fromCharCode(code) : match;
      }
      var lower = body.toLowerCase();
      return Object.prototype.hasOwnProperty.call(Entities, lower) ? Entities[lower] : match;
    });
  }

  function absUrl(base, url) {
    var target = String(url || '').trim();
    if (!target) {
      return '';
    }
    if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(target)) {
      return target;
    }
    if (target.indexOf('//') === 0) {
      var scheme = String(base || '').split(':')[0] || 'https';
      return scheme + ':' + target;
    }
    var origin = '';
    var match = /^([a-zA-Z][a-zA-Z0-9+.-]*:\/\/[^/]+)/.exec(String(base || ''));
    if (match) {
      origin = match[1];
    }
    if (target.charAt(0) === '/') {
      return origin + target;
    }
    var path = String(base || '');
    var cut = path.replace(/[?#].*$/, '').lastIndexOf('/');
    if (cut < 0) {
      return origin + '/' + target;
    }
    return path.slice(0, cut + 1) + target;
  }

  var Tools = { absUrl: absUrl, decodeEntities: decodeEntities };
  global.Tools = Tools;

  // ---------------------------------------------------------------- 极简 HTML 解析

  function Node(tag, attrs, parent) {
    this.tag = tag;
    this.attrs = attrs || {};
    this.parent = parent || null;
    this.children = [];
    this.parts = [];
  }

  Node.prototype.attr = function attr(name) {
    var value = this.attrs[String(name).toLowerCase()];
    return value === undefined ? '' : value;
  };

  Node.prototype.text = function text() {
    var buffer = [];
    (function walk(node) {
      for (var index = 0; index < node.children.length; index += 1) {
        var child = node.children[index];
        if (typeof child === 'string') {
          buffer.push(child);
        } else {
          walk(child);
        }
      }
    })(this);
    return decodeEntities(buffer.join('')).replace(/[ \t\r\f\v]+/g, ' ').replace(/\s*\n\s*/g, '\n').trim();
  };

  Node.prototype.html = function html() {
    var buffer = [];
    (function walk(node) {
      for (var index = 0; index < node.children.length; index += 1) {
        var child = node.children[index];
        if (typeof child === 'string') {
          buffer.push(child);
          continue;
        }
        var attrs = [];
        for (var name in child.attrs) {
          if (Object.prototype.hasOwnProperty.call(child.attrs, name)) {
            attrs.push(name + '="' + child.attrs[name] + '"');
          }
        }
        buffer.push('<' + child.tag + (attrs.length ? ' ' + attrs.join(' ') : '') + '>');
        walk(child);
        buffer.push('</' + child.tag + '>');
      }
    })(this);
    return buffer.join('');
  };

  Node.prototype.findAll = function findAll(selector) {
    var steps = String(selector || '').trim().split(/\s+/).filter(Boolean);
    var found = [];
    (function walk(node) {
      for (var index = 0; index < node.children.length; index += 1) {
        var child = node.children[index];
        if (typeof child === 'string') {
          continue;
        }
        if (matchesSteps(child, steps, [node])) {
          found.push(child);
        }
        walk(child);
      }
    })(this);
    return found;
  };

  Node.prototype.find = function find(selector) {
    var all = this.findAll(selector);
    return all.length ? all[0] : null;
  };

  /**
   * 取字段：`get('a')` 取文字、`get('a@href')` 取属性，和 JSON 图源的写法一致；
   * 也可以写成 `get('a', 'href')`。选择器为空时取当前节点。
   */
  Node.prototype.get = function get(selector, name) {
    var sel = String(selector || '');
    var attr = name;
    if (attr === undefined) {
      var at = sel.lastIndexOf('@');
      if (at > 0) {
        attr = sel.slice(at + 1);
        sel = sel.slice(0, at);
      }
    }
    var node = sel ? this.find(sel) : this;
    if (!node) {
      return '';
    }
    return attr ? node.attr(attr) : node.text();
  };

  Node.prototype.getText = function getText(selector) {
    var node = this.find(selector);
    return node ? node.text() : '';
  };

  function matchesSteps(node, steps, ancestors) {
    if (!matchesCompound(node, steps[steps.length - 1])) {
      return false;
    }
    var cursor = steps.length - 2;
    var parent = node.parent;
    while (cursor >= 0) {
      var matched = false;
      while (parent) {
        if (matchesCompound(parent, steps[cursor])) {
          matched = true;
          parent = parent.parent;
          break;
        }
        parent = parent.parent;
      }
      if (!matched) {
        return false;
      }
      cursor -= 1;
    }
    return true;
  }

  function matchesCompound(node, compound) {
    if (!compound) {
      return false;
    }
    var rest = compound;
    var tagMatch = /^[a-zA-Z][\w-]*/.exec(rest);
    if (tagMatch) {
      if (node.tag !== tagMatch[0].toLowerCase()) {
        return false;
      }
      rest = rest.slice(tagMatch[0].length);
    }
    var pattern = /([#.])([\w-]+)|\[([\w:-]+)(?:([~^$*|]?=)("[^"]*"|'[^']*'|[^\]]*))?\]/g;
    var match;
    while ((match = pattern.exec(rest)) !== null) {
      if (match[1] === '#') {
        if (node.attr('id') !== match[2]) {
          return false;
        }
      } else if (match[1] === '.') {
        var classes = (' ' + node.attr('class') + ' ').replace(/\s+/g, ' ');
        if (classes.indexOf(' ' + match[2] + ' ') < 0) {
          return false;
        }
      } else {
        var attrName = String(match[3]).toLowerCase();
        var actual = node.attrs[attrName];
        if (actual === undefined) {
          return false;
        }
        if (!match[4]) {
          continue;
        }
        var expected = String(match[5] || '').replace(/^["']|["']$/g, '');
        switch (match[4]) {
          case '=':
            if (actual !== expected) return false;
            break;
          case '*=':
            if (actual.indexOf(expected) < 0) return false;
            break;
          case '^=':
            if (actual.indexOf(expected) !== 0) return false;
            break;
          case '$=':
            if (actual.slice(-expected.length) !== expected) return false;
            break;
          default:
            break;
        }
      }
    }
    return true;
  }

  var VOID_TAGS = { br: 1, hr: 1, img: 1, input: 1, meta: 1, link: 1, source: 1, area: 1, base: 1, col: 1, embed: 1, param: 1, track: 1, wbr: 1 };
  var RAW_TAGS = { script: 1, style: 1, textarea: 1, title: 1 };

  function parseHtml(source) {
    var html = String(source == null ? '' : source);
    var root = new Node('#root', {}, null);
    var stack = [root];
    var index = 0;

    function current() {
      return stack[stack.length - 1];
    }

    function pushText(value) {
      if (!value) {
        return;
      }
      var node = current();
      node.children.push(value);
    }

    while (index < html.length) {
      var open = html.indexOf('<', index);
      if (open < 0) {
        pushText(html.slice(index));
        break;
      }
      if (open > index) {
        pushText(html.slice(index, open));
      }
      if (html.charAt(open + 1) === '!') {
        var closeMark = html.indexOf('>', open);
        index = closeMark < 0 ? html.length : closeMark + 1;
        continue;
      }
      var closeTag = /^<\/([a-zA-Z][\w:-]*)\s*>/.exec(html.slice(open));
      if (closeTag) {
        var closeName = closeTag[1].toLowerCase();
        for (var depth = stack.length - 1; depth > 0; depth -= 1) {
          if (stack[depth].tag === closeName) {
            stack.length = depth;
            break;
          }
        }
        index = open + closeTag[0].length;
        continue;
      }
      var openTag = /^<([a-zA-Z][\w:-]*)((?:"[^"]*"|'[^']*'|[^>"'])*?)(\/?)>/.exec(html.slice(open));
      if (!openTag) {
        pushText('<');
        index = open + 1;
        continue;
      }
      var name = openTag[1].toLowerCase();
      var node = new Node(name, parseAttrs(openTag[2]), current());
      current().children.push(node);
      index = open + openTag[0].length;
      if (openTag[3] === '/' || VOID_TAGS[name]) {
        continue;
      }
      if (RAW_TAGS[name]) {
        var endMark = html.toLowerCase().indexOf('</' + name, index);
        if (endMark < 0) {
          node.children.push(html.slice(index));
          index = html.length;
        } else {
          node.children.push(html.slice(index, endMark));
          var skip = html.indexOf('>', endMark);
          index = skip < 0 ? html.length : skip + 1;
        }
        continue;
      }
      stack.push(node);
    }
    return root;
  }

  function parseAttrs(text) {
    var attrs = {};
    var pattern = /([\w:.-]+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s"'>]+))?/g;
    var match;
    while ((match = pattern.exec(text || '')) !== null) {
      var value = match[2];
      if (value === undefined) {
        attrs[match[1].toLowerCase()] = '';
      } else {
        attrs[match[1].toLowerCase()] = decodeEntities(value.replace(/^["']|["']$/g, ''));
      }
    }
    return attrs;
  }

  global.Html = {
    parse: function parse(html) {
      return parseHtml(html);
    }
  };

  // ---------------------------------------------------------------- 文本 / JSON 工具

  global.Text = {
    between: function between(text, start, end) {
      var source = String(text == null ? '' : text);
      var from = source.indexOf(start);
      if (from < 0) {
        return '';
      }
      from += String(start).length;
      if (end == null) {
        return source.slice(from);
      }
      var to = source.indexOf(end, from);
      return to < 0 ? '' : source.slice(from, to);
    },
    after: function after(text, start) {
      return global.Text.between(text, start, null);
    },
    match: function match(text, pattern, flags) {
      var re = new RegExp(pattern, flags || '');
      var found = re.exec(String(text == null ? '' : text));
      return found ? found[1] !== undefined ? found[1] : found[0] : '';
    },
    matchAll: function matchAll(text, pattern, flags) {
      var re = new RegExp(pattern, flags || 'g');
      var source = String(text == null ? '' : text);
      var results = [];
      var found;
      while ((found = re.exec(source)) !== null) {
        results.push(found[1] !== undefined ? found[1] : found[0]);
        if (found[0] === '') {
          re.lastIndex += 1;
        }
      }
      return results;
    },
    trim: function trim(text) {
      return String(text == null ? '' : text).trim();
    }
  };

  global.Json = {
    path: function path(value, path) {
      var parts = String(path || '').split('.').filter(function (part) {
        return part !== '';
      });
      var cursor = value;
      for (var index = 0; index < parts.length; index += 1) {
        if (cursor === null || cursor === undefined) {
          return undefined;
        }
        cursor = cursor[parts[index]];
      }
      return cursor;
    }
  };

  // ---------------------------------------------------------------- 加密 / 日志

  function digest(algo, data) {
    var result = host('digest', { algo: algo, data: String(data == null ? '' : data) });
    return result && result.value ? result.value : result;
  }

  function hmac(algo, key, data) {
    var result = host('hmac', {
      algo: algo,
      key: String(key == null ? '' : key),
      data: String(data == null ? '' : data)
    });
    return result && result.value ? result.value : result;
  }

  global.Crypto = {
    md5: function md5(data) {
      return digest('md5', data);
    },
    sha1: function sha1(data) {
      return digest('sha1', data);
    },
    sha256: function sha256(data) {
      return digest('sha256', data);
    },
    hmacSha256: function hmacSha256(key, data) {
      return hmac('sha256', key, data);
    },
    hmacSha1: function hmacSha1(key, data) {
      return hmac('sha1', key, data);
    }
  };

  global.console = {
    log: function log() {
      host('log', { message: Array.prototype.map.call(arguments, String).join(' ') });
    },
    warn: function warn() {
      host('log', { message: '[warn] ' + Array.prototype.map.call(arguments, String).join(' ') });
    },
    error: function error() {
      host('log', { message: '[error] ' + Array.prototype.map.call(arguments, String).join(' ') });
    }
  };

  // ---------------------------------------------------------------- ES 补丁

  if (typeof global.TextEncoder !== 'function') {
    global.TextEncoder = function TextEncoder() {};
  }
}) (typeof globalThis !== 'undefined' ? globalThis : this);
