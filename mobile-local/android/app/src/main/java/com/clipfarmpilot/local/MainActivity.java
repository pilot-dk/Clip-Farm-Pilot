package com.clipfarmpilot.local;

import android.content.ContentResolver;
import android.content.Intent;
import android.content.res.Resources;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Typeface;
import android.media.MediaMetadataRetriever;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.text.TextPaint;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.SeekBar;
import android.widget.Spinner;
import android.widget.Switch;
import android.widget.TextView;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.annotation.Nullable;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.FileProvider;
import androidx.core.widget.NestedScrollView;
import androidx.media3.common.C;
import androidx.media3.common.Effect;
import androidx.media3.common.MediaItem;
import androidx.media3.common.MimeTypes;
import androidx.media3.common.util.UnstableApi;
import androidx.media3.datasource.RawResourceDataSource;
import androidx.media3.effect.BitmapOverlay;
import androidx.media3.effect.Contrast;
import androidx.media3.effect.HslAdjustment;
import androidx.media3.effect.OverlayEffect;
import androidx.media3.effect.Presentation;
import androidx.media3.effect.RgbFilter;
import androidx.media3.effect.StaticOverlaySettings;
import androidx.media3.exoplayer.ExoPlayer;
import androidx.media3.transformer.Composition;
import androidx.media3.transformer.EditedMediaItem;
import androidx.media3.transformer.EditedMediaItemSequence;
import androidx.media3.transformer.Effects;
import androidx.media3.transformer.ExportException;
import androidx.media3.transformer.ExportResult;
import androidx.media3.transformer.ProgressHolder;
import androidx.media3.transformer.Transformer;
import androidx.media3.ui.PlayerView;

import java.io.File;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Date;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

