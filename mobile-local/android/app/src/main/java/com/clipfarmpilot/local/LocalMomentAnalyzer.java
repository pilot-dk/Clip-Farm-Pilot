package com.clipfarmpilot.local;

import android.content.Context;
import android.media.MediaCodec;
import android.media.MediaExtractor;
import android.media.MediaFormat;
import android.net.Uri;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;

final class LocalMomentAnalyzer {
    interface ProgressListener {
        void onProgress(double value);
    }

    static final class Candidate {
        final long startMs;
        final long endMs;
        final int score;
        final String reason;

        Candidate(long startMs, long endMs, int score, String reason) {
            this.startMs = startMs;
            this.endMs = endMs;
            this.score = score;
            this.reason = reason;
        }
    }

    private static final class ScoredSecond {
        final int second;
        final double score;
        final double rise;
        final double sustain;

        ScoredSecond(int second, double score, double rise, double sustain) {
            this.second = second;
            this.score = score;
            this.rise = rise;
            this.sustain = sustain;
        }
    }

    static List<Candidate> analyze(Context context, Uri uri, long durationMs, ProgressListener listener) throws Exception {
        int seconds = Math.max(1, (int) Math.ceil(durationMs / 1000.0));
        double[] energy = new double[seconds];
        long[] counts = new long[seconds];
        MediaExtractor extractor = new MediaExtractor();
        MediaCodec decoder = null;

        try {
            extractor.setDataSource(context, uri, null);
            int audioTrack = -1;
            MediaFormat inputFormat = null;
            for (int index = 0; index < extractor.getTrackCount(); index++) {
                MediaFormat candidate = extractor.getTrackFormat(index);
                String mime = candidate.getString(MediaFormat.KEY_MIME);
                if (mime != null && mime.startsWith("audio/")) {
                    audioTrack = index;
                    inputFormat = candidate;
                    break;
                }
            }
            if (audioTrack < 0 || inputFormat == null) return fallback(durationMs);

            extractor.selectTrack(audioTrack);
            String mime = inputFormat.getString(MediaFormat.KEY_MIME);
            if (mime == null) return fallback(durationMs);
            decoder = MediaCodec.createDecoderByType(mime);
            decoder.configure(inputFormat, null, null, 0);
            decoder.start();

            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            boolean inputDone = false;
            boolean outputDone = false;
            int lastReported = -1;

            while (!outputDone) {
                if (!inputDone) {
                    int inputIndex = decoder.dequeueInputBuffer(10_000);
                    if (inputIndex >= 0) {
                        ByteBuffer input = decoder.getInputBuffer(inputIndex);
                        if (input == null) continue;
                        int size = extractor.readSampleData(input, 0);
                        if (size < 0) {
                            decoder.queueInputBuffer(inputIndex, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM);
                            inputDone = true;
                        } else {
                            decoder.queueInputBuffer(inputIndex, 0, size, extractor.getSampleTime(), 0);
                            extractor.advance();
                        }
                    }
                }

                int outputIndex = decoder.dequeueOutputBuffer(info, 10_000);
                if (outputIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) continue;
                if (outputIndex >= 0) {
                    ByteBuffer output = decoder.getOutputBuffer(outputIndex);
                    if (output != null && info.size > 1) {
                        output.position(info.offset);
                        output.limit(info.offset + info.size);
                        output.order(ByteOrder.nativeOrder());
                        long sum = 0;
                        int sampleCount = info.size / 2;
                        while (output.remaining() >= 2) {
                            int value = output.getShort();
                            sum += (long) value * value;
                        }
                        int second = Math.max(0, Math.min(seconds - 1, (int) (info.presentationTimeUs / 1_000_000L)));
                        energy[second] += Math.sqrt(sum / (double) Math.max(1, sampleCount)) * sampleCount;
                        counts[second] += sampleCount;
                        if (second != lastReported) {
                            lastReported = second;
                            listener.onProgress(Math.min(0.95, 0.05 + 0.9 * second / Math.max(1.0, seconds)));
                        }
                    }
                    outputDone = (info.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0;
                    decoder.releaseOutputBuffer(outputIndex, false);
                }
            }
        } finally {
            if (decoder != null) {
                try { decoder.stop(); } catch (Exception ignored) { }
                decoder.release();
            }
            extractor.release();
        }

        for (int index = 0; index < energy.length; index++) {
            double value = counts[index] > 0 ? energy[index] / counts[index] : 0.000001;
            energy[index] = Math.log10(Math.max(0.000001, value));
        }
        listener.onProgress(0.98);
        return rank(energy, durationMs);
    }

    private static List<Candidate> rank(double[] energy, long durationMs) {
        double mean = 0;
        for (double value : energy) mean += value;
        mean /= Math.max(1, energy.length);
        double variance = 0;
        for (double value : energy) variance += Math.pow(value - mean, 2);
        double deviation = Math.max(0.0001, Math.sqrt(variance / Math.max(1, energy.length)));

        List<ScoredSecond> scored = new ArrayList<>();
        for (int index = 0; index < energy.length; index++) {
            int setupStart = Math.max(0, index - 12);
            double setup = 0;
            int setupCount = 0;
            for (int i = setupStart; i < index; i++) { setup += energy[i]; setupCount++; }
            setup = setupCount == 0 ? mean : setup / setupCount;

            double prior = 0;
            int priorCount = 0;
            for (int i = Math.max(0, index - 3); i <= index; i++) { prior += energy[i]; priorCount++; }
            prior /= priorCount;
            double sustain = 0;
            int sustainCount = 0;
            for (int i = index; i <= Math.min(energy.length - 1, index + 3); i++) { sustain += energy[i]; sustainCount++; }
            sustain /= sustainCount;
            double z = (energy[index] - mean) / deviation;
            double rise = (energy[index] - setup) / deviation;
            double score = z * 0.52 + rise * 0.31 + (((prior + sustain) / 2) - mean) / deviation * 0.17;
            scored.add(new ScoredSecond(index, score, rise, sustain));
        }
        scored.sort((a, b) -> Double.compare(b.score, a.score));

        List<Candidate> chosen = new ArrayList<>();
        long clipLength = Math.min(30_000, durationMs);
        for (ScoredSecond item : scored) {
            if (chosen.size() >= 5) break;
            long peakMs = item.second * 1000L;
            boolean overlaps = false;
            for (Candidate candidate : chosen) {
                long candidatePeak = candidate.startMs + Math.round((candidate.endMs - candidate.startMs) * 0.67);
                if (Math.abs(candidatePeak - peakMs) < 14_000) { overlaps = true; break; }
            }
            if (overlaps) continue;
            long start = Math.max(0, Math.min(Math.max(0, durationMs - clipLength), peakMs - Math.round(clipLength * 0.67)));
            int score = (int) Math.max(50, Math.min(99, 70 + item.score * 9));
            String reason = item.rise > 1.3 ? "Sharp reaction rise and clear payoff"
                : item.sustain > mean + deviation * 0.6 ? "Sustained high-energy moment"
                : "Strong local audio contrast";
            chosen.add(new Candidate(start, Math.min(durationMs, start + clipLength), score, reason));
        }
        chosen.sort(Comparator.comparingInt((Candidate candidate) -> candidate.score).reversed());
        return chosen.isEmpty() ? fallback(durationMs) : chosen;
    }

    private static List<Candidate> fallback(long durationMs) {
        if (durationMs <= 0) return Collections.emptyList();
        long clipLength = Math.min(30_000, durationMs);
        List<Candidate> result = new ArrayList<>();
        int count = Math.min(5, Math.max(1, (int) (durationMs / Math.max(1, clipLength))));
        for (int index = 0; index < count; index++) {
            double fraction = (index + 1.0) / (count + 1.0);
            long start = Math.max(0, Math.min(durationMs - clipLength, Math.round(durationMs * fraction - clipLength * 0.65)));
            result.add(new Candidate(start, Math.min(durationMs, start + clipLength), 55, "Evenly spaced local fallback"));
        }
        return result;
    }

    private LocalMomentAnalyzer() { }
}
