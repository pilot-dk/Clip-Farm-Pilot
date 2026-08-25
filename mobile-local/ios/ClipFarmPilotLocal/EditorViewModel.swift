import AVFoundation
import Foundation
import SwiftUI

@MainActor
final class EditorViewModel: ObservableObject {
    @Published var sourceURL: URL?
    @Published var sourceName = "No source loaded"
    @Published var duration = 0.0
    @Published var clipStart = 0.0
    @Published var clipEnd = 30.0
    @Published var player = AVPlayer()
    @Published var candidates: [ClipCandidate] = []

    @Published var ratio: ExportRatio = .portrait
    @Published var gamingLayout = false
    @Published var faceCorner: FaceCorner = .topRight
    @Published var faceWidth = 0.30
    @Published var faceHeight = 0.34
    @Published var faceInsetX = 0.0
    @Published var faceInsetY = 0.0

    @Published var squareCaption = ""
    @Published var squareCaptionScale = 1.0
    @Published var squareCaptionPosition: CaptionPosition = .center
    @Published var videoLook: VideoLook = .none
    @Published var vineBoom = false
    @Published var smartSound = true
    @Published var visualEffect: MomentVisual = .none
    @Published var visualStrength = 1.0
    @Published var liveCaptions = false
    @Published var liveCaptionColour: LiveCaptionColour = .lime
    @Published var viralTitle = true

    @Published var status = "Preflight ready"
    @Published var progress = 0.0
    @Published var isBusy = false
    @Published var exportURL: URL?
    @Published var generatedTitle = ""

    private let engine = LocalVideoEngine()
    private var playbackToken = UUID()

    func handleImport(_ result: Result<[URL], Error>) async {
        switch result {
        case .failure(let error):
            status = "Could not open video: \(error.localizedDescription)"
        case .success(let urls):
            guard let selected = urls.first else { return }
            await importVideo(selected)
        }
    }

    func importVideo(_ selected: URL) async {
        isBusy = true
        status = "Copying video into local storage…"
        progress = 0.05
        exportURL = nil
        candidates = []

        let scoped = selected.startAccessingSecurityScopedResource()
        defer { if scoped { selected.stopAccessingSecurityScopedResource() } }

        do {
            let videos = try localDirectory(named: "Videos")
            let extensionName = selected.pathExtension.isEmpty ? "mp4" : selected.pathExtension
            let cleanBase = sanitizedFilename(selected.deletingPathExtension().lastPathComponent)
            let destination = uniqueURL(in: videos, base: cleanBase.isEmpty ? "Livestream" : cleanBase, extensionName: extensionName)
            try FileManager.default.copyItem(at: selected, to: destination)

            let asset = AVURLAsset(url: destination)
            let loadedDuration = try await asset.load(.duration).seconds
            guard loadedDuration.isFinite, loadedDuration > 0 else {
                throw LocalVideoError.unreadableVideo
            }

            sourceURL = destination
            sourceName = selected.lastPathComponent
            duration = loadedDuration
            clipStart = 0
            clipEnd = min(30, loadedDuration)
            player.replaceCurrentItem(with: AVPlayerItem(asset: asset))
            status = "Local source ready · \(destination.lastPathComponent)"
            progress = 1
        } catch {
            status = error.localizedDescription
        }
        isBusy = false
    }

    func seek(to seconds: Double) {
        let time = CMTime(seconds: max(0, min(duration, seconds)), preferredTimescale: 600)
        player.seek(to: time, toleranceBefore: .zero, toleranceAfter: .zero)
    }

    func playSelectedRange() {
        playbackToken = UUID()
        let token = playbackToken
        seek(to: clipStart)
        player.play()
        let delay = max(0.2, clipEnd - clipStart)
        Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard let self, self.playbackToken == token else { return }
            self.player.pause()
        }
    }

    func analyze() async {
        guard let sourceURL else { return }
        isBusy = true
        progress = 0.05
        status = "Moment Radar is reading audio locally…"
        exportURL = nil

        do {
            let found = try await engine.analyze(url: sourceURL) { fraction in
                Task { @MainActor [weak self] in self?.progress = fraction }
            }
            candidates = found
            if let first = found.first { select(first) }
            status = found.isEmpty ? "No strong moments found; set a range manually." : "\(found.count) local moments ready"
            progress = 1
        } catch {
            status = "Moment analysis failed: \(error.localizedDescription)"
        }
        isBusy = false
    }

    func select(_ candidate: ClipCandidate) {
        clipStart = candidate.start
        clipEnd = candidate.end
        seek(to: candidate.start)
        status = "Selected \(candidate.start.clockText) – \(candidate.end.clockText)"
    }

    func export() async {
        guard let sourceURL else { return }
        guard clipEnd > clipStart else {
            status = "End time must be after start time."
            return
        }

        isBusy = true
        progress = 0.01
        exportURL = nil
        status = liveCaptions ? "Preparing on-device speech captions…" : "Preparing local export…"

        let settings = ExportSettings(
            start: clipStart,
            end: clipEnd,
            ratio: ratio,
            gamingLayout: gamingLayout && ratio == .portrait,
            faceCorner: faceCorner,
            faceWidth: faceWidth,
            faceHeight: faceHeight,
            faceInsetX: faceInsetX,
            faceInsetY: faceInsetY,
            squareCaption: squareCaption,
            squareCaptionScale: squareCaptionScale,
            squareCaptionPosition: squareCaptionPosition,
            videoLook: videoLook,
            vineBoom: vineBoom,
            smartSound: smartSound,
            visualEffect: visualEffect,
            visualStrength: visualStrength,
            liveCaptions: liveCaptions,
            liveCaptionColour: liveCaptionColour,
            viralTitle: viralTitle
        )

        do {
            let result = try await engine.export(source: sourceURL, sourceName: sourceName, settings: settings) { stage, fraction in
                Task { @MainActor [weak self] in
                    self?.status = stage
                    self?.progress = fraction
                }
            }
            exportURL = result.url
            generatedTitle = result.title
            status = "Export ready · stored locally"
            progress = 1
        } catch {
            status = "Export failed: \(error.localizedDescription)"
        }
        isBusy = false
    }

    private func localDirectory(named name: String) throws -> URL {
        let root = try FileManager.default.url(
            for: .documentDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let directory = root.appendingPathComponent(name, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private func uniqueURL(in directory: URL, base: String, extensionName: String) -> URL {
        var candidate = directory.appendingPathComponent(base).appendingPathExtension(extensionName)
        var index = 2
        while FileManager.default.fileExists(atPath: candidate.path) {
            candidate = directory.appendingPathComponent("\(base)-\(index)").appendingPathExtension(extensionName)
            index += 1
        }
        return candidate
    }

    private func sanitizedFilename(_ value: String) -> String {
        value
            .components(separatedBy: CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_ ")).inverted)
            .joined()
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

private extension Double {
    var clockText: String {
        let total = max(0, Int(self.rounded()))
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let seconds = total % 60
        return hours > 0 ? String(format: "%d:%02d:%02d", hours, minutes, seconds) : String(format: "%d:%02d", minutes, seconds)
    }
}
