package com.dumbcat.manga;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import org.json.JSONArray;
import org.json.JSONObject;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * 手机上的「登录窗口」。
 *
 * <p>桌面版登录靠 Playwright 里的 Chromium 抓 Cookie；安卓上没有 Chromium，
 * 所以这里用一个真实的 WebView 让用户登录官方站点，登录完成后由
 * {@link #readCookies(String)} 通过 {@link CookieManager} 把该域名下的 Cookie
 * 读出来交给 Python 侧的会话。
 *
 * <p>只做三件事：打开页面、让用户点「关闭」，然后读 Cookie。不注入脚本、
 * 不读取页面内容。
 */
public class LoginActivity extends Activity {
    public static final String EXTRA_URL = "com.dumbcat.manga.LOGIN_URL";

    private static final String CHROME_UA =
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
                    + "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36";

    private WebView webView;
    private ProgressBar progress;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        String url = getIntent() == null ? "" : getIntent().getStringExtra(EXTRA_URL);
        if (TextUtils.isEmpty(url)) {
            finish();
            return;
        }

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.WHITE);
        root.setFitsSystemWindows(true);

        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(14), dp(10), dp(10), dp(10));
        bar.setBackgroundColor(Color.parseColor("#F2F6FD"));

        TextView title = new TextView(this);
        title.setText("登录后点「完成」返回");
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        title.setTextColor(Color.parseColor("#233043"));
        LinearLayout.LayoutParams titleParams =
                new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        bar.addView(title, titleParams);

        Button done = new Button(this);
        done.setText("完成");
        done.setAllCaps(false);
        done.setOnClickListener(v -> finish());
        bar.addView(done, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(bar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        progress.setVisibility(ProgressBar.GONE);
        root.addView(progress, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(3)));

        webView = new WebView(this);
        LinearLayout.LayoutParams webParams =
                new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f);
        root.addView(webView, webParams);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setSupportZoom(true);
        settings.setBuiltInZoomControls(true);
        settings.setDisplayZoomControls(false);
        settings.setUserAgentString(CHROME_UA);

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, true);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, android.webkit.WebResourceRequest request) {
                return false;
            }
        });
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                if (progress == null) return;
                progress.setVisibility(newProgress >= 100 ? ProgressBar.GONE : ProgressBar.VISIBLE);
                progress.setProgress(newProgress);
            }
        });

        setContentView(root);
        webView.loadUrl(url);
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        try {
            CookieManager.getInstance().flush();
        } catch (Throwable ignored) {
        }
        if (webView != null) {
            try {
                webView.stopLoading();
                webView.destroy();
            } catch (Throwable ignored) {
            }
            webView = null;
        }
        super.onDestroy();
    }

    private int dp(int value) {
        return Math.round(getResources().getDisplayMetrics().density * value);
    }

    /** 打开登录窗口（从 Python 侧调用）。 */
    public static void open(Context context, String url) {
        Intent intent = new Intent(context, LoginActivity.class);
        intent.putExtra(EXTRA_URL, url);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        context.startActivity(intent);
    }

    /**
     * 读取这些 URL 能看到的 Cookie，返回 JSON 数组 {@code [{"name","value","domain"}]}。
     * 把多个 URL 传进来是为了覆盖不同路径/子域名的登录 Cookie。
     */
    public static String readCookies(String... urls) {
        final String[] result = new String[]{"[]"};
        if (urls == null || urls.length == 0) return result[0];
        final CountDownLatch latch = new CountDownLatch(1);
        new Handler(Looper.getMainLooper()).post(() -> {
            try {
                CookieManager manager = CookieManager.getInstance();
                manager.flush();
                Map<String, String> merged = new LinkedHashMap<>();
                for (String url : urls) {
                    if (TextUtils.isEmpty(url)) continue;
                    String header = manager.getCookie(url);
                    if (TextUtils.isEmpty(header)) continue;
                    for (String part : header.split(";")) {
                        String item = part.trim();
                        if (item.isEmpty()) continue;
                        int eq = item.indexOf('=');
                        if (eq <= 0) continue;
                        merged.put(item.substring(0, eq), item.substring(eq + 1));
                    }
                }
                JSONArray array = new JSONArray();
                for (Map.Entry<String, String> entry : merged.entrySet()) {
                    JSONObject row = new JSONObject();
                    row.put("name", entry.getKey());
                    row.put("value", entry.getValue());
                    array.put(row);
                }
                result[0] = array.toString();
            } catch (Throwable ignored) {
            } finally {
                latch.countDown();
            }
        });
        try {
            latch.await(6, TimeUnit.SECONDS);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
        return result[0];
    }
}
