package com.dumbcat.manga;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

/**
 * 后台下载的前台服务：给系统一个「正在干活」的信号，切到别的应用也不会被冻住。
 * 任务本身由 Python 侧的 TaskManager 跑，这里只负责通知栏上的那一小块。
 */
public final class DownloadService extends Service {

    private static final String TAG = "DumbCat";
    private static final String CHANNEL_ID = "dumbcat-downloads";
    private static final int NOTIFICATION_ID = 1001;

    private static volatile DownloadService sInstance = null;
    private static volatile String sText = "正在下载…";
    private static volatile int sDone = 0;
    private static volatile int sTotal = 0;

    public static boolean start(Context context) {
        if (context == null) return false;
        try {
            Intent intent = new Intent(context, DownloadService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent);
            } else {
                context.startService(intent);
            }
            return true;
        } catch (Exception exc) {
            Log.w(TAG, "后台下载服务启动失败：" + exc.getMessage());
            return false;
        }
    }

    public static void stop(Context context) {
        if (context == null) return;
        try {
            context.stopService(new Intent(context, DownloadService.class));
        } catch (Exception exc) {
            Log.w(TAG, "后台下载服务停止失败：" + exc.getMessage());
        }
    }

    /** 更新通知里的进度；服务没起来时只记下数字，等它起来再画。 */
    public static void update(int done, int total, String text) {
        sDone = Math.max(0, done);
        sTotal = Math.max(0, total);
        if (text != null && !text.isEmpty()) sText = text;
        DownloadService service = sInstance;
        if (service != null) service.refresh();
    }

    @Override
    public void onCreate() {
        super.onCreate();
        sInstance = this;
        createChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        try {
            startForeground(NOTIFICATION_ID, buildNotification());
        } catch (Exception exc) {
            // 少数 ROM 在通知被禁用时会在这里抛异常。吞掉继续会让系统判定
            // 「startForegroundService 没在 5 秒内 startForeground」直接崩，
            // 所以干脆停掉服务：下载本身在 Python 线程里跑，不受影响。
            Log.w(TAG, "启动前台通知失败，停止后台服务：" + exc.getMessage());
            stopSelf(startId);
            return START_NOT_STICKY;
        }
        // 任务由 Python 侧持久化恢复，被杀掉不用系统重启服务
        return START_NOT_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        sInstance = null;
        super.onDestroy();
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        try {
            NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (manager == null) return;
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID, "后台下载", NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("下载漫画时显示进度");
            channel.setShowBadge(false);
            manager.createNotificationChannel(channel);
        } catch (Exception exc) {
            Log.w(TAG, "创建通知渠道失败：" + exc.getMessage());
        }
    }

    private Notification buildNotification() {
        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        Intent open = new Intent(this, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) flags |= PendingIntent.FLAG_IMMUTABLE;
        PendingIntent pending = PendingIntent.getActivity(this, 0, open, flags);
        String title = sTotal > 0
                ? String.format("下载中 %d/%d", sDone, sTotal)
                : "呆猫漫画正在下载";
        builder.setSmallIcon(android.R.drawable.stat_sys_download)
                .setContentTitle(title)
                .setContentText(sText)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setContentIntent(pending);
        if (sTotal > 0) {
            builder.setProgress(sTotal, sDone, false);
        } else {
            builder.setProgress(0, 0, true);
        }
        return builder.build();
    }

    private void refresh() {
        try {
            NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (manager != null) manager.notify(NOTIFICATION_ID, buildNotification());
        } catch (Exception exc) {
            Log.w(TAG, "更新下载通知失败：" + exc.getMessage());
        }
    }
}
