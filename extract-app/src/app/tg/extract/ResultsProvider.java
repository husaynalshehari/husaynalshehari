package app.tg.extract;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;

/** Gives the app the user shares the results with (WhatsApp, Telegram, Drive...) the file to read, and only it. */
public final class ResultsProvider extends ContentProvider {
    static final String AUTHORITY = "app.tg.extract.results";

    /** The folder of the shared copy, in the app's own files. */
    static File folder(File filesDir) {
        File folder = new File(filesDir, "share");
        folder.mkdirs();
        return folder;
    }

    static Uri uriOf(File file) {
        return Uri.parse("content://" + AUTHORITY + "/" + Uri.encode(file.getName()));
    }

    private File fileOf(Uri uri) {
        String name = uri.getLastPathSegment();
        if (name == null || name.contains("/") || name.startsWith(".") || getContext() == null) {
            return null;
        }
        File file = new File(folder(getContext().getFilesDir()), name);
        return file.isFile() ? file : null;
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public String getType(Uri uri) {
        return "text/csv";
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        if (mode == null || !mode.startsWith("r") || mode.contains("w")) {
            throw new SecurityException("read only");
        }
        File file = fileOf(uri);
        if (file == null) {
            throw new FileNotFoundException(String.valueOf(uri));
        }
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] args, String sortOrder) {
        File file = fileOf(uri);
        if (file == null) {
            return null;
        }
        String[] columns = projection != null ? projection : new String[] {OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE};
        Object[] row = new Object[columns.length];
        for (int i = 0; i < columns.length; i++) {
            if (OpenableColumns.DISPLAY_NAME.equals(columns[i])) {
                row[i] = file.getName();
            } else if (OpenableColumns.SIZE.equals(columns[i])) {
                row[i] = file.length();
            }
        }
        MatrixCursor cursor = new MatrixCursor(columns, 1);
        cursor.addRow(row);
        return cursor;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] args) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] args) {
        return 0;
    }
}
