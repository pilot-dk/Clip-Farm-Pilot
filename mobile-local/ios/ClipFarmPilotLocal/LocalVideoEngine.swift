@preconcurrency import AVFoundation
import CoreImage
import CoreMedia
@preconcurrency import Foundation
import Speech
import UIKit

enum LocalVideoError: LocalizedError {
    case unreadableVideo
    case noVideoTrack
    case exportUnavailable
    case exportFailed(String)
    case localSpeechUnavailable
    case speechPermissionDenied
    case transcriptionFailed(String)

    var errorDescription: String? {
        switch self {
        case .unreadableVideo: "The selected file is not a readable video."
        case .noVideoTrack: "The selected file does not contain a video track."
        case .exportUnavailable: "This device could not create a video exporter."
        case .exportFailed(let message): "The device could not finish the MP4: \(message)"
        case .localSpeechUnavailable: "On-device English speech recognition is not installed on this device. Nothing was uploaded."
        case .speechPermissionDenied: "Speech recognition permission is required for local live captions."
        case .transcriptionFailed(let message): "Local transcription failed: \(message)"
        }
    }
}

struct LocalExportResult {
    let url: URL
    let title: String
}

final class LocalVideoEngine: @unchecked Sendable {
    private let ciContext = CIContext(options: [.cacheIntermediates: true])

    func analyze(
        url: URL,
        progress: @escaping @Sendable (Double) -> Void
    ) async throws -> [ClipCandidate] {
        let asset = AVURLAsset(url: url)
        let duration = try await asset.load(.duration).seconds
        guard duration.isFinite, duration > 0 else { throw LocalVideoError.unreadableVideo }
        let tracks = try await asset.loadTracks(withMediaType: .audio)
        guard let track = tracks.first else { return fallbackCandidates(duration: duration) }

        return try await Task.detached(priority: .userInitiated) {
            let reader = try AVAssetReader(asset: asset)
            let output = AVAssetReaderTrackOutput(
                track: track,
                outputSettings: [
                    AVFormatIDKey: kAudioFormatLinearPCM,
                    AVLinearPCMBitDepthKey: 32,
                    AVLinearPCMIsFloatKey: true,
                    AVLinearPCMIsBigEndianKey: false,
                    AVLinearPCMIsNonInterleaved: false,
                    AVSampleRateKey: 16_000,
                    AVNumberOfChannelsKey: 1,
                ]
            )
            output.alwaysCopiesSampleData = false
            guard reader.canAdd(output) else { return self.fallbackCandidates(duration: duration) }
            reader.add(output)
            guard reader.startReading() else {
                throw LocalVideoError.exportFailed(reader.error?.localizedDescription ?? "Audio decoding did not start.")
            }

            let bucketCount = max(1, Int(ceil(duration)))
            var energy = Array(repeating: 0.0, count: bucketCount)
            var weights = Array(repeating: 0.0, count: bucketCount)
            var lastProgressBucket = -1

            while reader.status == .reading, let sample = output.copyNextSampleBuffer() {
                autoreleasepool {
                    guard let block = CMSampleBufferGetDataBuffer(sample) else { return }
                    var lengthAtOffset = 0
                    var totalLength = 0
                    var pointer: UnsafeMutablePointer<Int8>?
                    let result = CMBlockBufferGetDataPointer(
                        block,
                        atOffset: 0,
                        lengthAtOffsetOut: &lengthAtOffset,
                        totalLengthOut: &totalLength,
                        dataPointerOut: &pointer
                    )
                    guard result == kCMBlockBufferNoErr, let pointer, totalLength >= MemoryLayout<Float>.size else { return }
                    let count = totalLength / MemoryLayout<Float>.size
                    let samples = UnsafeRawPointer(pointer).bindMemory(to: Float.self, capacity: count)
                    var sum = 0.0
                    for index in 0..<count {
                        let value = Double(samples[index])
                        if value.isFinite { sum += value * value }
                    }
                    let rms = sqrt(sum / Double(max(1, count)))
                    let second = max(0, min(bucketCount - 1, Int(CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sample)))))
                    energy[second] += rms * Double(count)
                    weights[second] += Double(count)
                    if second != lastProgressBucket {
                        lastProgressBucket = second
                        progress(min(0.95, 0.05 + 0.9 * Double(second) / max(1, duration)))
                    }
                }
            }
            if reader.status == .failed {
                throw LocalVideoError.exportFailed(reader.error?.localizedDescription ?? "Audio analysis stopped.")
            }

