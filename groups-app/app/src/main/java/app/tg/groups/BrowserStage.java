package app.tg.groups;

import android.content.Context;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;

/**
 * Where the tasks' browsers show, on the «المتصفح» tab. Each browser's WebView is as wide as the phone's screen,
 * as any browser on this phone is, so the page has the phone's real width; only its height is the script's (taller
 * than the screen: up to 25000 CSS pixels while it collects from a group), and the tab scrolls down along it. The
 * browser opened last is on top.
 */
final class BrowserStage extends ViewGroup {
    // In the one-pixel window that holds the browsers while the app is not on the screen (see BrowserHost): the
    // phone's width, which that window does not have. 0 on the tab, where the stage's own width is that.
    int fixedWidth;

    BrowserStage(Context context) {
        super(context);
        setClipChildren(true);
    }

    int px(int css) {
        return Math.max(1, (int) Math.floor(css * getResources().getDisplayMetrics().density));
    }

    void add(BrowserHost.Browser browser) {
        addView(browser.view, new LayoutParams(LayoutParams.MATCH_PARENT, px(browser.cssHeight)));
        requestLayout();
    }

    void sized(BrowserHost.Browser browser) {
        requestLayout();
        invalidate();
    }

    void remove(WebView view) {
        if (view.getParent() == this) {
            removeView(view);
        }
    }

    void front(BrowserHost.Browser browser) {
        if (browser.view != null && browser.view.getParent() == this) {
            bringChildToFront(browser.view);
            requestLayout();
            invalidate();
        }
    }

    /** A finger on the tab scrolls the tab, never the page inside a browser (which would move the buttons from
     * under the script's mouse); only with «الكتابة في المتصفح بيدي» do touches reach the page. */
    @Override
    public boolean onInterceptTouchEvent(android.view.MotionEvent event) {
        return !BrowserHost.manualTyping;
    }

    @Override
    @SuppressWarnings("ClickableViewAccessibility")
    public boolean onTouchEvent(android.view.MotionEvent event) {
        return false;
    }

    private static BrowserHost.Browser of(View child) {
        Object tag = child.getTag();
        return tag instanceof BrowserHost.Browser ? (BrowserHost.Browser) tag : null;
    }

    @Override
    protected void onMeasure(int widthSpec, int heightSpec) {
        int own = MeasureSpec.getSize(widthSpec);
        int width = fixedWidth > 0 ? fixedWidth : own;
        int tall = 0;
        for (int i = 0; i < getChildCount(); i++) {
            View child = getChildAt(i);
            BrowserHost.Browser browser = of(child);
            int h = browser == null ? width : px(browser.cssHeight);
            child.measure(MeasureSpec.makeMeasureSpec(width, MeasureSpec.EXACTLY), MeasureSpec.makeMeasureSpec(h, MeasureSpec.EXACTLY));
            if (i == getChildCount() - 1) {
                tall = h;                                            // (the tab is as long as the browser on top)
            }
        }
        if (fixedWidth > 0) {
            setMeasuredDimension(own, MeasureSpec.getSize(heightSpec));      // (the window's one pixel; the browsers keep their size)
        } else {
            setMeasuredDimension(width, Math.max(tall, getSuggestedMinimumHeight()));
        }
    }

    @Override
    protected void onLayout(boolean changed, int left, int top, int right, int bottom) {
        for (int i = 0; i < getChildCount(); i++) {
            View child = getChildAt(i);
            child.layout(0, 0, child.getMeasuredWidth(), child.getMeasuredHeight());
        }
    }
}
