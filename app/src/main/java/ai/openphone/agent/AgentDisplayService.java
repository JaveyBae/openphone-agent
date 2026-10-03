package ai.openphone.agent;

import android.app.ActivityOptions;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.net.Uri;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.util.Log;
import android.view.Display;
import android.view.Surface;

import java.io.File;
import java.io.FileOutputStream;
import java.io.FileWriter;
import java.nio.ByteBuffer;
import java.util.concurrent.atomic.AtomicBoolean;

public class AgentDisplayService extends Service {
    public static final String ACTION_START = "ai.openphone.agent.START";
    public static final String ACTION_STOP = "ai.openphone.agent.STOP";
    public static final String ACTION_OPEN_BROWSER = "ai.openphone.agent.OPEN_BROWSER";
    // Kept so an older installed UI/controller can still request the browser.
    public static final String ACTION_OPEN_CHROME = "ai.openphone.agent.OPEN_CHROME";
    public static final String ACTION_STATUS = "ai.openphone.agent.STATUS";
    public static final String EXTRA_MESSAGE = "message";
    public static final String EXTRA_URL = "url";
    public static final String EXTRA_RESULT_CODE = "result_code";
    public static final String EXTRA_PROJECTION_DATA = "projection_data";

    public static final String PREFS = "agent_display";
    public static final String KEY_DISPLAY_ID = "display_id";
    public static final String KEY_STATUS = "status";

    private static final String TAG = "OpenPhoneDisplay";
    private static final String CHANNEL_ID = "agent_display";
    private static final int NOTIFICATION_ID = 41;
    private static final int WIDTH = 720;
    private static final int HEIGHT = 1280;
    private static final int DENSITY_DPI = 320;
    // These platform flags exist in Android's display service but are not part of
    // every public SDK surface. The service retries without them if the device ROM
    // does not permit an ordinary app to create a touch-capable display.
    private static final int FLAG_SUPPORTS_TOUCH = 1 << 6;
    private static final int FLAG_ROTATES_WITH_CONTENT = 1 << 7;

