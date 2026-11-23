import AppKit
import Darwin
import Foundation

// This helper can authorize exactly one file. Only these two fields leave the sandbox.
struct StorageSettings: Codable {
    let DataFolder: String
    let AutoStart: Bool
}

enum ReaderError: String, Error {
    case wrongFile, unsafeParent, unsafeFile, fileChanged, readFailed
    case grantMissing, grantUnavailable
}

let userRoot = URL(fileURLWithPath: "/Users/zack", isDirectory: true)
let settingsURL = userRoot.appendingPathComponent("Library/Group Containers/group.com.docker/settings-store.json")
let sizeLimit = 131_072
let bookmarkOptions: URL.BookmarkCreationOptions = [.withSecurityScope, .securityScopeAllowOnlyReadAccess]

func metadata(_ path: String) throws -> stat {
    var result = stat()
    guard lstat(path, &result) == 0 else { throw ReaderError.readFailed }
    return result
}

func readSettings(_ url: URL) throws -> StorageSettings {
    guard url.isFileURL, url.path == settingsURL.path else { throw ReaderError.wrongFile }
    let device = try metadata("/System/Volumes/Data").st_dev
    var parent = userRoot
    for part in ["", "Library", "Group Containers", "group.com.docker"] {
        if !part.isEmpty { parent.appendPathComponent(part, isDirectory: true) }
        let entry = try metadata(parent.path)
        guard entry.st_mode & S_IFMT == S_IFDIR, entry.st_dev == device,
              entry.st_uid == getuid() else { throw ReaderError.unsafeParent }
    }
    // Deny symlinks anywhere in the path, including during the open itself.
    let fd = open(url.path, O_RDONLY | O_NOFOLLOW_ANY | O_NONBLOCK | O_CLOEXEC)
    guard fd >= 0 else { throw ReaderError.readFailed }
    let file = FileHandle(fileDescriptor: fd, closeOnDealloc: false)
    defer { close(fd) }
    var st = stat()
    guard fstat(fd, &st) == 0, st.st_mode & S_IFMT == S_IFREG, st.st_dev == device,
          st.st_uid == getuid(), st.st_nlink == 1, st.st_mode & 0o022 == 0,
          st.st_size > 0, st.st_size <= sizeLimit else { throw ReaderError.unsafeFile }
    let bytes = try file.read(upToCount: sizeLimit + 1) ?? Data()
    let current = try metadata(url.path)
    guard bytes.count == st.st_size, current.st_ino == st.st_ino, current.st_dev == st.st_dev,
          current.st_ctimespec.tv_sec == st.st_ctimespec.tv_sec,
          current.st_ctimespec.tv_nsec == st.st_ctimespec.tv_nsec else { throw ReaderError.fileChanged }
    return try JSONDecoder().decode(StorageSettings.self, from: bytes)
}

func bookmarkFile(create: Bool) throws -> URL {
    let dir = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                          appropriateFor: nil, create: create)
    return dir.appendingPathComponent("DockerSettingsReader.bookmark")
}

func saveGrant(_ url: URL) throws {
    let data = try url.bookmarkData(options: bookmarkOptions, includingResourceValuesForKeys: nil, relativeTo: nil)
    try data.write(to: bookmarkFile(create: true), options: .atomic)
}

func readGrantedSettings() throws -> StorageSettings {
    guard let data = try? Data(contentsOf: bookmarkFile(create: false)), !data.isEmpty else {
        throw ReaderError.grantMissing
    }
    var stale = false
    let url = try URL(resolvingBookmarkData: data, options: [.withSecurityScope, .withoutUI, .withoutMounting],
                      relativeTo: nil, bookmarkDataIsStale: &stale)
    guard url.path == settingsURL.path, url.startAccessingSecurityScopedResource() else {
        throw ReaderError.grantUnavailable
    }
    defer { url.stopAccessingSecurityScopedResource() }
    let settings = try readSettings(url)
    if stale { try saveGrant(url) }
    return settings
}

final class SetupWindow: NSObject, NSApplicationDelegate {
    private var window: NSWindow!

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 145),
                          styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Docker Settings Reader"
        let explanation = NSTextField(wrappingLabelWithString:
            "Allow the Docker recovery jobs to read the disk location and login setting from settings-store.json.")
        explanation.frame = NSRect(x: 24, y: 77, width: 392, height: 44)
        let button = NSButton(title: "Choose Settings File…", target: self, action: #selector(chooseFile))
        button.sizeToFit()
        button.setFrameOrigin(NSPoint(x: 24, y: 26))
        window.contentView?.addSubview(explanation)
        window.contentView?.addSubview(button)
        window.defaultButtonCell = button.cell as? NSButtonCell
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    @objc private func chooseFile() {
        let panel = NSOpenPanel()
        panel.message = "Select Docker’s settings-store.json file."
        panel.prompt = "Allow Read Access"
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.resolvesAliases = false
        panel.directoryURL = settingsURL.deletingLastPathComponent()
        panel.beginSheetModal(for: window) { response in
            guard response == .OK, let url = panel.url else { return }
            do {
                _ = try readSettings(url)
                try saveGrant(url)
                _ = try readGrantedSettings()
                NSApp.terminate(nil)
            } catch {
                let alert = NSAlert()
                alert.messageText = "Docker settings access could not be saved"
                alert.informativeText = failureReason(error)
                alert.runModal()
            }
        }
    }
}

func failureReason(_ error: Error) -> String {
    (error as? ReaderError)?.rawValue ?? "\((error as NSError).domain) \((error as NSError).code)"
}

if CommandLine.arguments.dropFirst().elementsEqual(["--read-settings"]) {
    do {
        let data = try JSONEncoder().encode(readGrantedSettings())
        FileHandle.standardOutput.write(data)
    } catch {
        FileHandle.standardError.write(Data("Docker settings reader: \(failureReason(error))\n".utf8))
        exit(1)
    }
} else if CommandLine.arguments.dropFirst().allSatisfy({ $0.hasPrefix("-psn_") }) {
    let app = NSApplication.shared
    let setup = SetupWindow()
    app.setActivationPolicy(.regular)
    app.delegate = setup
    withExtendedLifetime(setup) { app.run() }
} else {
    exit(64)
}
