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

/**
 * Keeps the task running after the user leaves the app, without a floating window.
 * A quiet ongoing notification is all Android allows for that.
 */
public final class WorkService extends Service {
    static final String CHANNEL = "groups-work";
    static final int NOTE = 7;

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
    public int onStartCommand(Intent intent, int flags, int startId) {
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
