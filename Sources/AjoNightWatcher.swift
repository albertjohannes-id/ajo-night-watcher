import AppKit
import Darwin
import SwiftUI
import UserNotifications

struct WatchTask: Decodable, Identifiable {
    let id: String
    let title: String
    let cwd: String
    let state: String
    let armed: Bool
    let reset: Double?
    let note: String
    let available: Bool?
}
struct WatchEvent: Decodable { let id: String; let title: String; let body: String }
struct Snapshot: Decodable {
    let tasks: [WatchTask]?
    let prompt: String?
    let cli: String?
    let events: [WatchEvent]?
    let error: String?
}

final class Store: ObservableObject {
    @Published var tasks: [WatchTask] = []
    @Published var prompt = ""
    @Published var cli = ""
    @Published var error = ""
    @Published var busy = false
    private let queue = DispatchQueue(label: "ajo-night-watcher.backend")
    private var timer: Timer?
    private var seen = Set(UserDefaults.standard.stringArray(forKey: "seenEvents") ?? [])
    var backend: String { Bundle.main.path(forResource: "watcher", ofType: "py")! }
    init() {
        call(["op": "tick"])
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.tick() }
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in self?.tick() }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }
    func tick() { if !busy { call(["op": "tick"]) } }
    func call(_ request: [String: Any]) {
        guard !busy else { return }
        busy = true
        let resource = backend
        queue.async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = [resource]
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + (env["PATH"] ?? "")
            process.environment = env
            let input = Pipe(), output = Pipe(), errors = Pipe()
            process.standardInput = input; process.standardOutput = output; process.standardError = errors
            do {
                try process.run()
                input.fileHandleForWriting.write(try JSONSerialization.data(withJSONObject: request))
                try input.fileHandleForWriting.close()
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                let snapshot = try JSONDecoder().decode(Snapshot.self, from: data)
                DispatchQueue.main.async {
                    if let tasks = snapshot.tasks { self.tasks = tasks }
                    if let p = snapshot.prompt { self.prompt = p }
                    if let c = snapshot.cli { self.cli = c }
                    self.error = snapshot.error ?? ""
                    self.busy = false
                    for event in snapshot.events ?? [] where !self.seen.contains(event.id) {
                        self.seen.insert(event.id)
                        let content = UNMutableNotificationContent()
                        content.title = event.title; content.body = event.body; content.sound = .default
                        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: event.id, content: content, trigger: nil))
                    }
                    UserDefaults.standard.set(Array(self.seen.suffix(200)), forKey: "seenEvents")
                }
            } catch {
                DispatchQueue.main.async { self.error = error.localizedDescription; self.busy = false }
            }
        }
    }
}

