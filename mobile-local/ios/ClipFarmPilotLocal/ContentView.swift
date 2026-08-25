import AVKit
import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @EnvironmentObject private var editor: EditorViewModel
    @State private var showingImporter = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 14) {
                    localOnlyBanner
                    sourcePanel

                    if editor.sourceURL != nil {
                        previewPanel
                        rangePanel
                        momentPanel
                        framePanel
                        captionPanel
                        effectsPanel
                        exportPanel
                    }
                }
                .padding(16)
                .padding(.bottom, 36)
            }
            .background(Color(red: 0.025, green: 0.063, blue: 0.102))
            .navigationTitle("Clip Farm Pilot")
            .toolbarBackground(Color(red: 0.025, green: 0.063, blue: 0.102), for: .navigationBar)
            .fileImporter(
                isPresented: $showingImporter,
                allowedContentTypes: [.movie, .mpeg4Movie, .quickTimeMovie],
                allowsMultipleSelection: false
            ) { result in
                Task { await editor.handleImport(result) }
            }
        }
    }

    private var localOnlyBanner: some View {
        HStack(spacing: 10) {
            Image(systemName: "iphone.gen3.radiowaves.left.and.right")
                .foregroundStyle(.mint)
            VStack(alignment: .leading, spacing: 2) {
                Text("LOCAL FLIGHT MODE")
                    .font(.caption2.bold())
                    .tracking(1.2)
                    .foregroundStyle(.mint)
                Text("Videos stay on this device. No uploads or cloud processing.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Image(systemName: "lock.shield.fill")
                .foregroundStyle(.mint)
        }
        .padding(14)
        .background(Color(red: 0.035, green: 0.13, blue: 0.16), in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.mint.opacity(0.25)))
    }

    private var sourcePanel: some View {
        FlightPanel(code: "01", title: "Flight source") {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text(editor.sourceName)
                        .font(.subheadline.bold())
                        .lineLimit(1)
                    Text(editor.sourceURL == nil ? "Choose a video from Photos or Files" : editor.duration.clockText)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button(editor.sourceURL == nil ? "Choose video" : "Replace") {
                    showingImporter = true
                }
                .buttonStyle(PilotButtonStyle(primary: true))
                .disabled(editor.isBusy)
            }
        }
    }

    private var previewPanel: some View {
        FlightPanel(code: "02", title: "Preview") {
            VideoPlayer(player: editor.player)
                .aspectRatio(editor.ratio.renderSize, contentMode: .fit)
                .frame(maxWidth: .infinity)
                .background(.black)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(alignment: .top) {
                    if editor.gamingLayout {
                        Text("FACE CAM")
                            .font(.caption2.bold())
                            .tracking(1)
                            .padding(.horizontal, 9)
                            .padding(.vertical, 5)
                            .background(.black.opacity(0.7), in: Capsule())
                            .padding(8)
                    }
                }
            HStack {
                Button("Play clip range", systemImage: "play.fill") { editor.playSelectedRange() }
                    .buttonStyle(PilotButtonStyle(primary: false))
                Button("Stop", systemImage: "stop.fill") { editor.player.pause() }
                    .buttonStyle(PilotButtonStyle(primary: false))
            }
        }
    }

    private var rangePanel: some View {
        FlightPanel(code: "03", title: "Clip range") {
            HStack {
                Text("Start")
                Spacer()
                Text(editor.clipStart.clockText).monospacedDigit().foregroundStyle(.cyan)
            }
            Slider(value: $editor.clipStart, in: 0...max(0.1, editor.clipEnd - 0.5), step: 0.1) { editing in
                if !editing { editor.seek(to: editor.clipStart) }
            }

            HStack {
                Text("End")
                Spacer()
                Text(editor.clipEnd.clockText).monospacedDigit().foregroundStyle(.cyan)
            }
            Slider(value: $editor.clipEnd, in: min(editor.duration, editor.clipStart + 0.5)...max(editor.duration, editor.clipStart + 0.5), step: 0.1) { editing in
                if !editing { editor.seek(to: editor.clipEnd) }
            }
            Text("Duration: \(max(0, editor.clipEnd - editor.clipStart).clockText)")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var momentPanel: some View {
        FlightPanel(code: "04", title: "Moment radar") {
            Button(editor.candidates.isEmpty ? "Auto-Find Clips" : "Scan again", systemImage: "waveform.path.ecg") {
                Task { await editor.analyze() }
            }
            .buttonStyle(PilotButtonStyle(primary: true))
            .disabled(editor.isBusy)

            ForEach(editor.candidates) { candidate in
                Button {
                    editor.select(candidate)
                } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("\(candidate.start.clockText) – \(candidate.end.clockText)")
                                .font(.subheadline.bold())
                            Text(candidate.reason)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text("\(candidate.score)")
                            .font(.headline.monospacedDigit())
                            .foregroundStyle(.cyan)
                            .padding(9)
                            .background(Color.cyan.opacity(0.12), in: RoundedRectangle(cornerRadius: 9))
                    }
                    .padding(11)
                    .background(Color.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 11))
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var framePanel: some View {
        FlightPanel(code: "05", title: "Output frame") {
            Picker("Aspect ratio", selection: $editor.ratio) {
                ForEach(ExportRatio.allCases) { ratio in Text(ratio.rawValue).tag(ratio) }
            }
            .pickerStyle(.segmented)

            Toggle("Gaming layout · face cam on top", isOn: $editor.gamingLayout)
                .disabled(editor.ratio != .portrait)

            if editor.gamingLayout {
                Picker("Source face-cam corner", selection: $editor.faceCorner) {
                    ForEach(FaceCorner.allCases) { corner in Text(corner.rawValue).tag(corner) }
                }
                LabeledSlider(label: "Face crop width", value: $editor.faceWidth, range: 0.12...0.55)
                LabeledSlider(label: "Face crop height", value: $editor.faceHeight, range: 0.12...0.55)
                LabeledSlider(label: "Horizontal inset", value: $editor.faceInsetX, range: 0...0.25)
                LabeledSlider(label: "Vertical inset", value: $editor.faceInsetY, range: 0...0.25)
            }
        }
        .onChange(of: editor.ratio) { _, ratio in
            if ratio != .portrait { editor.gamingLayout = false }
        }
    }

    private var captionPanel: some View {
        FlightPanel(code: "CC", title: "Captions") {
            if editor.ratio == .square {
                TextField("Square caption, including emoji ❤️", text: $editor.squareCaption, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                LabeledSlider(label: "Square caption size", value: $editor.squareCaptionScale, range: 0.5...1.75)
                Picker("Square caption position", selection: $editor.squareCaptionPosition) {
                    ForEach(CaptionPosition.allCases) { position in Text(position.rawValue).tag(position) }
                }
                .pickerStyle(.segmented)
            }

            Toggle("Live word-highlight captions", isOn: $editor.liveCaptions)
            if editor.liveCaptions {
                Picker("Highlight colour", selection: $editor.liveCaptionColour) {
                    ForEach(LiveCaptionColour.allCases) { colour in
                        Label(colour.rawValue, systemImage: "circle.fill").tag(colour)
                    }
                }
                Text("Speech recognition is forced into Apple’s on-device mode. If the device has no local English model, the export stops instead of sending audio to a server.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var effectsPanel: some View {
        FlightPanel(code: "FX", title: "Local effects") {
            Picker("Video filter", selection: $editor.videoLook) {
                ForEach(VideoLook.allCases) { look in Text(look.rawValue).tag(look) }
            }
            Picker("Moment visual", selection: $editor.visualEffect) {
                ForEach(MomentVisual.allCases) { effect in Text(effect.rawValue).tag(effect) }
            }
            if editor.visualEffect != .none {
                LabeledSlider(label: "Visual strength", value: $editor.visualStrength, range: 0.25...1.5)
            }
            Toggle("Vine Boom", isOn: $editor.vineBoom)
            if editor.vineBoom {
                Toggle("Smart repeat placement", isOn: $editor.smartSound)
            }
            Toggle("Generate a fresh local viral title", isOn: $editor.viralTitle)
        }
    }

    private var exportPanel: some View {
        FlightPanel(code: "06", title: "Export") {
            HStack(spacing: 10) {
                Circle()
                    .fill(editor.isBusy ? Color.cyan : Color.mint)
                    .frame(width: 9, height: 9)
                VStack(alignment: .leading, spacing: 3) {
                    Text("LOCAL STATUS")
                        .font(.caption2.bold())
                        .tracking(1)
                        .foregroundStyle(.secondary)
                    Text(editor.status)
                        .font(.caption)
                }
                Spacer()
                if editor.isBusy { ProgressView().tint(.cyan) }
            }
            if editor.isBusy {
                ProgressView(value: editor.progress)
                    .tint(.cyan)
            }

            Button("Export clip on this device", systemImage: "square.and.arrow.down") {
                Task { await editor.export() }
            }
            .buttonStyle(PilotButtonStyle(primary: true))
            .disabled(editor.isBusy)

            if let url = editor.exportURL {
                Text(editor.generatedTitle)
                    .font(.subheadline.bold())
                    .foregroundStyle(.green)
                ShareLink(item: url) {
                    Label("Save or share exported MP4", systemImage: "square.and.arrow.up")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(PilotButtonStyle(primary: false))
            }
        }
    }
}

private struct FlightPanel<Content: View>: View {
    let code: String
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Text(code)
                    .font(.caption2.bold())
                    .foregroundStyle(.cyan)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 3)
                    .overlay(RoundedRectangle(cornerRadius: 5).stroke(Color.cyan.opacity(0.4)))
                Text(title).font(.headline)
                Rectangle().fill(Color.cyan.opacity(0.2)).frame(height: 1)
            }
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(Color(red: 0.043, green: 0.094, blue: 0.141), in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color(red: 0.1, green: 0.24, blue: 0.33)))
    }
}

private struct LabeledSlider: View {
    let label: String
    @Binding var value: Double
    let range: ClosedRange<Double>

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(label).font(.caption)
                Spacer()
                Text("\(Int(value * 100))%")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.cyan)
            }
            Slider(value: $value, in: range)
        }
    }
}

private struct PilotButtonStyle: ButtonStyle {
    let primary: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.subheadline.bold())
            .frame(maxWidth: .infinity)
            .padding(.vertical, 11)
            .padding(.horizontal, 12)
            .background(primary ? Color(red: 1, green: 0.74, blue: 0.35) : Color(red: 0.07, green: 0.17, blue: 0.24))
            .foregroundStyle(primary ? Color.black : Color.white)
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .overlay {
                if !primary { RoundedRectangle(cornerRadius: 10).stroke(Color.cyan.opacity(0.25)) }
            }
            .opacity(configuration.isPressed ? 0.72 : 1)
    }
}

private extension Double {
    var clockText: String {
        guard isFinite else { return "0:00" }
        let total = max(0, Int(self.rounded()))
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let seconds = total % 60
        return hours > 0 ? String(format: "%d:%02d:%02d", hours, minutes, seconds) : String(format: "%d:%02d", minutes, seconds)
    }
}