@UnstableApi
public final class MainActivity extends AppCompatActivity {
    private static final int NAVY = Color.rgb(6, 16, 26);
    private static final int PANEL = Color.rgb(11, 24, 36);
    private static final int CYAN = Color.rgb(91, 214, 255);
    private static final int GOLD = Color.rgb(255, 189, 89);
    private static final String[] RATIOS = {"16:9 landscape", "9:16 vertical", "1:1 square"};
    private static final String[] FILTERS = {"None", "Black & white", "Cinematic", "Vivid", "Warm", "Cool", "Faded / Vintage", "High contrast"};
    private static final String[] CAPTION_POSITIONS = {"Top", "Centre", "Bottom"};

    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());
    private ActivityResultLauncher<String[]> videoPicker;
    private ExoPlayer player;
    private PlayerView playerView;
    private Uri sourceUri;
    private long durationMs;
    private long clipStartMs;
    private long clipEndMs;
    private Transformer transformer;
    private File exportFile;
    private boolean exportInProgress;

    private TextView sourceLabel;
    private TextView startLabel;
    private TextView endLabel;
    private TextView statusLabel;
    private SeekBar startSeek;
    private SeekBar endSeek;
    private ProgressBar progressBar;
    private LinearLayout candidatesContainer;
    private Spinner ratioSpinner;
    private Spinner filterSpinner;
    private Spinner captionPositionSpinner;
    private EditText captionInput;
    private SeekBar captionSizeSeek;
    private Switch vineBoomSwitch;
    private Switch smartSoundSwitch;
    private Switch viralTitleSwitch;
    private Button exportButton;
    private Button shareButton;

    @Override
    protected void onCreate(@Nullable Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().setStatusBarColor(NAVY);
        getWindow().setNavigationBarColor(NAVY);

        videoPicker = registerForActivityResult(new ActivityResultContracts.OpenDocument(), this::loadVideo);
        player = new ExoPlayer.Builder(this).build();
        setContentView(buildInterface());
    }

    private View buildInterface() {
        NestedScrollView scroll = new NestedScrollView(this);
        scroll.setFillViewport(true);
        scroll.setBackgroundColor(NAVY);
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(dp(16), dp(16), dp(16), dp(48));
        scroll.addView(content, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView title = text("Clip Farm Pilot", 26, Color.WHITE, true);
        title.setPadding(0, 0, 0, dp(4));
        content.addView(title);
        TextView subtitle = text("CREATOR FLIGHT DECK · ANDROID", 10, Color.rgb(112, 147, 168), true);
        subtitle.setLetterSpacing(0.12f);
        content.addView(subtitle);

        LinearLayout local = panel();
        TextView localTitle = text("●  LOCAL FLIGHT MODE", 12, Color.rgb(108, 230, 171), true);
        local.addView(localTitle);
        local.addView(text("No account, internet permission, upload, analytics, or cloud processing. Videos remain on this device.", 12, Color.LTGRAY, false));
        content.addView(local, spacedParams());

        LinearLayout source = section("01", "Flight source");
        sourceLabel = text("No source loaded", 14, Color.WHITE, true);
        source.addView(sourceLabel);
        Button choose = button("Choose local video", true);
        choose.setOnClickListener(view -> videoPicker.launch(new String[] {"video/*"}));
        source.addView(choose);
        content.addView(source, spacedParams());

        LinearLayout preview = section("02", "Preview");
        playerView = new PlayerView(this);
        playerView.setPlayer(player);
        playerView.setUseController(true);
        playerView.setBackgroundColor(Color.BLACK);
        preview.addView(playerView, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(250)));
        Button playRange = button("Play selected range", false);
        playRange.setOnClickListener(view -> playSelectedRange());
        preview.addView(playRange);
        content.addView(preview, spacedParams());

        LinearLayout range = section("03", "Clip range");
        startLabel = text("Start · 0:00", 12, CYAN, true);
        range.addView(startLabel);
        startSeek = new SeekBar(this);
        startSeek.setMax(1);
        startSeek.setOnSeekBarChangeListener(seekListener(true));
        range.addView(startSeek);
        endLabel = text("End · 0:00", 12, CYAN, true);
        range.addView(endLabel);
        endSeek = new SeekBar(this);
        endSeek.setMax(1);
        endSeek.setProgress(1);
        endSeek.setOnSeekBarChangeListener(seekListener(false));
        range.addView(endSeek);
        content.addView(range, spacedParams());

        LinearLayout radar = section("04", "Moment radar");
        Button analyze = button("Auto-Find Clips on this device", true);
        analyze.setOnClickListener(view -> analyzeLocally());
        radar.addView(analyze);
        candidatesContainer = new LinearLayout(this);
        candidatesContainer.setOrientation(LinearLayout.VERTICAL);
        radar.addView(candidatesContainer);
        content.addView(radar, spacedParams());

        LinearLayout output = section("05", "Output frame");
        ratioSpinner = spinner(RATIOS);
        ratioSpinner.setSelection(1);
        output.addView(labeled("Aspect ratio", ratioSpinner));
        filterSpinner = spinner(FILTERS);
        output.addView(labeled("Video filter", filterSpinner));
        captionInput = new EditText(this);
        captionInput.setHint("Square caption, including emoji ❤️");
        captionInput.setTextColor(Color.WHITE);
        captionInput.setHintTextColor(Color.GRAY);
        captionInput.setBackgroundColor(Color.rgb(8, 22, 33));
        captionInput.setPadding(dp(12), dp(10), dp(12), dp(10));
        output.addView(captionInput, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        captionPositionSpinner = spinner(CAPTION_POSITIONS);
        captionPositionSpinner.setSelection(1);
        output.addView(labeled("Square caption position", captionPositionSpinner));
        captionSizeSeek = new SeekBar(this);
        captionSizeSeek.setMax(125);
        captionSizeSeek.setProgress(50);
        output.addView(labeled("Square caption size · 50%–175%", captionSizeSeek));
        content.addView(output, spacedParams());

        LinearLayout effects = section("FX", "Local effects and title");
        vineBoomSwitch = toggle("Vine Boom", false);
        effects.addView(vineBoomSwitch);
        smartSoundSwitch = toggle("Smart repeat placement", true);
        effects.addView(smartSoundSwitch);
        viralTitleSwitch = toggle("Fresh local viral title", true);
        effects.addView(viralTitleSwitch);
        content.addView(effects, spacedParams());

        LinearLayout export = section("06", "Export");
        statusLabel = text("Preflight ready", 12, Color.WHITE, false);
        export.addView(statusLabel);
        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        progressBar.setProgress(0);
        export.addView(progressBar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(8)));
        exportButton = button("Export MP4 locally", true);
        exportButton.setEnabled(false);
        exportButton.setOnClickListener(view -> exportLocally());
        export.addView(exportButton);
        shareButton = button("Save or share exported MP4", false);
        shareButton.setVisibility(View.GONE);
        shareButton.setOnClickListener(view -> shareExport());
        export.addView(shareButton);
        content.addView(export, spacedParams());

        return scroll;
    }

    private void loadVideo(Uri uri) {
        if (uri == null) return;
        try {
            getContentResolver().takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
        } catch (SecurityException ignored) { }

        MediaMetadataRetriever retriever = new MediaMetadataRetriever();
        try {
            retriever.setDataSource(this, uri);
            String value = retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION);
            durationMs = value == null ? 0 : Long.parseLong(value);
        } catch (Exception error) {
            setStatus("Could not open that video: " + error.getMessage());
            return;
        } finally {
            try {
                retriever.release();
            } catch (Exception ignored) { }
        }
        if (durationMs <= 0) {
            setStatus("That file does not contain a readable video duration.");
            return;
        }

        sourceUri = uri;
        clipStartMs = 0;
        clipEndMs = Math.min(30_000, durationMs);
        int max = Math.max(1, (int) Math.min(Integer.MAX_VALUE, durationMs / 100));
        startSeek.setMax(max);
        endSeek.setMax(max);
        startSeek.setProgress(0);
        endSeek.setProgress((int) (clipEndMs / 100));
        updateRangeLabels();
        sourceLabel.setText("Local video · " + formatTime(durationMs));
        player.setMediaItem(MediaItem.fromUri(uri));
        player.prepare();
        exportButton.setEnabled(true);
        shareButton.setVisibility(View.GONE);
        setStatus("Local source ready");
    }

    private SeekBar.OnSeekBarChangeListener seekListener(boolean start) {
        return new SeekBar.OnSeekBarChangeListener() {
            @Override public void onProgressChanged(SeekBar seekBar, int progress, boolean fromUser) {
                if (!fromUser) return;
                long value = progress * 100L;
                if (start) clipStartMs = Math.min(value, Math.max(0, clipEndMs - 500));
                else clipEndMs = Math.max(value, clipStartMs + 500);
                updateRangeLabels();
            }
            @Override public void onStartTrackingTouch(SeekBar seekBar) { }
            @Override public void onStopTrackingTouch(SeekBar seekBar) {
                player.seekTo(start ? clipStartMs : clipEndMs);
            }
        };
    }

    private void updateRangeLabels() {
        startLabel.setText("Start · " + formatTime(clipStartMs));
        endLabel.setText("End · " + formatTime(clipEndMs));
    }

    private void playSelectedRange() {
        if (sourceUri == null) return;
        player.seekTo(clipStartMs);
        player.play();
        long delay = Math.max(200, clipEndMs - clipStartMs);
        main.postDelayed(() -> {
            if (player.getCurrentPosition() >= clipEndMs - 250) player.pause();
        }, delay);
    }

    private void analyzeLocally() {
        if (sourceUri == null) {
            setStatus("Choose a video first.");
            return;
        }
        setBusy(true);
        setStatus("Moment Radar is decoding audio locally…");
        progressBar.setProgress(4);
        candidatesContainer.removeAllViews();
        Uri uri = sourceUri;
        long localDuration = durationMs;
        worker.execute(() -> {
            try {
                List<LocalMomentAnalyzer.Candidate> candidates = LocalMomentAnalyzer.analyze(
                    this,
                    uri,
                    localDuration,
                    fraction -> main.post(() -> progressBar.setProgress((int) Math.round(fraction * 100)))
                );
                main.post(() -> showCandidates(candidates));
            } catch (Exception error) {
                main.post(() -> {
                    setBusy(false);
                    setStatus("Local analysis failed: " + error.getMessage());
                });
            }
        });
    }

    private void showCandidates(List<LocalMomentAnalyzer.Candidate> candidates) {
        candidatesContainer.removeAllViews();
        for (LocalMomentAnalyzer.Candidate candidate : candidates) {
            Button button = button(
                candidate.score + " · " + formatTime(candidate.startMs) + " – " + formatTime(candidate.endMs) + "\n" + candidate.reason,
                false
            );
            button.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
            button.setOnClickListener(view -> {
                clipStartMs = candidate.startMs;
                clipEndMs = candidate.endMs;
                startSeek.setProgress((int) (clipStartMs / 100));
                endSeek.setProgress((int) (clipEndMs / 100));
                updateRangeLabels();
                player.seekTo(clipStartMs);
                setStatus("Moment selected");
            });
            candidatesContainer.addView(button);
        }
        progressBar.setProgress(100);
        setBusy(false);
        setStatus(candidates.size() + " local moments ready");
    }

    private void exportLocally() {
        if (sourceUri == null || clipEndMs <= clipStartMs) return;
        exportInProgress = true;
        setBusy(true);
        shareButton.setVisibility(View.GONE);
        progressBar.setProgress(1);
        setStatus("Building an on-device timeline…");

        try {
            int ratioIndex = ratioSpinner.getSelectedItemPosition();
            int width = ratioIndex == 0 ? 1920 : 1080;
            int height = ratioIndex == 0 ? 1080 : ratioIndex == 1 ? 1920 : 1080;
            List<Effect> videoEffects = new ArrayList<>();
            addFilter(videoEffects, filterSpinner.getSelectedItemPosition());
            videoEffects.add(Presentation.createForWidthAndHeight(width, height, Presentation.LAYOUT_SCALE_TO_FIT_WITH_CROP));

            String caption = captionInput.getText().toString().trim();
            if (ratioIndex == 2 && !caption.isEmpty()) {
                Bitmap bitmap = captionBitmap(caption, width, height);
                StaticOverlaySettings settings = new StaticOverlaySettings.Builder().build();
                videoEffects.add(new OverlayEffect(Collections.singletonList(BitmapOverlay.createStaticBitmapOverlay(bitmap, settings))));
            }

            MediaItem.ClippingConfiguration clip = new MediaItem.ClippingConfiguration.Builder()
                .setStartPositionMs(clipStartMs)
                .setEndPositionMs(clipEndMs)
                .build();
            MediaItem media = new MediaItem.Builder().setUri(sourceUri).setClippingConfiguration(clip).build();
            EditedMediaItem mainItem = new EditedMediaItem.Builder(media)
                .setEffects(new Effects(Collections.emptyList(), videoEffects))
                .build();
            List<EditedMediaItemSequence> sequences = new ArrayList<>();
            sequences.add(EditedMediaItemSequence.withAudioAndVideoFrom(Collections.singletonList(mainItem)));

            if (vineBoomSwitch.isChecked()) {
                long clipDurationUs = (clipEndMs - clipStartMs) * 1000L;
                long[] moments = smartSoundSwitch.isChecked() && clipDurationUs >= 24_000_000L
                    ? new long[] {Math.round(clipDurationUs * 0.37), Math.round(clipDurationUs * 0.69)}
                    : new long[] {Math.round(clipDurationUs * 0.69)};
                Uri soundUri = RawResourceDataSource.buildRawResourceUri(R.raw.vine_boom);
                EditedMediaItem sound = new EditedMediaItem.Builder(MediaItem.fromUri(soundUri)).build();
                for (long moment : moments) {
                    Set<Integer> audioOnly = new HashSet<>();
                    audioOnly.add(C.TRACK_TYPE_AUDIO);
                    EditedMediaItemSequence sequence = new EditedMediaItemSequence.Builder(audioOnly)
                        .addGap(Math.max(1, moment))
                        .addItem(sound)
                        .build();
                    sequences.add(sequence);
                }
            }

            Composition composition = new Composition.Builder(sequences).build();
            String title = viralTitle(caption);
            File exportDirectory = new File(getExternalFilesDir(Environment.DIRECTORY_MOVIES), "Exports");
            if (!exportDirectory.exists() && !exportDirectory.mkdirs()) throw new IllegalStateException("Could not create the local Exports folder.");
            exportFile = uniqueFile(exportDirectory, sanitize(title), ".mp4");

            transformer = new Transformer.Builder(this)
                .setVideoMimeType(MimeTypes.VIDEO_H264)
                .setAudioMimeType(MimeTypes.AUDIO_AAC)
                .addListener(new Transformer.Listener() {
                    @Override
                    public void onCompleted(Composition completed, ExportResult result) {
                        exportInProgress = false;
                        setBusy(false);
                        progressBar.setProgress(100);
                        shareButton.setVisibility(View.VISIBLE);
                        setStatus("Export ready · stored locally as " + exportFile.getName());
                    }

                    @Override
                    public void onError(Composition failed, ExportResult result, ExportException exception) {
                        exportInProgress = false;
                        setBusy(false);
                        setStatus("Local export failed: " + exception.getMessage());
                    }
                })
                .build();
            transformer.start(composition, exportFile.getAbsolutePath());
            pollExportProgress();
        } catch (Exception error) {
            exportInProgress = false;
            setBusy(false);
            setStatus("Local export failed: " + error.getMessage());
        }
    }

    private void pollExportProgress() {
        if (transformer == null) return;
        ProgressHolder holder = new ProgressHolder();
        int state = transformer.getProgress(holder);
        if (state == Transformer.PROGRESS_STATE_AVAILABLE) {
            progressBar.setProgress(holder.progress);
            setStatus("Rendering locally · " + holder.progress + "%");
        }
        if (exportInProgress) main.postDelayed(this::pollExportProgress, 400);
    }

    private void addFilter(List<Effect> effects, int index) {
        switch (index) {
            case 1 -> effects.add(RgbFilter.createGrayscaleFilter());
            case 2 -> {
                effects.add(new HslAdjustment.Builder().adjustSaturation(-18).adjustLightness(-3).build());
                effects.add(new Contrast(0.18f));
            }
            case 3 -> {
                effects.add(new HslAdjustment.Builder().adjustSaturation(30).adjustLightness(2).build());
                effects.add(new Contrast(0.09f));
            }
            case 4 -> effects.add(new HslAdjustment.Builder().adjustHue(-7).adjustSaturation(8).adjustLightness(2).build());
            case 5 -> effects.add(new HslAdjustment.Builder().adjustHue(8).adjustSaturation(4).build());
            case 6 -> {
                effects.add(new HslAdjustment.Builder().adjustSaturation(-28).adjustLightness(5).build());
                effects.add(new Contrast(-0.08f));
            }
            case 7 -> effects.add(new Contrast(0.34f));
            default -> { }
        }
    }

    private Bitmap captionBitmap(String caption, int width, int height) {
        Bitmap bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(bitmap);
        float scale = 0.5f + captionSizeSeek.getProgress() / 100f;
        float textSize = 76f * scale;
        TextPaint paint = new TextPaint(Paint.ANTI_ALIAS_FLAG | Paint.SUBPIXEL_TEXT_FLAG);
        paint.setTypeface(Typeface.create(Typeface.DEFAULT, Typeface.BOLD));
        paint.setTextSize(textSize);
        paint.setTextAlign(Paint.Align.CENTER);
        float maxWidth = width * 0.88f;
        List<String> lines = wrapText(caption, paint, maxWidth);
        float lineHeight = textSize * 1.2f;
        float totalHeight = lines.size() * lineHeight;
        int position = captionPositionSpinner.getSelectedItemPosition();
        float firstBaseline = position == 0 ? height * 0.16f : position == 2 ? height * 0.84f - totalHeight : (height - totalHeight) / 2f;
        firstBaseline += textSize;

        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(Math.max(5f, textSize * 0.09f));
        paint.setColor(Color.BLACK);
        for (int index = 0; index < lines.size(); index++) canvas.drawText(lines.get(index), width / 2f, firstBaseline + index * lineHeight, paint);
        paint.setStyle(Paint.Style.FILL);
        paint.setColor(Color.WHITE);
        for (int index = 0; index < lines.size(); index++) canvas.drawText(lines.get(index), width / 2f, firstBaseline + index * lineHeight, paint);
        return bitmap;
    }

    private List<String> wrapText(String text, Paint paint, float maxWidth) {
        List<String> lines = new ArrayList<>();
        StringBuilder line = new StringBuilder();
        for (String word : text.split("\\s+")) {
            String test = line.length() == 0 ? word : line + " " + word;
            if (paint.measureText(test) > maxWidth && line.length() > 0) {
                lines.add(line.toString());
                line = new StringBuilder(word);
            } else {
                line = new StringBuilder(test);
            }
        }
        if (line.length() > 0) lines.add(line.toString());
        return lines.isEmpty() ? Collections.singletonList(text) : lines;
    }

    private String viralTitle(String caption) {
        String subject = caption.isEmpty() ? "This Stream Moment" : caption;
        if (!viralTitleSwitch.isChecked()) return "Clip Farm Pilot " + new SimpleDateFormat("yyyy-MM-dd HH-mm", Locale.US).format(new Date());
        List<String> hooks = Arrays.asList(
            "Nobody Expected " + subject,
            "Wait for It – " + subject,
            "This Changed Everything – " + subject,
            "The Moment " + subject + " Happened",
            "I Still Cannot Believe " + subject,
            "This Was Not Supposed to Happen – " + subject
        );
        int index = Math.floorMod((int) (System.currentTimeMillis() / 1000L) + subject.hashCode(), hooks.size());
        return hooks.get(index);
    }

    private void shareExport() {
        if (exportFile == null || !exportFile.exists()) return;
        Uri uri = FileProvider.getUriForFile(this, getPackageName() + ".files", exportFile);
        Intent share = new Intent(Intent.ACTION_SEND);
        share.setType("video/mp4");
        share.putExtra(Intent.EXTRA_STREAM, uri);
        share.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        startActivity(Intent.createChooser(share, "Save or share clip"));
    }

    private File uniqueFile(File directory, String base, String suffix) {
        File candidate = new File(directory, base + suffix);
        int index = 2;
        while (candidate.exists()) candidate = new File(directory, base + " " + index++ + suffix);
        return candidate;
    }

    private String sanitize(String value) {
        String clean = value.replaceAll("[/\\\\:*?\"<>|\\n\\r]", " ").replaceAll("\\s+", " ").trim();
        if (clean.isEmpty()) clean = "Clip Farm Pilot";
        return clean.substring(0, Math.min(80, clean.length()));
    }

    private void setBusy(boolean busy) {
        exportButton.setEnabled(!busy && sourceUri != null);
    }

    private void setStatus(String status) {
        statusLabel.setText(status);
    }

    private String formatTime(long milliseconds) {
        long total = Math.max(0, Math.round(milliseconds / 1000.0));
        long hours = total / 3600;
        long minutes = (total % 3600) / 60;
        long seconds = total % 60;
        return hours > 0 ? String.format(Locale.US, "%d:%02d:%02d", hours, minutes, seconds) : String.format(Locale.US, "%d:%02d", minutes, seconds);
    }

    private LinearLayout section(String code, String title) {
        LinearLayout section = panel();
        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        TextView badge = text(code, 9, CYAN, true);
        badge.setPadding(dp(7), dp(4), dp(7), dp(4));
        header.addView(badge);
        TextView heading = text(title, 16, Color.WHITE, true);
        heading.setPadding(dp(9), 0, 0, 0);
        header.addView(heading);
        section.addView(header);
        return section;
    }

    private LinearLayout panel() {
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setPadding(dp(16), dp(15), dp(16), dp(15));
        panel.setBackgroundColor(PANEL);
        return panel;
    }

    private LinearLayout labeled(String label, View control) {
        LinearLayout block = new LinearLayout(this);
        block.setOrientation(LinearLayout.VERTICAL);
        block.setPadding(0, dp(10), 0, 0);
        block.addView(text(label.toUpperCase(Locale.US), 10, Color.rgb(128, 204, 232), true));
        block.addView(control, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        return block;
    }

    private Spinner spinner(String[] values) {
        Spinner spinner = new Spinner(this);
        ArrayAdapter<String> adapter = new ArrayAdapter<>(this, android.R.layout.simple_spinner_item, values);
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        spinner.setAdapter(adapter);
        return spinner;
    }

    private Switch toggle(String title, boolean checked) {
        Switch toggle = new Switch(this);
        toggle.setText(title);
        toggle.setTextColor(Color.WHITE);
        toggle.setChecked(checked);
        toggle.setPadding(0, dp(8), 0, dp(3));
        return toggle;
    }

    private Button button(String title, boolean primary) {
        Button button = new Button(this);
        button.setText(title);
        button.setAllCaps(false);
        button.setTextColor(primary ? Color.BLACK : Color.WHITE);
        button.setBackgroundColor(primary ? GOLD : Color.rgb(18, 43, 61));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.topMargin = dp(10);
        button.setLayoutParams(params);
        return button;
    }

    private TextView text(String value, int sp, int color, boolean bold) {
        TextView text = new TextView(this);
        text.setText(value);
        text.setTextSize(sp);
        text.setTextColor(color);
        if (bold) text.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        return text;
    }

    private LinearLayout.LayoutParams spacedParams() {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.topMargin = dp(14);
        return params;
    }

    private int dp(int value) {
        return Math.round(value * Resources.getSystem().getDisplayMetrics().density);
    }

    @Override
    protected void onDestroy() {
        exportInProgress = false;
        if (transformer != null) transformer.cancel();
        player.release();
        worker.shutdownNow();
        super.onDestroy();
    }
}
