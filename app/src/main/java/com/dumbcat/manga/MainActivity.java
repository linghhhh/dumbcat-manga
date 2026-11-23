package com.dumbcat.manga;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.drawable.Drawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

import java.io.InputStream;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/**
 * 呆猫漫画安卓壳：后台线程启动内嵌的 Python FastAPI 服务，再用 WebView 加载本地页面。
 */
public class MainActivity extends Activity {

    private static final String TAG = "DumbCat";
    private static final int REQ_FILE = 1001;
    private static boolean sPythonStarted = false;
    /** Python 服务起来后的完整地址；Activity 被重建时直接拿它加载，不用再等。 */
    private static volatile String sServerUrl = null;

    private WebView web;
    private View splash;
    private TextView tipView;
    private ProgressBar progress;
    private ValueCallback<Uri[]> filePathCallback;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        Window window = getWindow();
        // 和网页顶栏同色，系统栏不抢戏；图标用深色
        window.setStatusBarColor(Color.parseColor("#F7FAFF"));
        window.setNavigationBarColor(Color.parseColor("#F7FAFF"));
        window.getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);

        buildViews();

        if (sServerUrl != null) {
            // 服务已经在跑（Activity 被系统重建过），直接加载
            web.loadUrl(sServerUrl);
        } else {
            if (!sPythonStarted) {
                sPythonStarted = true;
                Thread boot = new Thread(this::bootPython, "dumbcat-boot");
                boot.setDaemon(true);
                boot.start();
            }
            // 服务可能正由上一个（已销毁的）Activity 的线程启动，这里轮询等它就绪
            pollServer();
        }
    }

    private void buildViews() {
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#F7FAFF"));
        // targetSdk 35 强制贴边显示，这里让根视图自己让出状态栏/导航栏/输入法
        root.setFitsSystemWindows(true);

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#F7FAFF"));
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        LinearLayout splashBox = new LinearLayout(this);
        splashBox.setOrientation(LinearLayout.VERTICAL);
        splashBox.setGravity(Gravity.CENTER);
        splashBox.setBackgroundColor(Color.parseColor("#F7FAFF"));

        // 启动图直接用网页端同一张 loading.webp（呆猫和 doro 一起看书），
        // 原生启动页和网页启动页是同一张图，切换不跳。
        ImageView logo = new ImageView(this);
        try (InputStream in = getAssets().open("appdata/web/assets/loading.webp")) {
            logo.setImageDrawable(Drawable.createFromStream(in, "loading.webp"));
        } catch (Exception ignored) {
            logo.setImageResource(R.mipmap.ic_launcher);
        }
        logo.setScaleType(ImageView.ScaleType.FIT_CENTER);
        logo.setAdjustViewBounds(true);
        LinearLayout.LayoutParams logoParams = new LinearLayout.LayoutParams(
                dp(240), ViewGroup.LayoutParams.WRAP_CONTENT);
        logoParams.bottomMargin = dp(10);
        splashBox.addView(logo, logoParams);

        TextView title = new TextView(this);
        title.setText("呆猫漫画");
        title.setTextColor(Color.parseColor("#1F2B45"));
        title.setTextSize(24f);
        title.setGravity(Gravity.CENTER);
        splashBox.addView(title);

        tipView = new TextView(this);
        tipView.setText("正在准备书架…");
        tipView.setTextColor(Color.parseColor("#6B7689"));
        tipView.setTextSize(14f);
        tipView.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams tipParams =
                new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                        ViewGroup.LayoutParams.WRAP_CONTENT);
        tipParams.topMargin = dp(8);
        splashBox.addView(tipView, tipParams);

        progress = new ProgressBar(this);
        LinearLayout.LayoutParams pbParams = new LinearLayout.LayoutParams(dp(34), dp(34));
        pbParams.topMargin = dp(22);
        splashBox.addView(progress, pbParams);

        root.addView(splashBox, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        splash = splashBox;

        setContentView(root);

        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setSupportZoom(false);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            settings.setSafeBrowsingEnabled(false);
        }

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                String host = uri.getHost() == null ? "" : uri.getHost();
                if (host.equals("127.0.0.1") || host.equals("localhost")) {
                    return false;
                }
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, uri));
                } catch (Exception ignored) {
                    // 没有浏览器可打开时就算了
                }
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                hideSplash();
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (filePathCallback != null) {
                    filePathCallback.onReceiveValue(null);
                }
                filePathCallback = callback;
                try {
                    Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    intent.setType("*/*");
                    startActivityForResult(
                            Intent.createChooser(intent, "选择文件"), REQ_FILE);
                    return true;
                } catch (Exception exc) {
                    filePathCallback = null;
                    return false;
                }
            }
        });
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    /**
     * Chaquopy 首次启动会把标准库解压到 files/chaquopy。如果上一次启动在解压中途崩了
     * （例如 Python.start 里 abort），留下的半截文件会让之后每次启动都崩，只能清数据。
     * 这里用一个完成标记：标记缺失但目录已存在，说明上次没解压完，先删干净再来。
     */
    private void preparePythonFiles() {
        java.io.File files = getFilesDir();
        java.io.File done = new java.io.File(files, ".chaquopy-ok");
        java.io.File cq = new java.io.File(files, "chaquopy");
        if (!done.exists() && cq.exists()) {
            Log.w(TAG, "上次解压没完成，清掉重来：" + cq);
            deleteRecursive(cq);
        }
    }

    private static void deleteRecursive(java.io.File target) {
        if (target == null || !target.exists()) return;
        java.io.File[] children = target.listFiles();
        if (children != null) {
            for (java.io.File child : children) deleteRecursive(child);
        }
        //noinspection ResultOfMethodCallIgnored
        target.delete();
    }

    private void markPythonFilesReady() {
        try {
            java.io.File done = new java.io.File(getFilesDir(), ".chaquopy-ok");
            if (!done.exists()) {
                //noinspection ResultOfMethodCallIgnored
                done.createNewFile();
            }
        } catch (Exception ignored) {
        }
    }

    private void bootPython() {
        String info;
        try {
            preparePythonFiles();
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(this));
            }
            Log.i(TAG, "Python started");

            String filesDir = getFilesDir().getAbsolutePath();
            String cacheDir = getCacheDir().getAbsolutePath();
            String apkPath = getPackageCodePath();
            String externalDir = "";
            try {
                java.io.File ext = getExternalFilesDir(null);
                if (ext != null) {
                    externalDir = ext.getAbsolutePath();
                }
            } catch (Exception ignored) {
            }

            PyObject module = Python.getInstance().getModule("android_main");
            info = module.callAttr("start", filesDir, cacheDir, apkPath, externalDir)
                    .toString();
            Log.i(TAG, "server ready: " + info);
        } catch (Throwable exc) {
            Log.e(TAG, "Python 启动失败", exc);
            final String message = String.valueOf(exc.getMessage());
            runOnUiThread(() -> {
                if (web == null || isFinishing() || isDestroyed()) {
                    return;
                }
                if (progress != null) {
                    progress.setVisibility(View.GONE);
                }
                if (tipView != null) {
                    tipView.setText("启动失败：" + message
                            + "\n请查看日志或重装应用后再试。");
                }
            });
            return;
        }

        String[] parts = info.trim().split("\\|");
        if (parts.length < 2) {
            runOnUiThread(() -> {
                if (web != null && tipView != null) {
                    tipView.setText("启动失败：服务没有返回地址");
                }
            });
            return;
        }
        final String url = "http://127.0.0.1:" + parts[0] + "/?token=" + parts[1];
        markPythonFilesReady();
        sServerUrl = url;
        Log.i(TAG, "loading " + url);
        // 页面加载交给 onCreate 里启动的轮询，避免 Activity 已销毁时的空指针
    }

    /** 等 Python 服务就绪；服务地址由后台线程写进 sServerUrl。 */
    private void pollServer() {
        final Handler handler = new Handler(Looper.getMainLooper());
        final long deadline = System.currentTimeMillis() + 120_000L;
        handler.postDelayed(new Runnable() {
            @Override
            public void run() {
                if (web == null || isFinishing() || isDestroyed()) {
                    return;
                }
                if (sServerUrl != null) {
                    web.loadUrl(sServerUrl);
                    return;
                }
                if (System.currentTimeMillis() > deadline) {
                    if (progress != null) {
                        progress.setVisibility(View.GONE);
                    }
                    if (tipView != null) {
                        tipView.setText("启动超时，请退出后重试");
                    }
                    return;
                }
                handler.postDelayed(this, 250);
            }
        }, 250);
    }

    private void hideSplash() {
        View view = splash;
        if (view == null || view.getVisibility() != View.VISIBLE) {
            return;
        }
        view.animate().alpha(0f).setDuration(220).withEndAction(() -> {
            view.setVisibility(View.GONE);
        }).start();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQ_FILE) {
            if (filePathCallback == null) {
                super.onActivityResult(requestCode, resultCode, data);
                return;
            }
            Uri[] results = null;
            if (resultCode == RESULT_OK && data != null && data.getData() != null) {
                results = new Uri[]{data.getData()};
            }
            filePathCallback.onReceiveValue(results);
            filePathCallback = null;
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
