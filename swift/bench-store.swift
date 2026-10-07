// bench-store: loads synthetic DayDream events into an ISOLATED store with DayDream 0.1.6's own MemoryCore code
// (compiled from the shipped commit with the release typing flags), and serves that store's typed words over the same
// private socket the DayDream app serves (`AssistantTypedBridgeServer` + `MemoryStore.assistantBridgeAnswer`), so the
// shipped `mac-mem mcp` sees typed words exactly as it would with the app running and "Let AI apps see your typed
// words" on.
//
//   bench-store load   --home DIR --events FILE.jsonl --key FILE [--typed-retention 1d|7d|30d|forever] [--report FILE]
//   bench-store bridge --home DIR --key FILE
//
// Safety: refuses any --home that is not under .../bench-context/stores/ or that names the real DayDream folders.
import Foundation
import MemoryCore
import PrivacyPolicy

nonisolated(unsafe) var _keep: [Any] = []
func fail(_ s: String) -> Never { FileHandle.standardError.write(Data((s + "\n").utf8)); exit(2) }

var args = Array(CommandLine.arguments.dropFirst())
guard let command = args.first else { fail("usage: bench-store load|bridge ...") }
args.removeFirst()
func opt(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}
guard let homePath = opt("--home"), let keyPath = opt("--key") else { fail("--home and --key are required") }
let home = URL(fileURLWithPath: homePath, isDirectory: true).standardizedFileURL
let real = home.resolvingSymlinksInPath().path
if real.range(of: "Library/Application Support/DayDream", options: .caseInsensitive) != nil
    || real.range(of: "Library/Application Support/Mac Mem", options: .caseInsensitive) != nil
    || !real.contains("/bench-context/stores/") {
    fail("refusing --home outside bench-context/stores: \(real)")
}
let keyURL = URL(fileURLWithPath: keyPath)