    private final AtomicBoolean writingFrame = new AtomicBoolean(false);
    private HandlerThread captureThread;
    private Handler captureHandler;
    private ImageReader imageReader;
    private VirtualDisplay virtualDisplay;
    private MediaProjection mediaProjection;
    private long lastFrameWriteMs;

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
        captureThread = new HandlerThread("agent-display-capture");
        captureThread.start();
        captureHandler = new Handler(captureThread.getLooper());
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            releaseDisplay("Stopped by user");
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
            return START_NOT_STICKY;
        }

        startForeground(NOTIFICATION_ID, notification("Preparing agent display…"));
        ensureDisplay(intent);
        if (ACTION_OPEN_BROWSER.equals(action) || ACTION_OPEN_CHROME.equals(action)) {
            String url = intent == null ? null : intent.getStringExtra(EXTRA_URL);
            launchAgentBrowser(url);
        }
        return START_STICKY;
    }

    private void ensureDisplay(Intent startIntent) {
        if (virtualDisplay != null) {
            publish("Ready");
            return;
        }

        if (mediaProjection == null) {
            int resultCode = startIntent == null
                    ? 0 : startIntent.getIntExtra(EXTRA_RESULT_CODE, 0);
            Intent projectionData = startIntent == null
                    ? null : startIntent.getParcelableExtra(EXTRA_PROJECTION_DATA);
            if (resultCode == 0 || projectionData == null) {
                publish("Tap Start and approve the one-time capture permission");
                return;
            }
            MediaProjectionManager projectionManager =
                    (MediaProjectionManager) getSystemService(MEDIA_PROJECTION_SERVICE);
            mediaProjection = projectionManager.getMediaProjection(resultCode, projectionData);
            mediaProjection.registerCallback(new MediaProjection.Callback() {
                @Override
                public void onStop() {
                    if (captureHandler != null) {
                        captureHandler.post(() -> releaseDisplay("Screen-capture permission ended"));
                    }
                }
            }, captureHandler);
        }

        imageReader = ImageReader.newInstance(WIDTH, HEIGHT, PixelFormat.RGBA_8888, 3);
        imageReader.setOnImageAvailableListener(this::consumeFrame, captureHandler);
        int flags = android.hardware.display.DisplayManager.VIRTUAL_DISPLAY_FLAG_PUBLIC
                | android.hardware.display.DisplayManager.VIRTUAL_DISPLAY_FLAG_PRESENTATION
                | FLAG_ROTATES_WITH_CONTENT
                | FLAG_SUPPORTS_TOUCH;

        try {
            virtualDisplay = mediaProjection.createVirtualDisplay(
                    "OpenPhone Agent Display",
                    WIDTH,
                    HEIGHT,
                    DENSITY_DPI,
                    flags,
                    imageReader.getSurface(),
                    null,
                    captureHandler);
        } catch (SecurityException touchNotAllowed) {
            Log.w(TAG, "Touch-capable display rejected; retrying without touch flag", touchNotAllowed);
            flags &= ~FLAG_SUPPORTS_TOUCH;
            virtualDisplay = mediaProjection.createVirtualDisplay(
                    "OpenPhone Agent Display",
                    WIDTH,
                    HEIGHT,
                    DENSITY_DPI,
                    flags,
                    imageReader.getSurface(),
                    null,
                    captureHandler);
        }

        if (virtualDisplay == null || virtualDisplay.getDisplay() == null) {
            releaseDisplay("Android rejected the virtual display");
            return;
        }
        publish("Ready");
    }

    private void launchAgentBrowser(String requestedUrl) {
        if (virtualDisplay == null) {
            publish("Cannot launch browser: display is unavailable");
            return;
        }
        Display display = virtualDisplay.getDisplay();
        String url = requestedUrl == null || requestedUrl.isBlank()
                ? "https://www.wikipedia.org/" : requestedUrl;
        Intent browser = new Intent(Intent.ACTION_VIEW, Uri.parse(url),
                this, AgentBrowserActivity.class);
        browser.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        ActivityOptions options = ActivityOptions.makeBasic();
        options.setLaunchDisplayId(display.getDisplayId());
        try {
            startActivity(browser, options.toBundle());
            publish("Isolated browser launched on the agent display");
        } catch (Exception error) {
            Log.e(TAG, "Could not launch isolated browser on virtual display", error);
            publish("Ready — launch apps with the Mac controller");
        }
    }

    private void consumeFrame(ImageReader reader) {
        Image image = reader.acquireLatestImage();
        if (image == null) {
            return;
        }
        long now = System.currentTimeMillis();
        if (now - lastFrameWriteMs < 750 || !writingFrame.compareAndSet(false, true)) {
            image.close();
            return;
        }
        lastFrameWriteMs = now;
        try {
            Image.Plane plane = image.getPlanes()[0];
            ByteBuffer buffer = plane.getBuffer();
            int pixelStride = plane.getPixelStride();
            int rowStride = plane.getRowStride();
            int paddedWidth = WIDTH + (rowStride - pixelStride * WIDTH) / pixelStride;
            Bitmap padded = Bitmap.createBitmap(paddedWidth, HEIGHT, Bitmap.Config.ARGB_8888);
            padded.copyPixelsFromBuffer(buffer);
            Bitmap cropped = Bitmap.createBitmap(padded, 0, 0, WIDTH, HEIGHT);
            writeBitmap(cropped);
            cropped.recycle();
            padded.recycle();
        } catch (Exception error) {
            Log.e(TAG, "Frame capture failed", error);
        } finally {
            image.close();
            writingFrame.set(false);
        }
    }

    private void writeBitmap(Bitmap bitmap) throws Exception {
        File directory = new File(getFilesDir(), "display");
        if (!directory.exists() && !directory.mkdirs()) {
            throw new IllegalStateException("Could not create capture directory");
        }
        File temp = new File(directory, "latest.tmp");
        File target = new File(directory, "latest.png");
        try (FileOutputStream output = new FileOutputStream(temp)) {
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, output);
        }
        if (!temp.renameTo(target)) {
            throw new IllegalStateException("Could not publish latest frame");
        }
    }

    private void publish(String message) {
        int displayId = virtualDisplay == null || virtualDisplay.getDisplay() == null
                ? -1 : virtualDisplay.getDisplay().getDisplayId();
        getSharedPreferences(PREFS, MODE_PRIVATE)
                .edit()
                .putInt(KEY_DISPLAY_ID, displayId)
                .putString(KEY_STATUS, message)
                .apply();
        writeState(displayId, message);
        sendBroadcast(new Intent(ACTION_STATUS)
                .setPackage(getPackageName())
                .putExtra(EXTRA_MESSAGE, message));
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.notify(NOTIFICATION_ID, notification(message));
    }

    private void writeState(int displayId, String message) {
        String safeMessage = message.replace("\\", "\\\\").replace("\"", "\\\"");
        try (FileWriter writer = new FileWriter(new File(getFilesDir(), "display-state.json"))) {
            writer.write("{\n"
                    + "  \"displayId\": " + displayId + ",\n"
                    + "  \"width\": " + WIDTH + ",\n"
                    + "  \"height\": " + HEIGHT + ",\n"
                    + "  \"status\": \"" + safeMessage + "\"\n"
                    + "}\n");
        } catch (Exception error) {
            Log.e(TAG, "Could not write display state", error);
        }
    }

    private Notification notification(String message) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pending = PendingIntent.getActivity(
                this,
                0,
                open,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        return new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_menu_view)
                .setContentTitle("OpenPhone agent display")
                .setContentText(message)
                .setOngoing(true)
                .setContentIntent(pending)
                .build();
    }

    private void createNotificationChannel() {
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                "Agent display",
                NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("Shows when the private agent display is running.");
        getSystemService(NotificationManager.class).createNotificationChannel(channel);
    }

    private void releaseDisplay(String message) {
        if (virtualDisplay != null) {
            virtualDisplay.release();
            virtualDisplay = null;
        }
        if (imageReader != null) {
            imageReader.close();
            imageReader = null;
        }
        if (mediaProjection != null) {
            MediaProjection projection = mediaProjection;
            mediaProjection = null;
            projection.stop();
        }
        publish(message);
    }

    @Override
    public void onDestroy() {
        releaseDisplay("Service stopped");
        if (captureThread != null) {
            captureThread.quitSafely();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
