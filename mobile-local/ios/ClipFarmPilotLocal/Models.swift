import CoreGraphics
import Foundation
import SwiftUI

enum ExportRatio: String, CaseIterable, Identifiable {
    case landscape = "16:9"
    case portrait = "9:16"
    case square = "1:1"

    var id: String { rawValue }

    var renderSize: CGSize {
        switch self {
        case .landscape: CGSize(width: 1920, height: 1080)
        case .portrait: CGSize(width: 1080, height: 1920)
        case .square: CGSize(width: 1080, height: 1080)
        }
    }
}

enum FaceCorner: String, CaseIterable, Identifiable {
    case topLeft = "Top left"
    case topRight = "Top right"
    case bottomLeft = "Bottom left"
    case bottomRight = "Bottom right"

    var id: String { rawValue }
}

enum CaptionPosition: String, CaseIterable, Identifiable {
    case top = "Top"
    case center = "Centre"
    case bottom = "Bottom"

    var id: String { rawValue }
}

enum VideoLook: String, CaseIterable, Identifiable {
    case none = "None"
    case monochrome = "Black & white"
    case cinematic = "Cinematic"
    case vivid = "Vivid"
    case warm = "Warm"
    case cool = "Cool"
    case vintage = "Faded / Vintage"
    case contrast = "High contrast"

    var id: String { rawValue }
}

enum MomentVisual: String, CaseIterable, Identifiable {
    case none = "None"
    case lensFlare = "Lens flare"
    case punchZoom = "Punch zoom"
    case whiteFlash = "White flash"

    var id: String { rawValue }
}

enum LiveCaptionColour: String, CaseIterable, Identifiable {
    case lime = "Pilot Lime"
    case ocean = "Ocean Blue"
    case gold = "Sunset Gold"
    case pink = "Neon Pink"
    case violet = "Electric Violet"

    var id: String { rawValue }

    var color: UIColor {
        switch self {
        case .lime: UIColor(red: 0.73, green: 0.95, blue: 0.29, alpha: 1)
        case .ocean: UIColor(red: 0.21, green: 0.86, blue: 1, alpha: 1)
        case .gold: UIColor(red: 1, green: 0.82, blue: 0.29, alpha: 1)
        case .pink: UIColor(red: 1, green: 0.31, blue: 0.85, alpha: 1)
        case .violet: UIColor(red: 0.66, green: 0.55, blue: 1, alpha: 1)
        }
    }

    var swiftUIColor: Color { Color(uiColor: color) }
}

struct ClipCandidate: Identifiable, Hashable {
    let id = UUID()
    let start: Double
    let end: Double
    let score: Int
    let reason: String
}

struct SpokenWord: Hashable {
    let text: String
    let start: Double
    let duration: Double
}

struct ExportSettings {
    let start: Double
    let end: Double
    let ratio: ExportRatio
    let gamingLayout: Bool
    let faceCorner: FaceCorner
    let faceWidth: Double
    let faceHeight: Double
    let faceInsetX: Double
    let faceInsetY: Double
    let squareCaption: String
    let squareCaptionScale: Double
    let squareCaptionPosition: CaptionPosition
    let videoLook: VideoLook
    let vineBoom: Bool
    let smartSound: Bool
    let visualEffect: MomentVisual
    let visualStrength: Double
    let liveCaptions: Bool
    let liveCaptionColour: LiveCaptionColour
    let viralTitle: Bool
}