struct RegistryView: View {
    @ObservedObject var store: Store
    @State var selection: String?
    @State var filter = ""
    @State var watchedOnly = false
    @State var resetDate = Date().addingTimeInterval(3600)
    @State var settings = false
    @State var draftPrompt = ""
    @State var draftCLI = ""
    @State var showIdleConfirmation = false
    var selected: WatchTask? { store.tasks.first { $0.id == selection } }
    var filtered: [WatchTask] { store.tasks.filter { (!watchedOnly || $0.armed) && (filter.isEmpty || ($0.title + $0.cwd + $0.id).localizedCaseInsensitiveContains(filter)) } }
    func color(_ state: String) -> Color {
        switch state { case "Running": return .blue; case "Waiting": return .orange; case "Completed": return .green; case "Needs Input": return .red; default: return .secondary }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Image(nsImage: NSImage(contentsOfFile: Bundle.main.path(forResource: "AjoNightWatcher", ofType: "png")!)!).resizable().frame(width: 46, height: 46)
                VStack(alignment: .leading) {
                    Text("Ajo Night Watcher").font(.title2.bold())
                    Text("Continue your work when usage resets").foregroundStyle(.secondary)
                }
                Spacer()
                if store.busy { ProgressView().controlSize(.small) }
                Button { draftPrompt = store.prompt; draftCLI = store.cli; settings = true } label: { Image(systemName: "gearshape") }
                Button { store.tick() } label: { Image(systemName: "arrow.clockwise") }.disabled(store.busy)
            }
            HStack {
                TextField("Search task, repository or session ID", text: $filter).textFieldStyle(.roundedBorder)
                Toggle("Watched only", isOn: $watchedOnly).toggleStyle(.checkbox)
            }
            HSplitView {
                List(filtered, selection: $selection) { task in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack {
                            if task.armed { Image(systemName: "eye.fill").foregroundStyle(.orange) }
                            Text(task.title.isEmpty ? "Untitled task" : task.title).fontWeight(.medium).lineLimit(1)
                            Spacer()
                            Text(task.state).font(.caption).foregroundStyle(color(task.state))
                        }
                        Text(task.cwd.replacingOccurrences(of: NSHomeDirectory(), with: "~")).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                        if let reset = task.reset {
                            Text("Resume: " + Date(timeIntervalSince1970: reset + 15).formatted(date: .abbreviated, time: .shortened)).font(.caption).foregroundStyle(.orange)
                        }
                    }.padding(.vertical, 5).tag(task.id)
                }.frame(minWidth: 370)
                VStack(alignment: .leading, spacing: 14) {
                    if let task = selected {
                        Text(task.title).font(.headline).lineLimit(4).textSelection(.enabled)
                        Label(task.state, systemImage: "circle.fill").foregroundStyle(color(task.state))
                        Text(task.cwd).font(.callout).textSelection(.enabled)
                        Text(task.id).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                        Toggle("Automatically resume this task", isOn: Binding(get: { task.armed }, set: { store.call(["op": "arm", "id": task.id, "armed": $0]) })).disabled(store.busy)
                        Text(task.note.isEmpty ? "Watch for a recorded usage-limit error, or enter the reset time below." : task.note).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                        Divider()
                        Text("Reset time (local time)").font(.subheadline.bold())
                        DatePicker("Reset", selection: $resetDate, displayedComponents: [.date, .hourAndMinute]).labelsHidden()
                        Button("Schedule continuation") { store.call(["op": "schedule", "id": task.id, "reset": resetDate.timeIntervalSince1970]) }.disabled(store.busy || task.state == "Running")
                        Text("Runs about 15 seconds after the reset, or after this Mac wakes. Scheduling also enables auto-resume.").font(.caption).foregroundStyle(.secondary)
                        HStack {
                            Button("Resume Now") { store.call(["op": "resume", "id": task.id]) }.buttonStyle(.borderedProminent).disabled(store.busy || task.state == "Running")
                            Button("Run log") {
                                let p = NSHomeDirectory() + "/Library/Application Support/Ajo Night Watcher/" + task.id + ".jsonl"
                                if FileManager.default.fileExists(atPath: p) { NSWorkspace.shared.open(URL(fileURLWithPath: p)) }
                                else { store.error = "No watcher run log yet for this task." }
                            }
                        }
                        Button("Mark idle after stopping in Codex…") { showIdleConfirmation = true }.font(.caption)
                        Spacer()
                    } else {
                        Spacer()
                        Image(systemName: "sidebar.left").font(.largeTitle).foregroundStyle(.secondary)
                        Text("Select a task to watch").font(.headline)
                        Text("Sessions are discovered from Codex’s local registry. No folders are assumed to be apps.").foregroundStyle(.secondary)
                        Spacer()
                    }
                }.padding().frame(minWidth: 310, maxWidth: 390)
            }
            if !store.error.isEmpty { Text(store.error).foregroundStyle(.red).font(.callout).textSelection(.enabled) }
            HStack {
                Text("\(store.tasks.filter { $0.armed }.count) watched · \(store.tasks.filter { $0.state == "Waiting" && $0.armed }.count) waiting").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Text("Local only · CLI session resume").font(.caption).foregroundStyle(.secondary)
            }
        }.padding(20).frame(minWidth: 830, minHeight: 570)
        .onChange(of: selection) { _ in if let value = selected?.reset { resetDate = Date(timeIntervalSince1970: value) } }
        .alert("Confirm the task has stopped", isPresented: $showIdleConfirmation) {
            Button("Cancel", role: .cancel) {}
            Button("Mark Idle") { if let id = selection { store.call(["op": "idle", "id": id]) } }
        } message: { Text("Use this only after stopping the task in Codex. An old Running record cannot reliably tell whether another Codex client is still working.") }
        .sheet(isPresented: $settings) {
            VStack(alignment: .leading, spacing: 14) {
                Text("Settings").font(.title2.bold())
                Text("Codex executable")
                TextField("Absolute path", text: $draftCLI).textFieldStyle(.roundedBorder)
                Text("Continuation prompt")
                TextEditor(text: $draftPrompt).font(.body).frame(height: 120).border(Color.secondary.opacity(0.3))
                Text("Continuations use Codex’s workspace-write sandbox. If a run needs permissions or a reply, review it in Codex. Keep the watcher running for schedules to fire.").font(.callout).foregroundStyle(.secondary)
                HStack { Spacer(); Button("Cancel") { settings = false }; Button("Save") { store.call(["op": "settings", "cli": draftCLI, "prompt": draftPrompt]); settings = false }.buttonStyle(.borderedProminent) }
            }.padding(24).frame(width: 550)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    var instanceLock: Int32 = -1
    var statusItem: NSStatusItem!
    var window: NSWindow!
    var store: Store!
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        let directory = NSHomeDirectory() + "/Library/Application Support/Ajo Night Watcher"
        try? FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
        instanceLock = Darwin.open(directory + "/app.lock", O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard instanceLock >= 0, flock(instanceLock, LOCK_EX | LOCK_NB) == 0 else {
            DistributedNotificationCenter.default().postNotificationName(Notification.Name("com.ajo.night-watcher.show"), object: nil, userInfo: nil, deliverImmediately: true)
            NSApp.terminate(nil)
            return
        }
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(show), name: Notification.Name("com.ajo.night-watcher.show"), object: nil)
        UNUserNotificationCenter.current().delegate = self
        store = Store()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "moon.stars.fill", accessibilityDescription: "Ajo Night Watcher")
        statusItem.button?.toolTip = "Ajo Night Watcher"
        let menu = NSMenu()
        menu.addItem(withTitle: "Ajo Night Watcher", action: #selector(show), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Open task registry…", action: #selector(show), keyEquivalent: "o")
        menu.addItem(withTitle: "Refresh tasks", action: #selector(refresh), keyEquivalent: "r")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Watcher", action: #selector(quit), keyEquivalent: "q")
        for item in menu.items { item.target = self }
        statusItem.menu = menu
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 930, height: 640), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Ajo Night Watcher"
        window.isReleasedWhenClosed = false
        window.contentView = NSHostingView(rootView: RegistryView(store: store))
        window.center()
        if !CommandLine.arguments.contains("--background") { show() }
    }
    @objc func show() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func refresh() { store.tick() }
    @objc func quit() { NSApp.terminate(nil) }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { show(); return true }
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) { completionHandler([.banner, .sound]) }
}
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
