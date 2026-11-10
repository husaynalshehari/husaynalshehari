package app.tg.groups;

import android.app.Application;
import android.os.Build;

/** The app's process: the log is opened first, and an error that nothing caught is written to it before the app
 * stops, so that «السجل» shows it at the next start. */
public final class App extends Application {
    @Override
    public void onCreate() {
        super.onCreate();
        AppLog.init(this);
        AppLog.add("———— تشغيل: " + AppLog.device + " ————");
        if (Build.VERSION.SDK_INT >= 30) {
            LastExit.note(this);
        }
        Thread.UncaughtExceptionHandler before = Thread.getDefaultUncaughtExceptionHandler();
        Thread.setDefaultUncaughtExceptionHandler((thread, problem) -> {
            try {
                AppLog.error("توقف التطبيق بخطأ لم يُعالَج (" + thread.getName() + ")", problem);
            } catch (Throwable ignored) {
                // (the phone's own handling still follows)
            }
            if (before != null) {
                before.uncaughtException(thread, problem);
            }
        });
    }
}
