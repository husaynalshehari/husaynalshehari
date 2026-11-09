package app.tg.groups;

import android.content.Context;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;

/**
 * Where the tasks' browsers show, on the «المتصفح» tab. Each browser's WebView is exactly as large as the page
 * the script asked for (600 CSS pixels wide by default, and as tall as the script makes it: up to 25000 while it
 * collects from a group), which is larger than a phone's screen; it is drawn scaled down so that its whole width
 * shows, and the tab scrolls down along it. The browser opened last is on top.
 */
final class BrowserStage extends ViewGroup {
    BrowserStage(Context context) {
        super(context);
        setClipChildren(true);
    }

    int px(int css) {
        return Math.max(1, (int) Math.floor(css * getResources().getDisplayMetrics().density));
    }

    void add(BrowserHost.Browser browser) {
        addView(browser.view, new LayoutParams(px(browser.cssWidth), px(browser.cssHeight)));
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

    private static BrowserHost.Browser of(View child) {
        Object tag = child.getTag();
        return tag instanceof BrowserHost.Browser ? (BrowserHost.Browser) tag : null;
    }

    private float scaleFor(int childWidth, int width) {
        return childWidth <= 0 || width <= 0 ? 1f : Math.min(1f, width / (float) childWidth);
    }

    @Override
    protected void onMeasure(int widthSpec, int heightSpec) {
        int width = MeasureSpec.getSize(widthSpec);
        int tall = 0;
        for (int i = 0; i < getChildCount(); i++) {
            View child = getChildAt(i);
            BrowserHost.Browser browser = of(child);
            int w = browser == null ? width : px(browser.cssWidth);
            int h = browser == null ? width : px(browser.cssHeight);
            child.measure(MeasureSpec.makeMeasureSpec(w, MeasureSpec.EXACTLY), MeasureSpec.makeMeasureSpec(h, MeasureSpec.EXACTLY));
            if (i == getChildCount() - 1) {
                tall = Math.round(h * scaleFor(w, width));          // (the tab is as long as the browser on top)
            }
        }
        setMeasuredDimension(width, Math.max(tall, getSuggestedMinimumHeight()));
    }

    @Override
    protected void onLayout(boolean changed, int left, int top, int right, int bottom) {
        int width = right - left;
        for (int i = 0; i < getChildCount(); i++) {
            View child = getChildAt(i);
            int w = child.getMeasuredWidth();
            int h = child.getMeasuredHeight();
            child.layout(0, 0, w, h);
            float scale = scaleFor(w, width);
            child.setPivotX(0f);
            child.setPivotY(0f);
            child.setScaleX(scale);
            child.setScaleY(scale);
        }
    }
}
