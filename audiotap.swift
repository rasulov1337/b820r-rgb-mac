// audiotap.swift
//
// Captures the macOS system audio mix with ScreenCaptureKit and writes raw
// 32-bit float PCM to stdout, so the Python side can just read samples.
//
// Build:
//   xcrun swiftc -O -o audiotap audiotap.swift \
//       -framework ScreenCaptureKit -framework CoreMedia -framework AVFoundation
//
// Requires "Screen & System Audio Recording" permission for the app that
// launches it (System Settings > Privacy & Security).

import Foundation
import CoreMedia
import CoreAudio
import ScreenCaptureKit

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(("audiotap: " + message + "\n").data(using: .utf8)!)
    exit(1)
}

// --- options ---------------------------------------------------------------

var sampleRate = 48000
var channels = 1

var argv = CommandLine.arguments.dropFirst().makeIterator()
while let arg = argv.next() {
    switch arg {
    case "--rate":
        guard let value = argv.next(), let rate = Int(value) else { fail("--rate needs a number") }
        sampleRate = rate
    case "--channels":
        guard let value = argv.next(), let count = Int(value) else { fail("--channels needs a number") }
        channels = count
    case "-h", "--help":
        print("usage: audiotap [--rate 48000] [--channels 1]")
        exit(0)
    default:
        fail("unknown argument: \(arg)")
    }
}

// --- capture ---------------------------------------------------------------

let stdoutHandle = FileHandle.standardOutput
let writeLock = NSLock()

final class AudioSink: NSObject, SCStreamOutput {
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }
        audioBuffers += 1
        if debugEnabled && audioBuffers <= 3 {
            let frames = CMSampleBufferGetNumSamples(sampleBuffer)
            FileHandle.standardError.write("dbg: audio buffer #\(audioBuffers), \(frames) frames\n".data(using: .utf8)!)
        }
        guard let block = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }
        let length = CMBlockBufferGetDataLength(block)
        if length == 0 { return }
        var bytes = [UInt8](repeating: 0, count: length)
        let status = bytes.withUnsafeMutableBytes { raw -> OSStatus in
            guard let base = raw.baseAddress else { return -1 }
            return CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: base)
        }
        guard status == noErr else { return }
        writeLock.lock()
        stdoutHandle.write(Data(bytes))
        writeLock.unlock()
    }
}

final class ScreenSink: NSObject, SCStreamOutput {
    // A screen output is required by SCStream even for audio-only capture;
    // this one throws the frames away.
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        screenBuffers += 1
        if debugEnabled && screenBuffers <= 3 {
            FileHandle.standardError.write("dbg: screen buffer #\(screenBuffers)\n".data(using: .utf8)!)
        }
    }
}

final class StreamDelegate: NSObject, SCStreamDelegate {
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write("audiotap: stream stopped: \(error)\n".data(using: .utf8)!)
        exit(3)
    }
}

let debugEnabled = ProcessInfo.processInfo.environment["AUDIOTAP_DEBUG"] == "1"
var audioBuffers = 0
var screenBuffers = 0

var activeStream: SCStream?
// SCStream does not retain its outputs; without these the sinks are
// deallocated immediately and the stream silently produces nothing.
var activeSinks: [AnyObject] = []
let ready = DispatchSemaphore(value: 0)

Task {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else { fail("no display available for audio capture") }

        let filter = SCContentFilter(display: display, excludingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.capturesAudio = true
        configuration.sampleRate = sampleRate
        configuration.channelCount = channels
        // Must stay false: ScreenCaptureKit treats every process launched from
        // the same terminal as one "app", so excluding our own audio would also
        // exclude whatever the players (or afplay) are sending.
        configuration.excludesCurrentProcessAudio = false
        configuration.width = 320
        configuration.height = 240
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 10)
        configuration.queueDepth = 3

        let stream = SCStream(filter: filter, configuration: configuration, delegate: StreamDelegate())
        let audioSink = AudioSink()
        let screenSink = ScreenSink()
        try stream.addStreamOutput(audioSink, type: .audio, sampleHandlerQueue: DispatchQueue(label: "audiotap.audio"))
        try stream.addStreamOutput(screenSink, type: .screen, sampleHandlerQueue: DispatchQueue(label: "audiotap.screen"))
        try await stream.startCapture()

        activeStream = stream
        activeSinks = [audioSink, screenSink]
        FileHandle.standardError.write(
            "audiotap: capturing \(sampleRate) Hz, \(channels) ch\n".data(using: .utf8)!
        )
        ready.signal()
    } catch {
        FileHandle.standardError.write(
            ("audiotap: " + error.localizedDescription + "\n").data(using: .utf8)!
        )
        FileHandle.standardError.write(
            ("audiotap: if this is a permission problem, enable 'Screen & System Audio Recording' "
             + "for your terminal in System Settings > Privacy & Security, then restart it.\n")
                .data(using: .utf8)!
        )
        exit(2)
    }
}

ready.wait()

// Exit as soon as the parent goes away. Without this, a killed visualiser
// leaves an orphan holding the system-audio capture, and later streams get
// silence while the orphan keeps the resource.
DispatchQueue.global().async {
    let stdinHandle = FileHandle.standardInput
    while true {
        let data = stdinHandle.availableData
        if data.isEmpty { exit(0) }
    }
}

RunLoop.main.run()
