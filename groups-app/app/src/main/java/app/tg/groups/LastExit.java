package app.tg.groups;

import android.annotation.TargetApi;
import android.app.ActivityManager;
import android.app.ApplicationExitInfo;
import android.content.Context;
import android.content.SharedPreferences;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/** Why the app's last run ended, as Android (11 and later) remembers it: a crash, the system closing the app for
 * memory, and the like go to the log as errors, once each. */
@TargetApi(30)
final class LastExit {
    private LastExit() {
    }

    static void note(Context context) {
        try {
            ActivityManager manager = (ActivityManager) context.getSystemService(Context.ACTIVITY_SERVICE);
            List<ApplicationExitInfo> past = manager.getHistoricalProcessExitReasons(null, 0, 1);
            if (past == null || past.isEmpty()) {
                return;
            }
            ApplicationExitInfo last = past.get(0);
            SharedPreferences prefs = context.getSharedPreferences("log", Context.MODE_PRIVATE);
            if (prefs.getLong("exit", 0) == last.getTimestamp()) {
                return;
            }
            prefs.edit().putLong("exit", last.getTimestamp()).apply();
            String why;
            boolean bad = true;
            switch (last.getReason()) {
                case ApplicationExitInfo.REASON_EXIT_SELF:
                    return;                                   // (the app's own restart)
                case ApplicationExitInfo.REASON_CRASH:
                    why = "خطأ في التطبيق";
                    break;
                case ApplicationExitInfo.REASON_CRASH_NATIVE:
                    why = "انهيار في المتصفح أو في جزء من النظام";
                    break;
                case ApplicationExitInfo.REASON_ANR:
                    why = "التطبيق لم يستجب فأغلقه النظام";
                    break;
                case ApplicationExitInfo.REASON_LOW_MEMORY:
                    why = "أغلقه النظام لنقص الذاكرة";
                    break;
                case ApplicationExitInfo.REASON_EXCESSIVE_RESOURCE_USAGE:
                    why = "أغلقه النظام لكثرة استهلاكه";
                    break;
                case ApplicationExitInfo.REASON_SIGNALED:
                    why = "أنهى النظام عمليته";
                    break;
                case ApplicationExitInfo.REASON_USER_REQUESTED:
                case ApplicationExitInfo.REASON_USER_STOPPED:
                    why = "أُغلق من الجوال";
                    bad = false;
                    break;
                default:
                    why = "سبب آخر (" + last.getReason() + ")";
                    break;
            }
            String more = last.getDescription() == null ? "" : " — " + last.getDescription();
            String when = new SimpleDateFormat("MM-dd HH:mm:ss", Locale.US).format(new Date(last.getTimestamp()));
            String line = "التشغيل السابق انتهى (" + when + "): " + why + more;
            if (bad) {
                AppLog.error(line);
            } else {
                AppLog.add(line);
            }
        } catch (Throwable e) {
            // (this phone does not tell)
        }
    }
}
