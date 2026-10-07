// notekarlo-sysaudio
// Captures everything the Mac is playing (Zoom, Teams, Meet, any app) with a Core Audio
// process tap (macOS 14.2+) and writes 16 kHz mono float32 little-endian PCM to stdout.
// No virtual audio driver needed. The first run asks for "System Audio Recording" permission.
//
// stderr protocol:  "READY <input-sample-rate>"  or  "ERROR <message>"
// Exits when stdin closes (parent died) or on SIGTERM/SIGINT.

import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

func say(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

func fail(_ s: String) -> Never {
    say("ERROR \(s)")
    exit(1)
}

func defaultOutputDeviceUID() -> String? {
    var deviceID = AudioObjectID(kAudioObjectUnknown)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultSystemOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &deviceID) == noErr
    else { return nil }
    addr.mSelector = kAudioDevicePropertyDeviceUID
    var uid: Unmanaged<CFString>?
    size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(deviceID, &addr, 0, nil, &size, &uid) == noErr, let value = uid
    else { return nil }
    return value.takeRetainedValue() as String
}

guard #available(macOS 14.2, *) else { fail("macOS 14.2 or newer is required for system audio capture") }

let tapDescription = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
tapDescription.uuid = UUID()
tapDescription.name = "NoteKarLo system audio"
tapDescription.isPrivate = true
tapDescription.muteBehavior = .unmuted

var tapID = AudioObjectID(kAudioObjectUnknown)
var status = AudioHardwareCreateProcessTap(tapDescription, &tapID)
guard status == noErr else { fail("could not create audio tap (\(status)). Allow System Audio Recording for your terminal app in System Settings > Privacy & Security.") }

guard let outputUID = defaultOutputDeviceUID() else { fail("no output device found") }

let aggregateDescription: [String: Any] = [
    kAudioAggregateDeviceNameKey: "NoteKarLo-Tap",
    kAudioAggregateDeviceUIDKey: UUID().uuidString,
    kAudioAggregateDeviceMainSubDeviceKey: outputUID,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
    kAudioAggregateDeviceTapListKey: [[
        kAudioSubTapDriftCompensationKey: true,
        kAudioSubTapUIDKey: tapDescription.uuid.uuidString,
    ]],
]

var aggregateID = AudioObjectID(kAudioObjectUnknown)
status = AudioHardwareCreateAggregateDevice(aggregateDescription as CFDictionary, &aggregateID)
guard status == noErr else {
    AudioHardwareDestroyProcessTap(tapID)
    fail("could not create aggregate device (\(status))")
}

var formatAddress = AudioObjectPropertyAddress(
    mSelector: kAudioTapPropertyFormat,
    mScope: kAudioObjectPropertyScopeGlobal,
    mElement: kAudioObjectPropertyElementMain)
var streamDescription = AudioStreamBasicDescription()
var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
status = AudioObjectGetPropertyData(tapID, &formatAddress, 0, nil, &formatSize, &streamDescription)
guard status == noErr, let inputFormat = AVAudioFormat(streamDescription: &streamDescription) else {
    fail("could not read tap format (\(status))")
}

let outputFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: true)!
guard let converter = AVAudioConverter(from: inputFormat, to: outputFormat) else { fail("could not create converter") }
converter.downmix = true

let stdout = FileHandle.standardOutput
let ioQueue = DispatchQueue(label: "notekarlo.tap", qos: .userInitiated)
var procID: AudioDeviceIOProcID?

status = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, ioQueue) { _, inputData, _, _, _ in
    guard let input = AVAudioPCMBuffer(pcmFormat: inputFormat, bufferListNoCopy: inputData, deallocator: nil),
          input.frameLength > 0
    else { return }
    let capacity = AVAudioFrameCount(Double(input.frameLength) * 16000.0 / inputFormat.sampleRate) + 64
    guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else { return }
    var supplied = false
    var error: NSError?
    converter.convert(to: output, error: &error) { _, inputStatus in
        if supplied {
            inputStatus.pointee = .noDataNow
            return nil
        }
        supplied = true
        inputStatus.pointee = .haveData
        return input
    }
    let frames = Int(output.frameLength)
    if frames > 0, let samples = output.floatChannelData {
        stdout.write(Data(bytes: samples[0], count: frames * MemoryLayout<Float>.size))
    }
}
guard status == noErr, let procID else { fail("could not create IO proc (\(status))") }

func cleanup() {
    AudioDeviceStop(aggregateID, procID)
    AudioDeviceDestroyIOProcID(aggregateID, procID)
    AudioHardwareDestroyAggregateDevice(aggregateID)
    AudioHardwareDestroyProcessTap(tapID)
}

status = AudioDeviceStart(aggregateID, procID)
guard status == noErr else {
    cleanup()
    fail("could not start capture (\(status))")
}

signal(SIGPIPE, SIG_IGN)
var signalSources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler {
        cleanup()
        exit(0)
    }
    source.resume()
    signalSources.append(source)
}

// Exit when the parent process goes away (stdin closes).
DispatchQueue.global().async {
    while true {
        let data = FileHandle.standardInput.availableData
        if data.isEmpty {
            DispatchQueue.main.async {
                cleanup()
                exit(0)
            }
            return
        }
    }
}

say("READY \(Int(inputFormat.sampleRate))")
dispatchMain()
