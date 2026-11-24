package app.tg.extract;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

/**
 * Keeps a task going after the user leaves the app: Android's "foreground service" (the app is listed among the
 * active apps, with a quiet notification showing the progress) and the processor kept awake while the task works.
 */
public final class WorkService extends Service {
    static final String CHANNEL = "extract-work";
    static final String DONE_CHANNEL = "extract-done";
    static final int NOTE = 7;
    static final int DONE_NOTE = 8;
    static volatile boolean on;
    private PowerManager.WakeLock awake;

    static void start(Context context) {
        try {
            context.startForegroundService(new Intent(context, WorkService.class));
        } catch (RuntimeException e) {
            Engine.log("⚠️ تعذّر تشغيل العمل في الخلفية: " + e.getClass().getSimpleName());
        }
    }

    static void stop(Context context) {
        try {
            context.stopService(new Intent(context, WorkService.class));
        } catch (RuntimeException ignore) {
            // (already gone)
        }
    }

    /** The notification follows the task's progress. */
    static void update(Context context) {
        if (!on) {
            return;
        }
        try {
            NotificationManager manager = context.getSystemService(NotificationManager.class);
            if (manager != null) {
                manager.notify(NOTE, note(context));
            }
        } catch (RuntimeException ignore) {
            // (the next change tries again)
        }
    }

    /** How the task ended, for a user who is not looking at the app. */
    static void done(Context context, String text) {
        try {
            NotificationManager manager = context.getSystemService(NotificationManager.class);
            if (manager == null) {
                return;
            }
            manager.createNotificationChannel(
                new NotificationChannel(DONE_CHANNEL, "انتهاء المهمة", NotificationManager.IMPORTANCE_DEFAULT));
            Notification note = new Notification.Builder(context, DONE_CHANNEL)
                .setSmallIcon(R.drawable.ic_note)
                .setContentTitle("المستخرج")
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(openApp(context))
                .setAutoCancel(true)
                .build();
            manager.notify(DONE_NOTE, note);
        } catch (RuntimeException ignore) {
            // (no notification, the log has it)
        }
    }

    private static PendingIntent openApp(Context context) {
        Intent intent = new Intent(context, MainActivity.class)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(context, 0, intent, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private static Notification note(Context context) {
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager != null) {
            NotificationChannel channel = new NotificationChannel(CHANNEL, "العمل في الخلفية", NotificationManager.IMPORTANCE_LOW);
            channel.setSound(null, null);
            channel.enableVibration(false);
            channel.setShowBadge(false);
            manager.createNotificationChannel(channel);
        }
        String title = Engine.running ? "المستخرج • " + Engine.done + "/" + Engine.target : "المستخرج";
        Notification.Builder builder = new Notification.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_note)
            .setContentTitle(title)
            .setContentText(Engine.status)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(openApp(context));
        if (Build.VERSION.SDK_INT >= 31) {
            builder.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE);
        }
        return builder.build();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        try {
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(NOTE, note(this), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            } else if (Build.VERSION.SDK_INT >= 29) {
                startForeground(NOTE, note(this), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
            } else {
                startForeground(NOTE, note(this));
            }
            on = true;
        } catch (RuntimeException e) {
            Engine.log("⚠️ رفض الجوال إشعار العمل في الخلفية: " + e.getClass().getSimpleName());
            stopSelf();
            return START_NOT_STICKY;
        }
        try {
            if (awake == null) {
                PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
                awake = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "extract:work");
                awake.setReferenceCounted(false);
            }
            if (!awake.isHeld()) {
                awake.acquire();                                // (let go when the task ends: see onDestroy)
            }
        } catch (RuntimeException e) {
            Engine.log("⚠️ تعذّر إبقاء الجوال مستيقظًا أثناء المهمة");
        }
        if (!Engine.running) {
            stopSelf();                                         // (the task ended before the service came up)
        }
        return START_NOT_STICKY;
    }

    /** Android 15's limit on long "data sync" work (not reached with "specialUse", but never a crash). */
    @Override
    public void onTimeout(int startId, int type) {
        stopSelf();
    }

    @Override
    public void onDestroy() {
        on = false;
        try {
            if (awake != null && awake.isHeld()) {
                awake.release();
            }
        } catch (RuntimeException ignore) {
            // (already let go)
        }
        awake = null;
        super.onDestroy();
    }
}