            for index in energy.indices {
                energy[index] = weights[index] > 0 ? energy[index] / weights[index] : 0
                energy[index] = log10(max(energy[index], 0.000_001))
            }
            progress(0.98)
            return self.rankCandidates(energy: energy, duration: duration)
        }.value
    }

    func export(
        source: URL,
        sourceName: String,
        settings: ExportSettings,
        progress: @escaping @Sendable (String, Double) -> Void
    ) async throws -> LocalExportResult {
        let sourceAsset = AVURLAsset(url: source)
        let videoTracks = try await sourceAsset.loadTracks(withMediaType: .video)
        guard let sourceVideo = videoTracks.first else { throw LocalVideoError.noVideoTrack }
        let sourceDuration = try await sourceAsset.load(.duration).seconds
        let safeStart = max(0, min(settings.start, sourceDuration))
        let safeEnd = max(safeStart + 0.1, min(settings.end, sourceDuration))
        let clipDuration = safeEnd - safeStart
        let clipRange = CMTimeRange(
            start: CMTime(seconds: safeStart, preferredTimescale: 600),
            duration: CMTime(seconds: clipDuration, preferredTimescale: 600)
        )

        progress("Building an on-device timeline…", 0.04)
        let composition = AVMutableComposition()
        guard let videoTrack = composition.addMutableTrack(
            withMediaType: .video,
            preferredTrackID: kCMPersistentTrackID_Invalid
        ) else { throw LocalVideoError.noVideoTrack }
        try videoTrack.insertTimeRange(clipRange, of: sourceVideo, at: .zero)
        videoTrack.preferredTransform = try await sourceVideo.load(.preferredTransform)

        if let sourceAudio = try await sourceAsset.loadTracks(withMediaType: .audio).first,
           let audioTrack = composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid) {
            try audioTrack.insertTimeRange(clipRange, of: sourceAudio, at: .zero)
        }

        var effectAudioTrack: AVMutableCompositionTrack?
        if settings.vineBoom, let effectURL = Bundle.main.url(forResource: "Vine boom sound effect", withExtension: "mp3") {
            let effectAsset = AVURLAsset(url: effectURL)
            if let effectSource = try await effectAsset.loadTracks(withMediaType: .audio).first {
                let effectDuration = min(try await effectAsset.load(.duration).seconds, clipDuration)
                let moments = smartEffectTimes(duration: clipDuration, repeated: settings.smartSound)
                effectAudioTrack = composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)
                for moment in moments where moment + 0.05 < clipDuration {
                    try effectAudioTrack?.insertTimeRange(
                        CMTimeRange(start: .zero, duration: CMTime(seconds: effectDuration, preferredTimescale: 600)),
                        of: effectSource,
                        at: CMTime(seconds: moment, preferredTimescale: 600)
                    )
                }
            }
        }

        var spokenWords: [SpokenWord] = []
        if settings.liveCaptions || settings.viralTitle {
            do {
                progress("Transcribing speech on this device…", 0.10)
                spokenWords = try await transcribeLocally(asset: sourceAsset, range: clipRange)
            } catch {
                if settings.liveCaptions { throw error }
                spokenWords = []
            }
        }

        let title = makeViralTitle(
            sourceName: sourceName,
            squareCaption: settings.squareCaption,
            words: spokenWords,
            enabled: settings.viralTitle
        )
        let output = try exportURL(title: title)
        let targetSize = settings.ratio.renderSize
        let squareOverlay = settings.ratio == .square && !settings.squareCaption.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? makeSquareCaptionImage(
                text: settings.squareCaption,
                targetSize: targetSize,
                scale: settings.squareCaptionScale,
                position: settings.squareCaptionPosition
            )
            : nil
        let liveCaptionCache = CaptionImageCache()
        let payoff = clipDuration * 0.68

        let videoComposition = AVMutableVideoComposition(asset: composition) { [weak self] request in
            guard let self else {
                request.finish(with: NSError(domain: "ClipFarmPilotLocal", code: 1))
                return
            }
            autoreleasepool {
                var image = request.sourceImage
                image = self.applyLook(settings.videoLook, to: image)
                let seconds = CMTimeGetSeconds(request.compositionTime)
                if settings.visualEffect == .punchZoom {
                    image = self.applyPunchZoom(to: image, time: seconds, payoff: payoff, strength: settings.visualStrength)
                }
                var framed = settings.gamingLayout
                    ? self.gamingFrame(image, targetSize: targetSize, settings: settings)
                    : self.aspectFill(image, targetSize: targetSize)

                if let squareOverlay { framed = squareOverlay.composited(over: framed) }
                if settings.liveCaptions, !spokenWords.isEmpty,
                   let live = self.liveCaptionImage(
                    words: spokenWords,
                    time: seconds,
                    targetSize: targetSize,
                    colour: settings.liveCaptionColour.color,
                    cache: liveCaptionCache
                   ) {
                    framed = live.composited(over: framed)
                }
                framed = self.applyMomentVisual(
                    settings.visualEffect,
                    to: framed,
                    time: seconds,
                    payoff: payoff,
                    strength: settings.visualStrength,
                    targetSize: targetSize
                )
                request.finish(with: framed.cropped(to: CGRect(origin: .zero, size: targetSize)), context: self.ciContext)
            }
        }
        videoComposition.renderSize = targetSize
        videoComposition.frameDuration = CMTime(value: 1, timescale: 30)

        guard let exporter = AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetHighestQuality) else {
            throw LocalVideoError.exportUnavailable
        }
        exporter.outputURL = output
        exporter.outputFileType = .mp4
        exporter.shouldOptimizeForNetworkUse = false
        exporter.videoComposition = videoComposition
        if let effectAudioTrack {
            let mix = AVMutableAudioMix()
            let parameters = AVMutableAudioMixInputParameters(track: effectAudioTrack)
            parameters.setVolume(0.9, at: .zero)
            mix.inputParameters = [parameters]
            exporter.audioMix = mix
        }

        progress("Rendering locally with Apple’s media engine…", 0.18)
        try await run(exporter: exporter) { value in
            progress("Rendering locally · \(Int(value * 100))%", 0.18 + Double(value) * 0.80)
        }
        progress("Local MP4 ready", 1)
        return LocalExportResult(url: output, title: title)
    }

    private func fallbackCandidates(duration: Double) -> [ClipCandidate] {
        guard duration > 1 else { return [] }
        let clipLength = min(30, duration)
        let count = min(5, max(1, Int(duration / max(clipLength, 1))))
        return (0..<count).map { index in
            let fraction = Double(index + 1) / Double(count + 1)
            let start = max(0, min(duration - clipLength, duration * fraction - clipLength * 0.65))
            return ClipCandidate(start: start, end: min(duration, start + clipLength), score: 55, reason: "Evenly spaced local fallback")
        }
    }

    private func rankCandidates(energy: [Double], duration: Double) -> [ClipCandidate] {
        guard !energy.isEmpty else { return fallbackCandidates(duration: duration) }
        let finite = energy.filter(\.isFinite)
        let mean = finite.reduce(0, +) / Double(max(1, finite.count))
        let variance = finite.reduce(0) { $0 + pow($1 - mean, 2) } / Double(max(1, finite.count))
        let deviation = max(0.0001, sqrt(variance))

        var scored: [(index: Int, score: Double, rise: Double, sustain: Double)] = []
        for index in energy.indices {
            let localStart = max(0, index - 12)
            let setup = energy[localStart..<max(localStart + 1, index)].reduce(mean, +) / Double(max(1, index - localStart + 1))
            let prior = energy[max(0, index - 3)...index].reduce(0, +) / Double(min(4, index + 1))
            let afterEnd = min(energy.count - 1, index + 3)
            let sustain = energy[index...afterEnd].reduce(0, +) / Double(afterEnd - index + 1)
            let z = (energy[index] - mean) / deviation
            let rise = (energy[index] - setup) / deviation
            let score = z * 0.52 + rise * 0.31 + ((prior + sustain) / 2 - mean) / deviation * 0.17
            scored.append((index, score, rise, sustain))
        }
        scored.sort { $0.score > $1.score }

        var chosen: [ClipCandidate] = []
        for item in scored where chosen.count < 5 {
            let peak = Double(item.index)
            if chosen.contains(where: { abs(($0.start + ($0.end - $0.start) * 0.67) - peak) < 14 }) { continue }
            let clipLength = min(30, duration)
            let start = max(0, min(max(0, duration - clipLength), peak - clipLength * 0.67))
            let score = Int(max(50, min(99, 70 + item.score * 9)))
            let reason: String
            if item.rise > 1.3 { reason = "Sharp reaction rise and clear payoff" }
            else if item.sustain > mean + deviation * 0.6 { reason = "Sustained high-energy moment" }
            else { reason = "Strong local audio contrast" }
            chosen.append(ClipCandidate(start: start, end: min(duration, start + clipLength), score: score, reason: reason))
        }
        return chosen.sorted { $0.score > $1.score }
    }

    private func smartEffectTimes(duration: Double, repeated: Bool) -> [Double] {
        guard duration > 1 else { return [] }
        if repeated, duration >= 24 { return [duration * 0.37, duration * 0.69] }
        return [duration * 0.69]
    }

    private func applyLook(_ look: VideoLook, to image: CIImage) -> CIImage {
        func controls(saturation: Double = 1, contrast: Double = 1, brightness: Double = 0) -> CIImage {
            guard let filter = CIFilter(name: "CIColorControls") else { return image }
            filter.setValue(image, forKey: kCIInputImageKey)
            filter.setValue(saturation, forKey: kCIInputSaturationKey)
            filter.setValue(contrast, forKey: kCIInputContrastKey)
            filter.setValue(brightness, forKey: kCIInputBrightnessKey)
            return filter.outputImage ?? image
        }

        switch look {
        case .none: return image
        case .monochrome: return controls(saturation: 0, contrast: 1.08)
        case .cinematic:
            let base = controls(saturation: 0.82, contrast: 1.18, brightness: -0.025)
            guard let filter = CIFilter(name: "CITemperatureAndTint") else { return base }
            filter.setValue(base, forKey: kCIInputImageKey)
            filter.setValue(CIVector(x: 6_500, y: 0), forKey: "inputNeutral")
            filter.setValue(CIVector(x: 5_600, y: 4), forKey: "inputTargetNeutral")
            return filter.outputImage ?? base
        case .vivid: return controls(saturation: 1.32, contrast: 1.09, brightness: 0.015)
        case .warm:
            let base = controls(saturation: 1.07, contrast: 1.04)
            guard let filter = CIFilter(name: "CITemperatureAndTint") else { return base }
            filter.setValue(base, forKey: kCIInputImageKey)
            filter.setValue(CIVector(x: 6_500, y: 0), forKey: "inputNeutral")
            filter.setValue(CIVector(x: 5_100, y: 0), forKey: "inputTargetNeutral")
            return filter.outputImage ?? base
        case .cool:
            let base = controls(saturation: 1.03, contrast: 1.05)
            guard let filter = CIFilter(name: "CITemperatureAndTint") else { return base }
            filter.setValue(base, forKey: kCIInputImageKey)
            filter.setValue(CIVector(x: 6_500, y: 0), forKey: "inputNeutral")
            filter.setValue(CIVector(x: 8_000, y: 0), forKey: "inputTargetNeutral")
            return filter.outputImage ?? base
        case .vintage:
            guard let filter = CIFilter(name: "CISepiaTone") else { return controls(saturation: 0.72, contrast: 0.92) }
            filter.setValue(controls(saturation: 0.72, contrast: 0.92, brightness: 0.03), forKey: kCIInputImageKey)
            filter.setValue(0.32, forKey: kCIInputIntensityKey)
            return filter.outputImage ?? image
        case .contrast: return controls(saturation: 1.08, contrast: 1.36, brightness: -0.01)
        }
    }

    private func normalized(_ image: CIImage) -> CIImage {
        image.transformed(by: CGAffineTransform(translationX: -image.extent.minX, y: -image.extent.minY))
    }

    private func aspectFill(_ image: CIImage, targetSize: CGSize) -> CIImage {
        let source = normalized(image)
        guard source.extent.width > 0, source.extent.height > 0 else { return image }
        let scale = max(targetSize.width / source.extent.width, targetSize.height / source.extent.height)
        let scaled = source.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        let crop = CGRect(
            x: (scaled.extent.width - targetSize.width) / 2,
            y: (scaled.extent.height - targetSize.height) / 2,
            width: targetSize.width,
            height: targetSize.height
        )
        return scaled.cropped(to: crop).transformed(by: CGAffineTransform(translationX: -crop.minX, y: -crop.minY))
    }

    private func gamingFrame(_ image: CIImage, targetSize: CGSize, settings: ExportSettings) -> CIImage {
        let source = normalized(image)
        let faceW = max(80, source.extent.width * settings.faceWidth)
        let faceH = max(80, source.extent.height * settings.faceHeight)
        let insetX = source.extent.width * settings.faceInsetX
        let insetY = source.extent.height * settings.faceInsetY
        let left = settings.faceCorner == .topLeft || settings.faceCorner == .bottomLeft
        let top = settings.faceCorner == .topLeft || settings.faceCorner == .topRight
        let x = left ? insetX : source.extent.width - faceW - insetX
        let y = top ? source.extent.height - faceH - insetY : insetY
        let bounded = CGRect(
            x: max(0, min(source.extent.width - faceW, x)),
            y: max(0, min(source.extent.height - faceH, y)),
            width: min(faceW, source.extent.width),
            height: min(faceH, source.extent.height)
        )
        let faceSource = source.cropped(to: bounded).transformed(by: CGAffineTransform(translationX: -bounded.minX, y: -bounded.minY))
        let faceHeight = targetSize.height * 0.32
        let gameplayHeight = targetSize.height - faceHeight
        let face = aspectFill(faceSource, targetSize: CGSize(width: targetSize.width, height: faceHeight))
            .transformed(by: CGAffineTransform(translationX: 0, y: gameplayHeight))
        let gameplay = aspectFill(source, targetSize: CGSize(width: targetSize.width, height: gameplayHeight))
        let black = CIImage(color: .black).cropped(to: CGRect(origin: .zero, size: targetSize))
        let divider = CIImage(color: CIColor(red: 0.2, green: 0.82, blue: 1, alpha: 0.8))
            .cropped(to: CGRect(x: 0, y: gameplayHeight - 3, width: targetSize.width, height: 6))
        return divider.composited(over: face.composited(over: gameplay.composited(over: black)))
    }

    private func applyPunchZoom(to image: CIImage, time: Double, payoff: Double, strength: Double) -> CIImage {
        let distance = abs(time - payoff)
        guard distance < 0.38 else { return image }
        let pulse = 1 - distance / 0.38
        let factor = 1 + 0.10 * strength * pulse
        let normalizedImage = normalized(image)
        let center = CGPoint(x: normalizedImage.extent.midX, y: normalizedImage.extent.midY)
        return normalizedImage
            .transformed(by: CGAffineTransform(translationX: -center.x, y: -center.y))
            .transformed(by: CGAffineTransform(scaleX: factor, y: factor))
            .transformed(by: CGAffineTransform(translationX: center.x, y: center.y))
    }

    private func applyMomentVisual(
        _ effect: MomentVisual,
        to image: CIImage,
        time: Double,
        payoff: Double,
        strength: Double,
        targetSize: CGSize
    ) -> CIImage {
        let distance = abs(time - payoff)
        switch effect {
        case .none, .punchZoom:
            return image
        case .whiteFlash:
            guard distance < 0.20 else { return image }
            let alpha = max(0, (1 - distance / 0.20) * min(1, strength))
            let white = CIImage(color: CIColor(red: 1, green: 1, blue: 1, alpha: alpha))
                .cropped(to: CGRect(origin: .zero, size: targetSize))
            return white.composited(over: image)
        case .lensFlare:
            guard distance < 0.55, let flare = CIFilter(name: "CILenticularHaloGenerator") else { return image }
            let pulse = max(0, 1 - distance / 0.55) * strength
            flare.setValue(CIVector(x: targetSize.width * 0.73, y: targetSize.height * 0.72), forKey: kCIInputCenterKey)
            flare.setValue(targetSize.width * 0.22 * pulse, forKey: "inputHaloRadius")
            flare.setValue(0.55 * pulse, forKey: "inputHaloWidth")
            flare.setValue(0.65 * pulse, forKey: "inputHaloOverlap")
            flare.setValue(CIColor(red: 1, green: 0.8, blue: 0.45, alpha: min(1, pulse)), forKey: kCIInputColorKey)
            guard let output = flare.outputImage?.cropped(to: CGRect(origin: .zero, size: targetSize)) else { return image }
            return output.composited(over: image)
        }
    }

    private func makeSquareCaptionImage(
        text: String,
        targetSize: CGSize,
        scale: Double,
        position: CaptionPosition
    ) -> CIImage? {
        let font = UIFont.systemFont(ofSize: 76 * scale, weight: .heavy)
        let style = NSMutableParagraphStyle()
        style.alignment = .center
        style.lineBreakMode = .byWordWrapping
        let attributes: [NSAttributedString.Key: Any] = [
            .font: font,
            .foregroundColor: UIColor.white,
            .strokeColor: UIColor.black,
            .strokeWidth: -7,
            .paragraphStyle: style,
        ]
        let attributed = NSAttributedString(string: text, attributes: attributes)
        let maxWidth = targetSize.width * 0.88
        let measured = attributed.boundingRect(
            with: CGSize(width: maxWidth, height: targetSize.height * 0.42),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            context: nil
        ).integral
        let y: CGFloat
        switch position {
        case .top: y = targetSize.height * 0.12
        case .center: y = (targetSize.height - measured.height) / 2
        case .bottom: y = targetSize.height * 0.78 - measured.height / 2
        }
        let format = UIGraphicsImageRendererFormat()
        format.opaque = false
        format.scale = 1
        let renderer = UIGraphicsImageRenderer(size: targetSize, format: format)
        let bitmap = renderer.image { _ in
            attributed.draw(
                with: CGRect(x: (targetSize.width - maxWidth) / 2, y: y, width: maxWidth, height: measured.height + 12),
                options: [.usesLineFragmentOrigin, .usesFontLeading],
                context: nil
            )
        }
        return CIImage(image: bitmap)
    }

    private func liveCaptionImage(
        words: [SpokenWord],
        time: Double,
        targetSize: CGSize,
        colour: UIColor,
        cache: CaptionImageCache
    ) -> CIImage? {
        guard let activeIndex = words.lastIndex(where: { $0.start <= time && time <= $0.start + max(0.18, $0.duration + 0.12) }) else {
            return nil
        }
        let key = NSNumber(value: activeIndex)
        cache.lock.lock()
        if let cached = cache.images.object(forKey: key) {
            cache.lock.unlock()
            return cached
        }
        cache.lock.unlock()

        let groupStart = (activeIndex / 4) * 4
        let groupEnd = min(words.count, groupStart + 4)
        let attributed = NSMutableAttributedString()
        for index in groupStart..<groupEnd {
            if attributed.length > 0 { attributed.append(NSAttributedString(string: " ")) }
            let attributes: [NSAttributedString.Key: Any] = [
                .font: UIFont.systemFont(ofSize: targetSize.width * 0.067, weight: .heavy),
                .foregroundColor: index == activeIndex ? colour : UIColor.white,
                .strokeColor: UIColor.black,
                .strokeWidth: -6,
            ]
            attributed.append(NSAttributedString(string: words[index].text.uppercased(), attributes: attributes))
        }
        let style = NSMutableParagraphStyle()
        style.alignment = .center
        style.lineBreakMode = .byWordWrapping
        attributed.addAttribute(.paragraphStyle, value: style, range: NSRange(location: 0, length: attributed.length))
        let maxWidth = targetSize.width * 0.90
        let measured = attributed.boundingRect(
            with: CGSize(width: maxWidth, height: targetSize.height * 0.28),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            context: nil
        ).integral
        let y = targetSize.height * (targetSize.height > targetSize.width ? 0.68 : 0.73)
        let format = UIGraphicsImageRendererFormat()
        format.opaque = false
        format.scale = 1
        let renderer = UIGraphicsImageRenderer(size: targetSize, format: format)
        let bitmap = renderer.image { _ in
            attributed.draw(
                with: CGRect(x: (targetSize.width - maxWidth) / 2, y: y, width: maxWidth, height: measured.height + 16),
                options: [.usesLineFragmentOrigin, .usesFontLeading],
                context: nil
            )
        }
        guard let image = CIImage(image: bitmap) else { return nil }
        cache.lock.lock()
        cache.images.setObject(image, forKey: key, cost: Int(targetSize.width * targetSize.height * 4))
        cache.lock.unlock()
        return image
    }

    private func transcribeLocally(asset: AVAsset, range: CMTimeRange) async throws -> [SpokenWord] {
        let authorization = await speechAuthorization()
        guard authorization == .authorized else { throw LocalVideoError.speechPermissionDenied }
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en_US")), recognizer.supportsOnDeviceRecognition else {
            throw LocalVideoError.localSpeechUnavailable
        }
        let audioURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipfarmpilot-speech-\(UUID().uuidString)")
            .appendingPathExtension("m4a")
        defer { try? FileManager.default.removeItem(at: audioURL) }

        let audioComposition = AVMutableComposition()
        guard let sourceAudio = try await asset.loadTracks(withMediaType: .audio).first,
              let track = audioComposition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid) else {
            return []
        }
        try track.insertTimeRange(range, of: sourceAudio, at: .zero)
        guard let exporter = AVAssetExportSession(asset: audioComposition, presetName: AVAssetExportPresetAppleM4A) else {
            throw LocalVideoError.exportUnavailable
        }
        exporter.outputURL = audioURL
        exporter.outputFileType = .m4a
        try await run(exporter: exporter) { _ in }

        let request = SFSpeechURLRecognitionRequest(url: audioURL)
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = false
        request.taskHint = .dictation

        return try await withCheckedThrowingContinuation { continuation in
            let box = RecognitionBox(continuation: continuation)
            box.task = recognizer.recognitionTask(with: request) { result, error in
                if let error {
                    box.fail(LocalVideoError.transcriptionFailed(error.localizedDescription))
                    return
                }
                guard let result, result.isFinal else { return }
                let words = result.bestTranscription.segments.map {
                    SpokenWord(text: $0.substring, start: $0.timestamp, duration: $0.duration)
                }
                box.succeed(words)
            }
        }
    }

    private func speechAuthorization() async -> SFSpeechRecognizerAuthorizationStatus {
        let current = SFSpeechRecognizer.authorizationStatus()
        guard current == .notDetermined else { return current }
        return await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { continuation.resume(returning: $0) }
        }
    }

    private func run(exporter: AVAssetExportSession, progress: @escaping @Sendable (Float) -> Void) async throws {
        let monitor = Task {
            while !Task.isCancelled {
                progress(exporter.progress)
                try? await Task.sleep(for: .milliseconds(250))
            }
        }
        defer { monitor.cancel() }

        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let box = ExportSessionBox(exporter)
            box.session.exportAsynchronously {
                switch box.session.status {
                case .completed:
                    continuation.resume()
                case .failed, .cancelled:
                    continuation.resume(throwing: LocalVideoError.exportFailed(box.session.error?.localizedDescription ?? "Unknown media error"))
                default:
                    continuation.resume(throwing: LocalVideoError.exportFailed("The media exporter ended unexpectedly."))
                }
            }
        }
    }

    private func exportURL(title: String) throws -> URL {
        let documents = try FileManager.default.url(
            for: .documentDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let exports = documents.appendingPathComponent("Exports", isDirectory: true)
        try FileManager.default.createDirectory(at: exports, withIntermediateDirectories: true)
        let clean = sanitized(title).prefix(80)
        let base = clean.isEmpty ? "Clip Farm Pilot" : String(clean)
        var url = exports.appendingPathComponent(base).appendingPathExtension("mp4")
        var index = 2
        while FileManager.default.fileExists(atPath: url.path) {
            url = exports.appendingPathComponent("\(base) \(index)").appendingPathExtension("mp4")
            index += 1
        }
        return url
    }

    private func makeViralTitle(sourceName: String, squareCaption: String, words: [SpokenWord], enabled: Bool) -> String {
        let source = sourceName.replacingOccurrences(of: ".\(URL(fileURLWithPath: sourceName).pathExtension)", with: "")
        guard enabled else { return "Clip Farm Pilot – \(source)" }
        let caption = squareCaption.trimmingCharacters(in: .whitespacesAndNewlines)
        let transcript = words.prefix(8).map(\.text).joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
        let subject = caption.isEmpty ? (transcript.isEmpty ? source : transcript) : caption
        let hooks = [
            "Nobody Expected \(subject)",
            "This Changed Everything – \(subject)",
            "Wait for It – \(subject)",
            "The Moment \(subject) Happened",
            "I Still Cannot Believe \(subject)",
            "This Was Not Supposed to Happen – \(subject)",
        ]
        let seed = Int(Date().timeIntervalSince1970) + subject.hashValue
        return hooks[abs(seed) % hooks.count]
    }

    private func sanitized(_ value: String) -> String {
        value
            .components(separatedBy: CharacterSet(charactersIn: "/:\\?%*|\"<>\n\r")).joined(separator: " ")
            .replacingOccurrences(of: "  ", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

private final class CaptionImageCache: @unchecked Sendable {
    let images = NSCache<NSNumber, CIImage>()
    let lock = NSLock()
}

private final class ExportSessionBox: @unchecked Sendable {
    let session: AVAssetExportSession

    init(_ session: AVAssetExportSession) {
        self.session = session
    }
}

private final class RecognitionBox {
    private let lock = NSLock()
    private var completed = false
    private var continuation: CheckedContinuation<[SpokenWord], Error>?
    var task: SFSpeechRecognitionTask?

    init(continuation: CheckedContinuation<[SpokenWord], Error>) {
        self.continuation = continuation
    }

    func succeed(_ words: [SpokenWord]) {
        finish(.success(words))
    }

    func fail(_ error: Error) {
        finish(.failure(error))
    }

    private func finish(_ result: Result<[SpokenWord], Error>) {
        lock.lock()
        guard !completed, let continuation else {
            lock.unlock()
            return
        }
        completed = true
        self.continuation = nil
        lock.unlock()
        task?.cancel()
        switch result {
        case .success(let words): continuation.resume(returning: words)
        case .failure(let error): continuation.resume(throwing: error)
        }
    }
}
