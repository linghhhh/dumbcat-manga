package com.dumbcat.manga;

import android.util.Base64;
import android.util.Log;

import com.quickjs.JSObject;
import com.quickjs.JavaCallback;
import com.quickjs.JSFunction;
import com.quickjs.JSContext;
import com.quickjs.QuickJS;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * 内嵌 QuickJS：跑「JS 图源」插件。
 *
 * <p>Python 侧的 {@code dumbcat.plugins.js_source} 通过 Chaquopy 调这里的静态方法，
 * 一次调用 = 一次 JS 函数执行，入参和返回值都是 JSON 字符串，跨语言只传文本。
 *
 * <p>QuickJS 是单线程的，所有 JS 操作都排进同一个 executor；宿主能力（网络、缓存、
 * 摘要）以 {@code Native.call(name, payloadJson)} 一个入口暴露给 JS，prelude.js 在
 * JS 侧把它包装成 {@code Network.get/post}、{@code console}、{@code Crypto} 等。
 *
 * <p>脚本约定：文件里调用 {@code registerSource({key, name, ...})} 注册一个图源对象，
 * 之后 {@link #call} 按函数名调用它，返回值的 JSON 交给 Python 归一。
 */
public final class JsRuntime {

    private static final String TAG = "DumbCatJs";
    private static final long CALL_TIMEOUT_MS = 60_000L;
    private static final int CACHE_LIMIT = 200;

    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor(runnable -> {
        Thread thread = new Thread(runnable, "dumbcat-js");
        thread.setDaemon(true);
        return thread;
    });

    private static QuickJS runtime;
    private static JSContext context;
    /** key -> 源脚本里 registerSource 的元信息（JSON 字符串） */
    private static final Map<String, String> META = new ConcurrentHashMap<>();
    private static final Map<String, String> CACHE = new LinkedHashMap<String, String>(64, 0.75f, true) {
        @Override
        protected boolean removeEldestEntry(Map.Entry<String, String> eldest) {
            return size() > CACHE_LIMIT;
        }
    };

    private JsRuntime() {
    }

    /** 宿主里是否有可用的 JS 引擎（桌面版/裁剪版返回 false）。 */
    public static boolean available() {
        return true;
    }

    /**
     * 载入一个 JS 图源。
     *
     * @return 源元信息 JSON：{@code {"key","name","version","baseUrl","icon","nsfw"}}
     */
    public static String load(final String key, final String script) {
        return submit("载入 JS 图源 " + key, () -> {
            JSContext ctx = context();
            ctx.executeVoidScript(script, key + ".js");
            Object meta = ctx.executeScript(
                "(function(){var s=__dumbcat_sources[" + jsonString(key) + "];"
                    + "if(!s)return null;return JSON.stringify({key:s.key,name:s.name||s.key,"
                    + "version:s.version||'1.0.0',baseUrl:s.baseUrl||s.base_url||'',"
                    + "icon:s.icon||'🧩',nsfw:!!s.nsfw,pageSize:s.pageSize||30});})()",
                "__meta.js"
            );
            if (meta == null) {
                throw new IllegalStateException("脚本里没有 registerSource({key: '" + key + "'})");
            }
            String json = String.valueOf(meta);
            META.put(key, json);
            return json;
        });
    }

    /**
     * 调用图源的某个函数。
     *
     * @param fn      函数名，如 {@code search} / {@code detail} / {@code pages}
     * @param payload 入参 JSON（对象）
     * @return 返回值 JSON；函数不存在或抛错时抛 RuntimeException
     */
    public static String call(final String key, final String fn, final String payload) {
        if (!META.containsKey(key)) {
            throw new IllegalStateException("JS 图源没有载入：" + key);
        }
        return submit("调用 JS 图源 " + key + "." + fn, () -> {
            JSContext ctx = context();
            ctx.set("__dumbcat_call_key", key);
            ctx.set("__dumbcat_call_fn", fn);
            ctx.set("__dumbcat_call_payload", payload == null ? "{}" : payload);
            Object result = ctx.executeScript(
                "(function(){return __dumbcat_invoke(__dumbcat_call_key,"
                    + "__dumbcat_call_fn,__dumbcat_call_payload);})()",
                "__call.js"
            );
            if (result == null) {
                return "null";
            }
            if (result instanceof String) {
                return (String) result;
            }
            return String.valueOf(result);
        });
    }

    /** 卸载一个图源（插件禁用/删除时调用，防止内存里的旧脚本继续跑）。 */
    public static void unload(final String key) {
        META.remove(key);
        try {
            submit("卸载 JS 图源 " + key, () -> {
                context().executeVoidScript(
                    "delete __dumbcat_sources[" + jsonString(key) + "];", "__unload.js"
                );
                return "1";
            });
        } catch (RuntimeException error) {
            Log.w(TAG, "卸载失败：" + error.getMessage());
        }
    }

    // ------------------------------------------------------------------ 内部

    private interface Job {
        String run() throws Exception;
    }

    private static String submit(String what, Job job) {
        Future<String> future = EXECUTOR.submit(() -> {
            try {
                return job.run();
            } catch (RuntimeException error) {
                // QuickJS 抛的是非受检异常，统一成带可读信息的 IllegalStateException
                throw new IllegalStateException(cleanMessage(error.getMessage()), error);
            }
        });
        try {
            return future.get(CALL_TIMEOUT_MS, TimeUnit.MILLISECONDS);
        } catch (TimeoutException error) {
            future.cancel(true);
            throw new IllegalStateException(what + "超时（" + (CALL_TIMEOUT_MS / 1000) + " 秒）");
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException(what + "被中断");
        } catch (java.util.concurrent.ExecutionException error) {
            Throwable cause = error.getCause() == null ? error : error.getCause();
            throw new IllegalStateException(what + "失败：" + cleanMessage(cause.getMessage()), cause);
        }
    }

    private static String cleanMessage(String message) {
        if (message == null || message.isEmpty()) {
            return "脚本执行出错";
        }
        // QuickJS 的异常信息会带上堆栈，界面只留第一行
        int cut = message.indexOf('\n');
        return (cut > 0 ? message.substring(0, cut) : message).trim();
    }

    private static JSContext context() throws Exception {
        if (context == null) {
            closeQuietly();
            runtime = QuickJS.createRuntimeWithEventQueue();
            context = runtime.createContext();
            context.set("Native", nativeBridge(context));
            context.executeVoidScript(prelude(), "prelude.js");
            META.clear();
        }
        return context;
    }

    private static JSObject nativeBridge(JSContext ctx) {
        JSObject object = new JSObject(ctx);
        JavaCallback callback = (receiver, args) -> {
            String name = args.length() > 0 ? args.getString(0) : "";
            String payload = args.length() > 1 ? args.getString(1) : "{}";
            try {
                return hostCall(name, payload == null ? "{}" : payload);
            } catch (Exception error) {
                Log.w(TAG, "原生调用失败 " + name + "：" + error);
                return "{\"error\":" + jsonString(String.valueOf(error.getMessage())) + "}";
            }
        };
        object.set("call", new JSFunction(ctx, callback));
        return object;
    }

    /** JS -> Java 的能力入口，全部返回 JSON 字符串。 */
    private static String hostCall(String name, String payloadJson) throws Exception {
        JSONObject payload = new JSONObject(payloadJson);
        switch (name) {
            case "http":
                return http(payload);
            case "log":
                Log.i(TAG, payload.optString("message"));
                return "{\"ok\":true}";
            case "cache.get":
                return jsonString(cacheGet(payload.optString("key")));
            case "cache.set":
                cacheSet(payload.optString("key"), payload.optString("value"));
                return "{\"ok\":true}";
            case "digest":
                return jsonString(digest(payload.optString("algo"), payload.optString("data")));
            case "hmac":
                return jsonString(hmac(payload.optString("algo"), payload.optString("key"),
                    payload.optString("data")));
            default:
                throw new IllegalArgumentException("未知的原生调用：" + name);
        }
    }

    private static String http(JSONObject payload) throws Exception {
        String method = payload.optString("method", "GET").toUpperCase();
        String target = payload.optString("url");
        URL url = new URL(target);
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod(method);
        connection.setConnectTimeout(20_000);
        connection.setReadTimeout(30_000);
        connection.setInstanceFollowRedirects(true);
        connection.setRequestProperty(
            "User-Agent",
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
                + "Chrome/120.0 Mobile Safari/537.36"
        );
        JSONObject headers = payload.optJSONObject("headers");
        if (headers != null) {
            for (java.util.Iterator<String> it = headers.keys(); it.hasNext(); ) {
                String key = it.next();
                connection.setRequestProperty(key, headers.optString(key));
            }
        }
        String body = payload.optString("body", "");
        if (!body.isEmpty() && !"GET".equals(method) && !"HEAD".equals(method)) {
            connection.setDoOutput(true);
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            connection.setRequestProperty("Content-Length", String.valueOf(bytes.length));
            try (OutputStream out = connection.getOutputStream()) {
                out.write(bytes);
            }
        }
        int status = connection.getResponseCode();
        InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
        byte[] data = stream == null ? new byte[0] : readAll(stream);
        JSONObject result = new JSONObject();
        result.put("status", status);
        if (payload.optBoolean("base64", false)) {
            result.put("base64", Base64.encodeToString(data, Base64.NO_WRAP));
        } else {
            String charset = "UTF-8";
            String contentType = connection.getContentType();
            if (contentType != null && contentType.toLowerCase().contains("charset=")) {
                charset = contentType.substring(contentType.toLowerCase().indexOf("charset=") + 8)
                    .split("[;\\s]")[0];
            }
            result.put("body", new String(data, charset));
        }
        JSONObject outHeaders = new JSONObject();
        for (Map.Entry<String, java.util.List<String>> entry : connection.getHeaderFields().entrySet()) {
            if (entry.getKey() != null && !entry.getValue().isEmpty()) {
                outHeaders.put(entry.getKey(), entry.getValue().get(0));
            }
        }
        result.put("headers", outHeaders);
        connection.disconnect();
        return result.toString();
    }

    private static byte[] readAll(InputStream stream) throws Exception {
        try (InputStream input = stream; ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            int read;
            while ((read = input.read(buffer)) > 0) {
                out.write(buffer, 0, read);
            }
            return out.toByteArray();
        }
    }

    private static synchronized String cacheGet(String key) {
        return CACHE.get(key);
    }

    private static synchronized void cacheSet(String key, String value) {
        if (key == null || key.isEmpty()) {
            return;
        }
        if (value == null) {
            CACHE.remove(key);
        } else {
            CACHE.put(key, value);
        }
    }

    private static String digest(String algo, String data) throws Exception {
        MessageDigest md = MessageDigest.getInstance(normalizeDigest(algo));
        return toHex(md.digest(data.getBytes(StandardCharsets.UTF_8)));
    }

    private static String hmac(String algo, String key, String data) throws Exception {
        Mac mac = Mac.getInstance(normalizeMac(algo));
        mac.init(new SecretKeySpec(key.getBytes(StandardCharsets.UTF_8), normalizeMac(algo)));
        return toHex(mac.doFinal(data.getBytes(StandardCharsets.UTF_8)));
    }

    private static String normalizeDigest(String algo) {
        switch (algo == null ? "" : algo.toLowerCase()) {
            case "md5":
                return "MD5";
            case "sha1":
                return "SHA-1";
            default:
                return "SHA-256";
        }
    }

    private static String normalizeMac(String algo) {
        switch (algo == null ? "" : algo.toLowerCase()) {
            case "sha1":
                return "HmacSHA1";
            case "md5":
                return "HmacMD5";
            default:
                return "HmacSHA256";
        }
    }

    private static String toHex(byte[] bytes) {
        StringBuilder builder = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) {
            builder.append(String.format("%02x", value));
        }
        return builder.toString();
    }

    private static String prelude() {
        String cached = PRELUDE;
        if (cached != null) {
            return cached;
        }
        try {
            // prelude 随 assets 打包，Python 启动时会把绝对路径写进系统属性
            String path = System.getProperty("dumbcat.js.prelude", "");
            if (!path.isEmpty()) {
                java.io.File file = new java.io.File(path);
                if (file.isFile()) {
                    try (InputStream input = new java.io.FileInputStream(file)) {
                        PRELUDE = new String(readAll(input), StandardCharsets.UTF_8);
                        return PRELUDE;
                    }
                }
            }
        } catch (Exception error) {
            Log.w(TAG, "读取 prelude 失败：" + error.getMessage());
        }
        throw new IllegalStateException("找不到 prelude.js，JS 图源不可用");
    }

    private static volatile String PRELUDE;

    /** 供 Python 在启动时登记 prelude.js 的绝对路径。 */
    public static void setPreludePath(String path) {
        System.setProperty("dumbcat.js.prelude", path == null ? "" : path);
        PRELUDE = null;
    }

    private static void closeQuietly() {
        try {
            if (context != null) {
                context.close();
            }
        } catch (Exception ignored) {
            // 关闭失败不影响下次重建
        }
        try {
            if (runtime != null) {
                runtime.close();
            }
        } catch (Exception ignored) {
            // 同上
        }
        context = null;
        runtime = null;
    }

    private static String jsonString(String value) {
        if (value == null) {
            return "null";
        }
        StringBuilder builder = new StringBuilder(value.length() + 16);
        builder.append('"');
        for (int index = 0; index < value.length(); index++) {
            char ch = value.charAt(index);
            switch (ch) {
                case '"':
                    builder.append("\\\"");
                    break;
                case '\\':
                    builder.append("\\\\");
                    break;
                case '\n':
                    builder.append("\\n");
                    break;
                case '\r':
                    builder.append("\\r");
                    break;
                case '\t':
                    builder.append("\\t");
                    break;
                default:
                    if (ch < 0x20) {
                        builder.append(String.format("\\u%04x", (int) ch));
                    } else {
                        builder.append(ch);
                    }
            }
        }
        builder.append('"');
        return builder.toString();
    }
}