switch command {
case "load":
    guard let eventsPath = opt("--events") else { fail("--events is required") }
    guard !FileManager.default.fileExists(atPath: home.appendingPathComponent("memory.sqlite").path) else {
        fail("store already exists at \(home.path); delete it first (stores are rebuilt from events)")
    }
    try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
    let now = Date()
    let store = try MemoryStore(home: home, writable: true, automaticallySyncSearch: false)
    // A fresh install after setup with the defaults DayDream ships (typing on, Chrome pages on, AI apps may read
    // typed words): capture text, typed consent, Chrome pages consent.
    var policy = try store.policy()
    policy.captureText = true; policy.typedConsentVersion = 1
    policy.browserPages = true; policy.browserPagesConsentVersion = PrivacySettings.browserPagesConsentCurrent
    try store.updatePolicy(policy, now: now)
    let keys = InMemoryTypedKeyStore()
    try store.attachVault(TypedTextVault(keyStore: keys), now: now)
    try store.acceptSafeTyping(now: now)
    try store.setUpTypedVault(now: now)
    var typedPolicy = try store.typedTextPolicy()
    if let r = opt("--typed-retention") {
        guard let value = TypedRetention(rawValue: r) else { fail("bad --typed-retention \(r)") }
        typedPolicy.retention = value
        try store.updateTypedTextPolicy(typedPolicy, confirmed: true, now: now)
    }

    let revision = try store.policy().revision
    let lines = try String(contentsOfFile: eventsPath, encoding: .utf8).split(separator: "\n")
    var rows: [Evidence] = []
    for line in lines where !line.trimmingCharacters(in: .whitespaces).isEmpty {
        var e = try JSONDecoder().decode(Evidence.self, from: Data(line.utf8))
        // The recorder stamps the live policy revision on its proofs; the simulator can't know it.
        if e.browserVerification != nil { e.browserVerification!.policyRevision = revision }
        if e.captureProvenance != nil {
            e.captureProvenance!.policyRevision = revision
            e.captureProvenance!.classifierVersion = UnitClassifier.version
        }
        // Send facts are decided by DayDream's own SendRules from what the simulator says happened (`_typing`), exactly
        // as the recorder decides them at seal time; the simulator never writes surface/field/send itself.
        if e.kind == "keyboard.text_input", e.captureProvenance?.unit != nil,
           let obj = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
           let t = obj["_typing"] as? [String: Any] {
            let seal = SealReason(rawValue: t["seal"] as? String ?? "idle") ?? .idle
            var facts = SendRules.facts(bundle: e.bundle, host: t["host"] as? String, title: t["host"] as? String ?? e.title,
                                        field: t["field"] as? String ?? "unknown", composerPlace: t["place"] as? String,
                                        recipient: t["recipient"] as? String, seal: seal, terminalTool: t["terminalTool"] as? String)
            var confirm: String? = nil
            if (t["sent"] as? Bool) == true {
                if facts.surface == "text" {
                    let s = SendRules.messagesClearedSend(surface: facts.surface, field: facts.field, seal: seal.rawValue)
                    facts.send = s.send; facts.sendBy = s.sendBy; if s.send == "detected" { confirm = "fieldCleared" }
                } else if facts.surface == "email" && seal == .submitChord {
                    facts.send = "detected"; facts.sendBy = "commandReturn"; confirm = "composerClosed"
                }
            }
            e.captureProvenance!.unit!.apply(facts, pasted: false)
            if let confirm { e.captureProvenance!.unit!.confirm = confirm }
            if let subject = t["subject"] as? String, facts.surface == "email" { e.captureProvenance!.unit!.subject = subject }
        }
        rows.append(e)
    }
    var kept: [String: Int] = [:], refused: [String: Int] = [:], refusedIDs: [String] = []
    for e in rows.sorted(by: { ($0.at, $0.id) < ($1.at, $1.id) }) {
        let saved = try store.ingest(e, now: now)
        if saved { kept[e.kind, default: 0] += 1 } else { refused[e.kind, default: 0] += 1; refusedIDs.append(e.id) }
    }
    // Saved after ingest: sealing adds one key per UTC day (typed_text.epoch) to the keyring.
    guard let raw = keys.raw else { fail("no typed key was created") }
    FileManager.default.createFile(atPath: keyURL.path, contents: raw, attributes: [.posixPermissions: 0o600])
    let report: [String: Any] = ["home": home.path, "rows": rows.count, "kept": kept, "refused": refused,
                                 "refusedIDs": refusedIDs, "policyRevision": revision,
                                 "typedRetention": try store.typedTextPolicy().retention.rawValue,
                                 "loadedAt": iso(now)]
    let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
    if let out = opt("--report") { try data.write(to: URL(fileURLWithPath: out)) }
    print(String(decoding: data, as: UTF8.self))

case "bridge":
    guard let raw = FileManager.default.contents(atPath: keyURL.path) else { fail("missing key file") }
    let store = try MemoryStore(home: home, writable: true, automaticallySyncSearch: false)
    try store.attachVault(TypedTextVault(keyStore: InMemoryTypedKeyStore(item: raw)))
    let bridge = AssistantTypedBridgeServer(home: store.home) { request in
        store.assistantBridgeAnswer(request, enabled: true)
    }
    guard bridge.start() else { fail("bridge could not listen at \(bridge.path)") }
    // The app's recorder heartbeat (CaptureSession.health -> refreshCaptureState every 0.5 s): the person is at their
    // Mac with DayDream on, so `status` says "recording" as it would in real use. It writes only the capture-state
    // metadata row, never activity.
    try store.setCaptureState("recording", reason: CaptureSession.recordingReason)
    let heartbeat = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "bench.heartbeat"))
    heartbeat.schedule(deadline: .now() + 0.5, repeating: 0.5)
    heartbeat.setEventHandler { _ = try? store.refreshCaptureState("recording", reason: CaptureSession.recordingReason) }
    heartbeat.resume()
    _keep.append(heartbeat)
    print("listening \(bridge.path)"); fflush(stdout)
    for sig in [SIGTERM, SIGINT] {
        signal(sig, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler { bridge.stop(); exit(0) }
        source.resume()
        _keep.append(source)
    }
    dispatchMain()

default:
    fail("unknown command \(command)")
}

