package app.tg.groups;

/** Lines of a report, as the check (ggcheck.py) makes them: each one is shown on the app's page as it comes. */
public final class Lines {
    private final MainActivity page;

    Lines(MainActivity page) {
        this.page = page;
    }

    public void add(String line) {
        page.runOnUiThread(() -> page.reportAdd(line));
    }
}
