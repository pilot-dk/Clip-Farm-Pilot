import SwiftUI

@main
struct ClipFarmPilotLocalApp: App {
    @StateObject private var editor = EditorViewModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(editor)
                .preferredColorScheme(.dark)
        }
    }
}
