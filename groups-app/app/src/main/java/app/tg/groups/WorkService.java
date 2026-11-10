package app.tg.groups;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

/**
 * Keeps the task running after the user leaves the app, without a floating window.
 * A quiet ongoing notification is all Android allows for that.
 */
public final class WorkService extends Service {
    static final String CHANNEL = "groups-work";
    static final int NOTE = 7;
    // While a task runs the phone's processor is kept awake, also with the screen off.
    private PowerManager.WakeLock awake;

    static void start(Context context) {
        try {
            Intent intent = new Intent(context, WorkService.class);
            if (Build.VERSION.SDK_INT >= 26) {
                context.startForegroundService(intent);
            } else {
                context.startService(intent);
            }
        } catch (RuntimeException e) {
            // (a test, or this phone refused the quiet notification)
        }
    }

    static void stop(Context context) {
        try {
            context.stopService(new Intent(context, WorkService.class));
        } catch (RuntimeException e) {
            // (already gone)
        }
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        try {
            if (awake != null && awake.isHeld()) {
                awake.release();
            }
        } catch (RuntimeException e) {
            // (already let go)
        }
        awake = null;
        super.onDestroy();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        try {
            if (awake == null) {
                PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
                awake = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "groups:work");
                awake.setReferenceCounted(false);
            }
            awake.acquire(12 * 60 * 60 * 1000L);           // (let go when the last browser closes; at most 12 hours)
        } catch (RuntimeException e) {
            AppLog.error("تعذّر إبقاء الجوال مستيقظًا أثناء المهمة", e);
        }
        Notification note = note();
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTE, note, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(NOTE, note);
        }
        return START_STICKY;
    }

    private Notification note() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(CHANNEL, "العمل في الخلفية", NotificationManager.IMPORTANCE_LOW);
            channel.setSound(null, null);
            channel.enableVibration(false);
            channel.setShowBadge(false);
            NotificationManager manager = getSystemService(NotificationManager.class);
            if (manager != null) {
                manager.createNotificationChannel(channel);
            }
        }
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
            ? new Notification.Builder(this, CHANNEL)
            : new Notification.Builder(this);
        return builder
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("قروبات")
            .setContentText("يعمل في الخلفية")
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build();
    }
}
