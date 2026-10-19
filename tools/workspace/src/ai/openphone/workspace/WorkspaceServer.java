package ai.openphone.workspace;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.Process;
import android.view.Display;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.UUID;

/** ADB-shell-owned, non-mirroring workspace. No network or command server. */
public final class WorkspaceServer {
    private static final String DIRECTORY = "/data/local/tmp/openphone-workspace";
    // Android 15 AOSP DisplayManager flags, distinct from Display.getFlags().
    private static final int VD_TOUCH = 1 << 6;
    private static final int VD_DESTROY_ON_REMOVAL = 1 << 8;
    private static final int VD_TRUSTED = 1 << 10;
    private static final int VD_OWN_FOCUS = 1 << 14;
    private static final int VD_NO_TOP_FOCUS_STEAL = 1 << 16;
    private static final int REQUIRED_DISPLAY_FLAGS = (1 << 7) | (1 << 11) | (1 << 12);
    private static final int WIDTH = 720, HEIGHT = 1280;

    private final File directory = new File(DIRECTORY);
    private final String session = UUID.randomUUID().toString();
    private VirtualDisplay display;
    private ImageReader reader;
    private HandlerThread captureThread;
    private Handler capture;
    private boolean frameQueued;

    public static void main(String[] args) throws Exception {
        if (Process.myUid() != 2000 || Build.VERSION.SDK_INT < 35) {
            throw new SecurityException("Workspace requires an authorized ADB shell on Android 15+");
        }
        Looper.prepareMainLooper();
        Class<?> activityThread = Class.forName("android.app.ActivityThread");
        Object thread = activityThread.getMethod("systemMain").invoke(null);
        Context systemContext = (Context) activityThread.getMethod("getSystemContext").invoke(thread);
        Context shellContext = systemContext.createPackageContext("com.android.shell", 0);
        WorkspaceServer server = new WorkspaceServer();
        server.start(shellContext);
        Runtime.getRuntime().addShutdownHook(new Thread(server::close));
        Looper.loop();
    }

    private void start(Context context) throws Exception {
        if (!directory.isDirectory() && !directory.mkdirs()) {
            throw new IllegalStateException("Cannot create workspace directory");
        }
        captureThread = new HandlerThread("openphone-private-capture");
        captureThread.start();
        capture = new Handler(captureThread.getLooper());
        reader = ImageReader.newInstance(WIDTH, HEIGHT, PixelFormat.RGBA_8888, 3);
        reader.setOnImageAvailableListener(ignored -> {
            if (!frameQueued) {
                frameQueued = true;
                // Acquire the newest frame after coalescing, rather than dropping
                // the final render of a static page during a throttle interval.
                capture.postDelayed(this::writeLatestFrame, 200);
            }
        }, capture);
        int flags = DisplayManager.VIRTUAL_DISPLAY_FLAG_PUBLIC
                | DisplayManager.VIRTUAL_DISPLAY_FLAG_OWN_CONTENT_ONLY
                | DisplayManager.VIRTUAL_DISPLAY_FLAG_PRESENTATION
                | VD_TOUCH | VD_DESTROY_ON_REMOVAL | VD_TRUSTED | VD_OWN_FOCUS | VD_NO_TOP_FOCUS_STEAL;
        display = context.getSystemService(DisplayManager.class).createVirtualDisplay(
                "OpenPhone Isolated Workspace " + session, WIDTH, HEIGHT, 320,
                reader.getSurface(), flags);
        Display actual = display == null ? null : display.getDisplay();
        if (actual == null || (actual.getFlags() & REQUIRED_DISPLAY_FLAGS) != REQUIRED_DISPLAY_FLAGS) {
            close();
            throw new SecurityException("Android did not retain the required focus-isolation flags");
        }
        JSONObject state = new JSONObject()
                .put("backend", "adb-shell")
                .put("displayId", actual.getDisplayId())
                .put("width", WIDTH).put("height", HEIGHT)
                .put("pid", Process.myPid()).put("session", session)
                .put("displayName", actual.getName())
                .put("displayFlags", actual.getFlags())
                .put("status", "Isolated workspace created");
        writeAtomic(new File(directory, "state.json"), state.toString(2).getBytes(StandardCharsets.UTF_8));
        System.out.println(state.toString());
    }

    private void writeLatestFrame() {
        frameQueued = false;
        Image image = null;
        Bitmap padded = null, cropped = null;
        try {
            image = reader.acquireLatestImage();
            if (image == null) return;
            Image.Plane plane = image.getPlanes()[0];
            ByteBuffer pixels = plane.getBuffer();
            int paddedWidth = WIDTH + (plane.getRowStride() - plane.getPixelStride() * WIDTH)
                    / plane.getPixelStride();
            padded = Bitmap.createBitmap(paddedWidth, HEIGHT, Bitmap.Config.ARGB_8888);
            padded.copyPixelsFromBuffer(pixels);
            cropped = Bitmap.createBitmap(padded, 0, 0, WIDTH, HEIGHT);
            File target = new File(directory, "frame-" + session + ".png");
            File temporary = new File(directory, "frame-" + session + ".tmp");
            try (FileOutputStream output = new FileOutputStream(temporary)) {
                cropped.compress(Bitmap.CompressFormat.PNG, 100, output);
            }
            if (!temporary.renameTo(target)) throw new IllegalStateException("Cannot publish frame");
        } catch (Exception error) {
            error.printStackTrace();
        } finally {
            if (cropped != null && cropped != padded) cropped.recycle();
            if (padded != null) padded.recycle();
            if (image != null) image.close();
        }
    }

    private static void writeAtomic(File target, byte[] bytes) throws Exception {
        File temporary = new File(target.getParentFile(), target.getName() + ".tmp");
        try (FileOutputStream output = new FileOutputStream(temporary)) {
            output.write(bytes);
        }
        if (!temporary.renameTo(target)) throw new IllegalStateException("Cannot publish state");
    }

    private void close() {
        if (display != null) display.release();
        if (reader != null) reader.close();
        if (captureThread != null) captureThread.quitSafely();
    }
}
